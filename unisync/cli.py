"""uni-sync command line. Run `uni-sync --help`."""
from __future__ import annotations

import argparse
import json
import re
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from . import config as config_mod
from .db import DB
from .paths import private

TAG = "[Uni Sync]"


# ---------------------------------------------------------------- utilities
def _try_lock(f) -> bool:
    if os.name == "nt":
        import msvcrt
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _lock(cfg):
    f = open(cfg.data_dir / "run.lock", "a+")
    for _ in range(600):  # wait up to 10 min for another job (the model can be slow)
        if _try_lock(f):
            return f
        time.sleep(1)
    sys.exit("Another uni-sync job is still running.")


def _cal(cfg):
    from .gcal import Calendar
    return Calendar(cfg)


def _notify(cfg, title: str, notes: list[str], maybe: list[str] | None = None):
    from . import mail
    maybe = maybe or []
    if not notes and not maybe:
        return
    text = "\n".join(notes)
    if maybe:
        text += "\n\nNot added — please check:\n" + "\n".join(maybe)
    print(text)
    if cfg.notify_changes:
        mail.send(cfg, f"{TAG} {title}", text)


def _fail(cfg, db, job: str, err: Exception):
    """Email a failure once per job per 12 h, so a broken portal doesn't spam you."""
    from . import mail
    traceback.print_exc()
    k = f"fail:{job}"
    last = db.get(k, 0)
    if time.time() - last > 12 * 3600:
        db.set(k, time.time())
        try:
            mail.send(cfg, f"{TAG} {job} failed", f"{type(err).__name__}: {err}\n\nIt will keep retrying.")
        except Exception:
            traceback.print_exc()


def _ok(db, job: str):
    db.set(f"fail:{job}", 0)


# ---------------------------------------------------------------- commands
def cmd_doctor(cfg, db, a):
    from . import llm, mail
    checks = [
        ("Google Calendar", lambda: _cal(cfg).check()),
        ("Local AI (Ollama)", lambda: llm.check(cfg)),
        ("Email inbox (IMAP)", lambda: mail.check(cfg) if cfg.imap_host else "not configured"),
        ("WhatsApp accounts", lambda: ", ".join(_wa_accounts(cfg)) if _wa_accounts(cfg)
            else "none linked (optional: uni-sync wa-link personal)"),
        ("Email updates", lambda: f"to {cfg.notify_to} via {cfg.smtp_host}" if cfg.notify_to and cfg.smtp_host
            else "off (optional: uni-sync setup)"),
        ("Canvas", lambda: __import__("unisync.canvas", fromlist=["check"]).check(cfg)
            if (cfg.canvas_token or cfg.canvas_ical_url) else "not set up (optional)"),
        ("WeBWorK", lambda: __import__("unisync.webwork", fromlist=["check"]).check(cfg)
            if cfg.webwork_courses else "not set up (optional)"),
        ("Portal login details", lambda: "set" if cfg.portal_username and cfg.portal_password
            else (_ for _ in ()).throw(RuntimeError("missing — run: uni-sync setup"))),
    ]
    bad = 0
    for name, fn in checks:
        try:
            print(f"  ✔ {name}: {fn()}")
        except Exception as e:
            bad += 1
            print(f"  ✘ {name}: {type(e).__name__}: {e}")
    if a.send_test and cfg.notify_to:
        mail.send(cfg, f"{TAG} test", "If you can read this, notifications work.")
        print(f"  ✔ test email sent to {cfg.notify_to}")
    sys.exit(1 if bad else 0)


def cmd_portal_discover(cfg, db, a):
    from . import portal
    out = portal.discover(cfg)
    print(f"Saved screenshots, HTML and links to {out}")


