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
