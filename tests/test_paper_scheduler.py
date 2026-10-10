from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json
import pytest
from cio_market_lab.engine.paper_scheduler import run_daily


class FakeSession:
    calls = 0
    def run_session(self, ticks):
        type(self).calls += 1
        return {"ticks": ticks, "paper_only": True}


def cfg(tmp_path, market="US", **kw):
    return {"report_dir": str(tmp_path), "markets": {market: {"session_factory": FakeSession, "ticks": 2, "journal_path": str(tmp_path / "journal.jsonl"), **kw}}}


def dt(y, m, d, h, minute=0, zone="America/New_York"):
    return datetime(y, m, d, h, minute, tzinfo=ZoneInfo(zone))


def test_weekend(tmp_path):
    assert run_daily(dt(2026, 10, 10, 10), cfg(tmp_path))["markets"]["US"] == "SKIPPED_CALENDAR"


def test_holiday(tmp_path):
    assert run_daily(dt(2026, 10, 12, 10), cfg(tmp_path, holidays=["2026-10-12"]))["markets"]["US"] == "SKIPPED_CALENDAR"


def test_lock_contention(tmp_path):
    (tmp_path / ".paper_scheduler.lock").write_text("busy")
    assert run_daily(dt(2026, 10, 12, 10), cfg(tmp_path))["status"] == "LOCKED"


def test_before_open(tmp_path):
    assert run_daily(dt(2026, 10, 12, 9), cfg(tmp_path))["markets"]["US"] == "BEFORE_OPEN"


def test_session_once(tmp_path):
    c = cfg(tmp_path)
    assert run_daily(dt(2026, 10, 12, 10), c)["markets"]["US"] == "SESSION_RAN"
    assert run_daily(dt(2026, 10, 12, 11), c)["markets"]["US"] == "ALREADY_RAN"


def test_post_close_once(tmp_path):
    c = cfg(tmp_path)
    run_daily(dt(2026, 10, 12, 10), c)
    assert run_daily(dt(2026, 10, 12, 16), c)["markets"]["US"] == "FEEDBACK_DONE"
    assert run_daily(dt(2026, 10, 12, 17), c)["markets"]["US"] == "ALREADY_PROCESSED"
    assert (tmp_path / "2026-10-12.json").exists()
    assert (tmp_path / "2026-10-12.md").exists()


def test_no_session_no_feedback(tmp_path):
    assert run_daily(dt(2026, 10, 12, 17), cfg(tmp_path))["markets"]["US"] == "NO_SESSION"


def test_dst_summer(tmp_path):
    now = datetime(2026, 7, 6, 14, tzinfo=timezone.utc)
    assert run_daily(now, cfg(tmp_path))["markets"]["US"] == "SESSION_RAN"


def test_dst_winter(tmp_path):
    now = datetime(2026, 12, 7, 14, tzinfo=timezone.utc)
    assert run_daily(now, cfg(tmp_path))["markets"]["US"] == "BEFORE_OPEN"


def test_tw_hours(tmp_path):
    assert run_daily(dt(2026, 10, 12, 9, zone="Asia/Taipei"), cfg(tmp_path, "TW"))["markets"]["TW"] == "SESSION_RAN"


def test_naive_rejected(tmp_path):
    with pytest.raises(ValueError):
        run_daily(datetime(2026, 10, 12, 10), cfg(tmp_path))


def test_lock_released(tmp_path):
    run_daily(dt(2026, 10, 12, 10), cfg(tmp_path))
    assert not (tmp_path / ".paper_scheduler.lock").exists()
