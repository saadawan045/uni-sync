"""Turns messages (WhatsApp / email) into calendar changes."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from . import llm
from .gcal import Ev, event_id, similarity

# ---------------------------------------------------------------- cheap pre-filter
# Only messages that pass this go to the (slow, CPU-only) model.
EVENT_WORDS = re.compile(
    r"\b(quiz|quizz|exam|mid ?terms?|mids|finals?|viva|presentation|assignments?|deadline|submi(t|ssion)|due|"
    r"lab|class(es)?|lectures?|cancel+(ed)?|postpone[d]?|reschedul\w*|shift(ed)?|moved?|make ?up|extra class|"
    r"holiday|off hai|off h[ae]?|chutti|nahi? ho ?g[ia]|nhi ho ?g[ia]|ho ?g[ia]|ho ?ga|test|project|report|"
    r"meeting|seminar|workshop|event|orientation|convocation|registration|fee|result|timetable|time ?table|venue|room|"
    # group announcements: "1:15pm student centre @all", "Reminder @all"
    r"reminder|@all|everyone|sab log|rso|society|club|session|gathering|student cent(re|er)|centre|center|"
    r"auditorium|hall|cafe|cafeteria|library|lawn|ground|reception|office|block)\b",
    re.I,
)
# personal plans and promises (English + Roman Urdu)
SOCIAL_WORDS = re.compile(
    r"\b(hang ?out|meet(ing|up)?|mil(te|ain|en|na|o)|chal(o|ein|en|te)|plan|dinner|lunch|breakfast|brunch|"
    r"chai|coffee|movie|cinema|film|match|game|futsal|cricket|football|gym|party|birthday|bday|wedding|"
    r"shaadi|shadi|mehndi|walima|barat|trip|outing|picnic|drive|concert|appointment|doctor|dentist|"
    # going somewhere together: "lets go to uni tmrw?", "chalein campus kal?", "need a lift wed"
    r"let'?s go|lets go|go (to|on)|going|jana|jaana|jayenge|chalein|chalen|uni|university|campus|"
    r"ride|lift|carpool|pick ?up|"
    r"interview|call|pick (you|u|me) up|drop|aa ?(jaun|jaon|jana|ja|raha|rahi|rahe)\w*|pohanch\w*|"
    r"i'?ll|i will|i'?m going to|will send|will bring|promise|remind me|kar ?d(unga|ungi|enge)|"
    r"bhej ?d(unga|ungi|enge)|de ?d(unga|ungi)|la ?d(unga|ungi)|done|pakka|confirm\w*)\b",
    re.I,
)
WHEN_WORDS = re.compile(
    r"\b(today|tomorrow|tonight|tmrw|tmr|tmrow|2moro|aaj|kal|parson|parso|next week|agl[ae] hafte|this week|is hafte|"
    r"weekend|morning|evening|afternoon|subah|shaam|sham|raat|dopehar|at \d{1,2}|"
    r"mon(day)?|tue(s|sday)?|wed(nesday)?|thu(r|rs|rsday)?|fri(day)?|sat(urday)?|sun(day)?|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
    r"\d{1,2}(st|nd|rd|th)|\d{1,2}[/.-]\d{1,2}|\d{1,2}\s?[:.]\s?\d{2}|\d{1,2}\s?(am|pm|baje))\b",
    re.I,
)
# A cancel/move is only accepted if the message itself says so (guards against the model inventing one)
CHANGE_WORDS = re.compile(
    r"\b(cancel+(ed|led|ing)?|call(ed)? off|postpone[ds]?|reschedul\w*|shift(ed)?|moved?|delay(ed)?|"
    r"off|won'?t|not (happening|coming|possible)|can'?t (make|come)|nahi?|nhi|na(hin)? ho|"
    r"instead|change[ds]?|push(ed)? (back|to)|preponed?|baad mein|ki jagah)\b", re.I)

# Someone saying THEY can't come is not the event being cancelled
ABSENCE = re.compile(
    r"\b(won'?t be able to (attend|come|make it|join)|can'?t (attend|come|make it|join)|won'?t be (there|in|coming)|"
    r"not be able to (attend|come|join)|unable to (attend|come|join)|(family|personal) (commitment|emergency)|"
    r"i'?m out|count me out|absent|leave (today|tomorrow)|nahi aa (sakta|sakti|sakte|payega|paungi|paunga)|"
    r"nahi aa raha|nahi aa rahi|nahi aa rahe|nhi aa)\b", re.I)
EVENT_OFF = re.compile(
    r"\b(cancel+(ed|led)?|call(ed)? off|postpone[ds]?|reschedul\w*|not happening|nahi? ho(gi|ga|gay|ge)|"
    r"nhi ho(gi|ga)|off hai|band hai|chutti|holiday|moved|shift(ed)?|instead)\b", re.I)

STRONG = re.compile(r"\b(cancel+(ed)?|postpone[d]?|reschedul\w*|quiz|deadline|mid ?terms?|viva)\b", re.I)


FILTER_MODES = ("strict", "loose", "off")


def worth_checking(text: str, personal: bool = True, mode: str = "loose") -> bool:
    """Which messages are worth the (slow) AI's time.
    strict: a plan word AND a time word          (least AI work, misses casual plans)
    loose:  any mention of a time or day at all  (default — catches "lets go wed?", "1:15pm student centre")
    off:    every message                        (most thorough; the AI may fall behind in busy groups)"""
    if mode == "off":
        return len(text.strip()) >= 2
    if len(text) < 6:
        return False
    w = bool(WHEN_WORDS.search(text))
    if mode == "loose" and w:
        return True
    e = bool(EVENT_WORDS.search(text)) or (personal and bool(SOCIAL_WORDS.search(text)))
    return (e and w) or bool(STRONG.search(text))


# ---------------------------------------------------------------- helpers
def _d(s: str) -> date | None:
    try:
        return date.fromisoformat((s or "").strip()[:10])
    except ValueError:
        return None


def _t(s: str) -> time | None:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", s or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return time(h, mi) if h < 24 and mi < 60 else None


def clean_time(s) -> str:
    """Whatever the model wrote ("23:59:59", "2026-11-09T23:59:59", "8pm", "8:30 PM", "20.00", "8 baje")
    → "HH:MM", or "" if it isn't a clock time."""
    s = str(s or "").strip().lower()
    if "t" in s and re.match(r"\d{4}-\d{2}-\d{2}t", s):
        s = s.split("t", 1)[1]
    m = re.fullmatch(r"(\d{1,2})(?:\s*[:.]\s*(\d{2}))?(?::\d{2}(?:\.\d+)?)?\s*(am|pm|a\.m\.|p\.m\.|baje)?"
                     r"(?:[+-]\d{2}:?\d{2}|z)?", s)
    if not m or (m.group(2) is None and not m.group(3)):
        return ""
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else ""


