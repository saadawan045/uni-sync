from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from unisync.extract import Applier, worth_checking
from unisync.gcal import Ev, best_match, event_id, similarity
from unisync.portal import parse_range, parse_timetable, occurrences

TZ = ZoneInfo("Asia/Karachi")

# --- three common timetable layouts --------------------------------------------------
DAYS_IN_ROWS = """
<table><tr><th>Day</th><th>08:30 - 09:50</th><th>10:00 - 11:20</th><th>11:30 - 12:50</th><th>01:30 - 02:50</th></tr>
<tr><td>Monday</td><td>Data Structures<br>Room 12<br>Dr. Ali</td><td colspan="2">OOP Lab<br>Lab 3</td><td></td></tr>
<tr><td>Tuesday</td><td></td><td>Calculus II<br>Room 5</td><td>Lunch Break</td><td>Physics<br>Hall B</td></tr>
</table>"""

DAYS_IN_COLS = """
<table><thead><tr><th>Time</th><th>Mon</th><th>Tue</th><th>Wed</th></tr></thead>
<tr><td>8:30 AM</td><td rowspan="2">DSA<br>CR-4</td><td>-</td><td>English</td></tr>
<tr><td>9:30 AM</td><td>Calculus</td><td></td></tr>
<tr><td>10:30 AM</td><td></td><td></td><td>Physics Lab</td></tr>
</table>"""

LIST = """
<table><tr><th>Course</th><th>Day</th><th>Time</th><th>Room</th><th>Teacher</th></tr>
<tr><td>Operating Systems</td><td>Wednesday</td><td>11:00 AM - 12:20 PM</td><td>R-201</td><td>Sir Usman</td></tr>
<tr><td>Databases</td><td>Thursday</td><td>2:00 PM - 3:20 PM</td><td>Lab 2</td><td>Ms. Sara</td></tr>
</table>"""


def test_days_in_rows():
    s = parse_timetable(DAYS_IN_ROWS)
    titles = {(x.day, x.title, x.start, x.end) for x in s}
    assert (0, "Data Structures", time(8, 30), time(9, 50)) in titles
    assert (0, "OOP Lab", time(10, 0), time(12, 50)) in titles          # colspan merged
    assert (1, "Physics", time(13, 30), time(14, 50)) in titles         # "01:30" → afternoon
    assert not any("Lunch" in x.title for x in s)
    ds = next(x for x in s if x.title == "Data Structures")
    assert ds.location == "Room 12" and "Dr. Ali" in ds.detail


def test_days_in_cols():
    s = parse_timetable(DAYS_IN_COLS)
    got = {(x.day, x.title, x.start, x.end) for x in s}
    assert (0, "DSA", time(8, 30), time(10, 30)) in got                 # rowspan merged
    assert (1, "Calculus", time(9, 30), time(10, 30)) in got
    assert (2, "Physics Lab", time(10, 30), time(11, 30)) in got
    assert len(s) == 4


def test_list_layout():
    s = parse_timetable(LIST)
    assert [(x.day, x.title, x.start, x.end, x.location) for x in s] == [
        (2, "Operating Systems", time(11, 0), time(12, 20), "R-201"),
        (3, "Databases", time(14, 0), time(15, 20), "Lab 2"),
    ]


def test_ranges():
    assert parse_range("11:30 AM - 12:50 PM") == (time(11, 30), time(12, 50))
    assert parse_range("12:30 - 1:50") == (time(12, 30), time(13, 50))
    assert parse_range("9.00 to 10.15") == (time(9, 0), time(10, 15))
    assert parse_range("1-2") is None


def test_occurrences_and_ids_stable():
    cfg = SimpleNamespace(tz=TZ, weeks_ahead=2, semester_end="")
    s = parse_timetable(LIST)
    a = occurrences(s, cfg, date(2030, 1, 7))
    b = occurrences(s, cfg, date(2030, 1, 7))
    assert len(a) == 4 and [e.id for e in a] == [e.id for e in b]
    assert all(e.id[:2] == "us" and set(e.id[2:]) <= set("0123456789abcdef") for e in a)


# --- pre-filter ------------------------------------------------------------------------
def test_prefilter():
    yes = ["DSA quiz kal 10 baje", "Tomorrow's OOP class is cancelled", "Assignment 3 deadline Friday 11:59pm",
           "kal class nahi hogi", "Mid terms start from 20th Oct", "Physics lab shifted to Thursday",
           "Quiz postponed"]
    no = ["hahaha 😂", "who has the notes?", "ok", "Happy birthday bro!!", "kya scene hai"]
    assert all(worth_checking(t) for t in yes), [t for t in yes if not worth_checking(t)]
    assert not any(worth_checking(t) for t in no), [t for t in no if worth_checking(t)]


def test_similarity():
    assert similarity("DSA", "Data Structures and Algorithms") >= 0.8
    assert similarity("OOP class", "OOP Lab") >= 0.55
    assert similarity("Physics", "Calculus II") < 0.55


# --- applying AI output with a fake calendar -------------------------------------------
class FakeCal:
    def __init__(self, evs):
        self.evs = {e.id: e for e in evs}

    def upsert(self, ev):
        self.evs[ev.id] = ev

    def delete(self, eid):
        self.evs.pop(eid, None)

    def list(self, s, e, source=None):
        def at(x):  # all-day events start at midnight
            return x if isinstance(x, datetime) else datetime.combine(x, time(0), TZ)
        return [v for v in self.evs.values() if s <= at(v.start) < e and (not source or v.source == source)]

    def list_all(self, s, e):
        return [v for v in self.evs.values() if isinstance(v.start, datetime) and s <= v.start < e]

    def find(self, day, title, kind=None, threshold=0.55):
        evs = [v for v in self.evs.values() if (v.start.date() if isinstance(v.start, datetime) else v.start) == day]
        return best_match(evs, title, kind, threshold)


class FakeDB:
    def __init__(self):
        self.o = {}

    def add_override(self, i, k, r):
        self.o[i] = k


def _cfg():
    return SimpleNamespace(tz=TZ, min_confidence=0.6)


