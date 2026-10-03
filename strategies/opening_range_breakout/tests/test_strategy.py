from datetime import datetime, timezone
import pytest
from cio_market_lab.domain.models import Bar, OrderSide
from cio_market_lab.strategies.base import StrategyContext
from strategies.opening_range_breakout.strategy import OpeningRangeBreakoutStrategy


def test_orb_describe_and_validate():
    strat = OpeningRangeBreakoutStrategy({"range_bars": 3, "rvol_threshold": 1.5})
    desc = strat.describe()
    assert "Opening Range Breakout" in desc["name"]
    assert strat.validate_config({"range_bars": 3, "rvol_threshold": 1.5}) == []
    assert len(strat.validate_config({"range_bars": 0})) > 0


def test_orb_signals_on_breakout():
    strat = OpeningRangeBreakoutStrategy({"range_bars": 2, "rvol_threshold": 1.0})
    ctx = StrategyContext(strategy_id="opening_range_breakout")

    # Bar 1 & Bar 2 form range: high=105, low=95, avg_vol=1000
    b1 = Bar(
        symbol="AAPL",
        timestamp=datetime(2026, 9, 23, 9, 30, tzinfo=timezone.utc),
        observed_at=datetime(2026, 9, 23, 9, 30, tzinfo=timezone.utc),
        open=100.0,
        high=105.0,
        low=98.0,
        close=102.0,
        volume=1000.0,
    )
    b2 = Bar(
        symbol="AAPL",
        timestamp=datetime(2026, 9, 23, 9, 45, tzinfo=timezone.utc),
        observed_at=datetime(2026, 9, 23, 9, 45, tzinfo=timezone.utc),
        open=102.0,
        high=104.0,
        low=95.0,
        close=100.0,
        volume=1000.0,
    )
    ctx.bars_history["AAPL"] = [b1, b2]

    # Bar 3 breaks above 105 with volume=1500
    b3 = Bar(
        symbol="AAPL",
        timestamp=datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc),
        observed_at=datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc),
        open=101.0,
        high=108.0,
        low=101.0,
        close=107.0,
        volume=1500.0,
    )

    signals = strat.on_bar(ctx, b3)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.side == OrderSide.BUY
    assert sig.symbol == "AAPL"
    assert "ORB_BREAKOUT_HIGH" in sig.reason_codes
    assert sig.evidence["range_high"] == 105.0
