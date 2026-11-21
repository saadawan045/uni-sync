"""Canvas LMS → calendar: assignment, quiz and discussion due dates.

Two ways in (set either in [canvas]):
  token     Canvas access token → uses the API; also knows what you've already submitted
  ical_url  your Canvas "Calendar Feed" link → no token needed, but can't see submissions
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

import requests

from .gcal import Ev, event_id

HORIZON_DAYS = 90


class Item(dict):
    """id, title, course, due (datetime), url, kind, submitted (True/False/None = unknown)"""


# ---------------------------------------------------------------- API (token)
def _get_all(cfg, path: str, params: dict | None = None) -> list:
    url = cfg.canvas_url.rstrip("/") + "/api/v1" + path
    out, params = [], {"per_page": 100, **(params or {})}
    while url:
        r = requests.get(url, params=params, timeout=60,
                         headers={"Authorization": f"Bearer {cfg.canvas_token}"})
        if r.status_code == 401:
            raise RuntimeError("Canvas rejected the access token (expired or revoked). Make a new one in "
                               "Canvas → Account → Settings → + New access token.")
        r.raise_for_status()
        data = r.json()
        out += data if isinstance(data, list) else [data]
        url = r.links.get("next", {}).get("url")
        params = None  # the next link already has them
    return out


def enrolled(cfg) -> list[dict]:
    """The courses you're in this term: [{"code": "MAT266", "name": "Calculus for Engineers II"}]."""
    out = []
    for c in _get_all(cfg, "/courses", {"enrollment_state": "active"}):
        code = str(c.get("course_code") or "").split("|")[0].strip()
        name = str(c.get("name") or "").split("|")[-1].strip()
        if code or name:
            out.append({"code": code, "name": name})
    return out


def _kind(a: dict) -> str:
    types = a.get("submission_types") or []
    if "online_quiz" in types or a.get("is_quiz_assignment") or a.get("quiz_id"):
        return "quiz"
    if "discussion_topic" in types:
        return "deadline"
    return "assignment"


def fetch_api(cfg) -> list[Item]:
    now = datetime.now(cfg.tz)
    items = []
    courses = _get_all(cfg, "/courses", {"enrollment_state": "active", "include[]": "term"})
    for c in courses:
        if not isinstance(c, dict) or c.get("access_restricted_by_date") or "id" not in c:
            continue
        cname = c.get("course_code") or c.get("name") or ""
        for bucket in ("upcoming", "future", "overdue", "undated"):
            try:
                assignments = _get_all(cfg, f"/courses/{c['id']}/assignments",
                                       {"bucket": bucket, "include[]": "submission", "order_by": "due_at"})
            except requests.HTTPError:
                continue  # some courses hide assignments
            for a in assignments:
                if not a.get("published", True):
                    continue
                sub = a.get("submission") or {}
                submitted = bool(sub.get("submitted_at")) or sub.get("workflow_state") in ("submitted", "graded",
                                                                                          "pending_review")
                if sub.get("excused"):
                    submitted = True
                late = False
                if a.get("due_at"):
                    due = datetime.fromisoformat(a["due_at"].replace("Z", "+00:00")).astimezone(cfg.tz)
                    if due < now:
                        late = _still_open(cfg, a, due, now) and not submitted
                    if due > now + timedelta(days=HORIZON_DAYS) or (due < now - timedelta(days=2) and not late):
                        continue
                else:
                    # No due date. Real work (an essay, a speech) still needs doing, but gradebook columns
                    # like "Roll Call Attendance" or "Class Participation" have nothing to submit — skip those.
                    name = (a.get("name") or "").lower()
                    ignore = getattr(cfg, "canvas_ignore", ["roll call", "attendance", "participation"])
                    if any(w in name for w in ignore) or a.get("submission_types") == ["not_graded"]:
                        continue
                    due = None
                it = Item(id=str(a["id"]), title=a.get("name", "Assignment"), course=cname, due=due,
                          url=a.get("html_url", ""), kind=_kind(a), submitted=submitted)
                if late:
                    it["late"] = True
                    it["lock"] = _parse(a.get("lock_at"), cfg.tz)
                items.append(it)
    # same assignment can show in two buckets
    return list({i["id"]: i for i in items}.values())