def test_apply_add_cancel_move():
    tomorrow = datetime.now(TZ).date() + timedelta(days=1)
    st = datetime.combine(tomorrow, time(8, 30), TZ)
    cls = Ev("Data Structures", st, st + timedelta(minutes=80), "portal", id=event_id("x"))
    cal, db = FakeCal([cls]), FakeDB()
    ap = Applier(_cfg(), cal, db)

    ap.apply({"action": "add", "kind": "quiz", "title": "DSA Quiz 2", "date": tomorrow.isoformat(),
              "start": "10:00", "confidence": 0.9}, "whatsapp", "WhatsApp · BSCS", "quiz kal 10 baje")
    quiz = [e for e in cal.evs.values() if "Quiz" in e.title][0]
    assert quiz.start.time() == time(10) and quiz.kind == "quiz"

    # same quiz announced again → no duplicate
    ap.apply({"action": "add", "kind": "quiz", "title": "DSA Quiz 2", "date": tomorrow.isoformat(),
              "start": "10:00", "confidence": 0.9}, "whatsapp", "x", "again")
    assert sum("Quiz" in e.title for e in cal.evs.values()) == 1

    # move the class → original removed + remembered so portal sync won't re-add it
    ap.apply({"action": "reschedule", "kind": "class", "title": "DSA class", "course": "DSA",
              "date": tomorrow.isoformat(), "new_start": "14:00", "confidence": 0.85}, "whatsapp", "x", "shifted to 2")
    assert cls.id not in cal.evs and db.o[cls.id] == "cancelled"
    moved = [e for e in cal.evs.values() if e.title == "Data Structures"][0]
    assert moved.start.time() == time(14) and moved.end - moved.start == timedelta(minutes=80)

    # low confidence → only reported, not added
    ap.apply({"action": "add", "kind": "event", "title": "Maybe party", "date": tomorrow.isoformat(),
              "confidence": 0.3}, "whatsapp", "x", "party?")
    assert not any("party" in e.title.lower() for e in cal.evs.values()) and ap.maybe


def test_cancel_class_not_quiz():
    tomorrow = datetime.now(TZ).date() + timedelta(days=1)
    st = datetime.combine(tomorrow, time(8, 30), TZ)
    cls = Ev("Data Structures", st, st + timedelta(hours=1), "portal", kind="class", id="c")
    quiz = Ev("📝 DSA Quiz 2", st + timedelta(hours=2), st + timedelta(hours=3), "whatsapp", kind="quiz", id="q")
    cal = FakeCal([cls, quiz])
    Applier(_cfg(), cal, FakeDB()).apply({"action": "cancel", "kind": "class", "title": "DSA class", "course": "DSA",
        "date": tomorrow.isoformat(), "confidence": 0.9}, "whatsapp", "x", "kal DSA ki class nahi hogi")
    assert set(cal.evs) == {"q"}


# --- NIT/effii FullCalendar timetable ---------------------------------------------------
import html as _html, json as _json
from unisync.portal import parse_fullcalendar, _timetable_url


def _fc_page(events):
    return f"<div id='time_section' data-events='{_html.escape(_json.dumps(events), quote=True)}'></div><div id='calendar1'></div>"


def test_fullcalendar_dated_events():
    ev = [
        {"title": "Data Structures<br>Room 12<br>Dr. Ali", "start": "2026-10-05T08:30:00", "end": "2026-10-05T09:50:00"},
        {"title": "Data Structures<br>Room 12<br>Dr. Ali", "start": "2026-10-12T08:30:00", "end": "2026-10-12T09:50:00"},
        {"title": "Calculus", "start": "2026-10-06T13:00:00", "end": "2026-10-06T14:20:00", "extendedProps": {"room": "Hall B"}},
        {"title": "Holiday", "start": "2026-10-09T00:00:00", "end": "2026-10-10T00:00:00"},
    ]
    s = parse_fullcalendar(_fc_page(ev))
    assert [(x.day, x.title, x.start, x.end, x.location) for x in s] == [
        (0, "Data Structures", time(8, 30), time(9, 50), "Room 12"),
        (1, "Calculus", time(13, 0), time(14, 20), "Hall B"),
    ]
    assert "Dr. Ali" in s[0].detail
    assert parse_timetable(_fc_page(ev)) == s


def test_fullcalendar_recurring_events():
    ev = [{"title": "OOP Lab", "daysOfWeek": [3, 5], "startTime": "10:00:00", "endTime": "12:50:00"}]
    s = parse_fullcalendar(_fc_page(ev))
    assert [(x.day, x.start) for x in s] == [(2, time(10)), (4, time(10))]


def test_timetable_tile_url():
    class P:
        url = "https://nit.effii.app/portal/portal.php"
        def content(self):
            return """<a type="button" onclick="document.location.href='timetable.php?__PID=ab%2Bc&amp;__ID=x%3D%3D'">"""
    assert _timetable_url(P()) == "https://nit.effii.app/portal/timetable.php?__PID=ab%2Bc&__ID=x%3D%3D"


NIT_TITLE = ("(Lecture)<br><br>\r\n        <br><br>\r\n        Public Speaking and Presentations<br><br>\r\n"
             "        Section: S3<br><br>Ground Floor - A003<br><br>\r\n        Instructors:<br>Ms. Umam Khalid")
NIT_LAB = ("(Lab)<br><br>\r\n        <br><br>\r\n        Application to ICT Lab<br><br>\r\n"
           "        Section: S3<br><br>1st Floor - Computer Lab - A106<br><br>\r\n        Instructors:<br>Mr. X")


def test_nit_real_format():
    from unisync.portal import parse_fullcalendar_dated, dated_events
    ev = [{"title": NIT_TITLE, "start": "2030-10-07T12:00:00", "end": "2030-10-07T13:15:00"},
          {"title": NIT_TITLE, "start": "2030-10-14T12:00:00", "end": "2030-10-14T13:15:00"},
          {"title": NIT_LAB, "start": "2030-10-08T10:30:00", "end": "2030-10-08T11:45:00"}]
    s = parse_fullcalendar(_fc_page(ev))
    assert [(x.title, x.location) for x in s] == [
        ("Public Speaking and Presentations", "Ground Floor - A003"),
        ("Application to ICT Lab", "1st Floor - Computer Lab - A106")]
    assert s[0].detail == "Lecture · Section S3 · Ms. Umam Khalid"
    d = parse_fullcalendar_dated(_fc_page(ev))
    cfg = SimpleNamespace(tz=TZ, weeks_ahead=1, semester_end="")
    evs = dated_events(d, cfg, date(2030, 10, 6))
    assert [e.start.date().isoformat() for e in evs] == ["2030-10-07", "2030-10-08"]   # 14th is past the 1-week horizon


def test_cancel_finds_event_when_model_gets_day_wrong():
    thu = datetime.now(TZ).date() + timedelta(days=1)
    st = datetime.combine(thu, time(14), TZ)
    viva = Ev("📝 OOP viva", st, st + timedelta(hours=1), "whatsapp", kind="exam", id="v")
    cal = FakeCal([viva])
    ap = Applier(_cfg(), cal, FakeDB())
    # model says "today" instead of Thursday, but the title is unambiguous
    ap.apply({"action": "cancel", "kind": "exam", "title": "OOP viva", "date": datetime.now(TZ).date().isoformat(),
              "confidence": 0.9}, "whatsapp", "x", "[replying to: “OOP viva Thursday 2pm”] cancelled")
    assert "v" not in cal.evs and ap.notes