def cmd_portal_sync(cfg, db, a):
    from . import portal
    try:
        notes = portal.sync(cfg, None if a.dry_run else _cal(cfg), db, dry_run=a.dry_run)
        _ok(db, "Timetable sync")
        if notes and not a.dry_run:
            _notify(cfg, "Your timetable changed", notes)
        print("Portal sync OK" + (f" — {len(notes)} change(s)" if notes else ""))
    except Exception as e:
        if a.dry_run:
            raise
        _fail(cfg, db, "Timetable sync", e)
        sys.exit(1)


def cmd_canvas_sync(cfg, db, a):
    from . import canvas
    if not (cfg.canvas_token or cfg.canvas_ical_url):
        print("Canvas not set up — add token or ical_url under [canvas] in config.toml.")
        return
    if cfg.canvas_token:
        try:  # the AI's course check uses this; on failure the last good list stays
            courses = canvas.enrolled(cfg)
            if courses:
                db.set("enrolled_courses", courses)
        except Exception:
            pass
    try:
        notes = canvas.sync(cfg, None if a.dry_run else _cal(cfg), db, dry_run=a.dry_run)
        _ok(db, "Canvas sync")
        if notes and not a.dry_run:
            _notify(cfg, "Canvas update", notes)
        print("Canvas sync OK" + (f" — {len(notes)} change(s)" if notes else ""))
    except Exception as e:
        if a.dry_run:
            raise
        _fail(cfg, db, "Canvas sync", e)
        sys.exit(1)


def cmd_webwork_sync(cfg, db, a):
    from . import webwork
    if not cfg.webwork_courses:
        print("WeBWorK not set up — add a [[webwork]] block to config.toml.")
        return
    try:
        notes = webwork.sync(cfg, None if a.dry_run else _cal(cfg), db, dry_run=a.dry_run)
        _ok(db, "WeBWorK sync")
        if notes and not a.dry_run:
            _notify(cfg, "WeBWorK update", notes)
        print("WeBWorK sync OK" + (f" — {len(notes)} change(s)" if notes else ""))
    except Exception as e:
        if a.dry_run:
            raise
        _fail(cfg, db, "WeBWorK sync", e)
        sys.exit(1)


def _chat_allowed(cfg, name: str) -> bool:
    n = (name or "").lower()
    if any(x in n for x in cfg.ignore_chats):
        return False
    return not cfg.only_chats or any(x in n for x in cfg.only_chats)


def _wa_accounts(cfg) -> list[str]:
    def linked(p):  # "account" only exists after WhatsApp confirms the pairing
        try:
            return "account" in json.loads((p / "creds.json").read_text())
        except (OSError, ValueError):
            return False
    return sorted(p.name[len("wa-auth-"):] for p in cfg.data_dir.glob("wa-auth-*") if linked(p))


def _take_spools(cfg) -> list[Path]:
    """Move each account's spool aside so the bridges can keep appending to a fresh file."""
    works = []
    for spool in sorted(cfg.data_dir.glob("wa-spool*.jsonl")):
        if spool.name.endswith(".processing.jsonl"):
            continue
        work = spool.with_name(spool.stem + ".processing.jsonl")
        if not work.exists():
            spool.rename(work)
    time.sleep(1)  # let any in-flight append land
    works = sorted(cfg.data_dir.glob("wa-spool*.processing.jsonl"))
    return works


MENTION = re.compile(r"@\d{8,}")
CONTEXT_KEEP = 8  # messages per chat


def _ckey(key) -> str:
    return f"{key[0]}|{key[1]}"


def _load_context(cfg) -> dict:
    f = cfg.data_dir / "wa-context.json"
    if getattr(cfg, "context_hours", 12) <= 0 or not f.exists():
        return {}
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError):
        return {}
    cutoff = time.time() - getattr(cfg, "context_hours", 12) * 3600
    return {k: [m for m in v if m.get("ts", 0) >= cutoff] for k, v in data.items()}


