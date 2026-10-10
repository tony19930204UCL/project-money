import json
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from cio_market_lab.engine.paper_session import PaperSession


class Feed:
    def clock(self):
        return datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)


class Loop:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def run_once(self, feed, account, strategies):
        self.calls += 1
        if self.fail:
            from cio_market_lab.data.intraday_feed import FeedUnavailable
            raise FeedUnavailable("stale")
        account.cash += 2
        return {"events": [{"status": "SIMULATED", "ticker": "AAPL",
                             "strategy_id": "test"}], "marks": {"AAPL": 10}}


def make(tmp_path, symbols=("AAPL",), loop=None):
    return PaperSession(Feed(), loop or Loop(), [SimpleNamespace(ticker="AAPL")],
                        tmp_path / "account.json", tmp_path / "journal.jsonl", symbols)


def journal(tmp_path):
    return [json.loads(x) for x in (tmp_path / "journal.jsonl").read_text().splitlines()]


def test_initial_cash(tmp_path):
    assert make(tmp_path).account.cash == 0


def test_resume(tmp_path):
    make(tmp_path).run_session(2)
    assert make(tmp_path).account.cash == 4


def test_resume_positions(tmp_path):
    s = make(tmp_path)
    s.account.positions["AAPL"] = 3
    s.run_session(1)
    assert make(tmp_path).account.positions["AAPL"] == 3


def test_resume_marks(tmp_path):
    make(tmp_path).run_session(1)
    assert make(tmp_path).account.marks["AAPL"] == 10


def test_closed_market(tmp_path):
    loop = Loop()
    result = make(tmp_path, ("2330.TW",), loop).run_session(1)
    assert loop.calls == 0
    assert result["trades"] == 0
    assert any(x.get("reason") == "MARKET_CLOSED" for x in journal(tmp_path))


def test_stale_continue(tmp_path):
    loop = Loop(fail=True)
    make(tmp_path, loop=loop).run_session(3)
    assert loop.calls == 3
    assert sum(x["status"] == "ERROR" for x in journal(tmp_path)) == 3


def test_atomic_replace(tmp_path, monkeypatch):
    import cio_market_lab.engine.paper_session as module
    called = []
    real = module.os.replace
    def replace(a, b):
        called.append((a, b))
        return real(a, b)
    monkeypatch.setattr(module.os, "replace", replace)
    make(tmp_path).run_session(1)
    assert len(called) == 1
    assert called[0][0] != str(tmp_path / "account.json")


def test_corrupt_json(tmp_path):
    (tmp_path / "account.json").write_text("{broken")
    with pytest.raises(ValueError, match="CORRUPT"):
        make(tmp_path)


def test_corrupt_schema(tmp_path):
    (tmp_path / "account.json").write_text('{"cash": 10}')
    with pytest.raises(ValueError, match="CORRUPT"):
        make(tmp_path)


def test_nonfinite_state(tmp_path):
    s = make(tmp_path)
    s.run_session(0)
    state = json.loads((tmp_path / "account.json").read_text()) if (tmp_path / "account.json").exists() else {
        "cash": 0, "positions": {}, "realized_pnl": 0, "marks": {}, "daily_start_equity": None}
    state["cash"] = float("nan")
    (tmp_path / "account.json").write_text(json.dumps(state))
    with pytest.raises(ValueError, match="CORRUPT"):
        make(tmp_path)


def test_summary_math(tmp_path):
    s = make(tmp_path)
    s.account.positions["AAPL"] = 2
    s.account.realized_pnl = 7
    result = s.run_session(2)
    assert result["equity"] == 24
    assert result["pnl"] == 7
    assert result["trades"] == 2


def test_summary_journal(tmp_path):
    make(tmp_path).run_session(1)
    assert journal(tmp_path)[-1]["status"] == "SESSION_SUMMARY"


def test_sleep_injected(tmp_path):
    waits = []
    make(tmp_path).run_session(3, sleep=waits.append)
    assert waits == [1, 1]


def test_invalid_ticks(tmp_path):
    with pytest.raises(ValueError):
        make(tmp_path).run_session(-1)
