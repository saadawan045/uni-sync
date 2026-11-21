"""Tests for the laptop pieces: scheduler timing, per-OS paths, the setup wizard's config file."""
import tomllib
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from unisync import paths, runner

TZ = ZoneInfo("Asia/Karachi")


def at(h, m=0, day=10):
    return datetime(2030, 10, day, h, m, tzinfo=TZ)


def test_every_job_due():
    sched = ("every", 5)
    now = at(12)
    assert runner.is_due(sched, None, now)
    assert not runner.is_due(sched, (now - timedelta(minutes=4)).timestamp(), now)
    assert runner.is_due(sched, (now - timedelta(minutes=5)).timestamp(), now)


def test_daily_job_runs_once_per_slot():
    sched = ("at", ["06:00", "13:00", "19:00"], 24)
    assert runner.is_due(sched, None, at(6, 1))
    assert not runner.is_due(sched, at(6, 1).timestamp(), at(12, 59))   # already ran for 06:00
    assert runner.is_due(sched, at(6, 1).timestamp(), at(13, 0))        # 13:00 slot
    assert not runner.is_due(sched, at(13, 0).timestamp(), at(13, 30))


def test_daily_job_catches_up_after_sleep_but_not_too_late():
    digest = ("at", ["07:00"], 4)
    yesterday = at(7, 0, day=9).timestamp()
    assert runner.is_due(digest, yesterday, at(9, 30))      # laptop opened at 9:30: still send today's digest
    assert not runner.is_due(digest, yesterday, at(15, 0))  # 8 h late: skip, it would be useless
    portal = ("at", ["06:00", "13:00", "19:00"], 24)
    assert runner.is_due(portal, at(19, 0, day=8).timestamp(), at(10, 0))  # off for a day: sync on wake


def test_daily_job_just_after_midnight_uses_yesterdays_slot():
    evening = ("at", ["21:30"], 2)
    last = at(21, 30, day=8).timestamp()
    assert runner.is_due(evening, last, at(23, 0, day=9))          # 21:30 slot missed by 1.5 h: run
    assert runner.is_due(evening, last, at(23, 15, day=9))
    assert runner.is_due(evening, None, at(23, 20, day=9))
    assert runner.is_due(evening, last, datetime(2030, 10, 9, 23, 29, tzinfo=TZ))
    # opened at 00:30: yesterday's 21:30 slot is 3 h old (> 2 h): skip
    assert not runner.is_due(evening, last, at(0, 30, day=10))
    assert not runner.is_due(evening, None, at(3, 0))


def test_jobs_only_when_set_up():
    cfg = SimpleNamespace(imap_host="", imap_user="", canvas_token="", canvas_ical_url="", webwork_courses=[],
                          portal_username="u", portal_password="p", notify_to="", smtp_host="")
    on = {n for n, _, _ in runner.JOBS if runner.job_enabled(cfg, n)}
    assert on == {"wa-process", "portal-sync"}


def test_paths_respect_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("UNISYNC_CONFIG", str(tmp_path / "c" / "config.toml"))
    monkeypatch.setenv("UNISYNC_DATA", str(tmp_path / "d"))
    assert paths.config_file() == tmp_path / "c" / "config.toml"
    assert paths.config_dir() == tmp_path / "c"
    assert paths.default_data_dir() == tmp_path / "d"


def test_setup_writes_valid_config_with_awkward_values(tmp_path, monkeypatch):
    from unisync import setup
    monkeypatch.setenv("UNISYNC_CONFIG", str(tmp_path / "config.toml"))
    conf = {"portal": {"username": "FA25-BCS-001", "password": 'pa"ss\\word #1'},
            "google": {"auth": "oauth", "calendar_id": "abc@group.calendar.google.com"},
            "mail": {}, "canvas": {"url": "https://nit.instructure.com", "token": "7~xyz"},
            "whatsapp": {"my_names": ["Saad", "سعد"]}, "llm": {},
            "webwork": [{"name": "MATH-101", "url": "https://w/webwork2/M/", "username": "u", "password": "p'q"}]}
    setup._write(conf)
    back = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert back["portal"]["password"] == 'pa"ss\\word #1'
    assert back["whatsapp"]["my_names"] == ["Saad", "سعد"]
    assert back["webwork"][0]["password"] == "p'q"
    assert back["google"]["auth"] == "oauth"


def test_linked_accounts(tmp_path):
    (tmp_path / "wa-auth-personal").mkdir()
    (tmp_path / "wa-auth-personal/creds.json").write_text('{"account": {}}', encoding="utf-8")
    (tmp_path / "wa-auth-half").mkdir()
    (tmp_path / "wa-auth-half/creds.json").write_text('{"noise": 1}', encoding="utf-8")
    assert runner.linked_accounts(tmp_path) == ["personal"]