_WEEKDAYS = {
    0: r"mon(day)?|peer|pir|somwar", 1: r"tue(s|sday)?|mangal", 2: r"wed(nesday)?|budh|buddh",
    3: r"thu(r|rs|rsday)?|jumm?e?ra+t", 4: r"fri(day)?|jumm?a+h?", 5: r"sat(urday)?|hafta|hafte|haftay",
    6: r"sun(day)?|itwa+r|itvar",
}
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def resolve_day(words: str, ref: date, model_date: date | None = None) -> date | None:
    """Turn the day words the model quoted ("Friday", "kal", "parson", "next Monday", "3rd Nov", "5/11")
    into a date, counted from the day the message was sent. Small models are bad at calendar arithmetic,
    so code does it. Returns None when the words aren't understood (the model's own date is then kept)."""
    w = f" {(words or '').lower()} "
    if re.search(r"\b(day after tomorrow|parson|parso|parsoon)\b", w):
        return ref + timedelta(days=2)
    if re.search(r"\b(tomorrow|tmrw?|tmrow|tomorow|2moro|kal|kl)\b", w):
        if model_date == ref - timedelta(days=1):  # "kal" is also "yesterday"; trust the model there
            return model_date
        return ref + timedelta(days=1)
    if re.search(r"\b(today|tonight|aaj|aj|abhi)\b", w):
        return ref
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+)?(" + "|".join(_MONTHS) + r")[a-z]*\b", w) or \
        re.search(r"\b(" + "|".join(_MONTHS) + r")[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b", w)
    if m:
        a, b = m.groups()
        day, mon = (int(a), b) if a.isdigit() else (int(b), a)
        return _future_date(ref, _MONTHS.index(mon[:3]) + 1, day)
    m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", w)  # Pakistan writes day/month
    if m:
        return _future_date(ref, int(m.group(2)), int(m.group(1)))
    for wd, pat in _WEEKDAYS.items():
        if re.search(rf"\b({pat})\b", w):
            ahead = (wd - ref.weekday()) % 7
            if re.search(r"\b(next|agl[ae]|agley)\b", w) and ahead == 0:
                ahead = 7
            return ref + timedelta(days=ahead)
    return None


def _future_date(ref: date, month: int, day: int) -> date | None:
    for y in (ref.year, ref.year + 1):
        try:
            d = date(y, month, day)
        except ValueError:
            return None
        if d >= ref - timedelta(days=1):
            return d
    return None


DAYTIME_KINDS = {"class", "quiz", "exam", "presentation"}
EVENING = re.compile(r"\b(pm|p\.m\.|night|tonight|evening|raat|shaam|sham|midnight|online|zoom|meet\.google)\b"
                     r"|\d\s*pm\b", re.I)


MORNING = re.compile(r"\b(am|a\.m\.|morning|subah|subha|savere|breakfast|nashta|fajr|sehri|sehr)\b|\d\s*am\b", re.I)


HOLIDAY = re.compile(r"\b(holidays?|chutti|chhutti|chuttee|day off|campus closed|university closed|uni closed|"
                     r"gazetted)\b", re.I)


CLOCK = re.compile(r"\d{1,2}\s*[:.]\s*\d{2}|\b\d{1,2}\s*(am|pm|baje)\b|\b(noon|midnight|dopehar)\b", re.I)
SIGNUP = re.compile(r"\b(registrations?|trials?|try ?outs?|sign ?ups?)\b", re.I)
JUNK_TITLES = {"class", "lecture", "event", "other", "meeting @all", "@all"}


def _batch_text(msgs: list[dict]) -> str:
    return " ".join(str(m.get("text", "")) for m in msgs).lower()


def day_is_stated(it: dict, msgs: list[dict], src: dict, today: date) -> bool:
    """The date must come from the chat. No day words and no clock time means the model made the date up."""
    words = (it.get("day_words") or "").strip().lower()
    if not words:
        return bool(CLOCK.search(str(src.get("text", ""))))  # "Reminder @all 5pm" = next time 5pm comes round
    hay = _batch_text(msgs)
    if any(tok[:3] in hay for tok in re.findall(r"[a-z0-9]{3,}", words)):
        return True
    return resolve_day(words, today) is not None and bool(WHEN_WORDS.search(hay))  # model translated "aaj" → "today"


def drop_invented_times(it: dict, msgs: list[dict]) -> None:
    """The model likes to fill in 08:00 / 14:00 / 20:00 when no time was said. Keep a time only if that
    hour is actually written in the chat; otherwise the event becomes all-day."""
    hay = _batch_text(msgs)
    for k in ("start", "end"):
        t = clean_time(it.get(k, ""))
        h = int(t[:2]) if t else None
        said = t and (re.search(rf"(?<!\d)0?({h}|{h % 12 or 12})(?!\d)", hay)
                      or (h == 12 and re.search(r"\b(noon|dopehar)\b", hay)))
        if not said:
            it[k] = ""
    if not it.get("start"):
        it["end"] = ""


def not_for_me(cfg, it: dict, msgs: list[dict], src: dict, where: str) -> bool:
    """Deterministic checks for the things the small model keeps getting wrong."""
    kind, title = it.get("kind"), (it.get("title") or "").strip()
    if kind == "commitment" and src.get("sender") != "Me":
        return True  # a promise is only yours if you made it ("Stay healthy", "Lock in" from other people)
    chat = where.split("·")[-1].strip().lower()
    if kind in ("class", "event", "other") and title.lower() in JUNK_TITLES | {chat}:
        return True  # "Class", or just the group's name: nothing says what it is
    if msgs and msgs[0].get("group") and not any(m.get("sender") == "Me" for m in msgs):
        hay = _batch_text(msgs)
        named = any(re.search(rf"\b{re.escape(n)}\b", hay) for n in getattr(cfg, "my_names", []))
        official = kind in ("deadline", "assignment") or re.search(r"\b(course|semester|fee|exam)\b", title, re.I)
        if not named and (kind == "social" or (SIGNUP.search(title) and not official)):
            return True  # broadcast to a group you didn't reply to: sports sign-ups, other people's matches
    return False


COURSE_KINDS = {"class", "quiz", "exam", "assignment", "presentation"}
COURSE_CODE = re.compile(r"\b([a-z]{2,4})[-\s]?(\d{3}l?)\b")
# Everyday words in a title that don't say which course it is ("Quiz 1", "Final exams begin")
NOT_A_SUBJECT = set("""quiz quizzes quizz test exam exams examination mid mids midterm midterms terminal final finals
    assignment assignments homework hw project report lab labs class classes lecture lectures viva presentation
    presentations deadline due submission makeup extra surprise pop group online mock practice retake revision
    weekly first second third next last new old begin begins start starts schedule datesheet week section sec
    course subject room of the for and in on at to by a an my our all with from
    monday tuesday wednesday thursday friday saturday sunday today tomorrow kal aaj parson
    cancelled canceled postponed rescheduled moved shifted""".split())
NAME_NOISE = set("to of the and for i ii iii iv introduction university understanding application holy "
                 "engineers laboratory lab languages presentations".split())
# Other names people use for a course; only used when the word on the left is in your enrolled course's name
COURSE_ALIASES = {"calculus": {"calc", "math", "maths", "mat"}, "programming": {"pl", "ipl", "python", "coding"},
                  "physics": {"phy", "phys", "mechanics"}, "speaking": {"com", "communication", "public"},
                  "quran": {"quraan", "qur", "nazra", "tarjuma"}, "ict": {"ict"}, "ethics": {"ethics"}}


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z]+", re.sub(r"'s\b|['’]", "", (s or "").lower()))