def test_reply_context_passes_prefilter():
    from unisync.extract import worth_checking
    assert worth_checking("[replying to: “OOP viva Thursday 2pm”]\ncancelled")


def test_prefilter_personal():
    from unisync.extract import worth_checking
    yes = ["chalo kal 8 baje dinner pe milte hain", "Saturday movie?", "I'll send you the notes by Friday",
           "dentist appointment tomorrow 4pm", "kal tak bhej dunga", "Ali's birthday on 12th"]
    no = ["hahaha 😂", "kya scene hai", "ok", "send pics"]
    assert all(worth_checking(t) for t in yes), [t for t in yes if not worth_checking(t)]
    assert not any(worth_checking(t) for t in no), [t for t in no if worth_checking(t)]
    assert not worth_checking("chalo kal 8 baje dinner pe milte hain", personal=False, mode="strict")


def test_personal_off_skips_social():
    tomorrow = datetime.now(TZ).date() + timedelta(days=1)
    cal = FakeCal([])
    cfg = SimpleNamespace(tz=TZ, min_confidence=0.6, detect_personal=False)
    Applier(cfg, cal, FakeDB()).apply({"action": "add", "kind": "social", "title": "Dinner with Ali",
        "date": tomorrow.isoformat(), "start": "20:00", "confidence": 0.9}, "whatsapp", "x", "dinner kal 8")
    assert not cal.evs


def test_wa_context_window(tmp_path, monkeypatch):
    """a 'done 👍' that confirms a plan is sent to the model as context next to the plan."""
    import json as J, unisync.cli as cli, unisync.extract as ex
    from unisync.db import DB
    d = tmp_path
    (d / "wa-auth-personal").mkdir(); (d / "wa-auth-personal/creds.json").write_text('{"account":{}}')
    rows = [("1", "Ali", "kya scene"), ("2", "Ali", "kal 8 baje dinner pe milte hain?"), ("3", "Me", "done 👍"),
            ("4", "Ali", "lol"), ("5", "Ali", "x"), ("6", "Ali", "y"), ("7", "Ali", "z"), ("8", "Ali", "far away")]
    (d / "wa-spool-personal.jsonl").write_text("\n".join(J.dumps(
        {"account": "personal", "id": i, "chat": "c", "chat_name": "Ali", "sender": s, "ts": 1791190000 + int(i),
         "text": t, "from_me": s == "Me"}) for i, s, t in rows))
    seen = []
    monkeypatch.setattr(ex.llm, "extract", lambda cfg, msgs, ctx, **kw: seen.append([(m["text"], m["context"]) for m in msgs]) or [])
    monkeypatch.setattr(cli, "_cal", lambda cfg: object())
    monkeypatch.setattr(cli, "_forward_alerts", lambda cfg: None)
    cfg = SimpleNamespace(data_dir=d, tz=TZ, only_chats=[], ignore_chats=[], detect_personal=True, min_confidence=0.6,
                          notify_changes=False, smtp_host="", notify_to="")
    cli.cmd_wa_process(cfg, DB(d), SimpleNamespace(dry_run=False))
    assert seen == [[("kya scene", True), ("kal 8 baje dinner pe milte hain?", False), ("done 👍", True),
                     ("lol", True)]]   # 3 before, 2 after
    assert not list(d.glob("wa-spool*"))


