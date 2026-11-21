"""Loads config.toml (location per OS: see paths.py; `uni-sync setup` writes it)."""
from __future__ import annotations

import os
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from . import paths

CONFIG_PATH = paths.config_file()


@dataclass
class Config:
    raw: dict
    data_dir: Path
    tz: ZoneInfo

    # portal
    portal_url: str = ""
    portal_username: str = ""
    portal_password: str = ""
    portal_timetable_url: str = ""
    portal_selectors: dict = field(default_factory=dict)
    weeks_ahead: int = 3
    semester_end: str = ""

    # google
    google_auth: str = "oauth"      # "oauth" (sign in with Google) or "service_account"
    oauth_client_file: str = ""
    service_account_file: str = ""
    calendar_id: str = ""

    # llm
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:3b"
    min_confidence: float = 0.6

    # mail
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    notify_to: str = ""
    notify_changes: bool = True
    trusted_domains: list = field(default_factory=list)

    # canvas
    canvas_url: str = ""
    canvas_token: str = ""
    canvas_ical_url: str = ""
    canvas_ignore: list = field(default_factory=lambda: ["roll call", "attendance", "participation"])
    canvas_missed_days: int = 30
    webwork_courses: list = field(default_factory=list)

    # whatsapp
    only_chats: list = field(default_factory=list)
    detect_personal: bool = True
    ignore_chats: list = field(default_factory=list)
    context_hours: float = 12
    ai_filter: str = "loose"
    my_names: list = field(default_factory=list)  # a group message using one of these is addressed to you
    enrolled: list = field(default_factory=list)  # filled at startup from the last Canvas sync
    verify: bool = False         # second AI check on each new event (drops other people's plans, jokes)


def load() -> Config:
    if not CONFIG_PATH.exists():
        sys.exit(f"Not set up yet (no {CONFIG_PATH}).\nRun:  uni-sync setup")
    if not paths.WINDOWS and CONFIG_PATH.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        os.chmod(CONFIG_PATH, 0o600)  # it holds passwords

    try:
        raw = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))  # -sig: Notepad may add a BOM
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"Error in {CONFIG_PATH}: {e}\n"
                 "Common causes: a value without quotes, or text after a value that isn't a # comment.\n"
                 "Fix it in a text editor, or run `uni-sync setup` again.")
    g = raw.get("general", {})
    p = raw.get("portal", {})
    goo = raw.get("google", {})
    llm = raw.get("llm", {})
    m = raw.get("mail", {})
    wa = raw.get("whatsapp", {})
    cv = raw.get("canvas", {})

    data_dir = Path(os.path.expanduser(g["data_dir"])) if g.get("data_dir") else paths.default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cdir = paths.config_dir()

    return Config(
        raw=raw,
        data_dir=data_dir,
        tz=ZoneInfo(g.get("timezone", "Asia/Karachi")),
        portal_url=p.get("login_url", "https://nit.effii.app/portal/"),
        portal_username=p.get("username", ""),
        portal_password=p.get("password", ""),
        portal_timetable_url=p.get("timetable_url", ""),
        portal_selectors=p.get("selectors", {}),
        weeks_ahead=int(p.get("weeks_ahead", 3)),
        semester_end=p.get("semester_end", ""),
        google_auth=goo.get("auth", "service_account" if goo.get("service_account_file") else "oauth"),
        oauth_client_file=os.path.expanduser(goo.get("oauth_client_file", "")),
        service_account_file=os.path.expanduser(goo.get("service_account_file", str(cdir / "service-account.json"))),
        calendar_id=goo.get("calendar_id", ""),
        ollama_url=llm.get("ollama_url", "http://127.0.0.1:11434"),
        model=llm.get("model", "qwen2.5:3b"),
        min_confidence=float(llm.get("min_confidence", 0.6)),
        imap_host=m.get("imap_host", ""),
        imap_port=int(m.get("imap_port", 993)),
        imap_user=m.get("imap_user", ""),
        imap_password=m.get("imap_password", ""),
        imap_folder=m.get("imap_folder", "INBOX"),
        smtp_host=m.get("smtp_host", ""),
        smtp_port=int(m.get("smtp_port", 587)),
        smtp_user=m.get("smtp_user", m.get("imap_user", "")),
        smtp_password=m.get("smtp_password", m.get("imap_password", "")),
        notify_to=m.get("notify_to", ""),
        notify_changes=bool(m.get("notify_changes", True)),
        trusted_domains=[d.lower().lstrip("@") for d in m.get("trusted_domains", ["nit.edu.pk", "instructure.com"])],
        canvas_url=cv.get("url", "").rstrip("/"),
        canvas_token=cv.get("token", ""),
        canvas_ical_url=cv.get("ical_url", ""),
        canvas_ignore=[s.lower() for s in cv.get("ignore_undated", ["roll call", "attendance", "participation"])],
        canvas_missed_days=int(cv.get("missed_days", 30)),
        webwork_courses=[w for w in raw.get("webwork", []) if isinstance(w, dict) and w.get("url")],
        only_chats=[s.lower() for s in wa.get("only_chats", [])],
        detect_personal=bool(raw.get("llm", {}).get("detect_personal", True)),
        ignore_chats=[s.lower() for s in wa.get("ignore_chats", [])],
        context_hours=float(wa.get("context_hours", 12)),
        my_names=[s.lower() for s in wa.get("my_names", []) if s and s.lower() != "yourname"],
        ai_filter=(llm.get("filter", "loose") if llm.get("filter", "loose") in ("strict", "loose", "off") else "loose"),
        verify=bool(llm.get("verify", False)),
    )