def course_words(enrolled: list[dict]) -> tuple[set, set]:
    """→ (words that mean one of your courses, your course codes like "mat266")."""
    words, codes = set(), set()
    for c in enrolled:
        code = re.sub(r"[^a-z0-9]", "", (c.get("code") or "").lower())
        if code:
            codes.add(code)
            m = re.match(r"[a-z]+", code)
            if m:
                words.add(m.group())
        for w in _words(c.get("name", "")):
            if w not in NAME_NOISE:
                words.add(w)
                words |= COURSE_ALIASES.get(w, set())
    return words, codes


def other_course(cfg, it: dict, text: str) -> bool:
    """True when the event is about a course you aren't enrolled in ("OOP quiz" in a batch group chat).
    Titles that name no subject at all ("Quiz 1") can't be judged, so they are kept."""
    enrolled = getattr(cfg, "enrolled", None)
    kind, title = it.get("kind"), it.get("title") or ""
    if not enrolled or HOLIDAY.search(title):
        return False
    if kind == "deadline":          # could be a fee or form; only a stated course counts
        label = it.get("course") or ""
    elif kind in COURSE_KINDS:
        label = f"{title} {it.get('course') or ''}"
    else:
        return False
    words, codes = course_words(enrolled)
    named = [a + b for a, b in COURSE_CODE.findall(label.lower())]
    if named:
        return not any(c in codes for c in named)
    if words & set(_words(text)):
        return False                # the message itself mentions one of your courses
    toks, subject, skip = _words(label), [], False
    for t in toks:
        if skip:
            skip = False
        elif t in ("sir", "miss", "maam", "mam", "dr", "prof", "mr", "ms"):
            skip = True             # "Sir Ahmed's quiz": a teacher's name isn't a course
        elif t not in NOT_A_SUBJECT and len(t) > 1:
            subject.append(t)
    return bool(subject) and not (set(subject) & words)


