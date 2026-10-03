from datetime import datetime, timedelta, timezone
import pytest
from cio_market_lab.domain.models import Bar, OrderSide
from cio_market_lab.strategies.base import StrategyContext
from strategies.volatility_contraction.strategy import VolatilityContractionStrategy


def test_vcp_describe_and_validate():
    strat = VolatilityContractionStrategy({"lookback_bars": 10, "contraction_threshold": 0.5})
    desc = strat.describe()
    assert "Volatility Contraction" in desc["name"]
    assert strat.validate_config({"lookback_bars": 10, "contraction_threshold": 0.5}) == []
    assert len(strat.validate_config({"lookback_bars": 3})) > 0


def test_vcp_signals_on_contraction_and_volume():
    strat = VolatilityContractionStrategy(
        {"lookback_bars": 6, "contraction_threshold": 0.6, "rvol_threshold": 1.0}
    )
    ctx = StrategyContext(strategy_id="volatility_contraction")
    base_t = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)

    # 3 prior bars with wide range (high=120, low=100 -> range 20)
    b1 = Bar(
        symbol="2330.TW", timestamp=base_t, observed_at=base_t,
        open=105.0, high=120.0, low=100.0, close=110.0, volume=1000.0
    )
    b2 = Bar(
        symbol="2330.TW", timestamp=base_t + timedelta(minutes=15), observed_at=base_t + timedelta(minutes=15),
        open=110.0, high=118.0, low=102.0, close=105.0, volume=1000.0
    )
    b3 = Bar(
        symbol="2330.TW", timestamp=base_t + timedelta(minutes=30), observed_at=base_t + timedelta(minutes=30),
        open=105.0, high=115.0, low=101.0, close=110.0, volume=1000.0
    )

    # 3 recent bars with narrow range (high=112, low=108 -> range 4, contraction_ratio = 4/20 = 0.2 <= 0.6)
    b4 = Bar(
        symbol="2330.TW", timestamp=base_t + timedelta(minutes=45), observed_at=base_t + timedelta(minutes=45),
        open=110.0, high=112.0, low=108.0, close=109.0, volume=1000.0
    )
    b5 = Bar(
        symbol="2330.TW", timestamp=base_t + timedelta(minutes=60), observed_at=base_t + timedelta(minutes=60),
        open=109.0, high=111.0, low=108.5, close=110.0, volume=1000.0
    )
    b6 = Bar(
        symbol="2330.TW", timestamp=base_t + timedelta(minutes=75), observed_at=base_t + timedelta(minutes=75),
        open=110.0, high=112.0, low=109.0, close=111.0, volume=1000.0
    )
    ctx.bars_history["2330.TW"] = [b1, b2, b3, b4, b5, b6]

    # Bar 7 breaks above recent high 112 with close 114 and volume 2000
    b7 = Bar(
        symbol="2330.TW", timestamp=base_t + timedelta(minutes=90), observed_at=base_t + timedelta(minutes=90),
        open=111.0, high=114.5, low=110.5, close=114.0, volume=2000.0
    )

    signals = strat.on_bar(ctx, b7)
    assert len(signals) == 1
    assert signals[0].side == OrderSide.BUY
    assert signals[0].symbol == "2330.TW"
    assert "VCP_CONTRACTION_MET" in signals[0].reason_codes
