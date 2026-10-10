import pytest
from cio_market_lab.engine.pnl_attribution import attribute_pnl, PnLAttribution


def trade(pnl, **extra):
    return {"strategy_id": "s", "instrument_type": "stock", "horizon": "swing",
            "realized_pnl": pnl, "capital": 100, **extra}


def test_empty_journal():
    result = attribute_pnl()
    assert result["overall"]["closed_trades"] == 0
    assert result["overall"]["win_rate"] is None


def test_single_win():
    assert attribute_pnl(closed_trades=[trade(20)])["overall"]["win_rate"] == 1


def test_all_loss():
    result = attribute_pnl(closed_trades=[trade(-10), trade(-20)])
    assert result["overall"]["win_rate"] == 0
    assert result["overall"]["expectancy"] == -15
    assert result["learning_feedback"]["strategies_to_downweight"] == ["s"]


def test_fee_drag():
    result = attribute_pnl(closed_trades=[trade(5, fee=2, tax=1)])
    assert result["overall"]["fee_drag"] == 3


def test_drawdown():
    result = attribute_pnl(closed_trades=[trade(10, closed_at="2026-01-01"),
                                          trade(-15, closed_at="2026-01-02"),
                                          trade(4, closed_at="2026-01-03")])
    assert result["overall"]["max_drawdown"] == 15


def test_benchmark_relative():
    result = attribute_pnl(closed_trades=[trade(20)], benchmark_return=.1)
    assert result["overall"]["benchmark_relative_return"] == pytest.approx(.1)


def test_unrealized_snapshot():
    result = attribute_pnl(journal=[{"status": "OPEN_POSITION", "strategy_id": "s",
                                     "unrealized_pnl": 12, "capital": 100}])
    assert result["overall"]["unrealized_pnl"] == 12


def test_rejected_ignored():
    result = attribute_pnl(journal=[{"status": "REJECTED", "fee": 100}])
    assert result["overall"]["fee_drag"] == 0


def test_strategy_instrument_horizon_grouping():
    result = attribute_pnl(closed_trades=[trade(2, instrument_type="option", horizon="day")])
    assert result["by_strategy_instrument_horizon"]["s"]["option"]["day"]["realized_pnl"] == 2


def test_positive_feedback():
    result = attribute_pnl(closed_trades=[trade(5)])
    assert result["learning_feedback"]["what_worked"] == ["s"]


def test_markdown_summary():
    result = PnLAttribution().run(closed_trades=[trade(1)], as_of="2026-10-10")
    assert "# Paper PnL Attribution" in result["daily_markdown"]
    assert "2026-10-10" in result["daily_markdown"]


def test_nonfinite_rejected():
    with pytest.raises(ValueError):
        attribute_pnl(closed_trades=[trade(float("nan"))])


def test_negative_fees_rejected():
    with pytest.raises(ValueError):
        attribute_pnl(closed_trades=[trade(1, fee=-1)])


def test_open_position_not_counted_as_closed():
    result = attribute_pnl(journal=[{"status": "OPEN_POSITION", "unrealized_pnl": 10}])
    assert result["overall"]["closed_trades"] == 0


def test_cost_review_feedback():
    result = attribute_pnl(closed_trades=[trade(1, fee=4)])
    assert result["learning_feedback"]["suggested_parameter_changes"]["s"]["review_transaction_costs"] is True