def invented_name(title: str, msgs: list[dict], where: str) -> bool:
    """'Dinner with Jamal' when nobody in the chat wrote or is called Jamal → the model made it up
    (it copied a name from its own instructions once)."""
    m = re.search(r"\bwith\s+([A-Za-z][\w'-]+)", title or "")
    if not m or m.group(1).lower() in {"the", "a", "an", "my", "your", "friends", "family", "sir", "maam",
                                       "ma'am", "cr", "class", "team", "group", "everyone", "all", "others"}:
        return False
    hay = " ".join([where] + [f"{x.get('sender', '')} {x.get('text', '')}" for x in msgs]).lower()
    return m.group(1).lower() not in hay


def normalize(cfg, item: dict, text: str, sent: datetime | None = None) -> dict | None:
    """Fix the small model's usual slips before anything touches the calendar.
    Returns None when the item must be ignored. `text` is the message (+ its reply quote)."""
    it = dict(item)
    for k in ("start", "end", "new_start", "new_end"):
        it[k] = clean_time(it.get(k, ""))
    for k in ("date", "new_date"):
        it[k] = str(it.get(k) or "")[:10]
    changed = bool(CHANGE_WORDS.search(text))
    act = it.get("action") or "add"
    if act == "reschedule" and not changed:
        # The model often calls a brand-new event a "reschedule". With no change word in the message,
        # it can only be new: add it at the time given.
        act = "add"
        for k in ("date", "start", "end"):
            if it.get(f"new_{k}"):
                it[k] = it[f"new_{k}"]
    if act == "cancel" and not changed:
        return None  # nothing in the message says anything was cancelled
    if act == "cancel" and ABSENCE.search(text) and not EVENT_OFF.search(text):
        return None  # someone can't make it — the event itself still happens
    it["action"] = act
    title = it.get("title") or ""
    if re.search(r"\b(class|lecture|lab)\b", title, re.I) and not re.search(r"\b(quiz|exam|viva|test)\b", title, re.I):
        it["kind"] = "class"  # "Calculus class" labelled exam → wouldn't find the class to cancel
    if it.get("kind") not in ("assignment", "deadline", "quiz") and HOLIDAY.search(title) and act == "add":
        it["kind"] = "holiday"  # "Iqbal Day Holiday" labelled exam → wouldn't clear the day's classes
    if it.get("kind") in ("social", "meeting") and not MORNING.search(text):
        # "mehndi 7 baje" → 19:00, not 07:00
        for k in ("start", "end", "new_start", "new_end"):
            if it[k] and 1 <= int(it[k][:2]) < 8:
                it[k] = f"{int(it[k][:2]) + 12:02d}{it[k][2:]}"
    if it.get("kind") in DAYTIME_KINDS and not EVENING.search(text):
        # "11 baje" has no am/pm; the model sometimes picks 23:00. Classes and exams happen in the daytime.
        for k in ("start", "end", "new_start", "new_end"):
            if it[k]:
                h, mi = map(int, it[k].split(":"))
                if h >= 20 or h < 7:
                    it[k] = f"{(h - 12) % 24 if h >= 20 else h + 12:02d}:{mi:02d}"
    ref =(sent or datetime.now(cfg.tz)).astimezone(cfg.tz).date()
    d = resolve_day(it.get("day_words", ""), ref, _d(it["date"]))
    if d:
        it["date"] = d.isoformat()
    if act == "reschedule":
        nd = resolve_day(it.get("new_day_words", ""), ref, _d(it["new_date"]))
        if nd:
            it["new_date"] = nd.isoformat()
        if not it["new_start"] and not it["new_date"] and it["start"]:
            it["new_start"] = it["start"]  # "will be at 8:30 instead of 10" → model put 8:30 in start
    return it


