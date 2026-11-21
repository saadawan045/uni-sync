"""WeBWorK homework sets → calendar.

WeBWorK has no feed or API for students, so this signs into each course with your
WeBWorK username/password (like you do in the browser), reads the list of homework
sets and their dates, and — if the Grades page is available — your score per set.
Read-only: it never opens a problem or submits anything.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dparser
from dateutil import tz as dtz

from .canvas import HORIZON_DAYS, Item, sync_items

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

# WeBWorK prints the server's zone abbreviation; map the common ones so the time is right
TZ_ABBR = {
    "PKT": "Asia/Karachi", "GST": "Asia/Dubai", "IST": "Asia/Kolkata", "UTC": "UTC", "GMT": "UTC",
    "EST": "America/New_York", "EDT": "America/New_York", "CST": "America/Chicago", "CDT": "America/Chicago",
    "MST": "America/Denver", "MDT": "America/Denver", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "AST": "Asia/Riyadh", "BST": "Europe/London", "CET": "Europe/Paris", "CEST": "Europe/Paris",
}
TZINFOS = {k: dtz.gettz(v) for k, v in TZ_ABBR.items()}

# "10/12/2026 at 11:59pm PKT", "Oct 12, 2026, 11:59 PM PKT", "12 October 2026 at 23:59"
DATE_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{2,4}(?:\s*(?:at|,)?\s*\d{1,2}:\d{2}\s*[ap]\.?m\.?)?(?:\s*[A-Z]{2,5}\b)?"
    r"|(?:[A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Z][a-z]{2,8}\.?\s+\d{4})"
    r"(?:\s*(?:at|,)?\s*\d{1,2}:\d{2}\s*(?:[ap]\.?m\.?)?)?(?:\s*[A-Z]{2,5}\b)?)",
    re.I,
)
NOT_SETS = {"grades", "options", "logout", "instructor", "achievements", "feedback", "hardcopy",
            "show_me_another", "equation_display", "login", "proctoredquiz_mode", "test_mode"}


def _parse_date(s: str, tz) -> datetime | None:
    s = re.sub(r"\s+at\s+", " ", s.strip())
    try:
        d = dparser.parse(s, tzinfos=TZINFOS, fuzzy=True)
    except (ValueError, OverflowError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz)
    return d.astimezone(tz)


def _dates_after(text: str, words: str, tz) -> datetime | None:
    """The first date that follows one of `words` in text."""
    m = re.search(rf"\b(?:{words})\b[^0-9A-Za-z]*(?:on|at|in)?\s*", text, re.I)
    if not m:
        return None
    d = DATE_RE.search(text, m.end())
    return _parse_date(d.group(0), tz) if d and d.start() - m.end() < 25 else None


def parse_sets(html: str, course_url: str, course: str, tz) -> list[Item]:
    soup = BeautifulSoup(html, "lxml")
    base_path = urlparse(course_url).path.rstrip("/") + "/"
    items: dict[str, Item] = {}
    now = datetime.now(tz)
    for a in soup.find_all("a", href=True):
        path = urlparse(urljoin(course_url, a["href"])).path
        if not path.startswith(base_path):
            continue
        rest = path[len(base_path):].strip("/")
        if not rest or "/" in rest or rest.lower() in NOT_SETS:
            continue
        set_id = unquote(rest)
        box = a.find_parent(["tr", "li"]) or a.parent
        text = re.sub(r"\s+", " ", box.get_text(" ", strip=True))
        due = (_dates_after(text, "closes|close|closing|due|will close", tz)
               or _dates_after(text, "reduced scoring period ends|ends", tz))
        if not due:
            if re.search(r"\b(closed|answers available)\b", text, re.I):
                continue
            ds = [_parse_date(m.group(0), tz) for m in DATE_RE.finditer(text)]
            ds = [d for d in ds if d]
            due = max(ds) if ds else None   # e.g. "open: <date> – <date>"
        if not due or due < now - timedelta(days=2) or due > now + timedelta(days=HORIZON_DAYS):
            continue
        opens = _dates_after(text, "opens|will open|open on", tz)
        note = f"Opens {opens:%a %d %b %H:%M}" if opens and opens > now else ""
        title = a.get_text(" ", strip=True).replace("_", " ") or set_id.replace("_", " ")
        items[set_id] = Item(id=f"{course}:{set_id}", title=title, course=course, due=due,
                             url=urljoin(course_url, set_id + "/"), kind="assignment", submitted=None, note=note)
    return list(items.values())


def parse_grades(html: str) -> dict[str, float]:
    """Set name → fraction done, from the Grades page (best effort)."""
    soup = BeautifulSoup(html, "lxml")
    out = {}
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        name = cells[0].replace("_", " ").strip()
        pct = next((float(m.group(1)) for c in cells[1:] if (m := re.fullmatch(r"(\d{1,3}(?:\.\d+)?)\s*%", c))), None)
        if pct is None:
            nums = next(((float(a), float(b)) for c in cells[1:]
                         if (mm := re.fullmatch(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", c)) for a, b in [mm.groups()]), None)
            if nums and nums[1] > 0:
                pct = 100 * nums[0] / nums[1]
        if pct is not None and name:
            out[name.lower()] = pct / 100
    return out


class LoginError(RuntimeError):
    pass


def _login(sess: requests.Session, url: str, user: str, pw: str) -> str:
    r = sess.get(url, timeout=60)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    form = next((f for f in soup.find_all("form") if f.find("input", attrs={"type": "password"})), None)
    if not form:
        return r.text  # already signed in (cookie still valid)
    data = {i.get("name"): i.get("value", "") for i in form.find_all("input") if i.get("name")}
    ufield = next((i.get("name") for i in form.find_all("input")
                   if i.get("type", "text") in ("text", "email") and i.get("name")), "user")
    pfield = form.find("input", attrs={"type": "password"}).get("name", "passwd")
    data.update({ufield: user, pfield: pw})
    data.pop("send_cookie", None)
    data["send_cookie"] = "on"
    r = sess.post(urljoin(r.url, form.get("action") or r.url), data=data, timeout=60)
    r.raise_for_status()
    if BeautifulSoup(r.text, "lxml").find("input", attrs={"type": "password"}):
        msg = BeautifulSoup(r.text, "lxml").find(class_=re.compile("alert|error|ResultsWithError", re.I))
        hint = f" WeBWorK says: {msg.get_text(' ', strip=True)}" if msg else ""
        raise LoginError(f"WeBWorK login failed for {url}.{hint} If you normally open WeBWorK from Canvas "
                         "(no separate password), ask your instructor to set a WeBWorK password, or rely on "
                         "the Canvas sync if the sets also appear there.")
    return r.text


def fetch_course(cfg, c: dict) -> list[Item]:
    url = c["url"].rstrip("/") + "/"
    course = c.get("name") or urlparse(url).path.rstrip("/").split("/")[-1]
    sess = requests.Session()
    sess.headers["User-Agent"] = UA
    html = _login(sess, url, c.get("username", ""), c.get("password", ""))
    if not parse_sets(html, url, course, cfg.tz):  # login may have landed elsewhere
        html = sess.get(url, timeout=60).text
    items = parse_sets(html, url, course, cfg.tz)
    try:
        g = parse_grades(sess.get(url + "grades/", timeout=60).text)
    except requests.RequestException:
        g = {}
    for it in items:
        frac = g.get(it["title"].lower())
        if frac is not None:
            it["submitted"] = frac >= 0.999
            it["note"] = (it.get("note", "") + f"  Score: {frac:.0%}").strip()
    return items


def fetch(cfg) -> list[Item]:
    items, errors = [], []
    for c in cfg.webwork_courses:
        try:
            items += fetch_course(cfg, c)
        except Exception as e:
            errors.append(f"{c.get('name') or c.get('url')}: {e}")
    if errors and not items:
        raise RuntimeError("; ".join(errors))
    if errors:
        print("WeBWorK warnings:", "; ".join(errors))
    return items


def sync(cfg, cal, db, dry_run: bool = False) -> list[str]:
    return sync_items(cfg, cal, db, fetch(cfg), source="webwork", name="WeBWorK", icon="📐",
                      done_word="100% done", dry_run=dry_run)


def check(cfg) -> str:
    items = fetch(cfg)
    return f"{len(items)} open/upcoming set(s) in {len(cfg.webwork_courses)} course(s)"