# --- Canvas -------------------------------------------------------------------------------
CANVAS_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Instructure//Canvas//EN
BEGIN:VEVENT
UID:event-assignment-111
DTSTART:20301010T185900Z
DTEND:20301010T185900Z
SUMMARY:OOP Assignment 3 [CS-112 S3]
URL:https://nit.instructure.com/courses/5/assignments/111
END:VEVENT
BEGIN:VEVENT
UID:event-assignment-222
DTSTART;VALUE=DATE:20301012
DTEND;VALUE=DATE:20301013
SUMMARY:Quiz 2 [MATH-101]
END:VEVENT
BEGIN:VEVENT
UID:event-calendar-event-9
DTSTART:20301011T050000Z
SUMMARY:Orientation
END:VEVENT
END:VCALENDAR
"""


def test_canvas_ics(monkeypatch):
    import unisync.canvas as cv
    real = cv.datetime
    class FixedDT(real):
        @classmethod
        def now(cls, tz=None):
            return real(2030, 10, 8, 12, 0, tzinfo=tz)
    monkeypatch.setattr(cv, "datetime", FixedDT)
    items = sorted(cv.parse_ics(CANVAS_ICS, TZ), key=lambda i: i["due"])
    assert [(i["id"], i["title"], i["course"], i["kind"]) for i in items] == [
        ("111", "OOP Assignment 3", "CS-112 S3", "assignment"), ("222", "Quiz 2", "MATH-101", "quiz")]
    assert items[0]["due"].hour == 23 and items[0]["due"].minute == 59          # 18:59 UTC → 23:59 PKT
    assert items[1]["due"].time() == time(23, 59)                               # all-day → end of day
    ev = cv.to_event(items[0], TZ)
    assert ev.title == "📌 OOP Assignment 3 due [CS-112 S3]" and ev.end == items[0]["due"] and ev.kind == "assignment"


def test_canvas_sync_notes(monkeypatch):
    import unisync.canvas as cv
    now = datetime.now(TZ)
    state = {}
    class KV:
        def get(self, k, d=None): return state.get(k, d)
        def set(self, k, v): state[k] = v
    cal = FakeCal([])
    cfg = SimpleNamespace(tz=TZ, canvas_token="t", canvas_url="u", canvas_ical_url="")
    a1 = cv.Item(id="1", title="Lab report", course="PHY", due=now + timedelta(days=3), url="", kind="assignment", submitted=False)
    monkeypatch.setattr(cv, "fetch", lambda cfg: [a1])
    assert cv.sync(cfg, cal, KV()) == []                       # first run: quiet, just fills the calendar
    assert len(cal.evs) == 1
    a2 = cv.Item(id="2", title="Essay", course="ENG", due=now + timedelta(days=5), url="", kind="assignment", submitted=False)
    a1b = cv.Item(**{**a1, "due": now + timedelta(days=4), "submitted": True})
    monkeypatch.setattr(cv, "fetch", lambda cfg: [a1b, a2])
    notes = cv.sync(cfg, cal, KV())
    assert any("New on Canvas: Essay" in n for n in notes) and any("Due date changed: Lab report" in n for n in notes)
    titles = sorted(e.title for e in cal.evs.values())
    assert titles == ["✅ Lab report due [PHY] (submitted)", "📌 Essay due [ENG]"]
    assert cv.due_soon(cfg, KV(), hours=24 * 6) == [n for n in cv.due_soon(cfg, KV(), hours=24 * 6) if "Essay" in n]


# --- WeBWorK ------------------------------------------------------------------------------
def test_webwork_parse_both_layouts():
    from unisync.webwork import parse_sets, parse_grades
    soon = (datetime.now(TZ) + timedelta(days=3)).replace(hour=23, minute=59, second=0, microsecond=0)
    later = soon + timedelta(days=7)
    new = (f'<ul><li><a href="/webwork2/M/HW_1/">HW_1</a> Open, closes {soon:%m/%d/%Y at %I:%M%p} PKT.</li>'
           f'<li><a href="/webwork2/M/HW_2/">HW_2</a> Will open on {soon:%m/%d/%Y at %I:%M%p} PKT, closes {later:%m/%d/%Y at %I:%M%p} PKT.</li>'
           '<li><a href="/webwork2/M/HW_0/">HW_0</a> Closed, answers available.</li>'
           '<li><a href="/webwork2/M/grades/">Grades</a></li></ul>')
    s = {i["title"]: i for i in parse_sets(new, "https://w/webwork2/M/", "MATH", TZ)}
    assert set(s) == {"HW 1", "HW 2"} and s["HW 1"]["due"] == soon and s["HW 2"]["due"] == later
    assert s["HW 2"]["note"].startswith("Opens")
    old = (f'<table><tr><td><a href="/webwork2/M/Quiz_3/?user=a&key=b">Quiz_3</a></td>'
           f'<td>open, due {soon:%b %d, %Y, %I:%M %p} PKT.</td></tr></table>')
    (q,) = parse_sets(old, "https://w/webwork2/M/", "MATH", TZ)
    assert q["title"] == "Quiz 3" and q["due"] == soon and q["id"] == "MATH:Quiz_3"
    g = parse_grades('<table><tr><td>HW_1</td><td>100%</td></tr><tr><td>HW_2</td><td>3 / 12</td></tr></table>')
    assert g == {"hw 1": 1.0, "hw 2": 0.25}


def test_no_invented_cancel():
    tomorrow = datetime.now(TZ).date() + timedelta(days=1)
    st = datetime.combine(tomorrow, time(9), TZ)
    cal = FakeCal([Ev("🎂 Ali Jaffer's Birthday", tomorrow, tomorrow + timedelta(days=1), "whatsapp", kind="birthday", id="b")])
    ap = Applier(_cfg(), cal, FakeDB())
    ap.apply({"action": "cancel", "kind": "birthday", "title": "Ali Jaffer's Birthday", "date": tomorrow.isoformat(),
              "confidence": 0.9}, "email", "Email from NayaPay",
             "NayaPay: Subject: You got Rs. 310 from Ali Jaffer  Cha-Ching!  08 Oct 2026, 07:25 PM")
    assert "b" in cal.evs and not ap.notes and not ap.maybe      # silently ignored
    ap.apply({"action": "cancel", "kind": "class", "title": "DSA", "date": tomorrow.isoformat(), "confidence": 0.9},
             "whatsapp", "x", "kal DSA ki class nahi hogi")
    assert ap.maybe or ap.notes                                   # real cancel wording still goes through


def test_email_skip():
    from unisync.mail import skip_reason
    cfg = SimpleNamespace(trusted_domains=["nit.edu.pk", "instructure.com"])
    pay = {"from": "NayaPay <noreply@nayapay.com>", "subject": "You got Rs. 310 from Ali Jaffer", "body": "Cha-Ching!"}
    otp = {"from": "Bank <alerts@bank.pk>", "subject": "Your OTP", "body": "Your verification code is 1234"}
    news = {"from": "Daraz <promo@daraz.pk>", "subject": "Mega sale Friday!", "body": "x", "bulk": True}
    uni = {"from": "Saad <saad@nit.edu.pk>", "subject": "FW: Fee challan Rs. 85000 due 15 Oct", "body": "pay by", "bulk": True}
    prof = {"from": "Dr Ali <ali@gmail.com>", "subject": "Quiz moved to Friday", "body": "see you"}
    assert skip_reason(cfg, pay) and skip_reason(cfg, otp) and skip_reason(cfg, news)
    assert skip_reason(cfg, uni) is None and skip_reason(cfg, prof) is None


def test_rso_conversation(tmp_path, monkeypatch):
    """Real case: meeting time posted, someone says they can't come, a reminder later."""
    import json as J, unisync.cli as cli, unisync.extract as ex
    from unisync.db import DB
    from unisync.extract import worth_checking
    assert worth_checking("1:15pm student centre \n@all") and worth_checking("1:15pm student centre @all Reminder @all")

    # the absence must never become a cancellation, even if the model proposes one
    mon = datetime.now(TZ).date()
    st = datetime.combine(mon, time(13, 15), TZ)
    cal = FakeCal([Ev("RSO meeting", st, st + timedelta(hours=1), "whatsapp", kind="meeting", id="rso")])
    ap = Applier(_cfg(), cal, FakeDB())
    ap.apply({"action": "cancel", "kind": "meeting", "title": "RSO meeting", "date": mon.isoformat(), "confidence": 0.8},
             "whatsapp", "NIT RSO", "Abdullah: Due to a family commitment me and @someone won't be in Lahore on Monday, "
             "we are so sorry we won't be able to attend the RSO meeting")
    assert "rso" in cal.evs and not ap.maybe
    # a real cancellation still works
    ap.apply({"action": "cancel", "kind": "meeting", "title": "RSO meeting", "date": mon.isoformat(), "confidence": 0.9},
             "whatsapp", "NIT RSO", "Muneeb: today's meeting is cancelled guys")
    assert "rso" not in cal.evs

    # messages arriving in different 5-minute runs still reach the model together
    d = tmp_path
    (d / "wa-auth-business").mkdir(); (d / "wa-auth-business/creds.json").write_text('{"account":{}}')
    seen = []
    monkeypatch.setattr(ex.llm, "extract", lambda cfg, msgs, ctx, **kw: seen.append([(m["text"], m["context"]) for m in msgs]) or [])
    monkeypatch.setattr(cli, "_cal", lambda cfg: object())
    monkeypatch.setattr(cli, "_forward_alerts", lambda cfg: None)
    cfg = SimpleNamespace(data_dir=d, tz=TZ, only_chats=[], ignore_chats=[], detect_personal=True, min_confidence=0.6,
                          notify_changes=False, smtp_host="", notify_to="", context_hours=12)
    now = int(datetime.now().timestamp())
    runs = [[("a", "Muneeb Sukhera", "Guys"), ("b", "Muneeb Sukhera", "1:15pm student centre \n@all")],
            [("c", "Abdullah", "Due to a family commitment me and @66700875706567 won't be in Lahore on Monday")],
            [("d", "Muneeb Sukhera", "1:15pm student centre \n@all\nReminder @all")]]
    db = DB(d)
    for k, run in enumerate(runs):
        (d / "wa-spool-business.jsonl").write_text("\n".join(J.dumps(
            {"account": "business", "id": i, "chat": "g", "chat_name": "NIT RSO 2025-26", "sender": s,
             "ts": now - 3600 + k * 600 + n, "text": t}) for n, (i, s, t) in enumerate(run)))
        cli.cmd_wa_process(cfg, db, SimpleNamespace(dry_run=False))
    last = seen[-1]
    assert [c for _, c in last] == [True, True, True, False]          # earlier runs come along as context
    assert "@66700875706567" not in J.dumps(seen) and "@someone" in J.dumps(seen)
    assert (d / "wa-context.json").stat().st_mode & 0o777 == 0o600


