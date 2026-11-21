"""`uni-sync setup`: a step-by-step wizard that writes config.toml and tests each part.
Safe to run again: it keeps your previous answers as defaults (press Enter to keep)."""
from __future__ import annotations

import getpass
import json
import shutil
import subprocess
import sys
import tomllib
import urllib.request

from . import paths
from .runner import ROOT

PY = [sys.executable, "-X", "utf8", "-m", "unisync"]


def _ask(q: str, default: str = "", secret: bool = False, required: bool = False) -> str:
    shown = ("(saved)" if secret else default) if default else ""
    while True:
        prompt = f"{q}" + (f" [{shown}]" if shown else "") + ": "
        v = (getpass.getpass(prompt) if secret else input(prompt)).strip()
        v = v or default
        if v or not required:
            return v
        print("  This one is needed.")


def _yes(q: str, default: bool = True) -> bool:
    v = input(f"{q} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not v else v.startswith("y")


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # a valid TOML basic string


def _write(conf: dict) -> None:
    p, g, m, cv, wa, llm = (conf.get(k, {}) for k in ("portal", "google", "mail", "canvas", "whatsapp", "llm"))
    lines = [
        "# uni-sync settings. Written by `uni-sync setup`; run it again to change anything.",
        "", "[general]", f"timezone = {_q(conf.get('general', {}).get('timezone', 'Asia/Karachi'))}",
        "", "[portal]",
        f"login_url = {_q(p.get('login_url', 'https://nit.effii.app/portal/'))}",
        f"username = {_q(p.get('username', ''))}",
        f"password = {_q(p.get('password', ''))}",
        f"timetable_url = {_q(p.get('timetable_url', ''))}",
        f"weeks_ahead = {int(p.get('weeks_ahead', 3))}",
        f"semester_end = {_q(p.get('semester_end', ''))}",
        "", "[google]", f"auth = {_q(g.get('auth', 'oauth'))}", f"calendar_id = {_q(g.get('calendar_id', ''))}",
    ]
    if g.get("oauth_client_file"):
        lines.append(f"oauth_client_file = {_q(g['oauth_client_file'])}")
    if g.get("service_account_file"):
        lines.append(f"service_account_file = {_q(g['service_account_file'])}")
    lines += [
        "", "[llm]", f"model = {_q(llm.get('model', 'qwen2.5:3b'))}",
        f"min_confidence = {float(llm.get('min_confidence', 0.6))}",
        f"detect_personal = {'true' if llm.get('detect_personal', True) else 'false'}",
        f"filter = {_q(llm.get('filter', 'loose'))}",
        "", "[mail]",
        f"imap_host = {_q(m.get('imap_host', ''))}", "imap_port = 993",
        f"imap_user = {_q(m.get('imap_user', ''))}", f"imap_password = {_q(m.get('imap_password', ''))}",
        f"smtp_host = {_q(m.get('smtp_host', ''))}", "smtp_port = 587",
        f"notify_to = {_q(m.get('notify_to', ''))}",
        f"notify_changes = {'true' if m.get('notify_changes', True) else 'false'}",
        f"trusted_domains = {json.dumps(m.get('trusted_domains', ['nit.edu.pk', 'instructure.com']))}",
        "", "[canvas]",
        f"url = {_q(cv.get('url', ''))}", f"token = {_q(cv.get('token', ''))}", f"ical_url = {_q(cv.get('ical_url', ''))}",
        f"missed_days = {int(cv.get('missed_days', 30))}",
        "", "[whatsapp]",
        f"my_names = {json.dumps(wa.get('my_names', []), ensure_ascii=False)}",
        f"only_chats = {json.dumps(wa.get('only_chats', []), ensure_ascii=False)}",
        f"ignore_chats = {json.dumps(wa.get('ignore_chats', []), ensure_ascii=False)}",
        f"context_hours = {float(wa.get('context_hours', 12))}",
    ]
    for w in conf.get("webwork", []):
        lines += ["", "[[webwork]]"] + [f"{k} = {_q(str(w.get(k, '')))}" for k in ("name", "url", "username", "password")]
    f = paths.config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    paths.private(f.parent)
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    paths.private(f)


def _run(*args: str) -> bool:
    return subprocess.run(PY + list(args), cwd=ROOT).returncode == 0


def _ollama_ok(model: str) -> str:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as r:
            names = [m["name"] for m in json.load(r).get("models", [])]
        return "ready" if any(n.startswith(model) for n in names) else "no-model"
    except Exception:
        return "down"


def main() -> None:
    f = paths.config_file()
    conf = tomllib.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    for k in ("portal", "google", "mail", "canvas", "whatsapp", "llm"):
        conf.setdefault(k, {})
    p, g, m, cv, wa = conf["portal"], conf["google"], conf["mail"], conf["canvas"], conf["whatsapp"]

    print("\n=== uni-sync setup ===")
    print("Your logins are saved only on this laptop (in " + str(f.parent) + ").")
    print("Press Enter to keep an answer in [brackets].\n")

    # 1. portal
    print("1/6  Student portal (timetable)")
    p["username"] = _ask("  Portal username / registration number", p.get("username", ""), required=True)
    p["password"] = _ask("  Portal password (hidden as you type)", p.get("password", ""), secret=True, required=True)
    p["semester_end"] = _ask("  Last day of this semester, e.g. 2027-01-20 (optional)", p.get("semester_end", ""))
    _write(conf)

    # 2. google
    print("\n2/6  Google Calendar")
    print("  You'll sign in with Google in your browser. uni-sync creates a calendar called 'University'")
    print("  and can only see that calendar, nothing else in your account.")
    g.setdefault("auth", "oauth")
    if g["auth"] == "oauth" and (not g.get("calendar_id") or _yes("  Sign in again?", False)):
        from . import config as config_mod
        from .gcal import google_login
        cfg = config_mod.load()
        try:
            g["calendar_id"] = google_login(cfg)
            _write(conf)
            print("  ✔ Connected. Add the 'University' calendar to your phone's calendar app to see it there.")
        except SystemExit as e:  # no sign-in client bundled in this copy
            print(f"  ✘ {e}\n  Skipping Google for now; everything else can still be set up.")
        except Exception as e:
            print(f"  ✘ Google sign-in didn't finish ({e}). Try again later with: uni-sync google-login")

    # 3. timetable test
    print("\n3/6  Testing the portal login (opens a hidden browser, about 30 s)…")
    if not _run("portal-sync", "--dry-run"):
        print("  ✘ That didn't work. Check your username/password and run `uni-sync setup` again.\n"
              "    If your login is right, run `uni-sync portal-discover` and share the output with the maintainer.")

    # 4. canvas
    print("\n4/6  Canvas (assignment due dates) — optional")
    if _yes("  Set up Canvas?", bool(cv.get("token")) or not cv):
        cv["url"] = _ask("  Canvas address", cv.get("url", "https://nit.instructure.com")).rstrip("/")
        print("  Get a token: open Canvas → Account → Settings → '+ New access token', name it uni-sync, copy it.")
        cv["token"] = _ask("  Paste the token (hidden)", cv.get("token", ""), secret=True)
        _write(conf)
        if cv["token"]:
            _run("canvas-sync", "--dry-run")

    # 5. whatsapp + AI
    print("\n5/6  WhatsApp (optional): reads class and group chats for quizzes, cancellations and deadlines")
    names = _ask("  Names people call you in groups, comma-separated (e.g. Saad, Awan)", ", ".join(wa.get("my_names", [])))
    wa["my_names"] = [n.strip() for n in names.split(",") if n.strip()]
    _write(conf)
    model = conf["llm"].get("model", "qwen2.5:3b")
    state = _ollama_ok(model)
    if state == "down":
        print("  The local AI (Ollama) isn't running. Install it from https://ollama.com, then run `uni-sync setup` again.")
    elif state == "no-model" and _yes(f"  Download the local AI model ({model}, about 2 GB)?"):
        subprocess.run(["ollama", "pull", model])
    if shutil.which("node") and _yes("  Link a WhatsApp number now?", False):
        from . import config as config_mod
        from . import wa as wa_mod
        name = _ask("  Short name for this number", "personal")
        phone = _ask("  Phone number with country code, digits only (or Enter to use a QR code)", "")
        wa_mod.link(config_mod.load(), name, phone)

    # 6. email (optional)
    print("\n6/6  Email updates (optional): a 7:00 summary and alerts when something changes")
    if _yes("  Set up email updates?", bool(m.get("notify_to"))):
        print("  You need a Gmail account with 2-Step Verification and an App Password:\n"
              "  myaccount.google.com → search 'App passwords' → create one called uni-sync.")
        m["imap_user"] = _ask("  Gmail address", m.get("imap_user", ""), required=True)
        m["imap_password"] = _ask("  App password (16 letters, hidden)", m.get("imap_password", ""), secret=True, required=True)
        m["imap_host"], m["smtp_host"] = "imap.gmail.com", "smtp.gmail.com"
        m["notify_to"] = _ask("  Send updates to", m.get("notify_to") or m["imap_user"])
        _write(conf)
        _run("doctor", "--send-test")

    print("\nAll set. Turning on the background service…")
    from . import autostart, config as config_mod
    autostart.on(config_mod.load())
    print("\nUseful commands:  uni-sync status   ·   uni-sync logs   ·   uni-sync setup (change settings)")
