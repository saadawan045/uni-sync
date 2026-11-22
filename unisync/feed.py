"""The phone app's "Updates" feed, stored on the calendar itself.

uni-sync keeps one hidden all-day event (dated 2000-01-01, so no calendar view shows it) whose
description is a small JSON document: recent changes, and "please check" items the AI wasn't sure
about. The phone app reads it with the same Google sign-in it uses for events, and when you tap
"Add it" the app inserts the ready-made event below and marks the item resolved here.
Nothing leaves your Google account; there is no extra server.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from googleapiclient.errors import HttpError

FEED_ID = "usfeed0001"      # Google event ids allow 0-9 and a-v only
MAX_CHARS = 7000            # stay well under Google's description limit
MAX_ITEMS = 40

EMOJI_VERB = {"➕": "Added", "❌": "Cancelled", "🔁": "Moved", "🏖️": "Holiday", "❔": "Check",
              "📌": "Canvas", "📐": "WeBWorK", "🆕": "New", "✏️": "Changed"}


def _item_id(text: str, t: float) -> str:
    return hashlib.sha1(f"{int(t // 60)}|{text}".encode()).hexdigest()[:12]


def _split(text: str) -> tuple[str, str, str]:
    """'🔁 Moved OOP: Tue 10:30 → Tue 13:00 — WhatsApp · BSCS: “…”' → (verb, headline, source)."""
    verb = next((v for e, v in EMOJI_VERB.items() if text.startswith(e)), "Update")
    body = re.sub(r"^\W+", "", text).strip()
    head, _, src = body.partition(" — ")
    return verb, head.strip(), src.strip()


def _empty() -> dict:
    return {"v": 1, "updated": 0, "items": [], "resolved": []}


def load(cal) -> dict:
    try:
        ev = cal.svc.events().get(calendarId=cal.cal, eventId=FEED_ID).execute()
        data = json.loads(ev.get("description") or "{}")
        if data.get("v") == 1:
            data.setdefault("items", []); data.setdefault("resolved", [])
            return data
    except (HttpError, ValueError):
        pass
    return _empty()


def build(old: dict, notes: list[str], checks: list[dict], now: float | None = None) -> dict:
    """Merge new notes/checks into the feed. Pure function (tested)."""
    now = now or time.time()
    new = []
    for text in notes:
        verb, head, src = _split(text)
        new.append({"id": _item_id(text, now), "t": now, "type": "note", "verb": verb, "title": head[:160], "src": src[:220]})
    for c in checks:
        verb, head, src = _split(c["text"])
        it = {"id": _item_id(c["text"], now), "t": now, "type": "check", "verb": "Check", "title": head[:160], "src": src[:220]}
        if c.get("event"):
            it["event"] = c["event"]
        new.append(it)
    resolved = set(old.get("resolved", []))
    seen, items = set(), []
    for it in new + old.get("items", []):
        if it["id"] in seen or it["id"] in resolved:
            continue
        seen.add(it["id"]); items.append(it)
    data = {"v": 1, "updated": now, "items": items[:MAX_ITEMS],
            "resolved": [r for r in old.get("resolved", []) if r in seen][-MAX_ITEMS:]}
    while len(json.dumps(data, ensure_ascii=False)) > MAX_CHARS and data["items"]:
        data["items"].pop()  # oldest first
    return data


def save(cal, data: dict) -> None:
    body = {"id": FEED_ID, "summary": "uni-sync updates (used by the app)", "start": {"date": "2000-01-01"},
            "end": {"date": "2000-01-02"}, "transparency": "transparent", "visibility": "private",
            "description": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            "extendedProperties": {"private": {"unisync": "feed"}}, "status": "confirmed"}
    try:
        cal.svc.events().update(calendarId=cal.cal, eventId=FEED_ID, body=body).execute()
    except HttpError as e:
        if e.resp.status != 404:
            raise
        cal.svc.events().insert(calendarId=cal.cal, body=body).execute()


def record(cal, notes: list[str], checks: list[dict]) -> None:
    """Add this run's changes to the feed. Never lets a feed problem break a sync."""
    if not notes and not checks:
        return
    try:
        save(cal, build(load(cal), notes, checks))
    except Exception as e:  # noqa: BLE001
        print(f"(couldn't update the app's feed: {type(e).__name__}: {e})")


def touch(cal) -> None:
    """Mark 'synced just now' for the app even when nothing changed (at most every 10 min)."""
    try:
        data = load(cal)
        if time.time() - data.get("updated", 0) > 600:
            data["updated"] = time.time()
            save(cal, data)
    except Exception:  # noqa: BLE001
        pass