def test_go_to_uni_conversation(tmp_path, monkeypatch):
    """Real case: plan to travel to uni together, confirmed with 'Done'."""
    import json as J, unisync.cli as cli, unisync.extract as ex
    from unisync.db import DB
    d = tmp_path
    (d / "wa-auth-personal").mkdir(); (d / "wa-auth-personal/creds.json").write_text('{"account":{}}')
    convo = [("Me", "thanks man it means a lot, lets go to uni tmrw?"),
             ("Muhammad Jamal Roots", "Bro i have class at 3 tmr"),
             ("Muhammad Jamal Roots", "We can go on Wednesday if have class at 10:30 wbu"),
             ("Me", "i have class at 10:30 on wed asw"), ("Me", "so lets go then"),
             ("Muhammad Jamal Roots", "Done")]
    now = int(datetime.now().timestamp())
    (d / "wa-spool-personal.jsonl").write_text("\n".join(J.dumps(
        {"account": "personal", "id": f"j{n}", "chat": "jamal", "chat_name": "Muhammad Jamal Roots", "sender": s,
         "ts": now - 600 + n * 60, "text": t, "from_me": s == "Me"}) for n, (s, t) in enumerate(convo)))
    seen = []
    monkeypatch.setattr(ex.llm, "extract", lambda cfg, msgs, ctx, **kw: seen.append([m["text"] for m in msgs]) or [])
    monkeypatch.setattr(cli, "_cal", lambda cfg: object())
    monkeypatch.setattr(cli, "_forward_alerts", lambda cfg: None)
    cfg = SimpleNamespace(data_dir=d, tz=TZ, only_chats=[], ignore_chats=[], detect_personal=True, min_confidence=0.6,
                          notify_changes=False, smtp_host="", notify_to="", context_hours=12)
    cli.cmd_wa_process(cfg, DB(d), SimpleNamespace(dry_run=False))
    assert len(seen) == 1 and seen[0] == [t for _, t in convo]   # the whole plan, incl. "Done", in one batch


def test_canvas_undated_assignment(monkeypatch):
    import unisync.canvas as cv
    state = {}
    class KV:
        def get(self, k, d=None): return state.get(k, d)
        def set(self, k, v): state[k] = v
    cal = FakeCal([])
    cfg = SimpleNamespace(tz=TZ, canvas_token="t", canvas_url="u", canvas_ical_url="")
    now = datetime.now(TZ)
    seed = cv.Item(id="0", title="Old", course="X", due=now + timedelta(days=9), url="", kind="assignment", submitted=False)
    monkeypatch.setattr(cv, "fetch", lambda cfg: [seed])
    cv.sync(cfg, cal, KV())                                     # first run, quiet

    essay = cv.Item(id="7", title="Reflection essay", course="ENG", due=None, url="https://c/7", kind="assignment",
                    submitted=False)
    monkeypatch.setattr(cv, "fetch", lambda cfg: [seed, essay])
    notes = cv.sync(cfg, cal, KV())
    assert any("Reflection essay" in n and "no due date" in n for n in notes)      # you're told about it
    ev = cal.evs[event_id("canvas", "7")]
    assert ev.start == now.date() and ev.all_day and "no due date" in ev.title and ev.kind == "undated"
    assert cv.undated(KV()) == ["📌 Reflection essay [ENG]"]                         # morning email list

    # the teacher adds a due date later → becomes a normal timed deadline, and you're told
    dated = cv.Item(**{**essay, "due": now + timedelta(days=2)})
    monkeypatch.setattr(cv, "fetch", lambda cfg: [seed, dated])
    notes = cv.sync(cfg, cal, KV())
    assert any("no due date →" in n for n in notes) and not cal.evs[event_id("canvas", "7")].all_day

    # submitted while still undated → disappears quietly
    monkeypatch.setattr(cv, "fetch", lambda cfg: [seed, cv.Item(**{**essay, "submitted": True})])
    notes = cv.sync(cfg, cal, KV())
    assert event_id("canvas", "7") not in cal.evs and not any("Removed" in n for n in notes)


def test_canvas_api_keeps_undated(monkeypatch):
    import unisync.canvas as cv
    asked = []
    def fake_get_all(cfg, path, params=None):
        if path == "/courses":
            return [{"id": 1, "course_code": "ENG"}]
        asked.append(params["bucket"])
        if params["bucket"] == "undated":
            return [{"id": 7, "name": "Reflection essay", "due_at": None, "submission": {}},
                    {"id": 8, "name": "Roll Call Attendance", "due_at": None, "submission_types": ["external_tool"]},
                    {"id": 9, "name": "Attendance/Class Participation", "due_at": None, "submission_types": ["none"]},
                    {"id": 10, "name": "Mid-term (in class)", "due_at": None, "submission_types": ["not_graded"]}]
        return []
    monkeypatch.setattr(cv, "_get_all", fake_get_all)
    items = cv.fetch_api(SimpleNamespace(tz=TZ))
    assert "undated" in asked and [(i["title"], i["due"]) for i in items] == [("Reflection essay", None)]


# ---- AI output cleanup (failures seen when scoring qwen2.5:3b with tools/eval_ai.py) ----
def test_clean_time():
    from unisync.extract import clean_time
    assert clean_time("2026-11-09T23:59:59") == "23:59"
    assert clean_time("23:59:59") == "23:59"
    assert clean_time("8pm") == "20:00" and clean_time("8:30 PM") == "20:30" and clean_time("12 am") == "00:00"
    assert clean_time("20.00") == "20:00" and clean_time("8 baje") == "08:00"
    assert clean_time("") == "" and clean_time("evening") == "" and clean_time("8") == ""


def test_resolve_day():
    from unisync.extract import resolve_day
    mon = date(2026, 11, 2)  # a Monday
    assert resolve_day("Friday", mon) == date(2026, 11, 6)
    assert resolve_day("Tuesday", mon) == date(2026, 11, 3)
    assert resolve_day("Monday", mon) == mon and resolve_day("next Monday", mon) == date(2026, 11, 9)
    assert resolve_day("kal", mon) == date(2026, 11, 3) and resolve_day("Parson", mon) == date(2026, 11, 4)
    assert resolve_day("kal", mon, model_date=date(2026, 11, 1)) == date(2026, 11, 1)  # "kal" = yesterday
    assert resolve_day("tonight", mon) == mon and resolve_day("aaj raat", mon) == mon
    assert resolve_day("3rd Nov", mon) == date(2026, 11, 3) and resolve_day("Nov 10", mon) == date(2026, 11, 10)
    assert resolve_day("5/11", mon) == date(2026, 11, 5)
    assert resolve_day("2 Jan", mon) == date(2027, 1, 2)
    assert resolve_day("jumma", mon) == date(2026, 11, 6)
    assert resolve_day("", mon) is None and resolve_day("soon", mon) is None


