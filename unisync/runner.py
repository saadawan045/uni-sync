"""`uni-sync run`: the background scheduler for laptops (Windows, macOS, Linux).

It replaces the systemd timers used on a server. It runs each sync job on schedule as a
separate process, catches up on jobs it missed while the laptop was asleep or off, and keeps
one receive-only WhatsApp bridge running per linked number.

Started at login by `uni-sync autostart on`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console pop-ups

# name, command args, schedule. Schedules: ("every", minutes) or ("at", ["HH:MM", ...], catch_up_hours)
JOBS = [
    ("wa-process", ["wa-process"], ("every", 5)),
    ("mail-scan", ["mail-scan"], ("every", 10)),
    ("canvas-sync", ["canvas-sync"], ("every", 120)),
    ("webwork-sync", ["webwork-sync"], ("every", 180)),
    ("portal-sync", ["portal-sync"], ("at", ["06:00", "13:00", "19:00"], 24)),
    ("digest", ["digest"], ("at", ["07:00"], 4)),
    ("evening", ["digest", "--tomorrow"], ("at", ["21:30"], 2)),
]


def job_enabled(cfg, name: str) -> bool:
    return {
        "mail-scan": bool(cfg.imap_host and cfg.imap_user),
        "canvas-sync": bool(cfg.canvas_token or cfg.canvas_ical_url),
        "webwork-sync": bool(cfg.webwork_courses),
        "portal-sync": bool(cfg.portal_username and cfg.portal_password),
        "digest": bool(cfg.notify_to and cfg.smtp_host),
        "evening": bool(cfg.notify_to and cfg.smtp_host),
    }.get(name, True)


def is_due(schedule, last: float | None, now: datetime) -> bool:
    """last = epoch seconds of the last start (None = never). now is timezone-aware."""
    if schedule[0] == "every":
        return last is None or now.timestamp() - last >= schedule[1] * 60
    _, times, catch_up = schedule
    # most recent scheduled moment at or before now
    slots = []
    for d in (now.date() - timedelta(days=1), now.date()):
        for t in times:
            h, m = map(int, t.split(":"))
            slots.append(datetime(d.year, d.month, d.day, h, m, tzinfo=now.tzinfo))
    past = [s for s in slots if s <= now]
    if not past:
        return False
    slot = max(past)
    if now - slot > timedelta(hours=catch_up):
        return False  # too late to be useful (e.g. a morning digest at 3 pm)
    return last is None or last < slot.timestamp()


def _python() -> str:
    return sys.executable  # pythonw.exe when autostarted on Windows, so children stay windowless too


def linked_accounts(data_dir: Path) -> list[str]:
    out = []
    for p in sorted(data_dir.glob("wa-auth-*")):
        try:
            if "account" in json.loads((p / "creds.json").read_text(encoding="utf-8")):
                out.append(p.name[len("wa-auth-"):])
        except (OSError, ValueError):
            pass
    return out


class Runner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.data = cfg.data_dir
        (self.data / "logs").mkdir(exist_ok=True)
        self.log = logging.getLogger("uni-sync")
        self.log.setLevel(logging.INFO)
        h = RotatingFileHandler(self.data / "logs" / "uni-sync.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M"))
        self.log.addHandler(h)
        if sys.stdout and sys.stdout.isatty():
            self.log.addHandler(logging.StreamHandler(sys.stdout))
        self.state_file = self.data / "runner-state.json"
        self.state = self._load_state()
        self.running: dict[str, subprocess.Popen] = {}
        self.bridges: dict[str, subprocess.Popen] = {}
        self.bridge_stopped: dict[str, float] = {}   # account -> creds mtime when it stopped cleanly
        self.bridge_retry: dict[str, float] = {}

    # ---------------------------------------------------------------- state
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"last": {}, "result": {}}

    def _save_state(self):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        tmp.replace(self.state_file)

    # ---------------------------------------------------------------- jobs
    def _spawn(self, args: list[str], logname: str) -> subprocess.Popen:
        out = open(self.data / "logs" / f"{logname}.log", "a", encoding="utf-8")
        out.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} {' '.join(args)}\n")
        out.flush()
        return subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                cwd=ROOT, creationflags=NO_WINDOW)

    def tick_jobs(self):
        now = datetime.now(self.cfg.tz)
        for name, args, schedule in JOBS:
            p = self.running.get(name)
            if p is not None:
                if p.poll() is None:
                    continue
                self.state["result"][name] = {"code": p.returncode, "at": time.time()}
                self.log.info(f"{name} finished ({'ok' if p.returncode == 0 else f'exit {p.returncode}'})")
                del self.running[name]
                self._save_state()
            if not job_enabled(self.cfg, name):
                continue
            if is_due(schedule, self.state["last"].get(name), now):
                self.state["last"][name] = time.time()
                self._save_state()
                self.log.info(f"starting {name}")
                self.running[name] = self._spawn([_python(), "-X", "utf8", "-m", "unisync", *args], name)

    # ---------------------------------------------------------------- WhatsApp bridges
    def tick_bridges(self):
        for flag in self.data.glob("UNLINK-*"):  # `uni-sync wa-unlink` asks us to let go of the session files
            acct = flag.name[len("UNLINK-"):]
            p = self.bridges.pop(acct, None)
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(15)
                except subprocess.TimeoutExpired:
                    p.kill()
            shutil.rmtree(self.data / f"wa-auth-{acct}", ignore_errors=True)
            self.bridge_stopped.pop(acct, None)
            self.log.info(f"WhatsApp '{acct}' unlinked")
        node = shutil.which("node")
        if not node:
            return
        accounts = linked_accounts(self.data)
        for acct in accounts:
            p = self.bridges.get(acct)
            if p is not None and p.poll() is None:
                continue
            creds = self.data / f"wa-auth-{acct}" / "creds.json"
            if p is not None:  # it exited
                del self.bridges[acct]
                if p.returncode == 0:  # clean stop: logged out, banned or paused. Don't loop.
                    self.bridge_stopped[acct] = creds.stat().st_mtime if creds.exists() else 0
                    self.log.info(f"WhatsApp bridge '{acct}' stopped (see logs/wa-{acct}.log)")
                else:
                    self.bridge_retry[acct] = time.time() + 120
                    self.log.info(f"WhatsApp bridge '{acct}' crashed (exit {p.returncode}); retrying in 2 min")
                continue
            if acct in self.bridge_stopped:
                # a clean exit means logged out / restricted / replaced: only start again once re-linked
                if not creds.exists() or creds.stat().st_mtime == self.bridge_stopped[acct]:
                    continue
                self.bridge_stopped.pop(acct, None)
            if time.time() < self.bridge_retry.get(acct, 0):
                continue  # the bridge handles PAUSE itself (stays up, disconnected)
            self.log.info(f"starting WhatsApp bridge '{acct}'")
            env = dict(os.environ, UNISYNC_DATA=str(self.data))
            out = open(self.data / "logs" / f"wa-{acct}.log", "a", encoding="utf-8")
            self.bridges[acct] = subprocess.Popen([node, "index.mjs", "--account", acct], cwd=ROOT / "wa-bridge",
                                                  env=env, stdout=out, stderr=subprocess.STDOUT,
                                                  stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        for acct in list(self.bridges):  # unlinked while running
            if acct not in accounts:
                self.bridges.pop(acct).terminate()

    # ---------------------------------------------------------------- main loop
    def write_status(self):
        st = {"pid": os.getpid(), "updated": time.time(),
              "bridges": {a: ("running" if p.poll() is None else "stopped") for a, p in self.bridges.items()}}
        (self.data / "runner-status.json").write_text(json.dumps(st), encoding="utf-8")

    def loop(self):
        self.log.info(f"uni-sync started (pid {os.getpid()})")
        try:
            while True:
                try:
                    self.tick_bridges()
                    self.tick_jobs()
                    self.write_status()
                except Exception as e:  # never let one bad tick stop the scheduler
                    self.log.exception(f"scheduler error: {e}")
                time.sleep(20)
        finally:
            for p in list(self.bridges.values()) + list(self.running.values()):
                if p.poll() is None:
                    p.terminate()
            (self.data / "runner-status.json").unlink(missing_ok=True)


def run(cfg):
    from .cli import _try_lock
    lock = open(cfg.data_dir / "runner.lock", "a+")
    if not _try_lock(lock):
        sys.exit("uni-sync is already running in the background.")
    Runner(cfg).loop()


def status(cfg) -> str:
    lines = []
    f = cfg.data_dir / "runner-status.json"
    try:
        st = json.loads(f.read_text(encoding="utf-8"))
        alive = time.time() - st["updated"] < 120
    except (OSError, ValueError, KeyError):
        st, alive = {}, False
    lines.append("Background service: " + ("running" if alive else "NOT running  (start it: uni-sync autostart on, or uni-sync run)"))
    state = {}
    try:
        state = json.loads((cfg.data_dir / "runner-state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    lines.append("")
    lines.append(f"{'Job':14} {'Last run':18} Result")
    for name, _, _ in JOBS:
        if not job_enabled(cfg, name):
            lines.append(f"{name:14} {'not set up':18}")
            continue
        last = state.get("last", {}).get(name)
        res = state.get("result", {}).get(name, {})
        when = datetime.fromtimestamp(last).strftime("%a %d %b %H:%M") if last else "never"
        ok = "" if not res else ("ok" if res.get("code") == 0 else f"failed (exit {res.get('code')}) — uni-sync logs {name}")
        lines.append(f"{name:14} {when:18} {ok}")
    accts = linked_accounts(cfg.data_dir)
    lines.append("")
    lines.append("WhatsApp: " + (", ".join(f"{a} ({st.get('bridges', {}).get(a, 'not running')})" for a in accts)
                                 if accts else "no number linked (uni-sync wa-link <name>)"))
    return "\n".join(lines)
