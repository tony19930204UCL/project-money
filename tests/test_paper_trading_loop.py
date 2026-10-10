"""Offline contract tests for paper-only trading loop."""
from datetime import datetime, timezone
from types import SimpleNamespace
import json
import pytest

from cio_market_lab.engine.paper_trading_loop import (
    PaperTradingLoop, PaperAccount, Signal, MomentumSwing, MeanReversion,
)

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


class Feed:
    def __init__(self, prices, stale=0):
        self.prices, self.stale = prices, stale

    def latest(self, symbol):
        return SimpleNamespace(c=self.prices[symbol], ts=NOW,
                               source="fixture", staleness_seconds=self.stale)


class Strategy:
    ticker = "AAA"
    def __init__(self, side="BUY", size=1, kind="stock"):
        self.side, self.size, self.kind = side, size, kind

    def signals(self, quotes, account):
        return [Signal(self.ticker, self.kind, self.side, self.size,
                       90, 120, "fixture thesis", "gain", "break 90")]


class Decision:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.reasons = [] if allowed else ["DENIED"]


class Orders:
    def __init__(self, allowed=True):
        self.allowed, self.submitted = allowed, []

    def _risk_decision(self, request):
        return Decision(self.allowed)

    def submit(self, request):
        self.submitted.append(request)
        return SimpleNamespace(order_id="paper-fixture")


def setup(tmp_path, *, cash=1000, positions=None, allowed=True, **kwargs):
    orders = Orders(allowed)
    loop = PaperTradingLoop(orders, tmp_path / "journal.jsonl",
                            max_position_pct=1, **kwargs)
    account = PaperAccount(cash, positions or {})
    return loop, account, orders


def test_buy_submits(tmp_path):
    loop, acc, orders = setup(tmp_path)
    result = loop.run_once(Feed({"AAA": 100}), acc, [Strategy()])
    assert len(orders.submitted) == 1
    assert result["events"][0]["status"] == "SIMULATED"


def test_buy_cash_decreases(tmp_path):
    loop, acc, _ = setup(tmp_path)
    loop.run_once(Feed({"AAA": 100}), acc, [Strategy()])
    assert acc.cash < 900


def test_position_increases(tmp_path):
    loop, acc, _ = setup(tmp_path)
    loop.run_once(Feed({"AAA": 100}), acc, [Strategy()])
    assert acc.positions["AAA"] == 1


def test_sell_position(tmp_path):
    loop, acc, _ = setup(tmp_path, positions={"AAA": 2})
    loop.run_once(Feed({"AAA": 100}), acc, [Strategy("SELL")])
    assert acc.positions["AAA"] == 1


def test_sell_tax_taiwan(tmp_path):
    loop, acc, _ = setup(tmp_path, positions={"2330.TW": 2})
    s = Strategy("SELL")
    s.ticker = "2330.TW"
    result = loop.run_once(Feed({"2330.TW": 100}), acc, [s])
    assert result["events"][0]["tax"] > 0


def test_no_us_sell_tax(tmp_path):
    loop, acc, _ = setup(tmp_path, positions={"AAA": 2})
    result = loop.run_once(Feed({"AAA": 100}), acc, [Strategy("SELL")])
    assert result["events"][0]["tax"] == 0


def test_position_cap(tmp_path):
    loop, acc, orders = setup(tmp_path, cash=100)
    result = loop.run_once(Feed({"AAA": 100}), acc, [Strategy(size=2)])
    assert result["events"][0]["reason"] == "POSITION_LIMIT"
    assert not orders.submitted


def test_leverage_cap(tmp_path):
    loop, acc, _ = setup(tmp_path, cash=1000, max_leverage=.05)
    result = loop.run_once(Feed({"AAA": 100}), acc, [Strategy()])
    assert result["events"][0]["reason"] == "LEVERAGE_LIMIT"


def test_insufficient_cash(tmp_path):
    loop, acc, _ = setup(tmp_path, cash=100)
    result = loop.run_once(Feed({"AAA": 100}), acc, [Strategy()])
    assert result["events"][0]["reason"] == "INSUFFICIENT_CASH"


def test_no_naked_stock_short(tmp_path):
    loop, acc, _ = setup(tmp_path)
    result = loop.run_once(Feed({"AAA": 100}), acc, [Strategy("SELL")])
    assert result["events"][0]["reason"] == "NO_SHORT_STOCK"


def test_circuit_breaker(tmp_path):
    loop, acc, _ = setup(tmp_path)
    acc.daily_start_equity = 2000
    result = loop.run_once(Feed({"AAA": 100}), acc, [Strategy()])
    assert result["circuit_breaker"]
    assert result["events"][0]["reason"] == "DAILY_LOSS_CIRCUIT_BREAKER"


def test_order_service_rejection(tmp_path):
    loop, acc, orders = setup(tmp_path, allowed=False)
    result = loop.run_once(Feed({"AAA": 100}), acc, [Strategy()])
    assert result["events"][0]["status"] == "REJECTED"
    assert not orders.submitted


def test_stale_quote(tmp_path):
    loop, acc, _ = setup(tmp_path)
    with pytest.raises(ValueError, match="STALE"):
        loop.run_once(Feed({"AAA": 100}, stale=301), acc, [Strategy()])


def test_invalid_price(tmp_path):
    loop, acc, _ = setup(tmp_path)
    with pytest.raises(ValueError, match="INVALID"):
        loop.run_once(Feed({"AAA": 0}), acc, [Strategy()])


def test_mark_to_market(tmp_path):
    loop, acc, _ = setup(tmp_path, cash=500, positions={"AAA": 2})
    result = loop.run_once(Feed({"AAA": 110}), acc, [])
    assert result["equity"] == 720


def test_journal_append_only(tmp_path):
    loop, acc, _ = setup(tmp_path)
    loop.run_once(Feed({"AAA": 100}), acc, [Strategy()])
    first = loop.journal_path.read_text()
    loop.run_once(Feed({"AAA": 100}), acc, [Strategy()])
    lines = loop.journal_path.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0] + "\n" == first
    assert json.loads(lines[0])["invalidation"] == "break 90"


def test_determinism(tmp_path):
    a, x, _ = setup(tmp_path / "a")
    b, y, _ = setup(tmp_path / "b")
    assert a.run_once(Feed({"AAA": 100}), x, [Strategy()]) == b.run_once(Feed({"AAA": 100}), y, [Strategy()])


@pytest.mark.parametrize("kind", ["option", "warrant", "future"])
def test_derivatives_fail_closed(tmp_path, kind):
    loop, acc, orders = setup(tmp_path)
    result = loop.run_once(Feed({"AAA": 100}), acc, [Strategy(kind=kind)])
    assert result["events"][0]["status"] == "REJECTED"
    assert not orders.submitted


def test_momentum_first_tick_empty():
    assert MomentumSwing("AAA").signals({"AAA": SimpleNamespace(c=100)}, None) == []


def test_momentum_second_tick():
    s = MomentumSwing("AAA")
    s.signals({"AAA": SimpleNamespace(c=100)}, None)
    assert len(s.signals({"AAA": SimpleNamespace(c=101)}, None)) == 1


def test_mean_reversion():
    s = MeanReversion("AAA")
    s.signals({"AAA": SimpleNamespace(c=100)}, None)
    assert len(s.signals({"AAA": SimpleNamespace(c=90)}, None)) == 1