def _span(cfg, d: date, s: str, e: str, kind: str):
    st, en = _t(s), _t(e)
    if not st:
        return d, d + timedelta(days=1)  # all-day
    start = datetime.combine(d, st, cfg.tz)
    if en and en > st:
        end = datetime.combine(d, en, cfg.tz)
    else:
        mins = {"deadline": 15, "assignment": 15, "commitment": 15, "quiz": 30, "exam": 120, "class": 60,
                "social": 120}.get(kind, 60)
        end = start + timedelta(minutes=mins)
    return start, end


PERSONAL_KINDS = {"social", "appointment", "birthday", "commitment"}
EMOJI = {"quiz": "📝", "exam": "📝", "assignment": "📌", "deadline": "📌", "presentation": "🎤",
         "holiday": "🏖️", "meeting": "👥", "event": "📅", "social": "🎉", "appointment": "🩺",
         "birthday": "🎂", "commitment": "🤝"}


# ---------------------------------------------------------------- apply
class Applier:
    def __init__(self, cfg, cal, db, dry_run=False):
        self.cfg, self.cal, self.db, self.dry = cfg, cal, db, dry_run
        self.notes: list[str] = []
        self.maybe: list[str] = []

    def apply(self, item: dict, source: str, where: str, snippet: str, sent: datetime | None = None) -> None:
        today = datetime.now(self.cfg.tz).date()
        item = normalize(self.cfg, item, snippet, sent)
        if item is None:
            return
        kind = item.get("kind", "other")
        title = (item.get("title") or "").strip()
        course = (item.get("course") or "").strip()
        d = _d(item.get("date", ""))
        conf = float(item.get("confidence", 0) or 0)
        action = item.get("action", "add")
        snippet = snippet.replace("\n", " ")[:160]
        if not title or not d or not (today - timedelta(days=1) <= d <= today + timedelta(days=180)):
            return
        if kind in PERSONAL_KINDS and not getattr(self.cfg, "detect_personal", True):
            return
        label = f"{title} — {d:%a %d %b}" + (f" {item.get('start')}" if item.get("start") else "")
        if conf < self.cfg.min_confidence:
            self.maybe.append(f"❔ {label}  ({action}, {conf:.0%} sure) — {where}: “{snippet}”")
            return

        if action == "add":
            if kind == "holiday":  # a holiday is a cancel-everything for that day
                return self._cancel_day(d, title, where, snippet)
            st, en = _span(self.cfg, d, item.get("start", ""), item.get("end", ""), kind)
            dup = self.cal.find(d, title, kind, 0.8) if not self.dry else None
            if dup:
                return
            ev = Ev(title=f"{EMOJI.get(kind, '')} {title}".strip(), start=st, end=en, source=source, kind=kind,
                    location=item.get("location", ""),
                    description=f"From {where}: “{snippet}”" + (f"\nCourse: {course}" if course else ""),
                    id=event_id(source, title.lower(), d.isoformat(), item.get("start", "")))
            if not self.dry:
                self.cal.upsert(ev)
            self.notes.append(f"➕ Added {label} — {where}: “{snippet}”")

        elif action in ("cancel", "reschedule") and self.dry:
            new = f" → {item.get('new_date') or d.isoformat()} {item.get('new_start', '')}".rstrip() if action == "reschedule" else ""
            self.notes.append(f"{'❌' if action == 'cancel' else '🔁'} Would {action} “{course or title}” on {d:%a %d %b}{new} "
                              f"(test mode: calendar not checked) — {where}: “{snippet}”")

        elif action in ("cancel", "reschedule"):
            target = None if self.dry else (self.cal.find(d, course or title, kind) or self.cal.find(d, title, kind))
            if not target and not self.dry:
                target = self._find_nearby(d, course or title, kind)
            if not target:
                self.maybe.append(f"❔ Couldn't find “{course or title}” on {d:%a %d %b} to {action} — {where}: “{snippet}”")
                return
            if action == "cancel":
                self._remove(target, f"cancelled per {where}")
                self.notes.append(f"❌ Removed {target.title} {target.start:%a %d %b %H:%M} — {where}: “{snippet}”")
            else:
                nd = _d(item.get("new_date", "")) or d
                if not item.get("new_start") and nd == d:
                    self.maybe.append(f"❔ “{target.title}” moved, but new time unclear — {where}: “{snippet}”")
                    return
                ns = item.get("new_start") or (f"{target.start:%H:%M}" if not target.all_day else "")
                dur = (target.end - target.start) if not target.all_day else timedelta(hours=1)
                st, en = _span(self.cfg, nd, ns, item.get("new_end", ""), target.kind)
                if isinstance(st, datetime) and not item.get("new_end"):
                    en = st + dur
                self._remove(target, f"moved per {where}")
                moved = Ev(title=target.title, start=st, end=en, source=source, kind=target.kind,
                           location=item.get("location") or target.location,
                           description=f"Moved from {target.start:%a %d %b %H:%M} — {where}: “{snippet}”",
                           id=event_id(source, "moved", target.id, nd.isoformat(), ns))
                if not self.dry:
                    self.cal.upsert(moved)
                self.notes.append(f"🔁 Moved {target.title}: {target.start:%a %d %b %H:%M} → "
                                  f"{st:%a %d %b %H:%M} — {where}: “{snippet}”")

    def _find_nearby(self, d: date, title: str, kind: str):
        """The model got the day wrong but the title is clear: accept a single strong match
        within a week either side (never guess between several)."""
        from .gcal import similarity
        start = datetime.combine(d - timedelta(days=7), time(0), self.cfg.tz)
        evs = self.cal.list_all(start, start + timedelta(days=15))
        strong = [ev for ev in evs if similarity(title, ev.title) >= 0.8]
        return strong[0] if len(strong) == 1 else None

    def _remove(self, ev: Ev, reason: str) -> None:
        if self.dry:
            return
        if ev.source == "portal":
            self.db.add_override(ev.id, "cancelled", reason)  # so the next portal sync doesn't bring it back
        self.cal.delete(ev.id)

    def _cancel_day(self, d: date, title: str, where: str, snippet: str) -> None:
        start = datetime.combine(d, time(0), self.cfg.tz)
        evs = [] if self.dry else self.cal.list(start, start + timedelta(days=1), source="portal")
        for ev in evs:
            self._remove(ev, f"{title} per {where}")
        self.notes.append(f"🏖️ {title} {d:%a %d %b}: removed {len(evs)} class(es) — {where}: “{snippet[:120]}”")