def _parse(s: str | None, tz) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(tz) if s else None


NOTHING_TO_SUBMIT = {"none", "on_paper", "not_graded"}


def _still_open(cfg, a: dict, due: datetime, now: datetime) -> bool:
    """Past due but Canvas still takes a (late) submission: not locked, something to hand in online,
    and missed within the last `missed_days` days (so last month's leftovers don't pile up forever)."""
    days = getattr(cfg, "canvas_missed_days", 30)
    if not days or due < now - timedelta(days=days) or a.get("locked_for_user"):
        return False
    lock = _parse(a.get("lock_at"), cfg.tz)
    if lock and lock <= now:
        return False
    if set(a.get("submission_types") or []) <= NOTHING_TO_SUBMIT:
        return False  # paper/in-class work: Canvas never sees a submission, so it would look missed forever
    name = (a.get("name") or "").lower()
    return not any(w in name for w in getattr(cfg, "canvas_ignore", ["roll call", "attendance", "participation"]))


# ---------------------------------------------------------------- calendar feed (no token)
COURSE_IN_TITLE = re.compile(r"\s*\[([^\]]+)\]\s*$")


def parse_ics(text: str, tz) -> list[Item]:
    from icalendar import Calendar
    now = datetime.now(tz)
    cal = Calendar.from_ical(text)
    items = []
    for ev in cal.walk("VEVENT"):
        uid = str(ev.get("UID", ""))
        # Canvas UIDs look like "event-assignment-12345" (assignments) or "event-calendar-event-678"
        m = re.search(r"assignment-(?:override-)?(\d+)", uid)
        if not m:
            continue  # ordinary calendar events — the portal covers classes
        start = ev.decoded("DTSTART")
        if isinstance(start, datetime):
            due = (start if start.tzinfo else start.replace(tzinfo=tz)).astimezone(tz)
        else:  # all-day → due end of that day
            due = datetime.combine(start, time(23, 59), tz)
        if due < now - timedelta(days=2) or due > now + timedelta(days=HORIZON_DAYS):
            continue
        summary = str(ev.get("SUMMARY", "Assignment"))
        cm = COURSE_IN_TITLE.search(summary)
        course = cm.group(1) if cm else ""
        title = COURSE_IN_TITLE.sub("", summary).strip()
        kind = "quiz" if re.search(r"\bquiz\b", title, re.I) else "assignment"
        items.append(Item(id=m.group(1), title=title, course=course, due=due,
                          url=str(ev.get("URL", "")), kind=kind, submitted=None))
    return items


def fetch_ics(cfg) -> list[Item]:
    url = cfg.canvas_ical_url.replace("webcal://", "https://")
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return parse_ics(r.text, cfg.tz)


def fetch(cfg) -> list[Item]:
    if cfg.canvas_token and cfg.canvas_url:
        return fetch_api(cfg)
    if cfg.canvas_ical_url:
        return fetch_ics(cfg)
    raise RuntimeError("Canvas isn't configured — set token (+ url) or ical_url under [canvas].")


# ---------------------------------------------------------------- sync
def undated_event(it: Item, tz, source: str = "canvas", icon: str = "📌") -> Ev:
    """No due date: show it as an all-day item on TODAY. Every sync moves it to the current day, so it
    sits at the top of your day until you submit it (then it disappears)."""
    today = datetime.now(tz).date()
    course = f" [{it['course']}]" if it["course"] else ""
    return Ev(
        title=f"{icon} {it['title']}{course} — no due date",
        start=today, end=today + timedelta(days=1),
        source=source, kind="undated",   # no pop-up reminders; the morning email lists these instead
        description="No due date set on Canvas. Shown on today's date until you submit it."
                    + (f"\n{it['url']}" if it["url"] else ""),
        id=event_id(source, it["id"]),
        extra={f"{source}_id": it["id"]},
    )


