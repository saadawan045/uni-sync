"""Logs into the student portal, reads the timetable, writes the next few weeks
of classes into Google Calendar."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from bs4 import BeautifulSoup

DAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
DAY_RE = re.compile(r"\b(" + "|".join(sorted(DAYS, key=len, reverse=True)) + r")\b", re.I)
T = r"(\d{1,2})(?:[:.](\d{2}))?\s*([ap]\.?m\.?)?"
RANGE_RE = re.compile(T + r"\s*(?:-|–|—|to)\s*" + T, re.I)
SINGLE_RE = re.compile(r"\b(\d{1,2})[:.](\d{2})\s*([ap]\.?m\.?)?", re.I)
ROOM_RE = re.compile(r"\b(room|rm|lab|hall|block|class ?room|cr|lt|auditorium|venue)\b|^[A-Z]-?\d{2,4}$", re.I)


@dataclass(frozen=True)
class Slot:
    day: int          # 0 = Monday
    start: time
    end: time
    title: str
    location: str = ""
    detail: str = ""


# ---------------------------------------------------------------- time helpers
def _mk(h, m, ap) -> time | None:
    h, m = int(h), int(m or 0)
    ap = (ap or "").lower().replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 7:  # university day: "1:30" means 13:30
        h += 12
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return time(h, m)


def parse_range(text: str) -> tuple[time, time] | None:
    m = RANGE_RE.search(text or "")
    if not m:
        return None
    h1, m1, a1, h2, m2, a2 = m.groups()
    if m1 is None and m2 is None and not (a1 or a2):
        return None  # "1-2" is too ambiguous
    a1 = a1 or (a2 if a2 and int(h1) <= int(h2) else None)
    s, e = _mk(h1, m1, a1), _mk(h2, m2, a2)
    if s and e and e <= s and not a2:
        e = _mk(int(h2) + 12, m2, None) if int(h2) < 12 else e
    return (s, e) if s and e and e > s else None


def parse_single(text: str) -> time | None:
    m = SINGLE_RE.search(text or "")
    return _mk(*m.groups()) if m else None


# ---------------------------------------------------------------- table parsing
def _grid(table) -> list[list[tuple[str, int]]]:
    """Expand a <table> with rowspan/colspan into a matrix of (text, cell_uid)."""
    grid: dict[tuple[int, int], tuple[str, int]] = {}
    uid = 0
    for r, tr in enumerate(table.find_all("tr")):
        c = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            while (r, c) in grid:
                c += 1
            uid += 1
            txt = "\n".join(s.strip() for s in cell.get_text("\n").split("\n") if s.strip())
            rs = max(1, int(cell.get("rowspan", 1) or 1))
            cs = max(1, int(cell.get("colspan", 1) or 1))
            for dr in range(rs):
                for dc in range(cs):
                    grid[(r + dr, c + dc)] = (txt, uid)
            c += cs
    if not grid:
        return []
    rows = max(k[0] for k in grid) + 1
    cols = max(k[1] for k in grid) + 1
    return [[grid.get((r, c), ("", -1)) for c in range(cols)] for r in range(rows)]


def _day_of(text: str) -> int | None:
    m = DAY_RE.fullmatch(text.strip().rstrip(".:").lower()) or DAY_RE.match(text.strip().lower())
    return DAYS[m.group(1).lower()] if m and len(text.strip()) <= 12 else None


def _slot_times(labels: list[str]) -> list[tuple[time, time] | None]:
    """Time range for each header label; fills in ends from the next label when only starts are given."""
    out = [parse_range(l) for l in labels]
    starts = [parse_single(l) if o is None else None for l, o in zip(labels, out)]
    for i, st in enumerate(starts):
        if st and out[i] is None:
            nxt = next((starts[j] or (out[j][0] if out[j] else None) for j in range(i + 1, len(labels))
                        if starts[j] or out[j]), None)
            if nxt and nxt > st:
                out[i] = (st, nxt)
            else:
                end = (datetime.combine(date.today(), st) + timedelta(minutes=60)).time()
                out[i] = (st, end)
    return out


def _cell_to_slot(day: int, text: str, start: time, end: time) -> Slot | None:
    lines = [l for l in text.split("\n") if l.strip() and l.strip() not in ("-", "--", "—")]
    if not lines:
        return None
    if re.fullmatch(r"(break|lunch|prayer|namaz|free|---*)\s*(break)?", lines[0], re.I):
        return None
    own = parse_range(text)              # some portals put the time inside the cell
    if own:
        start, end = own
    lines = [l for l in lines if not RANGE_RE.fullmatch(l.strip())]
    if not lines:
        return None
    title = lines[0]
    loc = next((l for l in lines[1:] if ROOM_RE.search(l)), "")
    rest = [l for l in lines[1:] if l != loc]
    return Slot(day, start, end, title, loc, " · ".join(rest))


def _parse_grid(g) -> list[Slot]:
    if len(g) < 2 or len(g[0]) < 2:
        return []
    header = [t for t, _ in g[0]]
    first_col = [row[0][0] for row in g]
    slots: list[Slot] = []

    # A) days down the first column, time slots across the header row
    if sum(_day_of(t) is not None for t in first_col[1:]) >= 2:
        times = _slot_times(header)
        for row in g[1:]:
            d = _day_of(row[0][0])
            if d is None:
                continue
            c = 1
            while c < len(row):
                txt, uid = row[c]
                c2 = c
                while c2 + 1 < len(row) and row[c2 + 1][1] == uid:
                    c2 += 1
                st, en = times[c], times[c2]
                if txt and st and en:
                    s = _cell_to_slot(d, txt, st[0], en[1])
                    if s:
                        slots.append(s)
                elif txt and parse_range(txt):
                    r = parse_range(txt)
                    s = _cell_to_slot(d, txt, *r)
                    if s:
                        slots.append(s)
                c = c2 + 1
        return slots

    # B) days across the header row, time slots down the first column
    if sum(_day_of(t) is not None for t in header[1:]) >= 2:
        times = _slot_times(first_col)
        for c in range(1, len(header)):
            d = _day_of(header[c])
            if d is None:
                continue
            r = 1
            while r < len(g):
                txt, uid = g[r][c]
                r2 = r
                while r2 + 1 < len(g) and g[r2 + 1][c][1] == uid:
                    r2 += 1
                st, en = times[r], times[r2]
                if txt and st and en:
                    s = _cell_to_slot(d, txt, st[0], en[1])
                    if s:
                        slots.append(s)
                r = r2 + 1
        return slots

    # C) a list: one class per row with Day / Time / Course columns
    h = [x.lower() for x in header]
    def col(*names):
        return next((i for i, x in enumerate(h) if any(n in x for n in names)), None)
    cd = col("day")
    ct = col("time", "slot", "timing")
    cs, ce = col("start", "from"), col("end", "to")
    cc = col("course", "subject", "title", "class")
    cr = col("room", "venue", "location", "lab")
    ci = col("teacher", "instructor", "faculty", "lecturer")
    if cd is None or cc is None or (ct is None and cs is None):
        return []
    for row in g[1:]:
        cells = [t for t, _ in row]
        d = _day_of(cells[cd])
        if d is None:
            continue
        rng = parse_range(cells[ct]) if ct is not None else None
        if not rng and cs is not None and ce is not None:
            s, e = parse_single(cells[cs]), parse_single(cells[ce])
            rng = (s, e) if s and e and e > s else None
        if not rng or not cells[cc].strip():
            continue
        slots.append(Slot(d, rng[0], rng[1], cells[cc].split("\n")[0],
                          cells[cr] if cr is not None else "",
                          cells[ci] if ci is not None else ""))
    return slots


def _clean_html(s: str) -> list[str]:
    s = re.sub(r"<\s*br\s*/?>|</p>|</div>|\n", "\n", s or "", flags=re.I)
    s = BeautifulSoup(s, "lxml").get_text()
    return [l.strip(" -|·\t") for l in s.split("\n") if l.strip(" -|·\t")]


def _hhmm(v) -> time | None:
    if not v:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", str(v))
    return time(int(m.group(1)), int(m.group(2))) if m else None


@dataclass(frozen=True)
class Dated:
    start: datetime
    end: datetime
    title: str
    location: str = ""
    detail: str = ""


def _split_title(raw: str, e: dict) -> tuple[str, str, str] | None:
    """NIT format: (Lecture) / Course / Section: S3 / Room / Instructors: / Name"""
    lines = _clean_html(raw)
    if not lines:
        return None
    kind = ""
    if re.fullmatch(r"\((.+)\)", lines[0]):
        kind = lines.pop(0)[1:-1].strip()
    section, teachers, rest, in_teachers = "", [], [], False
    for l in lines:
        if re.match(r"instructors?\s*:?\s*$", l, re.I):
            in_teachers = True
            continue
        m = re.match(r"instructors?\s*:\s*(.+)", l, re.I)
        if m:
            teachers.append(m.group(1)); in_teachers = True
            continue
        if in_teachers:
            teachers.append(l)
            continue
        m = re.match(r"section\s*:\s*(.+)", l, re.I)
        if m:
            section = m.group(1)
            continue
        rest.append(l)
    if not rest:
        return None
    course = rest[0]
    ext = e.get("extendedProps") or {}
    loc = next((str(e.get(k) or ext.get(k)) for k in ("location", "room", "venue") if e.get(k) or ext.get(k)), "")
    if not loc and len(rest) > 1:
        loc = rest[1]
    title = course
    if kind and kind.lower() not in course.lower() and kind.lower() != "lecture":
        title = f"{course} ({kind})"
    detail = " · ".join(x for x in [kind, f"Section {section}" if section else "", ", ".join(teachers)] + rest[2:] if x)
    return title, loc, detail


def parse_fullcalendar_dated(html: str) -> list[Dated]:
    """NIT/effii pages keep the timetable as FullCalendar events in
    <div id="time_section" data-events='[...]'>. Returns every dated class."""
    import json
    soup = BeautifulSoup(html, "lxml")
    out: set[Dated] = set()
    for el in soup.select("[data-events]"):
        try:
            events = json.loads(el["data-events"])
        except (ValueError, TypeError):
            continue
        if isinstance(events, dict):
            events = events.get("events") or list(events.values())
        for e in events if isinstance(events, list) else []:
            if not isinstance(e, dict) or e.get("daysOfWeek") is not None:
                continue
            parts = _split_title(e.get("title", ""), e)
            try:
                s = datetime.fromisoformat(str(e["start"]).replace("Z", "+00:00")).replace(second=0, microsecond=0)
                en = datetime.fromisoformat(str(e.get("end") or e["start"]).replace("Z", "+00:00")).replace(second=0, microsecond=0)
            except (KeyError, ValueError):
                continue
            if not parts or (s.time() == time(0) and en.time() == time(0)):
                continue  # all-day items (holidays etc.) aren't classes
            if en <= s:
                en = s + timedelta(hours=1)
            out.add(Dated(s, en, *parts))
    return sorted(out, key=lambda d: (d.start, d.title))


def parse_fullcalendar(html: str) -> list[Slot]:
    """Weekly pattern (for display/change detection), from dated or recurring events."""
    import json
    slots = {Slot(d.start.weekday(), d.start.time(), d.end.time(), d.title, d.location, d.detail)
             for d in parse_fullcalendar_dated(html)}
    soup = BeautifulSoup(html, "lxml")
    for el in soup.select("[data-events]"):
        try:
            events = json.loads(el["data-events"])
        except (ValueError, TypeError):
            continue
        for e in events if isinstance(events, list) else []:
            if isinstance(e, dict) and e.get("daysOfWeek") is not None:
                st, en = _hhmm(e.get("startTime")), _hhmm(e.get("endTime"))
                parts = _split_title(e.get("title", ""), e)
                if st and en and parts:
                    for d in e["daysOfWeek"]:
                        slots.add(Slot((int(d) - 1) % 7, st, en, *parts))  # FC: 0=Sunday
    return sorted(slots, key=lambda s: (s.day, s.start, s.title))


def parse_timetable(html: str) -> list[Slot]:
    soup = BeautifulSoup(html, "lxml")
    best: list[Slot] = []
    fc = parse_fullcalendar(html)
    if fc:
        return fc
    for table in soup.find_all("table"):
        if table.find("table"):
            continue  # skip layout tables that wrap other tables
        slots = _parse_grid(_grid(table))
        if len(slots) > len(best):
            best = slots
    return sorted(set(best), key=lambda s: (s.day, s.start, s.title))


# ---------------------------------------------------------------- browser
TIMETABLE_LINK = re.compile(r"time\s*-?\s*table|schedule|class\s*timing|routine", re.I)


def _login(page, cfg):
    sel = cfg.portal_selectors
    page.goto(cfg.portal_url, wait_until="networkidle", timeout=60_000)
    # NIT/effii field names first, then generic fallbacks
    user = page.locator(sel.get("username") or
                        "input[name=gua_cnic], input#gua_cnic1, "
                        "input:not([type=password]):not([type=hidden]):not([type=checkbox])"
                        ":not([type=submit]):not([type=button]):visible").first
    pw = page.locator(sel.get("password") or "input[name=user_password], input[type=password]:visible").first
    submit = page.locator(sel.get("submit") or "#LLForm button[type=submit], button[type=submit], input[type=submit]").first
    # The NIT page runs scripts that can clear the boxes right after loading,
    # so wait for it to settle, then fill and verify (re-typing if needed).
    page.wait_for_timeout(1500)
    for attempt in range(3):
        for box, val in ((user, cfg.portal_username), (pw, cfg.portal_password)):
            box.click()
            box.fill(val)
            if box.input_value() != val:
                box.fill("")
                box.press_sequentially(val, delay=40)
        page.wait_for_timeout(700)
        if user.input_value() == cfg.portal_username and pw.input_value() == cfg.portal_password:
            break
    else:
        raise RuntimeError("Couldn't fill in the portal's login boxes (they keep getting cleared).")
    start_url = page.url
    try:
        with page.expect_navigation(wait_until="load", timeout=30_000):
            if submit.count():
                submit.click()
            else:
                pw.press("Enter")
    except Exception:
        pass  # some portals log in via AJAX without a full navigation
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass
    # logged in = the login password box is gone (give slow pages up to 20 s)
    try:
        page.locator("input[name=user_password], input[type=password]").first.wait_for(state="hidden", timeout=20_000)
        return
    except Exception:
        pass

    dbg = cfg.data_dir / "discover"
    dbg.mkdir(exist_ok=True, mode=0o700)
    page.screenshot(path=dbg / "login-failed.png", full_page=True)
    (dbg / "login-failed.html").write_text(page.content())
    msg = ""
    for q in (".alert", ".error", ".text-danger", ".toast-message", ".swal2-html-container", ".sweet-alert p", "[role=alert]"):
        loc = page.locator(q)
        for i in range(min(loc.count(), 3)):
            t = loc.nth(i).inner_text().strip()
            if t:
                msg += f"\n  portal says: {t}"
    raise RuntimeError(
        "Portal login failed (still on the login page" + (", URL unchanged" if page.url == start_url else "") + ")."
        + (msg or "\n  The portal showed no error message.")
        + f"\n  Screenshot: {dbg/'login-failed.png'}")


TT_ONCLICK = re.compile(r"""(timetable\.php\?[^'"\s]+)""", re.I)


def _timetable_url(page) -> str | None:
    """The TimeTable tile is <a onclick="document.location.href='timetable.php?...'"> (no href)."""
    import html as _h
    from urllib.parse import urljoin
    m = TT_ONCLICK.search(page.content())
    return urljoin(page.url, _h.unescape(m.group(1))) if m else None


def _open_timetable(page, cfg) -> str:
    url = cfg.portal_timetable_url or _timetable_url(page)
    if url:
        page.goto(url, wait_until="networkidle", timeout=60_000)
    else:
        link = page.locator("#tour-tile-timetable, a:has-text('TimeTable'), a:has-text('Time Table')").first
        if not link.count():
            raise RuntimeError("Couldn't find the TimeTable tile. Run `uni-sync portal-discover`.")
        link.click()
        page.wait_for_load_state("networkidle", timeout=60_000)
    try:
        page.wait_for_selector("[data-events], table", timeout=15_000)
    except Exception:
        pass
    return page.content()


def fetch_html(cfg) -> str:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page()
        try:
            _login(page, cfg)
            return _open_timetable(page, cfg)
        finally:
            b.close()


def discover(cfg) -> Path:
    """Logs in and saves what it sees so the parser/selectors can be tuned."""
    from playwright.sync_api import sync_playwright
    out = cfg.data_dir / "discover"
    out.mkdir(exist_ok=True, mode=0o700)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page(viewport={"width": 1366, "height": 900})
        page.goto(cfg.portal_url, wait_until="networkidle", timeout=60_000)
        page.screenshot(path=out / "0-login.png", full_page=True)
        (out / "0-login.html").write_text(page.content())
        _login(page, cfg)
        page.screenshot(path=out / "1-dashboard.png", full_page=True)
        (out / "1-dashboard.html").write_text(page.content())
        links = page.eval_on_selector_all("a[href]", "els => els.map(e => [e.innerText.trim(), e.href])")
        (out / "links.txt").write_text("\n".join(f"{t!r:40} {h}" for t, h in links))
        cands = [(t, h) for t, h in links if TIMETABLE_LINK.search(t or "") or TIMETABLE_LINK.search(h or "")]
        tt = _timetable_url(page)
        if tt:
            cands.insert(0, ("TimeTable tile", tt))
        for i, (t, h) in enumerate(cands[:5], 2):
            page.goto(h, wait_until="networkidle", timeout=60_000)
            page.screenshot(path=out / f"{i}-candidate.png", full_page=True)
            (out / f"{i}-candidate.html").write_text(page.content())
            n = len(parse_timetable(page.content()))
            print(f"  candidate {t!r} -> {h}  ({n} classes parsed)")
        b.close()
    return out


# ---------------------------------------------------------------- sync
def occurrences(slots: list[Slot], cfg, today: date) -> list:
    from .gcal import Ev, event_id
    horizon = today + timedelta(weeks=cfg.weeks_ahead)
    if cfg.semester_end:
        horizon = min(horizon, date.fromisoformat(cfg.semester_end))
    evs = []
    d = today
    while d <= horizon:
        for s in slots:
            if s.day == d.weekday():
                st = datetime.combine(d, s.start, cfg.tz)
                if st < datetime.now(cfg.tz) - timedelta(hours=2):
                    continue
                evs.append(Ev(
                    title=s.title, start=st, end=datetime.combine(d, s.end, cfg.tz),
                    source="portal", kind="class", location=s.location, description=s.detail,
                    id=event_id("portal", d.isoformat(), s.start.isoformat(), s.title.lower()),
                ))
        d += timedelta(days=1)
    return evs


def dated_events(dated: list[Dated], cfg, today: date) -> list:
    """Real per-date classes from the portal (respects holidays and semester end)."""
    from .gcal import Ev, event_id
    horizon = today + timedelta(weeks=cfg.weeks_ahead)
    now = datetime.now(cfg.tz)
    evs = []
    for d in dated:
        st = d.start if d.start.tzinfo else d.start.replace(tzinfo=cfg.tz)
        en = d.end if d.end.tzinfo else d.end.replace(tzinfo=cfg.tz)
        if st < now - timedelta(hours=2) or st.date() > horizon:
            continue
        evs.append(Ev(title=d.title, start=st, end=en, source="portal", kind="class",
                      location=d.location, description=d.detail,
                      id=event_id("portal", st.date().isoformat(), st.time().isoformat(), d.title.lower())))
    return evs


DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _fmt(s: Slot) -> str:
    return f"{DAY_NAMES[s.day]} {s.start:%H:%M}-{s.end:%H:%M}  {s.title}" + (f" ({s.location})" if s.location else "")


def sync(cfg, cal, db, dry_run: bool = False) -> list[str]:
    """Returns human-readable change notes (empty if nothing changed)."""
    html = fetch_html(cfg)
    slots = parse_timetable(html)
    if not slots:
        (cfg.data_dir / "last-timetable.html").write_text(html)
        raise RuntimeError("Timetable page loaded but no classes were recognised — calendar left untouched. "
                           f"Page saved to {cfg.data_dir/'last-timetable.html'}.")

    notes = []
    prev = set(map(tuple, db.get("portal_slots", [])))
    cur = {(_fmt(s),) for s in slots}
    if prev and prev != cur:
        notes += [f"➕ Timetable: {x[0]}" for x in sorted(cur - prev)]
        notes += [f"➖ Timetable: {x[0]}" for x in sorted(prev - cur)]

    today = datetime.now(cfg.tz).date()
    dated = parse_fullcalendar_dated(html)
    wanted = dated_events(dated, cfg, today) if dated else occurrences(slots, cfg, today)
    skip = db.overridden()
    if dry_run:
        for s in slots:
            print("  ", _fmt(s) + (f"  [{s.detail}]" if s.detail else ""))
        print(f"{len(slots)} weekly classes → {len(wanted)} calendar events over the next {cfg.weeks_ahead} weeks"
              + (f" (using the portal's real dates; {len(dated)} classes this semester)" if dated else ""))
        return notes

    wanted_ids = set()
    for ev in wanted:
        if ev.id in skip:
            continue
        wanted_ids.add(ev.id)
        cal.upsert(ev)
    start = datetime.now(cfg.tz)
    for ev in cal.list(start, start + timedelta(weeks=cfg.weeks_ahead + 1), source="portal"):
        if ev.id not in wanted_ids:
            cal.delete(ev.id)
    db.set("portal_slots", [list(x) for x in sorted(cur)])
    db.set("portal_last_ok", start.isoformat())
    return notes
