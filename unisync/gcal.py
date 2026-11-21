"""Google Calendar access via a service account that you share ONE calendar with.

The service account can only see the calendar you explicitly share with it —
not the rest of your Google account.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def event_id(*parts) -> str:
    """Deterministic Google-valid event id (chars 0-9a-v)."""
    return "us" + hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()


@dataclass
class Ev:
    title: str
    start: datetime | date          # date => all-day
    end: datetime | date
    source: str                     # portal | whatsapp | email
    kind: str = "class"             # class quiz exam assignment deadline event other
    location: str = ""
    description: str = ""
    id: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)


class Calendar:
    def __init__(self, cfg):
        creds = service_account.Credentials.from_service_account_file(cfg.service_account_file, scopes=SCOPES)
        self.svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.cal = cfg.calendar_id
        self.tz = cfg.tz

    # ---------- conversion ----------
    def _body(self, ev: Ev) -> dict:
        tzname = str(self.tz)
        if ev.all_day:
            start = {"date": ev.start.isoformat()}
            end = {"date": ev.end.isoformat()}
        else:
            start = {"dateTime": ev.start.isoformat(), "timeZone": tzname}
            end = {"dateTime": ev.end.isoformat(), "timeZone": tzname}
        body = {
            "id": ev.id,
            "summary": ev.title,
            "location": ev.location or None,
            "description": ev.description or None,
            "start": start,
            "end": end,
            "status": "confirmed",
            "extendedProperties": {"private": {"unisync": "1", "source": ev.source, "kind": ev.kind, **ev.extra}},
        }
        if ev.kind in ("exam", "quiz", "assignment", "deadline", "commitment", "appointment"):
            body["reminders"] = {"useDefault": False, "overrides": [
                {"method": "popup", "minutes": 24 * 60}, {"method": "popup", "minutes": 60}]}
        return {k: v for k, v in body.items() if v is not None}

    def _from_api(self, e: dict) -> Ev:
        def parse(x):
            if "dateTime" in x:
                return datetime.fromisoformat(x["dateTime"]).astimezone(self.tz)
            return date.fromisoformat(x["date"])
        priv = e.get("extendedProperties", {}).get("private", {})
        return Ev(
            title=e.get("summary", ""), start=parse(e["start"]), end=parse(e["end"]),
            source=priv.get("source", "manual"), kind=priv.get("kind", "other"),
            location=e.get("location", ""), description=e.get("description", ""),
            id=e["id"], extra={k: v for k, v in priv.items() if k not in ("unisync", "source", "kind")},
        )

    # ---------- operations ----------
    def upsert(self, ev: Ev) -> str:
        """Returns 'created', 'updated' or 'unchanged'."""
        body = self._body(ev)
        try:
            cur = self.svc.events().get(calendarId=self.cal, eventId=ev.id).execute()
        except HttpError as e:
            if e.resp.status != 404:
                raise
            self.svc.events().insert(calendarId=self.cal, body=body).execute()
            return "created"
        same = (cur.get("status") == "confirmed" and cur.get("summary") == body["summary"]
                and cur.get("location") == body.get("location")
                and self._from_api(cur).start == ev.start and self._from_api(cur).end == ev.end)
        if same:
            return "unchanged"
        # also revives a previously deleted ("cancelled") event with the same id
        self.svc.events().update(calendarId=self.cal, eventId=ev.id, body=body).execute()
        return "updated"

    def delete(self, eid: str) -> None:
        try:
            self.svc.events().delete(calendarId=self.cal, eventId=eid).execute()
        except HttpError as e:
            if e.resp.status not in (404, 410):
                raise

    def list(self, start: datetime, end: datetime, source: str | None = None) -> list[Ev]:
        out, token = [], None
        props = ["unisync=1"] + ([f"source={source}"] if source else [])
        while True:
            res = self.svc.events().list(
                calendarId=self.cal, timeMin=start.isoformat(), timeMax=end.isoformat(),
                singleEvents=True, orderBy="startTime", privateExtendedProperty=props,
                maxResults=250, pageToken=token,
            ).execute()
            out += [self._from_api(e) for e in res.get("items", []) if e.get("status") != "cancelled"]
            token = res.get("nextPageToken")
            if not token:
                return out

    def list_all(self, start: datetime, end: datetime) -> list[Ev]:
        """Everything on the calendar, including events you added by hand."""
        res = self.svc.events().list(calendarId=self.cal, timeMin=start.isoformat(), timeMax=end.isoformat(),
                                     singleEvents=True, orderBy="startTime", maxResults=250).execute()
        return [self._from_api(e) for e in res.get("items", []) if e.get("status") != "cancelled"]

    def find(self, day: date, title: str, kind: str | None = None, threshold: float = 0.55) -> Ev | None:
        """Best fuzzy title match among events on `day`."""
        start = datetime.combine(day, datetime.min.time(), self.tz)
        return best_match(self.list_all(start, start + timedelta(days=1)), title, kind, threshold)

    def check(self) -> str:
        # only the "events" permission is requested, so test with an events call
        if not self.cal:
            raise RuntimeError("calendar_id is empty in config.toml")
        res = self.svc.events().list(calendarId=self.cal, maxResults=1).execute()
        return res.get("summary", self.cal)


def _norm(s: str) -> str:
    import re
    s = s.lower()
    s = re.sub(r"[❌✏️📝📌]|\b(cancelled|canceled|moved|rescheduled|class|lecture|lab|the)\b", " ", s)
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 0.9
    # token overlap catches "DSA" vs "Data Structures (DSA) - Room 4"
    ta, tb = set(a.split()), set(b.split())
    # acronyms: "dsa" vs "data structures and algorithms"
    stop = {"and", "of", "to", "in", "for", "&"}
    acr = lambda s: "".join(w[0] for w in s.split() if w not in stop)

    def acr_match(short, long):
        x, y = short.replace(" ", ""), acr(long)
        return len(short) <= 6 and len(y) >= 2 and len(x) >= 2 and (x.startswith(y) or y.startswith(x))
    if acr_match(a, b) or acr_match(b, a):
        return 0.85
    overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))
    return max(SequenceMatcher(None, a, b).ratio(), overlap * 0.85)


def best_match(evs: list[Ev], title: str, kind: str | None = None, threshold: float = 0.55) -> Ev | None:
    """Fuzzy match that prefers the same kind of event, so cancelling "DSA class"
    never hits "DSA Quiz 2" when the class itself is there."""
    def score(ev):
        s = similarity(title, ev.title)
        if kind:
            if ev.kind == kind:
                s += 0.1
            elif {ev.kind, kind} & {"quiz", "exam", "assignment", "deadline"}:
                s -= 0.3
        return s
    scored = sorted(((score(e), e) for e in evs), key=lambda x: -x[0])
    return scored[0][1] if scored and scored[0][0] >= threshold else None