def _closes(it) -> str:
    return f"closes {_when(it['lock'])}" if it.get("lock") else "no closing date"


def missed_event(it: Item, tz, source: str = "canvas") -> Ev:
    """Missed but Canvas still accepts it: an all-day item on TODAY (same event id as the deadline, so the
    old entry moves here) until you submit it or it locks."""
    today = datetime.now(tz).date()
    course = f" [{it['course']}]" if it["course"] else ""
    return Ev(
        title=f"⚠️ LATE: {it['title']}{course} — was due {it['due']:%a %d %b}, {_closes(it)}",
        start=today, end=today + timedelta(days=1),
        source=source, kind="missed",
        description=f"You missed this (due {_when(it['due'])}) but Canvas still accepts submissions — "
                    + (f"until {_when(it['lock'])}." if it.get("lock") else "no lock date set (may count as late).")
                    + (f"\n{it['url']}" if it["url"] else ""),
        id=event_id(source, it["id"]),
        extra={f"{source}_id": it["id"]},
    )


def to_event(it: Item, tz, source: str = "canvas", icon: str = "📌", done_word: str = "submitted") -> Ev:
    if it["due"] is None:
        return undated_event(it, tz, source, icon)
    if it.get("late"):
        return missed_event(it, tz, source)
    done = it["submitted"] is True
    label = {"quiz": "Quiz", "deadline": "Discussion"}.get(it["kind"], "")
    title = it["title"] if not label or label.lower() in it["title"].lower() else f"{label}: {it['title']}"
    course = f" [{it['course']}]" if it["course"] else ""
    return Ev(
        title=("✅ " if done else f"{icon} ") + f"{title} due{course}" + (f" ({done_word})" if done else ""),
        start=it["due"] - timedelta(minutes=15), end=it["due"],
        source=source, kind="submitted" if done else ("quiz" if it["kind"] == "quiz" else "assignment"),
        description=f"Due {it['due']:%a %d %b %H:%M}" + (f"\n{it['url']}" if it["url"] else "")
                    + (f"\n{it['note']}" if it.get("note") else "") + (f"\n{done_word.capitalize()} ✔" if done else ""),
        id=event_id(source, it["id"]),
        extra={f"{source}_id": it["id"]},
    )


def _when(d: datetime) -> str:
    return f"{d:%a %d %b %H:%M}"


def sync_items(cfg, cal, db, items: list[Item], *, source: str, name: str, icon: str = "📌",
               done_word: str = "submitted", dry_run: bool = False, how: str = "") -> list[str]:
    """Shared by Canvas and WeBWorK: put items on the calendar, return change notes."""
    key = f"{source}_state"
    prev: dict = db.get(key, {}) or {}
    first_run = not prev
    notes = []
    now = datetime.now(cfg.tz)

    cur = {}
    for it in items:
        due_iso = it["due"].isoformat() if it["due"] else None
        cur[it["id"]] = {"title": it["title"], "course": it["course"], "due": due_iso,
                         "submitted": it["submitted"]}
        if it.get("late"):
            cur[it["id"]].update(late=True, lock=it["lock"].isoformat() if it.get("lock") else None)
        p = prev.get(it["id"])
        label = f"{it['title']}" + (f" [{it['course']}]" if it["course"] else "")
        if first_run:
            continue
        link = f"\n   {it['url']}" if it["url"] else ""
        if it.get("late") and not (p or {}).get("late"):
            notes.append(f"⚠️ Missed but still open: {label} — was due {_when(it['due'])}, {_closes(it)}{link}")
        elif not p:
            if it["submitted"] is not True and (it["due"] is None or it["due"] > now):
                when = f"due {_when(it['due'])}" if it["due"] else "no due date set"
                notes.append(f"➕ New on {name}: {label} — {when}{link}")
        elif p["due"] != due_iso:
            old_when = _when(datetime.fromisoformat(p["due"])) if p["due"] else "no due date"
            new_when = _when(it["due"]) if it["due"] else "no due date"
            notes.append(f"🔁 Due date changed: {label} — {old_when} → {new_when}")
    if not first_run:
        for cid, p in prev.items():
            if cid not in cur and p.get("due") and datetime.fromisoformat(p["due"]) > now:
                notes.append(f"➖ Removed from {name}: {p['title']}" + (f" [{p['course']}]" if p.get("course") else ""))

    if dry_run:
        for it in sorted(items, key=lambda i: (i["due"] is None, i["due"] or now)):
            mark = "⚠️" if it.get("late") else {True: "✅", False: "⏳", None: "  "}[it["submitted"]]
            when = _when(it["due"]) if it["due"] else "no due date  "
            print(f"  {mark} {when}  {it['title']}" + (f"  [{it['course']}]" if it["course"] else "")
                  + (f"  (MISSED, {_closes(it)})" if it.get("late") else "")
                  + (f"  ({it['note']})" if it.get("note") else ""))
        print(f"{len(items)} upcoming item(s) from {name}" + (f" via {how}" if how else ""))
        return notes

    wanted = set()
    for it in items:
        if it["due"] is None and it["submitted"] is True:
            continue  # undated and already submitted — nothing left to show
        ev = to_event(it, cfg.tz, source, icon, done_word)
        wanted.add(ev.id)
        cal.upsert(ev)
    for ev in cal.list(now - timedelta(days=2), now + timedelta(days=HORIZON_DAYS + 1), source=source):
        if ev.id not in wanted:
            cal.delete(ev.id)
    db.set(key, cur)
    db.set(f"{source}_last_ok", now.isoformat())
    return notes