def _save_context(cfg, hist: dict) -> None:
    f = cfg.data_dir / "wa-context.json"
    if getattr(cfg, "context_hours", 12) <= 0:
        f.unlink(missing_ok=True)
        return
    cutoff = time.time() - getattr(cfg, "context_hours", 12) * 3600
    hist = {k: [m for m in v if m.get("ts", 0) >= cutoff] for k, v in hist.items()}
    hist = {k: v for k, v in hist.items() if v}
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")
    private(tmp)
    tmp.replace(f)


def cmd_wa_process(cfg, db, a):
    """Process messages the WhatsApp bridges (one per number) have spooled."""
    from .extract import Applier, run_batch, worth_checking
    works = _take_spools(cfg)
    if works:
        recs = []
        for w in works:
            for line in w.read_text().splitlines():
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        multi = len({r.get("account", "main") for r in recs} | set(_wa_accounts(cfg))) > 1
        by_chat: dict[tuple, list] = {}
        new_ids: set[str] = set()
        for r in recs:
            # the same group message reaches both numbers with the same id → handled once
            if db.seen("wa", r["id"]) or r["id"] in new_ids:
                continue
            new_ids.add(r["id"])
            if not _chat_allowed(cfg, r.get("chat_name", "")):
                continue
            if r.get("quoted"):
                r["text"] = f"[replying to: “{r['quoted'][:600]}”]\n{r['text']}"
            r["text"] = MENTION.sub("@someone", r["text"])  # unresolved WhatsApp mention ids
            by_chat.setdefault((r.get("account", "main"), r["chat"]), []).append(r)

        # For each chat: the messages that look like plans, plus a few around them as context
        # (so "done 👍" / "pakka" answering "dinner kal 8 baje?" is understood).
        # Recent earlier messages from the same chat (kept up to context_hours) so a plan posted
        # in one run and confirmed/cancelled 10 minutes later is still understood together.
        hist = _load_context(cfg)
        chats: dict[tuple, list[list]] = {}
        for key, new in by_chat.items():
            new.sort(key=lambda r: r["ts"])
            old_msgs = [dict(h, _old=True) for h in hist.get(_ckey(key), [])]
            rs = old_msgs + new
            hist[_ckey(key)] = [{k: r[k] for k in ("sender", "ts", "text") if k in r} for r in rs][-CONTEXT_KEEP:]
            hits = [i for i, r in enumerate(rs) if not r.get("_old")
                    and (r.get("self_note") or worth_checking(r["text"], cfg.detect_personal, getattr(cfg, "ai_filter", "loose")))]
            if not hits:
                continue
            keep = sorted({j for i in hits for j in range(max(0, i - 3), min(len(rs), i + 3))})
            batches, cur = [], []
            for j in keep:
                if cur and (len(cur) >= 8 or j != cur[-1][0] + 1):
                    batches.append(cur); cur = []
                cur.append((j, rs[j]))
            if cur:
                batches.append(cur)
            chats[key] = [[{"sender": r.get("sender", ""), "time": datetime.fromtimestamp(r["ts"], cfg.tz),
                            "text": r["text"], "context": j not in hits,
                            "group": str(key[1]).endswith("@g.us")} for j, r in b] for b in batches]

        ap = Applier(cfg, None if a.dry_run else _cal(cfg), db, a.dry_run)
        try:
            for (acct, chat), batches in chats.items():
                label = f"WhatsApp ({acct})" if multi else "WhatsApp"
                where = f"{label} · {by_chat[(acct, chat)][0].get('chat_name') or 'chat'}"
                for batch in batches:  # ≤ 8 messages each keeps the CPU model fast
                    run_batch(cfg, ap, "whatsapp", where, batch)
            _ok(db, "WhatsApp processing")
        except Exception as e:
            _fail(cfg, db, "WhatsApp processing", e)
            _notify(cfg, "Schedule updated from WhatsApp", ap.notes, ap.maybe)
            sys.exit(1)  # keep the work files and retry next run; event ids are deterministic so no duplicates
        if not a.dry_run:
            _save_context(cfg, hist)
        if a.dry_run:
            for w in works:  # leave messages for the real run
                w.rename(w.with_name(w.name.replace(".processing", "")))
        else:
            for i in new_ids:
                db.mark_seen("wa", i)
            for w in works:
                w.unlink()  # message text is not kept anywhere after processing
        _notify(cfg, "Schedule updated from WhatsApp", ap.notes, ap.maybe)
        sent = sum(len([m for m in b if not m["context"]]) for bs in chats.values() for b in bs)
        print(f"{len(new_ids)} new message(s) from {len(by_chat)} chat(s); {sent} looked like plans → AI; "
              f"{len(ap.notes)} calendar change(s), {len(ap.maybe)} to check")
    else:
        print("no new WhatsApp messages since last check")

    _forward_alerts(cfg)