def test_normalize_new_event_called_reschedule():
    from unisync.extract import normalize
    sent = datetime(2026, 11, 2, 10, 0, tzinfo=TZ)
    # model said "reschedule" for a brand-new makeup class; no change word in the message → it's an add
    it = normalize(_cfg(), {"action": "reschedule", "kind": "class", "title": "Makeup class", "day_words": "Saturday",
                            "date": "2026-11-09", "new_start": "9am", "confidence": 1},
                   "Makeup class of Linear Algebra on Saturday 9am room 204", sent)
    assert it["action"] == "add" and it["date"] == "2026-11-07" and it["start"] == "09:00"
    # model got the weekday wrong → code fixes it from the quoted words
    it = normalize(_cfg(), {"action": "add", "kind": "assignment", "title": "OOP A3", "day_words": "Friday",
                            "date": "2026-11-09", "start": "2026-11-09T23:59:59", "confidence": 1},
                   "OOP assignment 3 deadline is Friday 11:59pm", sent)
    assert it["date"] == "2026-11-06" and it["start"] == "23:59"
    # a real move keeps its reschedule; "8:30 instead of 10" with the time in start → new_start
    it = normalize(_cfg(), {"action": "reschedule", "kind": "class", "title": "OOP class", "day_words": "Tomorrow's",
                            "date": "2026-11-03", "start": "08:30", "confidence": 1},
                   "Tomorrow's OOP class will be at 8:30 instead of 10", sent)
    assert it["action"] == "reschedule" and it["date"] == "2026-11-03" and it["new_start"] == "08:30"
    # invented cancel is still dropped
    assert normalize(_cfg(), {"action": "cancel", "kind": "class", "title": "DSA", "date": "2026-11-03",
                              "confidence": 1}, "DSA quiz tomorrow", sent) is None


def test_apply_converts_mislabelled_reschedule_to_add():
    tomorrow = datetime.now(TZ).date() + timedelta(days=1)
    cal = FakeCal([])
    ap = Applier(_cfg(), cal, FakeDB())
    ap.apply({"action": "reschedule", "kind": "social", "title": "Dinner with Ali", "date": tomorrow.isoformat(),
              "start": "20:00", "confidence": 0.9}, "whatsapp", "x", "Kal raat dinner pe chalein? 8 baje / Done pakka")
    ev = [e for e in cal.evs.values() if "Dinner" in e.title][0]
    assert ev.start.time() == time(20) and ev.start.date() == tomorrow


def test_normalize_daytime_for_classes():
    from unisync.extract import normalize
    sent = datetime(2026, 11, 2, 10, 0, tzinfo=TZ)
    it = normalize(_cfg(), {"action": "add", "kind": "exam", "title": "DLD Viva", "day_words": "parson",
                            "date": "2026-11-07", "start": "23:00", "confidence": 1},
                   "Parson DLD ka viva hai 11 baje lab mein", sent)
    assert it["start"] == "11:00" and it["date"] == "2026-11-04"
    it = normalize(_cfg(), {"action": "add", "kind": "class", "title": "Online class", "day_words": "kal",
                            "date": "2026-11-03", "start": "21:00", "confidence": 1},
                   "kal online class 9 pm zoom pe", sent)
    assert it["start"] == "21:00"  # says pm → left alone
    it = normalize(_cfg(), {"action": "add", "kind": "social", "title": "Dinner", "day_words": "kal",
                            "date": "2026-11-03", "start": "21:00", "confidence": 1}, "kal dinner 9 baje", sent)
    assert it["start"] == "21:00"  # social plans can be at night


def test_normalize_kind_and_evening():
    from unisync.extract import normalize
    sent = datetime(2026, 11, 2, 10, 0, tzinfo=TZ)
    it = normalize(_cfg(), {"action": "cancel", "kind": "exam", "title": "Calculus class", "day_words": "kal",
                            "date": "2026-11-03", "confidence": 1}, "Kal ki Calculus class nahi hogi", sent)
    assert it["kind"] == "class"
    it = normalize(_cfg(), {"action": "add", "kind": "social", "title": "Sana's mehndi", "day_words": "Saturday",
                            "date": "2026-11-07", "start": "07:00", "confidence": 1},
                   "Sana ki mehndi Saturday ko hai, 7 baje tak pohanchna hai", sent)
    assert it["start"] == "19:00"
    it = normalize(_cfg(), {"action": "add", "kind": "social", "title": "Breakfast", "day_words": "kal",
                            "date": "2026-11-03", "start": "07:00", "confidence": 1}, "kal subah 7 baje nashta", sent)
    assert it["start"] == "07:00"


def test_verify_drops_rejected_adds(monkeypatch):
    import unisync.extract as ex
    item = {"msg": 1, "action": "add", "kind": "exam", "title": "Exam", "date": "2026-11-03", "confidence": 1}
    monkeypatch.setattr(ex.llm, "extract", lambda *a, **k: [dict(item)])
    msgs = [{"sender": "Hamza", "time": datetime(2026, 11, 2, 10, tzinfo=TZ), "text": "Mera exam kal 10 baje hai"}]
    monkeypatch.setattr(ex.llm, "verify", lambda *a, **k: False)
    assert ex.propose(SimpleNamespace(verify=True), "whatsapp", "x", list(msgs)) == []
    monkeypatch.setattr(ex.llm, "verify", lambda *a, **k: True)
    assert len(ex.propose(SimpleNamespace(verify=True), "whatsapp", "x", list(msgs))) == 1
    # cancels are not double-checked (they have their own word guards)
    monkeypatch.setattr(ex.llm, "extract", lambda *a, **k: [dict(item, action="cancel")])
    monkeypatch.setattr(ex.llm, "verify", lambda *a, **k: False)
    assert len(ex.propose(SimpleNamespace(verify=True), "whatsapp", "x", list(msgs))) == 1