def sync(cfg, cal, db, dry_run: bool = False) -> list[str]:
    items = fetch(cfg)
    how = "API (knows submissions)" if cfg.canvas_token else "calendar feed (can't see submissions)"
    return sync_items(cfg, cal, db, items, source="canvas", name="Canvas", dry_run=dry_run, how=how)


def due_soon(cfg, db, hours: int = 36, sources=("canvas", "webwork")) -> list[str]:
    """For the digest: not-yet-finished work due soon, from Canvas and WeBWorK."""
    now = datetime.now(cfg.tz)
    out = []
    for cid, p in [x for s in sources for x in (db.get(f"{s}_state", {}) or {}).items()]:
        if not p.get("due"):
            continue
        due = datetime.fromisoformat(p["due"])
        if now < due <= now + timedelta(hours=hours) and p.get("submitted") is not True:
            left = due - now
            hrs = int(left.total_seconds() // 3600)
            out.append((due, f"⏰ {p['title']}" + (f" [{p['course']}]" if p.get("course") else "")
                        + f" — due {_when(due)} (in {hrs} h)" + ("" if p.get("submitted") is False else "")))
    return [s for _, s in sorted(out)]


def check(cfg) -> str:
    items = fetch(cfg)
    via = "API" if cfg.canvas_token else "calendar feed"
    return f"{len(items)} upcoming item(s) via {via}"


def undated(db, sources=("canvas",)) -> list[str]:
    """For the digest: unsubmitted work with no due date."""
    out = []
    for cid, p in [x for s in sources for x in (db.get(f"{s}_state", {}) or {}).items()]:
        if not p.get("due") and p.get("submitted") is not True:
            out.append(f"📌 {p['title']}" + (f" [{p['course']}]" if p.get("course") else ""))
    return sorted(out)


def missed(db, sources=("canvas",)) -> list[str]:
    """For the digest: missed work Canvas still accepts, the one closing soonest first."""
    out = []
    for cid, p in [x for s in sources for x in (db.get(f"{s}_state", {}) or {}).items()]:
        if p.get("late") and p.get("submitted") is not True:
            lock = datetime.fromisoformat(p["lock"]) if p.get("lock") else None
            line = (f"⚠️ {p['title']}" + (f" [{p['course']}]" if p.get("course") else "")
                    + f" — was due {_when(datetime.fromisoformat(p['due']))}, "
                    + (f"closes {_when(lock)}" if lock else "no closing date"))
            out.append((lock is None, lock.timestamp() if lock else 0, line))
    return [line for *_, line in sorted(out)]