def _forward_alerts(cfg):
    from . import mail
    f = cfg.data_dir / "alerts.jsonl"
    if not f.exists():
        return
    lines = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    f.unlink()
    if lines:
        mail.send(cfg, f"{TAG} WhatsApp bridge needs attention",
                  "\n\n".join(f"{datetime.fromtimestamp(x['ts']/1000, cfg.tz):%a %d %b %H:%M} — {x['detail']}" for x in lines))


def cmd_mail_scan(cfg, db, a):
    from . import mail
    from .extract import Applier, run_batch, worth_checking
    try:
        msgs = mail.fetch_new(cfg, db)
        ap = Applier(cfg, None if a.dry_run else _cal(cfg), db, a.dry_run)
        own = (cfg.smtp_user or "").lower()
        for m in msgs:
            if TAG in m["subject"] or (own and own in m["from"].lower()):
                continue  # our own notifications
            why = mail.skip_reason(cfg, m)
            if why:
                print(f"skipped ({why}): {m['subject'][:60]}")
                continue
            text = f"Subject: {m['subject']}\n\n{m['body']}"
            if not worth_checking(text, cfg.detect_personal, getattr(cfg, "ai_filter", "loose")):
                continue
            sender = m["from"].split("<")[0].strip().strip('"') or m["from"]
            run_batch(cfg, ap, "email", f"Email from {sender}", [{"sender": sender, "time": m["time"], "text": text[:3000]}])
        _ok(db, "Email scan")
        _notify(cfg, "Schedule updated from email", ap.notes, ap.maybe)
        print(f"Checked {len(msgs)} new email(s)")
    except Exception as e:
        _fail(cfg, db, "Email scan", e)
        sys.exit(1)