def test_invented_name_dropped(monkeypatch):
    import unisync.extract as ex
    msgs = [{"sender": "Me", "time": datetime(2026, 11, 2, 10, tzinfo=TZ), "text": "kal uni chalein 10 baje?"},
            {"sender": "Hassan", "time": datetime(2026, 11, 2, 10, 5, tzinfo=TZ), "text": "done"}]
    item = {"msg": 1, "action": "add", "kind": "social", "title": "Go to uni with Jamal", "date": "2026-11-03",
            "confidence": 1}
    monkeypatch.setattr(ex.llm, "extract", lambda *a, **k: [dict(item)])
    assert ex.propose(SimpleNamespace(), "whatsapp", "WhatsApp · Hassan", list(msgs)) == []  # copied from prompt
    monkeypatch.setattr(ex.llm, "extract", lambda *a, **k: [dict(item, title="Go to uni with Hassan")])
    assert len(ex.propose(SimpleNamespace(), "whatsapp", "WhatsApp · Hassan", list(msgs))) == 1  # sender's name
    monkeypatch.setattr(ex.llm, "extract", lambda *a, **k: [dict(item, title="Meeting with the CR")])
    assert len(ex.propose(SimpleNamespace(), "whatsapp", "WhatsApp · BSCS", list(msgs))) == 1


def test_holiday_labelled_exam_clears_classes():
    mon = datetime.now(TZ).date() + timedelta(days=3)
    cls = [Ev(f"Class {h}", datetime.combine(mon, time(h), TZ), datetime.combine(mon, time(h + 1), TZ), "portal",
              id=event_id("c", str(h))) for h in (10, 13)]
    cal, db = FakeCal(cls), FakeDB()
    ap = Applier(_cfg(), cal, db)
    ap.apply({"action": "add", "kind": "exam", "title": "Iqbal Day Holiday", "date": mon.isoformat(),
              "confidence": 0.9}, "email", "Email from NIT", "The university will remain closed on 9th November")
    assert not any(e.source == "portal" for e in cal.evs.values())
    assert all(db.o[c.id] == "cancelled" for c in cls)


# ---- false positives seen on the real calendar, Oct-Nov 2026 ----
def _propose(monkeypatch, item, msgs, where="WhatsApp · x"):
    import unisync.extract as ex
    monkeypatch.setattr(ex.llm, "extract", lambda *a, **k: [dict(item)])
    return ex.propose(SimpleNamespace(tz=TZ, my_names=["saad"]), "whatsapp", where, list(msgs))


def _m(sender, text, group=False):
    return {"sender": sender, "text": text, "time": datetime(2026, 11, 2, 10, tzinfo=TZ), "group": group}


ADD = {"msg": 1, "action": "add", "date": "2026-11-13", "confidence": 1}


def test_commitment_must_be_my_promise(monkeypatch):
    it = dict(ADD, kind="commitment", title="Stay healthy", day_words="Friday", start="14:00")
    assert _propose(monkeypatch, it, [_m("Zain", "Friday tak lock in, stay healthy")]) == []
    it = dict(it, title="Send notes")
    assert len(_propose(monkeypatch, it, [_m("Me", "Friday 2 baje tak notes bhej dunga")])) == 1


def test_date_must_come_from_the_chat(monkeypatch):
    it = dict(ADD, kind="meeting", title="NCSS Meeting", day_words="")
    assert _propose(monkeypatch, it, [_m("Hira", "NCSS meeting planning, ideas please")]) == []
    assert len(_propose(monkeypatch, it, [_m("Hira", "Reminder @all 5pm student centre")])) == 1
    it = dict(ADD, kind="quiz", title="DSA Quiz", day_words="today")   # model translated "aaj"
    assert len(_propose(monkeypatch, it, [_m("Usman", "aaj DSA quiz hai")])) == 1
    it = dict(ADD, kind="quiz", title="DSA Quiz", day_words="Friday")  # day isn't in the chat at all
    assert _propose(monkeypatch, it, [_m("Usman", "DSA quiz ki tayari karo")]) == []


def test_invented_time_becomes_all_day(monkeypatch):
    it = dict(ADD, kind="quiz", title="Ethics Quiz", day_words="Friday", start="08:00", end="09:00")
    kept = _propose(monkeypatch, it, [_m("Usman", "Ethics quiz on Friday")])
    assert kept[0][0]["start"] == "" and kept[0][0]["end"] == ""
    it = dict(it, start="20:00", end="")
    assert _propose(monkeypatch, it, [_m("Usman", "Ethics quiz Friday 8 baje")])[0][0]["start"] == "20:00"


def test_group_broadcasts_need_my_involvement(monkeypatch):
    it = dict(ADD, kind="social", title="Match with Giki", day_words="Saturday", start="16:00")
    assert _propose(monkeypatch, it, [_m("Rayan", "Match vs Giki Saturday at 4", True)]) == []
    assert len(_propose(monkeypatch, it, [_m("Rayan", "Match vs Giki Saturday at 4", True), _m("Me", "I'm in", True)])) == 1
    assert len(_propose(monkeypatch, it, [_m("Ammi", "Saad, Giki match Saturday at 4", True)])) == 1
    assert len(_propose(monkeypatch, it, [_m("Rayan", "Match vs Giki Saturday at 4")])) == 1  # private chat
    it = dict(ADD, kind="meeting", title="Badminton Registration", day_words="Sunday")
    assert _propose(monkeypatch, it, [_m("Hira", "Badminton registration closes Sunday", True)]) == []
    it = dict(ADD, kind="deadline", title="Course registration closes", day_words="Sunday")
    assert len(_propose(monkeypatch, it, [_m("Hira", "Course registration closes Sunday", True)])) == 1
    it = dict(ADD, kind="meeting", title="MANDATORY TRAINING SESSION", day_words="Monday", start="15:45")
    assert len(_propose(monkeypatch, it, [_m("Hira", "Mandatory training session Monday 3:45pm", True)])) == 1


def test_junk_titles_dropped(monkeypatch):
    it = dict(ADD, kind="class", title="CSE-Sec2-Class", day_words="Wed")
    assert _propose(monkeypatch, it, [_m("x", "Wed ka kya scene")], "WhatsApp · CSE-Sec2-Class") == []
    it = dict(ADD, kind="class", title="Class", day_words="Wed")
    assert _propose(monkeypatch, it, [_m("x", "Wed ka kya scene")]) == []


# ---- only events for courses you're enrolled in ----
MINE = [{"code": "CSE240", "name": "Introduction to Programming Languages"},
        {"code": "MAT266", "name": "Calculus for Engineers II"}, {"code": "CSE301", "name": "Computing Ethics"},
        {"code": "ICT101L", "name": "Application to ICT Lab"}, {"code": "PHY121", "name": "University Physics I: Mechanics"},
        {"code": "COM225", "name": "Public Speaking and Presentations"},
        {"code": "QUR101", "name": "Understanding of the Holy Qur'an I"}]


