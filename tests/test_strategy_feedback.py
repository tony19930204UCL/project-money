import pytest
from cio_market_lab.engine.strategy_feedback import (
    update_weights, apply_weights, validate_table, load_table, save_table)
from cio_market_lab.engine.paper_trading_loop import Signal


def table(weight=1.0):
    return {"schema_version": 1, "weights": {"MomentumSwing": {"weight": weight}}}


def pnl(expectancy=-2, count=12):
    return {"by_strategy": {"MomentumSwing": {"expectancy": expectancy, "closed_trades": count}}}


def update(p, t=None, **kwargs):
    return update_weights(p, table() if t is None else t, hysteresis=1, **kwargs)[0]["weights"]["MomentumSwing"]["weight"]


def test_negative(): assert update(pnl()) == .9
def test_positive(): assert update(pnl(2)) == 1.1
def test_min_sample(): assert update(pnl(-2, 9)) == 1
def test_exact_min_sample(): assert update(pnl(-2, 10)) == .9
def test_ceiling(): assert update(pnl(2), table(1.5)) == 1.5
def test_floor(): assert update(pnl(-2), table(0)) == 0
def test_floor_reached(): assert update(pnl(-2), table(.05)) == 0
def test_zero_expectancy(): assert update(pnl(0)) == 1
def test_no_closed_trades(): assert update(pnl(None, 0)) == 1
def test_unknown_strategy_unchanged(): assert update_weights({"by_strategy": {"other": {"closed_trades": 10, "expectancy": 2}}}, table())[0] == table()


def test_hysteresis():
    first, log = update_weights(pnl(), table(), hysteresis=2, evidence_id="a")
    assert first["weights"]["MomentumSwing"]["weight"] == 1
    assert "hysteresis" in log
    second, _ = update_weights(pnl(), first, hysteresis=2, evidence_id="b")
    assert second["weights"]["MomentumSwing"]["weight"] == .9


def test_idempotent_evidence():
    first, _ = update_weights(pnl(), table(), hysteresis=1, evidence_id="a")
    second, log = update_weights(pnl(), first, hysteresis=1, evidence_id="a")
    assert second == first
    assert "already processed" in log


def test_reversal_requires_confirmation():
    a, _ = update_weights(pnl(-2), table(), hysteresis=2, evidence_id="a")
    b, _ = update_weights(pnl(2), a, hysteresis=2, evidence_id="b")
    assert b["weights"]["MomentumSwing"]["weight"] == 1
    assert b["weights"]["MomentumSwing"]["pending"] == 1


def test_corrupt_table(tmp_path):
    path = tmp_path / "weights.json"
    path.write_text("{broken")
    with pytest.raises(ValueError, match="CORRUPT"): load_table(path)


def test_invalid_weight():
    with pytest.raises(ValueError): validate_table(table(-1))


def test_save_load(tmp_path):
    path = tmp_path / "weights.json"
    save_table(path, table())
    assert load_table(path) == table()


class Fake:
    ticker = "MSFT"
    strategy_id = "MomentumSwing"
    def signals(self, quotes, account):
        return [Signal("MSFT", "stock", "BUY", 10, None, None, "paper")]


def test_apply_scales_size():
    assert apply_weights([Fake()], table(.5))[0].signals({}, None)[0].size == 5


def test_apply_disabled():
    assert apply_weights([Fake()], table(0))[0].signals({}, None) == []


def test_apply_idempotent_wrapper():
    once = apply_weights([Fake()], table(.5))
    twice = apply_weights(once, table(.5))
    assert twice[0].signals({}, None)[0].size == 5


def test_no_mutation():
    original = table()
    update_weights(pnl(), original, hysteresis=1)
    assert original == table()


def test_zero_weight_real_run_once_owned_exit_and_entry_suppression(tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from cio_market_lab.engine.paper_trading_loop import PaperAccount, PaperTradingLoop

    class Orders:
        def _risk_decision(self, request):
            return SimpleNamespace(allowed=True, reasons=[])

        def submit(self, request):
            return SimpleNamespace(order_id="synthetic")

    class Feed:
        def latest(self, symbol):
            return SimpleNamespace(c=100.0, staleness_seconds=0,
                                   source="synthetic", ts=datetime(2026, 10, 12, tzinfo=timezone.utc))

    class Strategy:
        ticker = "MSFT"
        strategy_id = "MomentumSwing"

        def __init__(self, side, size):
            self.side, self.size = side, size

        def signals(self, quotes, account):
            return [Signal("MSFT", "stock", self.side, self.size, None, None, "synthetic")]

    journal = tmp_path / "paper.jsonl"
    loop = PaperTradingLoop(Orders(), journal, max_position_pct=1)
    account = PaperAccount(10000)
    # Seed ownership through the real order path, not a fabricated journal.
    opened = loop.run_once(Feed(), account, [Strategy("BUY", 4)])
    assert opened["events"][0]["status"] == "SIMULATED"
    assert loop.ownership["MSFT"]["qty"] == 4

    zero_buy = apply_weights([Strategy("BUY", 3)], table(0))
    assert loop.run_once(Feed(), account, zero_buy)["events"] == []
    assert account.positions["MSFT"] == 4

    zero_sell = apply_weights([Strategy("SELL", 2)], table(0))
    closed = loop.run_once(Feed(), account, zero_sell)
    assert len(closed["events"]) == 1
    assert closed["events"][0]["side"] == "SELL"
    assert closed["events"][0]["status"] == "SIMULATED"
    assert closed["events"][0]["size"] == 2
    assert account.positions["MSFT"] == 2
    assert loop.ownership["MSFT"]["qty"] == 2

    # Rehydrate from successful fills; foreign exit must still be refused.
    restarted = PaperTradingLoop(Orders(), journal, max_position_pct=1)
    assert restarted.ownership["MSFT"]["qty"] == 2
    assert restarted.ownership["MSFT"]["owner"] == "MomentumSwing"
    foreign = Strategy("SELL", 1)
    foreign.strategy_id = "OtherStrategy"
    foreign_zero = apply_weights([foreign], {
        "schema_version": 1, "weights": {"OtherStrategy": {"weight": 0}}})
    rejected = restarted.run_once(Feed(), account, foreign_zero)["events"]
    assert len(rejected) == 1
    assert rejected[0]["status"] == "REJECTED"
    assert rejected[0]["reason"] == "FOREIGN_OR_UNOWNED_POSITION"
    assert restarted.ownership["MSFT"]["qty"] == 2
    assert account.positions["MSFT"] == 2

    # A rejected owned exit must not mutate surviving ownership.
    class RejectOrders(Orders):
        def _risk_decision(self, request):
            return SimpleNamespace(allowed=False, reasons=["synthetic_refusal"])

    refused_loop = PaperTradingLoop(RejectOrders(), journal, max_position_pct=1)
    refused = refused_loop.run_once(Feed(), account, zero_sell)["events"]
    assert refused[0]["status"] == "REJECTED"
    assert refused[0]["reason"] == "PAPER_ORDER_REJECTED:synthetic_refusal"
    assert refused_loop.ownership["MSFT"]["qty"] == 2
    assert account.positions["MSFT"] == 2
