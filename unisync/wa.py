"""Link, unlink and list WhatsApp numbers (cross-platform replacement for the old shell wrapper)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .runner import ROOT, linked_accounts

NAME_RE = re.compile(r"^[a-z0-9_-]+$")


def _node() -> str:
    node = shutil.which("node")
    if not node:
        sys.exit("Node.js isn't installed (needed for WhatsApp). Install it from https://nodejs.org (LTS), then try again.")
    return node


def _check_name(name: str) -> None:
    if not NAME_RE.match(name or ""):
        sys.exit("Give the number a short lowercase name, e.g. personal or business.")


def runner_alive(data: Path) -> bool:
    try:
        return time.time() - json.loads((data / "runner-status.json").read_text(encoding="utf-8"))["updated"] < 120
    except (OSError, ValueError, KeyError):
        return False


def link(cfg, name: str, phone: str = "") -> None:
    _check_name(name)
    node = _node()
    if not (ROOT / "wa-bridge" / "node_modules").exists():
        print("Installing the WhatsApp bridge (one time)…")
        subprocess.run(["npm", "ci", "--omit=dev", "--no-audit", "--no-fund"], cwd=ROOT / "wa-bridge",
                       check=True, shell=os.name == "nt")
    auth = cfg.data_dir / f"wa-auth-{name}"
    if name in linked_accounts(cfg.data_dir):
        print(f"'{name}' is already linked. To link it again, first run: uni-sync wa-unlink {name}")
        return
    shutil.rmtree(auth, ignore_errors=True)  # leftover from an unfinished attempt
    phone = re.sub(r"\D", "", phone or "")
    print(f"Linking WhatsApp '{name}'.")
    if phone:
        print("A pairing code will appear below. On your phone: WhatsApp → Settings → Linked devices →\n"
              "Link a device → Link with phone number instead, then type the code.")
    else:
        print("A QR code will appear below. On your phone: WhatsApp → Settings → Linked devices → Link a device, then scan it.")
    print("When it says Connected, press Ctrl+C.\n")
    args = [node, "index.mjs", "--account", name] + (["--pair", phone] if phone else [])
    env = dict(os.environ, UNISYNC_DATA=str(cfg.data_dir))
    try:
        subprocess.run(args, cwd=ROOT / "wa-bridge", env=env)
    except KeyboardInterrupt:
        pass
    if name in linked_accounts(cfg.data_dir):
        msg = "It will run in the background" + ("" if runner_alive(cfg.data_dir) else " once uni-sync is running (uni-sync autostart on)")
        print(f"\n✔ '{name}' linked. {msg}.")
    else:
        shutil.rmtree(auth, ignore_errors=True)
        print("\nNot linked, nothing saved. Try again: uni-sync wa-link " + name + (f" {phone}" if phone else ""))


def unlink(cfg, name: str) -> None:
    _check_name(name)
    auth = cfg.data_dir / f"wa-auth-{name}"
    if runner_alive(cfg.data_dir):
        # the background service holds the session files open; ask it to stop that bridge first
        (cfg.data_dir / f"UNLINK-{name}").touch()
        for _ in range(60):
            if not auth.exists():
                break
            time.sleep(1)
    shutil.rmtree(auth, ignore_errors=True)
    for f in (f"wa-groups-{name}.json", f"wa-dm-names-{name}.json", f"UNLINK-{name}"):
        (cfg.data_dir / f).unlink(missing_ok=True)
    if auth.exists():
        sys.exit("Couldn't delete the session (still in use). Run: uni-sync autostart off, then try again.")
    print(f"Session '{name}' deleted. Also remove it on the phone: WhatsApp → Settings → Linked devices.")


def listing(cfg) -> None:
    accts = linked_accounts(cfg.data_dir)
    if not accts:
        print("No WhatsApp number linked. Link one: uni-sync wa-link personal")
        return
    try:
        bridges = json.loads((cfg.data_dir / "runner-status.json").read_text(encoding="utf-8")).get("bridges", {})
    except (OSError, ValueError):
        bridges = {}
    alive = runner_alive(cfg.data_dir)
    for a in accts:
        print(f"{a:12} {bridges.get(a, 'not running') if alive else 'background service not running'}")


def update() -> None:
    _node()
    subprocess.run(["npm", "install", "--save-exact", "baileys@latest"], cwd=ROOT / "wa-bridge",
                   check=True, shell=os.name == "nt")
    ver = json.loads((ROOT / "wa-bridge/node_modules/baileys/package.json").read_text(encoding="utf-8"))["version"]
    print(f"Updated the WhatsApp library to {ver}. Restart uni-sync to use it: uni-sync autostart restart")
