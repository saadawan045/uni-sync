"""Local model via Ollama (runs on this server — nothing leaves your network)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import requests

KINDS = ["class", "quiz", "exam", "assignment", "deadline", "presentation", "event", "meeting", "holiday",
         "social", "appointment", "birthday", "commitment", "other"]
# Property order matters: the model writes fields in this order, so it reads the message (title, kind),
# quotes the day words, and only then commits to a date.
SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "msg": {"type": "integer"},
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": KINDS},
                    "action": {"type": "string", "enum": ["add", "cancel", "reschedule"]},
                    "day_words": {"type": "string"},
                    "date": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "new_day_words": {"type": "string"},
                    "new_date": {"type": "string"},
                    "new_start": {"type": "string"},
                    "new_end": {"type": "string"},
                    "course": {"type": "string"},
                    "location": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["msg", "title", "kind", "action", "day_words", "date", "start", "confidence"],
            },
        }
    },
    "required": ["events"],
}

SYSTEM_UNI = """You extract calendar events for ONE university student ("Me") from WhatsApp chats and emails.
Messages may be English, Urdu or Roman Urdu ("kal" = tomorrow, "parson" = day after tomorrow, "nahi hogi" = won't
happen, "baje" = o'clock, "shaam" = evening, "raat" = night).

action — choose carefully:
- add: something NEW for the calendar. Almost every event is "add": a quiz, exam, viva, assignment/fee deadline,
  presentation, extra/makeup class, lab, meeting, seminar, appointment, a confirmed plan. "Reminder" and
  "will be held" announcements are add.
- cancel: ONLY when the message says the event is off ("cancelled", "nahi hogi", "off hai", "won't happen").
- reschedule: ONLY when the message says an existing event MOVED ("shifted", "postponed", "instead of",
  "moved to"). date/day_words = the OLD day, new_date/new_day_words/new_start = the NEW day/time.

kind: lecture, lab, makeup class → class. viva, midterm, final → exam. Group meeting/gathering → meeting.
"""

SYSTEM_PERSONAL = """Also include the student's PERSONAL plans:
- social: hangouts, meals, chai/coffee, movies, matches, gym with friends, trips, parties, weddings
  ("kal 8 baje dinner pe milte hain", "chalo Saturday movie", "pick you up at 7").
- appointment: doctor, dentist, interview, a scheduled call.
- birthday: someone's birthday on a stated date (all-day).
- commitment: something "Me" promised to do by a time ("I'll send you the notes by Friday",
  "kal tak bhej dunga") — title it as the task, e.g. "Send Ali the DSA notes"; put the due time in start.
- going somewhere together: "lets go to uni tmrw?" … "Done" = social event "Go to uni with <that friend's name>".
  If the agreed time is tied to a class ("we can go Wednesday, I have class at 10:30"), use that day and
  put the time as about 45 minutes before the class (they need to travel), noting the class time in title or
  location only if helpful.
Personal rules:
- Other people describing THEIR OWN schedule ("Bro I have class at 3 tmr") is context, not an event for
  the student. Only add the student's own commitments and plans the student is part of.
- The student must be involved (they are invited, they said yes, or "Me" proposed it). Plans between other
  people that don't include the student are NOT events.
- A plan someone only suggested and nobody confirmed gets confidence 0.4-0.5 (it will be shown as "please check").
  Confirmed ("done", "pakka", "ok see you", 👍 from the other side or from Me) gets 0.8+.
- If a later message cancels or moves the plan ("aaj nahi ho payega", "let's do Sunday instead"), use cancel/reschedule.
- Put who it's with into title ("Dinner with Ali") and the place into location if mentioned.
"""

SYSTEM_RULES = """Fields:
- day_words: copy the EXACT words from the message that name the day ("Tuesday", "kal", "parson", "next Monday",
  "3rd Nov", "tonight"). "" if the message names no day.
- date: YYYY-MM-DD. Use the calendar in the user message: a weekday name means its next occurrence
  (today if it is that weekday and the time hasn't passed).
- start/end/new_start: 24-hour "HH:MM" only (8pm → "20:00", 11:59pm → "23:59"), "" if no time is said.
  For deadlines put the due time in start.
- title: short and specific — "DSA Quiz 2", "OOP Assignment 3 due", "Dinner with Ali". Course name in course.
- confidence 0..1: how sure you are this is a real event for Me with the right date.
- msg: the number of the message the event came from.

Return NO event for:
- payments, receipts, OTPs, orders/deliveries, ads, forwards, newsletters;
- other people's own schedules ("I have class at 3", "mera exam kal hai") and plans without Me;
- someone saying they personally can't attend ("can't come", "nahi aa sakta") — the event still happens;
- past events, questions nobody answered, jokes, chit-chat, greetings, birthday wishes;
- anything vague with no real day ("soon", "sometime", "kabhi bhi", "kabhi milte hain").
A clock time with no day ("Reminder @all 7pm main hall") means TODAY (tomorrow if that time already passed).
Official university notices (start of exams, last dates, fee/registration/form deadlines) are always events.
A holiday or "university closed" notice (Iqbal Day, Eid, "chutti") is kind "holiday", action "add".
Only use names that appear in the messages — never names from these instructions.
Messages marked (context) only help you understand the others. A message may start with
[replying to: "..."] — the earlier message being answered ("Cancelled" replying to "OOP viva Thursday 2pm" =
cancel the OOP viva on Thursday). Never invent a title like "Class" from nothing.
If nothing qualifies return {"events": []}.

Examples (TODAY Mon 2026-11-02):
"DSA quiz Thursday 10am" → add quiz "DSA Quiz", day_words "Thursday", date 2026-11-05, start 10:00
"kal OOP class nahi hogi" → cancel class "OOP class", day_words "kal", date 2026-11-03
"Lab shifted from Wed to Fri 2pm" → reschedule class "Lab", day_words "Wed", date 2026-11-04,
   new_day_words "Fri", new_date 2026-11-06, new_start 14:00
"Ali: chai at 5 tomorrow?" + "Me: done" → add social "Chai with Ali", day_words "tomorrow", date 2026-11-03, start 17:00
"Bro I have class at 3 tmr" → nothing
Group "NIT Debating Society": "Reminder @all 7pm main hall" → add meeting "Debating Society meeting",
   day_words "", date 2026-11-02, start 19:00, location "main hall"
"Final exams begin from next Wednesday" → add exam "Final exams begin", day_words "next Wednesday", date 2026-11-04
"Last date to submit the scholarship form is 10th Nov" → add deadline "Scholarship form due", day_words "10th Nov",
   date 2026-11-10
"""


def system_prompt(cfg) -> str:
    personal = getattr(cfg, "detect_personal", True)
    return SYSTEM_UNI + (SYSTEM_PERSONAL if personal else "") + SYSTEM_RULES



def extract(cfg, messages: list[dict], context: str, now: datetime | None = None) -> list[dict]:
    """messages: [{"n": 1, "sender": "...", "time": datetime, "text": "..."}]"""
    now = now or datetime.now(cfg.tz)
    cal = ", ".join(f"{(now + timedelta(days=i)):%a %Y-%m-%d}" for i in range(8))
    body = "\n\n".join(
        f"[{m['n']}]{' (context)' if m.get('context') else ''} ({m['time']:%a %Y-%m-%d %H:%M}) "
        f"{m['sender'] or 'Someone'}: {m['text'][:2500]}" for m in messages
    )
    user = (f"TODAY is {now:%A %Y-%m-%d %H:%M} ({cfg.tz}). Next days: {cal}.\n"
            f"Source: {context}\n\nMESSAGES:\n{body}")
    r = requests.post(
        f"{cfg.ollama_url}/api/chat",
        json={
            "model": cfg.model,
            "messages": [{"role": "system", "content": system_prompt(cfg)}, {"role": "user", "content": user}],
            "format": SCHEMA,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": 6144},
            "keep_alive": "10m",
            **({"think": False} if cfg.model.startswith("qwen3") else {}),
        },
        timeout=600,
    )
    r.raise_for_status()
    try:
        return json.loads(r.json()["message"]["content"]).get("events", [])
    except (KeyError, json.JSONDecodeError):
        return []


def check(cfg) -> str:
    r = requests.get(f"{cfg.ollama_url}/api/tags", timeout=10)
    r.raise_for_status()
    names = [m["name"] for m in r.json().get("models", [])]
    if not any(n.split(":")[0] == cfg.model.split(":")[0] and (":" not in cfg.model or n == cfg.model) for n in names):
        raise RuntimeError(f"Model {cfg.model} not pulled. Run: ollama pull {cfg.model}")
    return cfg.model


VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"reason": {"type": "string"}, "mine": {"type": "boolean"}, "real": {"type": "boolean"}},
    "required": ["reason", "mine", "real"],
}

VERIFY_SYSTEM = """You double-check ONE proposed calendar entry for a university student called "Me".
Read the chat and answer in JSON:
- reason: one short sentence.
- mine: true if Me has to attend or do it: Me's own class/quiz/exam/deadline, a notice to Me's class group or
  from the university, an appointment for Me, or a plan Me agreed to or was invited to.
  false if it is SOMEONE ELSE's own schedule (a friend's "mera exam kal hai", "humara quiz kal hai, tumhara kab
  hai?", "I have class at 3"), or a plan between other people.
- real: true if the chat announces or agrees that it happens at that time. false for jokes, memes, complaints,
  food/chit-chat, questions nobody answered, wishes, and things that already happened."""


def verify(cfg, messages: list[dict], context: str, item: dict) -> bool:
    """Second, narrow opinion on an 'add' the extractor proposed. Small models answer a yes/no question
    about one event far better than they do the whole extraction. Fails open (keeps the event) on errors."""
    chat = "\n".join(f"{m['sender'] or 'Someone'}: {m['text'][:800]}" for m in messages)
    when = f"{item.get('date', '')} {item.get('start', '')}".strip()
    user = (f"Chat ({context}):\n{chat}\n\nProposed entry: {item.get('kind')} \"{item.get('title')}\" on {when}.")
    try:
        r = requests.post(f"{cfg.ollama_url}/api/chat", json={
            "model": cfg.model,
            "messages": [{"role": "system", "content": VERIFY_SYSTEM}, {"role": "user", "content": user}],
            "format": VERIFY_SCHEMA, "stream": False, "keep_alive": "10m",
            "options": {"temperature": 0, "num_ctx": 4096},
            **({"think": False} if cfg.model.startswith("qwen3") else {}),
        }, timeout=300)
        r.raise_for_status()
        v = json.loads(r.json()["message"]["content"])
        return bool(v.get("mine", True)) and bool(v.get("real", True))
    except (requests.RequestException, KeyError, ValueError):
        return True
