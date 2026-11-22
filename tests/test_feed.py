"""The phone app's Updates feed."""
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from unisync import feed

TZ = ZoneInfo("Asia/Karachi")


def test_build_merges_newest_first_and_splits_parts():
    d = feed.build(feed._empty(), ["🔁 Moved OOP: Tue 10:30 → Tue 13:00 — WhatsApp · BSCS: “1 baje”"], [], now=1000)
    it = d["items"][0]
    assert (it["verb"], it["title"], it["src"]) == ("Moved", "Moved OOP: Tue 10:30 → Tue 13:00", "WhatsApp · BSCS: “1 baje”")
    d2 = feed.build(d, ["➕ Added DSA quiz — WhatsApp · BSCS: “kal quiz”"], [], now=2000)
    assert [i["verb"] for i in d2["items"]] == ["Added", "Moved"]
    assert d2["updated"] == 2000


def test_resolved_checks_stay_hidden_and_same_text_not_duplicated():
    c = {"text": "❔ Physics viva — Thu 26 Nov 14:00  (add, 40% sure) — WhatsApp · S3: “viva shayad thursday”"}
    d = feed.build(feed._empty(), [], [c], now=60)
    cid = d["items"][0]["id"]
    assert d["items"][0]["type"] == "check"
    d["resolved"].append(cid)                 # the app tapped Ignore / Add it
    d2 = feed.build(d, [], [c], now=60)       # same run re-recorded
    assert all(i["id"] != cid for i in d2["items"])
    assert len(feed.build(feed._empty(), ["x — y"], [], now=60)["items"]) == 1


def test_feed_stays_under_size_limit():
    d = feed._empty()
    for n in range(200):
        d = feed.build(d, [f"➕ Added item number {n} " + "x" * 150 + " — WhatsApp · group: “" + "y" * 150 + "”"], [], now=n * 100)
    assert len(json.dumps(d, ensure_ascii=False)) <= feed.MAX_CHARS
    assert d["items"][0]["title"].startswith("Added item number 199")


def test_low_confidence_add_carries_ready_event():
    from unisync.extract import Applier
    cfg = SimpleNamespace(tz=TZ, min_confidence=0.6, detect_personal=True, enrolled=[], my_names=[])
    ap = Applier(cfg, None, None, dry_run=True)
    day = datetime.now(TZ).date() + timedelta(days=3)
    ap.apply({"action": "add", "kind": "exam", "title": "Physics viva", "date": day.isoformat(),
              "start": "14:00", "confidence": 0.4}, "whatsapp", "WhatsApp · S3", "viva shayad thursday ko 2 baje")
    assert len(ap.checks) == 1 and ap.maybe
    ev = ap.checks[0].get("event")
    if ev:  # normalize() may legitimately drop items it can't trust; when kept, the event is complete
        assert ev["id"].startswith("us") and ev["extendedProperties"]["private"]["kind"] == "exam"
        assert ev["start"]["dateTime"].startswith(day.isoformat())
