from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest

from cio_market_lab.data.replay import ReplayAdapter, generate_synthetic_bars
from cio_market_lab.domain.events import EventType
from cio_market_lab.domain.models import (
    Bar,
    DecisionScope,
    Market,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from cio_market_lab.engine.execution import (
    ExecutionCostConfig,
    ExecutionEngine,
    OHLCAmbiguityPolicy,
)
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.engine.simulation import SimulationEngine
from cio_market_lab.events.store import EventStore
from strategies.opening_range_breakout.strategy import OpeningRangeBreakoutStrategy
from strategies.volatility_contraction.strategy import VolatilityContractionStrategy


def test_deterministic_replay_equality():
    """Verify that two identical runs produce identical event logs and portfolio states."""
    adapter1 = ReplayAdapter()
    adapter2 = ReplayAdapter()

    vcp1 = VolatilityContractionStrategy({"lookback_bars": 6, "contraction_threshold": 0.8, "rvol_threshold": 1.0})
    vcp2 = VolatilityContractionStrategy({"lookback_bars": 6, "contraction_threshold": 0.8, "rvol_threshold": 1.0})

    orb1 = OpeningRangeBreakoutStrategy({"range_bars": 4, "rvol_threshold": 1.0})
    orb2 = OpeningRangeBreakoutStrategy({"range_bars": 4, "rvol_threshold": 1.0})

    sim1 = SimulationEngine()
    sim2 = SimulationEngine()

    res1 = sim1.run(adapter1, strategies=[vcp1, orb1], symbols=["2330.TW", "AAPL"])
    res2 = sim2.run(adapter2, strategies=[vcp2, orb2], symbols=["2330.TW", "AAPL"])

    assert res1.total_bars == res2.total_bars
    assert res1.total_signals == res2.total_signals
    assert res1.total_orders == res2.total_orders
    assert res1.total_fills == res2.total_fills
    assert res1.total_rejections == res2.total_rejections
    assert res1.is_deterministic is True
    assert res2.is_deterministic is True

    assert round(res1.swing_portfolio.equity, 2) == round(res2.swing_portfolio.equity, 2)
    assert round(res1.intraday_portfolio.equity, 2) == round(res2.intraday_portfolio.equity, 2)
    assert sim1.event_store.count() == sim2.event_store.count()


def test_event_store_reconstruct_portfolio_parity():
    """Verify that replaying all events from EventStore perfectly reconstructs the engine's final portfolio."""
    adapter = ReplayAdapter()
    vcp = VolatilityContractionStrategy({"lookback_bars": 6, "contraction_threshold": 0.9, "rvol_threshold": 1.0})
    orb = OpeningRangeBreakoutStrategy({"range_bars": 3, "rvol_threshold": 1.0})

    sim = SimulationEngine()
    res = sim.run(adapter, strategies=[vcp, orb], symbols=["2330.TW", "NVDA"])

    # Reconstruct directly from SQLite EventStore
    rec_swing = sim.event_store.reconstruct_portfolio(DecisionScope.SWING, sim.initial_cash_swing)
    rec_intraday = sim.event_store.reconstruct_portfolio(DecisionScope.INTRADAY, sim.initial_cash_intraday)

    assert round(res.swing_portfolio.equity, 2) == round(rec_swing.equity, 2)
    assert round(res.swing_portfolio.cash, 2) == round(rec_swing.cash, 2)
    assert round(res.swing_portfolio.realized_pnl, 2) == round(rec_swing.realized_pnl, 2)
    assert len(res.swing_portfolio.fills) == len(rec_swing.fills)

    assert round(res.intraday_portfolio.equity, 2) == round(rec_intraday.equity, 2)
    assert round(res.intraday_portfolio.cash, 2) == round(rec_intraday.cash, 2)
    assert len(res.intraday_portfolio.fills) == len(rec_intraday.fills)


def test_separate_swing_and_intraday_ledgers():
    """Verify complete ledger isolation: swing orders do not alter intraday cash or positions and vice-versa."""
    port_mgr = PortfolioManager(initial_cash_swing=500_000.0, initial_cash_intraday=100_000.0)

    # Place and fill a swing order
    swing_order = Order(
        order_id="o-swing-1",
        symbol="2330.TW",
        market=Market.TW,
        bucket=DecisionScope.SWING,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100.0,
    )
    port_mgr.add_order(swing_order)

    # Fill swing order
    from cio_market_lab.domain.models import Fill
    fill_swing = Fill(
        fill_id="f-swing-1",
        order_id="o-swing-1",
        symbol="2330.TW",
        bucket=DecisionScope.SWING,
        side=OrderSide.BUY,
        quantity=100.0,
        fill_price=1000.0,
        fee=142.5,
        tax=0.0,
        slippage=5.0,
        timestamp=datetime.now(timezone.utc),
    )
    port_mgr.apply_fill(fill_swing)

    swing_port = port_mgr.get_portfolio(DecisionScope.SWING)
    intraday_port = port_mgr.get_portfolio(DecisionScope.INTRADAY)

    # Swing cash debited (100 * 1000 + 142.5 + 5.0 = 100147.5 -> cash = 399852.5)
    assert round(swing_port.cash, 2) == 399852.5
    assert "2330.TW" in swing_port.positions
    assert swing_port.positions["2330.TW"].quantity == 100.0

    # Intraday cash and positions must remain completely untouched!
    assert intraday_port.cash == 100_000.0
    assert intraday_port.positions == {}
    assert len(intraday_port.fills) == 0


def test_reject_stale_bars():
    """Verify that stale bars are recorded in event store but reject new simulated entries."""
    base_t = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
    bars = generate_synthetic_bars(
        symbol="AAPL", count=15, start_price=150.0, base_time=base_t, inject_stale_indices=[5, 6, 7]
    )

    sim = SimulationEngine(reject_stale_bars=True)
    orb = OpeningRangeBreakoutStrategy({"range_bars": 2, "rvol_threshold": 0.5})

    res = sim.run(bars, strategies=[orb])

    # Stale bars must be observed and persisted
    stale_bar_events = [
        env for _, env in sim.event_store.get_events(EventType.BAR_OBSERVED)
        if env.payload.get("is_stale") is True
    ]
    assert len(stale_bar_events) == 3

    # Direct execution check with stale bar
    exec_engine = ExecutionEngine(reject_stale=True)
    stale_bar = bars[5]
    assert stale_bar.is_stale is True

    pending_order = Order(
        order_id="stale-test-1",
        symbol="AAPL",
        market=Market.US,
        bucket=DecisionScope.INTRADAY,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=50.0,
    )
    result = exec_engine.process_bar(stale_bar, [pending_order])
    assert len(result.fills) == 0
    assert len(result.rejections) == 1
    assert "STALE_BAR_REJECTION" in result.rejections[0][1]


def test_configurable_costs_fee_tax_slippage():
    """Verify that configurable fee, tax, and slippage are computed and deducted correctly."""
    custom_cost = ExecutionCostConfig(
        fee_rate_tw=0.002,      # 0.2%
        min_fee_tw=50.0,        # Min 50 NTD
        tax_rate_tw_sell=0.003, # 0.3%
        slippage_bps=10.0,      # 10 bps = 0.10%
    )
    exec_engine = ExecutionEngine(cost_config=custom_cost)

    base_t = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
    bar1 = Bar(
        symbol="2330.TW", timestamp=base_t, observed_at=base_t,
        open=1000.0, high=1010.0, low=995.0, close=1005.0, volume=50000.0
    )

    buy_order = Order(
        order_id="b-cost-1", symbol="2330.TW", market=Market.TW,
        bucket=DecisionScope.SWING, side=OrderSide.BUY, order_type=OrderType.MARKET,
        quantity=100.0
    )

    res = exec_engine.process_bar(bar1, [buy_order])
    assert len(res.fills) == 1
    fill = res.fills[0]

    # Slippage: 10 bps on 1000.0 -> effective_price = 1000.0 * 1.0010 = 1001.0
    assert fill.fill_price == 1001.0
    assert fill.slippage == 100.0  # 100 shares * 1.0 slippage

    # Trade value = 100 * 1001.0 = 100100.0. Fee = 100100.0 * 0.002 = 200.2
    assert fill.fee == 200.2
    # Tax on BUY is 0
    assert fill.tax == 0.0

    # Now SELL order
    sell_order = Order(
        order_id="s-cost-1", symbol="2330.TW", market=Market.TW,
        bucket=DecisionScope.SWING, side=OrderSide.SELL, order_type=OrderType.MARKET,
        quantity=100.0
    )
    bar2 = Bar(
        symbol="2330.TW", timestamp=base_t + timedelta(minutes=15), observed_at=base_t + timedelta(minutes=15),
        open=1100.0, high=1110.0, low=1090.0, close=1105.0, volume=50000.0
    )
    res_sell = exec_engine.process_bar(bar2, [sell_order])
    assert len(res_sell.fills) == 1
    sell_fill = res_sell.fills[0]

    # Slippage: 1100.0 * (1 - 0.0010) = 1098.9
    assert sell_fill.fill_price == 1098.9
    # Trade value = 100 * 1098.9 = 109890.0
    # TW Sell Tax = 109890.0 * 0.003 = 329.67
    assert sell_fill.tax == 329.67


def test_explicit_next_bar_execution():
    """Verify that a market order triggered by a bar is executed at the OPEN of the NEXT bar."""
    base_t = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
    bar1 = Bar(
        symbol="AAPL", timestamp=base_t, observed_at=base_t,
        open=150.0, high=155.0, low=149.0, close=154.0, volume=10000.0
    )
    bar2 = Bar(
        symbol="AAPL", timestamp=base_t + timedelta(minutes=15), observed_at=base_t + timedelta(minutes=15),
        open=160.0, high=165.0, low=159.0, close=163.0, volume=10000.0
    )

    cost_cfg = ExecutionCostConfig(slippage_bps=0.0)
    exec_engine = ExecutionEngine(cost_config=cost_cfg)

    # Order created after observing bar 1 (e.g. entry_model = NEXT_OPEN)
    order = Order(
        order_id="next-bar-order", symbol="AAPL", market=Market.US,
        bucket=DecisionScope.INTRADAY, side=OrderSide.BUY, order_type=OrderType.MARKET,
        quantity=10.0
    )

    # Order is executed against bar 2
    res = exec_engine.process_bar(bar2, [order])
    assert len(res.fills) == 1
    # Fills at bar 2 open (160.0), NOT bar 1 close (154.0)
    assert res.fills[0].fill_price == 160.0
    assert res.fills[0].timestamp == bar2.timestamp


def test_ohlc_ambiguity_pessimistic_vs_optimistic():
    """Verify that when both Stop Loss and Profit Limit are breachable in the same bar,

    OHLCAmbiguityPolicy determines execution precedence and cancels the other.
    """
    base_t = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
    # Bar with wide range: low=90.0, high=115.0
    ambiguous_bar = Bar(
        symbol="AAPL", timestamp=base_t, observed_at=base_t,
        open=100.0, high=115.0, low=90.0, close=105.0, volume=20000.0
    )

    # Holding long: Stop Loss at 95 (touched: low=90), Take Profit Limit at 110 (touched: high=115)
    def make_orders():
        stop = Order(
            order_id="stop-1", symbol="AAPL", market=Market.US, bucket=DecisionScope.SWING,
            side=OrderSide.SELL, order_type=OrderType.STOP, stop_price=95.0, quantity=100.0
        )
        limit = Order(
            order_id="limit-1", symbol="AAPL", market=Market.US, bucket=DecisionScope.SWING,
            side=OrderSide.SELL, order_type=OrderType.LIMIT, limit_price=110.0, quantity=100.0
        )
        return stop, limit

    # 1. Pessimistic policy: Stop loss triggers first
    engine_pessimistic = ExecutionEngine(
        cost_config=ExecutionCostConfig(slippage_bps=0.0),
        ambiguity_policy=OHLCAmbiguityPolicy.PESSIMISTIC,
    )
    stop1, limit1 = make_orders()
    res_pessimistic = engine_pessimistic.process_bar(ambiguous_bar, [stop1, limit1])

    assert len(res_pessimistic.fills) == 1
    assert res_pessimistic.fills[0].order_id == "stop-1"
    assert res_pessimistic.fills[0].fill_price == 95.0  # Stopped out
    assert len(res_pessimistic.rejections) == 1
    assert res_pessimistic.rejections[0][0].order_id == "limit-1"
    assert "CANCELLED_DUE_TO_OHLC_AMBIGUITY_PESSIMISTIC" in res_pessimistic.rejections[0][1]

    # 2. Optimistic policy: Profit target limit triggers first
    engine_optimistic = ExecutionEngine(
        cost_config=ExecutionCostConfig(slippage_bps=0.0),
        ambiguity_policy=OHLCAmbiguityPolicy.OPTIMISTIC,
    )
    stop2, limit2 = make_orders()
    res_optimistic = engine_optimistic.process_bar(ambiguous_bar, [stop2, limit2])

    assert len(res_optimistic.fills) == 1
    assert res_optimistic.fills[0].order_id == "limit-1"
    assert res_optimistic.fills[0].fill_price == 110.0  # Profit taken
    assert len(res_optimistic.rejections) == 1
    assert res_optimistic.rejections[0][0].order_id == "stop-1"
    assert "CANCELLED_DUE_TO_OHLC_AMBIGUITY_OPTIMISTIC" in res_optimistic.rejections[0][1]