def cmd_digest(cfg, db, a):
    """Morning email: today's schedule + upcoming quizzes/exams/deadlines."""
    from . import mail
    cal = _cal(cfg)
    now = datetime.now(cfg.tz)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if a.tomorrow:
        day0 += timedelta(days=1)
    today = cal.list_all(day0, day0 + timedelta(days=1))
    upcoming = [e for e in cal.list_all(day0 + timedelta(days=1), day0 + timedelta(days=8))
                if e.kind in ("quiz", "exam", "assignment", "deadline", "presentation",
                              "social", "appointment", "birthday", "commitment")]

    def line(e):
        when = "All day" if e.all_day else f"{e.start:%H:%M}–{e.end:%H:%M}"
        return when, e.title, e.location

    label = "Tomorrow" if a.tomorrow else "Today"
    txt = [f"{label} — {day0:%A %d %B}", ""]
    txt += [f"{w:13} {t}" + (f"  @ {l}" if l else "") for w, t, l in map(line, today)] or ["Nothing scheduled 🎉"]
    if upcoming:
        txt += ["", "Coming up this week:"] + [f"{e.start:%a %d %b}  {e.title}" for e in upcoming]
    due = []
    if cfg.canvas_token or cfg.canvas_ical_url or cfg.webwork_courses:
        from . import canvas
        late = canvas.missed(db)
        if late:
            txt += ["", "Missed — Canvas still accepts these:"] + late
        due = canvas.due_soon(cfg, db)
        if due:
            txt += ["", "Due soon (Canvas / WeBWorK):"] + due
        due = late + due
        nodate = canvas.undated(db)
        if nodate:
            txt += ["", "No due date set (still to do):"] + nodate
            due = due + [f"{x} — no due date" for x in nodate]
    last = db.get("portal_last_ok")
    if last and now - datetime.fromisoformat(last) > timedelta(days=2):
        txt += ["", f"⚠️ Timetable hasn't synced since {datetime.fromisoformat(last):%a %d %b} — check the server."]

    rows = "".join(f"<tr><td style='padding:6px 12px 6px 0;color:#666;white-space:nowrap'>{escape(w)}</td>"
                   f"<td style='padding:6px 0'><b>{escape(t)}</b>{'<br><span style=color:#888>'+escape(l)+'</span>' if l else ''}</td></tr>"
                   for w, t, l in map(line, today)) or "<tr><td>Nothing scheduled 🎉</td></tr>"
    up = "".join(f"<li>{e.start:%a %d %b} — {escape(e.title)}</li>" for e in upcoming)
    due_html = "".join(f"<li>{escape(x)}</li>" for x in due)
    html = (f"<div style='font-family:system-ui,sans-serif;font-size:15px'><h2 style='margin:0 0 8px'>{label} · {day0:%A %d %B}</h2>"
            f"<table>{rows}</table>" + (f"<h3>Missed (still open) / due soon</h3><ul>{due_html}</ul>" if due_html else "")
            + (f"<h3>Coming up this week</h3><ul>{up}</ul>" if up else "") +
            "<p style='color:#999;font-size:12px'>Sent by uni-sync on your home server.</p></div>")
    if a.print:
        print("\n".join(txt))
        return
    mail.send(cfg, f"{TAG} {label}: {len(today)} item(s)" + (f", {len(upcoming)} due this week" if upcoming else ""),
              "\n".join(txt), html)
    db.prune()


def cmd_test(cfg, db, a):
    """Run the AI on a piece of text without touching the calendar."""
    from .extract import Applier, run_batch, worth_checking
    text = " ".join(a.text)
    print(f"pre-filter ({getattr(cfg, "ai_filter", "loose")}):", "pass" if worth_checking(text, cfg.detect_personal, getattr(cfg, "ai_filter", "loose"))
          else "would be SKIPPED — no time/day mentioned (see ai_filter in config)")
    ap = Applier(cfg, None, db, dry_run=True)
    t0 = time.time()
    n = run_batch(cfg, ap, "test", "test", [{"sender": "Test", "time": datetime.now(cfg.tz), "text": text}])
    print(f"model proposed {n} event(s) in {time.time()-t0:.0f}s")
    for x in ap.notes + ap.maybe:
        print(" ", x)


def cmd_pause(cfg, db, a):
    name = f"PAUSE-{a.account}" if a.account else "PAUSE"
    p = cfg.data_dir / name
    who = f"the '{a.account}' WhatsApp bridge" if a.account else "all WhatsApp bridges"
    if a.off:
        p.unlink(missing_ok=True)
        print(f"Resumed {who}. They reconnect within a minute.")
    else:
        p.touch()
        print(f"Paused {who}. Disconnects within a minute (sessions kept, not logged out).")


def cmd_run(cfg, db, a):
    from . import runner
    runner.run(cfg)


def cmd_status(cfg, db, a):
    from . import autostart, runner
    print(runner.status(cfg))
    print("\nStarts at login: " + ("yes" if autostart.enabled() else "no  (uni-sync autostart on)"))


def cmd_logs(cfg, db, a):
    d = cfg.data_dir / "logs"
    f = d / f"{a.job}.log" if a.job else d / "uni-sync.log"
    if not f.exists():
        print(f"No log yet ({f}). Logs: " + ", ".join(sorted(p.stem for p in d.glob("*.log"))) if d.exists() else "No logs yet.")
        return
    print("\n".join(f.read_text(encoding="utf-8", errors="replace").splitlines()[-a.lines:]))