def test_other_course():
    from unisync.extract import other_course as oc
    cfg = SimpleNamespace(enrolled=MINE)
    def it(title, kind="quiz", course=""):
        return {"title": title, "kind": kind, "course": course}
    # other courses
    assert oc(cfg, it("OOP Quiz 2"), "OOP quiz 2 on Tuesday")
    assert oc(cfg, it("Quiz 2", course="DSA"), "DSA quiz 2 on Tuesday")
    assert oc(cfg, it("Makeup class of Linear Algebra", "class"), "Makeup class of Linear Algebra Saturday")
    assert oc(cfg, it("MAT101 Midterm", "exam"), "mat101 midterm Friday")
    assert oc(cfg, it("Assignment 3 due", "deadline", "OOP"), "OOP assignment Friday")
    # yours, under the names people actually use
    assert not oc(cfg, it("Math Midterms", "exam"), "midterms Tuesday")
    assert not oc(cfg, it("MAT-266 Exam-II", "exam"), "Exam II on Wed")
    assert not oc(cfg, it("Ethics Quiz"), "quiz on Friday")
    assert not oc(cfg, it("Physics lab", "class"), "lab shifted")
    assert not oc(cfg, it("Quran class", "class"), "kal class")
    assert not oc(cfg, it("Public Speaking presentation", "presentation"), "Friday")
    # the title is odd but the message names your course
    assert not oc(cfg, it("Sana's quiz"), "Calculus quiz on Friday")
    # can't tell, or not about a course → kept
    assert not oc(cfg, it("Quiz 1"), "Quiz 1 on Friday")
    assert not oc(cfg, it("Final exams begin", "exam"), "Finals begin next Wed")
    assert not oc(cfg, it("Sir Ahmed's quiz"), "quiz Friday")
    assert not oc(cfg, it("Fee submission", "deadline"), "last date tomorrow")
    assert not oc(cfg, it("Iqbal Day Holiday", "exam"), "closed on Monday")
    assert not oc(cfg, it("Dinner with Ali", "social"), "dinner kal")
    # no course list yet → nothing is filtered
    assert not oc(SimpleNamespace(enrolled=[]), it("OOP Quiz 2"), "OOP quiz")


def test_propose_drops_other_course(monkeypatch):
    import unisync.extract as ex
    item = dict(ADD, kind="quiz", title="OOP Quiz", day_words="Friday")
    monkeypatch.setattr(ex.llm, "extract", lambda *a, **k: [dict(item)])
    cfg = SimpleNamespace(tz=TZ, my_names=[], enrolled=MINE)
    assert ex.propose(cfg, "whatsapp", "x", [_m("Zain", "OOP quiz Friday", True)]) == []
    monkeypatch.setattr(ex.llm, "extract", lambda *a, **k: [dict(item, action="cancel")])
    assert ex.propose(cfg, "whatsapp", "x", [_m("Zain", "OOP quiz Friday cancelled", True)]) == []


def test_canvas_api_missed_but_open(monkeypatch):
    import unisync.canvas as cv
    now = datetime.now(TZ)
    iso = lambda d: d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    online = ["online_upload"]
    def fake_get_all(cfg, path, params=None):
        if path == "/courses":
            return [{"id": 1, "course_code": "CS101"}]
        if params["bucket"] != "overdue":
            return []
        return [
            {"id": 1, "name": "Lab 3", "due_at": iso(now - timedelta(days=5)), "lock_at": iso(now + timedelta(days=2)),
             "submission_types": online, "submission": {}},                                   # missed, still open
            {"id": 2, "name": "Lab 2", "due_at": iso(now - timedelta(days=5)), "lock_at": iso(now - timedelta(hours=1)),
             "submission_types": online, "submission": {}},                                   # locked → gone
            {"id": 3, "name": "Lab 1", "due_at": iso(now - timedelta(days=5)), "lock_at": None,
             "submission_types": online, "submission": {"submitted_at": "x"}},                # submitted late → gone
            {"id": 4, "name": "Essay", "due_at": iso(now - timedelta(days=60)), "lock_at": None,
             "submission_types": online, "submission": {}},                                   # older than missed_days
            {"id": 5, "name": "Worksheet", "due_at": iso(now - timedelta(days=3)), "lock_at": None,
             "submission_types": ["on_paper"], "submission": {}},                             # handed in on paper
            {"id": 6, "name": "Quiz 2", "due_at": iso(now - timedelta(days=3)), "lock_at": None,
             "submission_types": ["online_quiz"], "locked_for_user": True, "submission": {}},  # locked for you
            {"id": 7, "name": "Report", "due_at": iso(now - timedelta(days=4)), "lock_at": None,
             "submission_types": online, "submission": {}},                                   # open, no lock date
        ]
    monkeypatch.setattr(cv, "_get_all", fake_get_all)
    items = cv.fetch_api(SimpleNamespace(tz=TZ, canvas_missed_days=30))
    got = {i["title"]: i for i in items}
    assert set(got) == {"Lab 3", "Report"} and all(i.get("late") for i in items)
    assert got["Report"]["lock"] is None and got["Lab 3"]["lock"] > now
    # missed_days = 0 turns the feature off
    assert cv.fetch_api(SimpleNamespace(tz=TZ, canvas_missed_days=0)) == []


def test_canvas_missed_sync(monkeypatch):
    import unisync.canvas as cv
    state = {}
    class KV:
        def get(self, k, d=None): return state.get(k, d)
        def set(self, k, v): state[k] = v
    now = datetime.now(TZ)
    cfg = SimpleNamespace(tz=TZ, canvas_token="t", canvas_url="u", canvas_ical_url="")
    lab = cv.Item(id="1", title="Lab 3", course="CS101", due=now + timedelta(hours=3), url="https://c/1",
                  kind="assignment", submitted=False)
    cal = FakeCal([])
    monkeypatch.setattr(cv, "fetch", lambda cfg: [lab])
    cv.sync(cfg, cal, KV())                                     # first run: a normal timed deadline
    assert not cal.evs[event_id("canvas", "1")].all_day

    # deadline passes unsubmitted, Canvas still open → moves to today, you're told once
    late = cv.Item(**{**lab, "due": now - timedelta(days=3), "late": True, "lock": now + timedelta(days=2)})
    monkeypatch.setattr(cv, "fetch", lambda cfg: [late])
    notes = cv.sync(cfg, cal, KV())
    assert len(notes) == 1 and notes[0].startswith("⚠️ Missed but still open: Lab 3 [CS101]")
    ev = cal.evs[event_id("canvas", "1")]
    assert ev.all_day and ev.start == now.date() and ev.kind == "missed" and "LATE" in ev.title
    assert cv.missed(KV()) and "Lab 3" in cv.missed(KV())[0] and "closes" in cv.missed(KV())[0]
    assert cv.sync(cfg, cal, KV()) == []                        # no repeat alert next sync

    # submitted (or locked) → Canvas stops returning it → it disappears quietly
    monkeypatch.setattr(cv, "fetch", lambda cfg: [])
    notes = cv.sync(cfg, cal, KV())
    assert event_id("canvas", "1") not in cal.evs and notes == [] and cv.missed(KV()) == []