def propose(cfg, source: str, where: str, msgs: list[dict], now: datetime | None = None):
    """Ask the model, then double-check every new event it proposes.
    Returns [(item, quote, sent_time)] ready for Applier.apply."""
    for i, m in enumerate(msgs, 1):
        m["n"] = i
    out = []
    for it in llm.extract(cfg, msgs, f"{source}: {where}", now=now):
        n = it.get("msg") or 1
        src = msgs[n - 1] if 1 <= n <= len(msgs) else msgs[0]
        quote = src["text"] if not src.get("sender") else f"{src['sender']}: {src['text']}"
        if invented_name(it.get("title", ""), msgs, where):
            continue
        if other_course(cfg, it, str(src.get("text", ""))):
            print("skipped 1 event: not one of your enrolled courses")  # the journal never gets the text
            continue
        if (it.get("action") or "add") == "add":
            today = (src.get("time") or now or datetime.now(cfg.tz)).date()
            if not_for_me(cfg, it, msgs, src, where) or not day_is_stated(it, msgs, src, today):
                continue
            drop_invented_times(it, msgs)
        if (it.get("action") or "add") == "add" and getattr(cfg, "verify", False) \
                and not llm.verify(cfg, msgs, where, it):
            continue  # someone else's plan, a joke, chit-chat…
        out.append((it, quote, src.get("time")))
    return out


def run_batch(cfg, applier: Applier, source: str, where: str, msgs: list[dict]) -> int:
    """msgs: [{"sender","time","text"}]. Returns number of events kept after the double-check."""
    kept = propose(cfg, source, where, msgs)
    for it, quote, sent in kept:
        applier.apply(it, source, where, quote, sent)
    return len(kept)