def cmd_autostart(cfg, db, a):
    from . import autostart
    if a.action in ("off", "restart"):
        autostart.off(cfg)
    if a.action in ("on", "restart"):
        autostart.on(cfg)


def cmd_google_login(cfg, db, a):
    from .gcal import google_login
    cal_id = google_login(cfg)
    if cal_id != cfg.calendar_id:
        f = config_mod.CONFIG_PATH
        txt = f.read_text(encoding="utf-8")
        new = re.sub(r'(?m)^calendar_id\s*=.*$', f"calendar_id = {json.dumps(cal_id)}", txt)
        f.write_text(new if new != txt else txt.replace("[google]", f"[google]\ncalendar_id = {json.dumps(cal_id)}", 1), encoding="utf-8")
    print("✔ Signed in to Google. Calendar: University")


def cmd_wa_link(cfg, db, a):
    from . import wa
    wa.link(cfg, a.name, a.phone or "")


def cmd_wa_unlink(cfg, db, a):
    from . import wa
    wa.unlink(cfg, a.name)


def cmd_wa_list(cfg, db, a):
    from . import wa
    wa.listing(cfg)


def cmd_wa_update(cfg, db, a):
    from . import wa
    wa.update()


NO_LOCK = {"pause", "doctor", "run", "status", "logs", "autostart", "google-login",
           "wa-link", "wa-unlink", "wa-list", "wa-update"}


def main():
    p = argparse.ArgumentParser(prog="uni-sync", description="Keeps a 'University' Google Calendar up to date. "
                                "New here? Run: uni-sync setup")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="step-by-step setup (run again to change settings)")
    sub.add_parser("run", help="run the background scheduler in this window (autostart does this for you)")
    sub.add_parser("status", help="is it running, and when did each job last run")
    s = sub.add_parser("logs", help="recent log lines"); s.add_argument("job", nargs="?", help="e.g. portal-sync, wa-personal"); s.add_argument("-n", "--lines", type=int, default=60)
    s = sub.add_parser("autostart", help="start automatically at login: on / off / restart"); s.add_argument("action", choices=["on", "off", "restart"])
    sub.add_parser("google-login", help="sign in to Google again")
    s = sub.add_parser("wa-link", help="link a WhatsApp number"); s.add_argument("name"); s.add_argument("phone", nargs="?")
    s = sub.add_parser("wa-unlink", help="remove a linked WhatsApp number"); s.add_argument("name")
    sub.add_parser("wa-list", help="linked WhatsApp numbers")
    sub.add_parser("wa-update", help="update the WhatsApp library (about monthly)")
    s = sub.add_parser("doctor", help="check every connection"); s.add_argument("--send-test", action="store_true")
    sub.add_parser("portal-discover", help="log in and save screenshots/HTML of the portal")
    s = sub.add_parser("portal-sync", help="timetable → Google Calendar"); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("canvas-sync", help="Canvas due dates → Google Calendar"); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("webwork-sync", help="WeBWorK due dates → Google Calendar"); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("wa-process", help="process spooled WhatsApp messages"); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("mail-scan", help="scan new email"); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("digest", help="email today's schedule"); s.add_argument("--tomorrow", action="store_true"); s.add_argument("--print", action="store_true")
    s = sub.add_parser("test", help="try the AI on some text"); s.add_argument("text", nargs="+")
    s = sub.add_parser("pause", help="pause WhatsApp bridges (--off to resume)"); s.add_argument("account", nargs="?"); s.add_argument("--off", action="store_true")
    a = p.parse_args()

    if a.cmd == "setup":
        from . import setup
        setup.main()
        return
    cfg = config_mod.load()
    os.umask(0o077)
    lock = _lock(cfg) if a.cmd not in NO_LOCK else None
    db = DB(cfg.data_dir)
    cfg.enrolled = db.get("enrolled_courses", [])
    globals()["cmd_" + a.cmd.replace("-", "_")](cfg, db, a)


if __name__ == "__main__":
    main()
