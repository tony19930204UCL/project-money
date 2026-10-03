"""Unit tests for standalone paper derivatives simulation engine candidate.

Verifies:
1. Multiplier and tick size enforcement
2. Expiry guard & pre-expiry close requirement (no holding across expiry)
3. Bid/ask spreads and cost deduction
4. Margin deficiency enforcement
5. Daily marked-to-market variation settlement resetting baseline to prevent double counting
6. Equity-margin financing cost accrual over elapsed time
7. Strict prohibition of naked option shorting
8. Production mode fixture and stale data rejection
9. Pricing failure blocks entry while preserving risk review
10. Accounting conservation, idempotency deduplication, and EventEnvelope compatibility
11. Runtime eligibility status remaining unavailable pending adapter acceptance
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import pytest

from cio_market_lab.domain.events import EventType
from cio_market_lab.domain.models import OptionRight, OrderSide
from cio_market_lab.engine.paper_derivatives import (
    AccountingDelta,
    ContractSpec,
    CostConfig,
    DerivativeInstrumentType,
    DerivativePosition,
    DerivativeQuote,
    ExecutionAttemptResult,
    FinancingConfig,
    NormalizedAccountingEvent,
    NormalizedStateReducer,
    PaperDerivativesEngine,
    RejectionReason,
    RUNTIME_ELIGIBILITY,
    SIMULATION_ASSUMPTION_TAG,
)


@pytest.fixture
def base_now() -> datetime:
    return datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def option_spec(base_now: datetime) -> ContractSpec:
    return ContractSpec(
        symbol="TXO-202610-22000-C",
        underlying_symbol="TX",
        instrument_type=DerivativeInstrumentType.OPTION,
        option_right=OptionRight.CALL,
        strike=22000.0,
        expiry=base_now + timedelta(days=20),
        multiplier=50.0,  # 50 TWD per point
        tick_size=1.0,
        currency="TWD",
    )


@pytest.fixture
def future_spec(base_now: datetime) -> ContractSpec:
    return ContractSpec(
        symbol="TX-202610",
        underlying_symbol="TX",
        instrument_type=DerivativeInstrumentType.FUTURE,
        expiry=base_now + timedelta(days=20),
        multiplier=200.0,  # 200 TWD per index point
        tick_size=1.0,
        initial_margin_per_contract=184000.0,
        maintenance_margin_per_contract=141000.0,
        currency="TWD",
    )


def test_runtime_eligibility_and_simulation_assumptions():
    """Verify runtime eligibility is UNAVAILABLE and rules are labeled simulation assumptions."""
    engine = PaperDerivativesEngine()
    assert engine.runtime_eligibility == "UNAVAILABLE_PENDING_ADAPTER_ACCEPTANCE"
    assert "UNAVAILABLE" in RUNTIME_ELIGIBILITY

    spec = ContractSpec(
        symbol="TEST",
        underlying_symbol="UND",
        instrument_type=DerivativeInstrumentType.FUTURE,
        multiplier=1.0,
        tick_size=1.0,
        initial_margin_per_contract=50000.0,
    )
    assert "SIMULATION_ASSUMPTION" in spec.margin_rules_label
    
    finance = FinancingConfig()
    assert "SIMULATION_ASSUMPTION" in finance.assumption_label


def test_option_long_premium_paid_and_bounded_loss(option_spec: ContractSpec, base_now: datetime):
    """Test long option purchase, cash debit, and bounded maximum loss."""
    engine = PaperDerivativesEngine(cost_config=CostConfig(fee_rate=0.001, min_fee=10.0))
    quote = DerivativeQuote(
        symbol=option_spec.symbol,
        timestamp=base_now,
        bid=150.0,
        ask=152.0,
        last_price=151.0,
        source="verified_feed",
    )

    # 1. Buy 2 contracts at ask price 152.0
    # Notional = 2 * 152.0 * 50 = 15,200. Fee = 15.2 (min 10) -> 15.2
    res = engine.execute_order(
        order_id="opt-ord-1",
        side=OrderSide.BUY,
        quantity=2.0,
        spec=option_spec,
        quote=quote,
        available_cash=50000.0,
        as_of=base_now,
    )

    assert res.success is True
    assert res.fill_price == 152.0
    assert res.executed_quantity == 2.0
    assert res.max_loss_bound is not None
    # Max loss is bounded to total cash outlay
    assert res.max_loss_bound == pytest.approx(15200.0 + res.fee, 0.01)
    assert res.cash_flow < 0
    assert res.position is not None
    assert res.position.quantity == 2.0
    assert res.position.total_premium_paid == pytest.approx(15200.0 + res.fee, 0.01)

    # 2. Mark to Market when price falls to zero: loss cannot exceed total_premium_paid
    zero_quote = DerivativeQuote(
        symbol=option_spec.symbol,
        timestamp=base_now + timedelta(hours=1),
        bid=0.0,
        ask=0.5,
        last_price=0.0,
    )
    mtm_pos = engine.mark_to_market(res.position, option_spec, zero_quote, as_of=base_now + timedelta(hours=1))
    assert mtm_pos.unrealized_pnl == -res.position.total_premium_paid
    assert mtm_pos.market_value == 0.0


def test_naked_option_shorting_is_strictly_forbidden(option_spec: ContractSpec, base_now: datetime):
    """Selling options without covering long contracts is rejected deterministically."""
    engine = PaperDerivativesEngine()
    quote = DerivativeQuote(
        symbol=option_spec.symbol,
        timestamp=base_now,
        bid=150.0,
        ask=152.0,
        last_price=151.0,
    )

    # Attempt to sell with no existing position
    res = engine.execute_order(
        order_id="naked-1",
        side=OrderSide.SELL,
        quantity=1.0,
        spec=option_spec,
        quote=quote,
        available_cash=500000.0,
        existing_position=None,
        as_of=base_now,
    )
    assert res.success is False
    assert res.rejection_reason == RejectionReason.NAKED_OPTION_SHORT_FORBIDDEN.value

    # Attempt to sell more than currently held long
    pos = DerivativePosition(
        position_id="p-1",
        symbol=option_spec.symbol,
        underlying_symbol="TX",
        instrument_type=DerivativeInstrumentType.OPTION,
        quantity=1.0,
        multiplier=50.0,
    )
    res_oversell = engine.execute_order(
        order_id="naked-2",
        side=OrderSide.SELL,
        quantity=2.0,
        spec=option_spec,
        quote=quote,
        available_cash=500000.0,
        existing_position=pos,
        as_of=base_now,
    )
    assert res_oversell.success is False
    assert res_oversell.rejection_reason == RejectionReason.NAKED_OPTION_SHORT_FORBIDDEN.value


def test_futures_multiplier_and_tick_validation(future_spec: ContractSpec, base_now: datetime):
    """Verify futures multiplier notional and tick size enforcement."""
    engine = PaperDerivativesEngine()

    # Invalid tick price: future_spec has tick_size=1.0, quote at 22000.5
    quote_bad_tick = DerivativeQuote(
        symbol=future_spec.symbol,
        timestamp=base_now,
        bid=22000.5,
        ask=22000.5,
        last_price=22000.5,
    )
    res = engine.execute_order(
        order_id="fut-1",
        side=OrderSide.BUY,
        quantity=1.0,
        spec=future_spec,
        quote=quote_bad_tick,
        available_cash=500000.0,
        as_of=base_now,
    )
    assert res.success is False
    assert res.rejection_reason == RejectionReason.INVALID_TICK_SIZE.value

    # Valid tick: 22000.0
    quote_valid = DerivativeQuote(
        symbol=future_spec.symbol,
        timestamp=base_now,
        bid=22000.0,
        ask=22000.0,
        last_price=22000.0,
    )
    res_valid = engine.execute_order(
        order_id="fut-2",
        side=OrderSide.BUY,
        quantity=1.0,
        spec=future_spec,
        quote=quote_valid,
        available_cash=500000.0,
        as_of=base_now,
    )
    assert res_valid.success is True
    assert res_valid.position is not None
    assert res_valid.position.multiplier == 200.0
    assert res_valid.position.margin_locked == 184000.0


def test_margin_deficiency_rejection(future_spec: ContractSpec, base_now: datetime):
    """Order must be rejected if cash is insufficient for required initial margin."""
    engine = PaperDerivativesEngine()
    quote = DerivativeQuote(
        symbol=future_spec.symbol,
        timestamp=base_now,
        bid=22000.0,
        ask=22000.0,
        last_price=22000.0,
    )

    # Initial margin required for 1 contract is 184,000
    res = engine.execute_order(
        order_id="fut-def",
        side=OrderSide.BUY,
        quantity=1.0,
        spec=future_spec,
        quote=quote,
        available_cash=100000.0,  # Insufficient!
        as_of=base_now,
    )
    assert res.success is False
    assert res.rejection_reason == RejectionReason.MARGIN_DEFICIENCY.value


def test_futures_daily_settlement_resets_and_avoids_double_counting(
    future_spec: ContractSpec, base_now: datetime
):
    """Verify MTM daily settlement resets baseline and prevents double-counting upon close."""
    engine = PaperDerivativesEngine(cost_config=CostConfig(fee_rate=0.0, tax_rate=0.0))

    # Day 0: Open 1 long future at 22,000 (multiplier = 200)
    open_quote = DerivativeQuote(
        symbol=future_spec.symbol,
        timestamp=base_now,
        bid=22000.0,
        ask=22000.0,
        last_price=22000.0,
    )
    open_res = engine.execute_order(
        order_id="fut-open",
        side=OrderSide.BUY,
        quantity=1.0,
        spec=future_spec,
        quote=open_quote,
        available_cash=500000.0,
        as_of=base_now,
    )
    assert open_res.success is True
    pos = open_res.position
    assert pos.last_settlement_price == 22000.0

    # Day 1: Settle at 22,100 (+100 points * 200 = +20,000 TWD)
    settle1 = engine.settle_daily_variation(
        pos, future_spec, settlement_price=22100.0, settlement_date="2026-09-28", as_of=base_now
    )
    assert settle1.variation_pnl == 20000.0
    assert settle1.delta.cash_delta == 20000.0
    assert pos.last_settlement_price == 22100.0  # Reset baseline
    assert pos.accumulated_settled_pnl == 20000.0

    # Day 2: Settle at 22,050 (-50 points * 200 = -10,000 TWD)
    settle2 = engine.settle_daily_variation(
        pos, future_spec, settlement_price=22050.0, settlement_date="2026-09-29", as_of=base_now + timedelta(days=1)
    )
    assert settle2.variation_pnl == -10000.0
    assert settle2.delta.cash_delta == -10000.0
    assert pos.last_settlement_price == 22050.0  # Reset baseline
    assert pos.accumulated_settled_pnl == 10000.0

    # Day 3: Exit position at 22,200 (+150 points from last settlement price 22050 = +30,000 TWD)
    exit_quote = DerivativeQuote(
        symbol=future_spec.symbol,
        timestamp=base_now + timedelta(days=2),
        bid=22200.0,
        ask=22200.0,
        last_price=22200.0,
    )
    exit_res = engine.execute_order(
        order_id="fut-close",
        side=OrderSide.SELL,
        quantity=1.0,
        spec=future_spec,
        quote=exit_quote,
        available_cash=500000.0,
        existing_position=pos,
        as_of=base_now + timedelta(days=2),
    )
    assert exit_res.success is True
    # Realized from last settlement is +30,000
    assert exit_res.delta.realized_pnl_delta == 30000.0
    assert exit_res.cash_flow == 30000.0

    # Lifecycle conservation:
    # Day 1: +20,000
    # Day 2: -10,000
    # Day 3: +30,000
    # Total cash gained: +40,000
    # Economic gain: (22200 - 22000) * 200 = 40,000. Exact equality, zero double counting!
    total_settled = settle1.variation_pnl + settle2.variation_pnl + exit_res.delta.realized_pnl_delta
    expected_economic_gain = (22200.0 - 22000.0) * 200.0
    assert total_settled == expected_economic_gain


def test_expiry_guard_and_pre_expiry_close_required(base_now: datetime):
    """Missing delivery handling disallows holding across expiry; deterministic close required event."""
    exp = base_now + timedelta(hours=2)
    spec = ContractSpec(
        symbol="TXO-EXPIRING",
        underlying_symbol="TX",
        instrument_type=DerivativeInstrumentType.OPTION,
        strike=22000.0,
        expiry=exp,
        multiplier=50.0,
        tick_size=1.0,
        pre_expiry_close_lead_seconds=3600.0,  # 1 hour lead window
    )
    engine = PaperDerivativesEngine()

    # Time is 30 minutes before expiry (inside lead window)
    inside_window_ts = exp - timedelta(minutes=30)
    quote = DerivativeQuote(
        symbol=spec.symbol,
        timestamp=inside_window_ts,
        bid=20.0,
        ask=21.0,
        last_price=20.5,
    )

    # Attempting new entry inside pre-expiry window must be blocked
    entry_res = engine.execute_order(
        order_id="ord-exp-1",
        side=OrderSide.BUY,
        quantity=1.0,
        spec=spec,
        quote=quote,
        available_cash=50000.0,
        as_of=inside_window_ts,
    )
    assert entry_res.success is False
    assert entry_res.rejection_reason == RejectionReason.PRE_EXPIRY_CLOSE_WINDOW_ACTIVE.value

    # Existing position inside window triggers deterministic PRE_EXPIRY_CLOSE_REQUIRED
    existing_pos = DerivativePosition(
        position_id="pos-exp",
        symbol=spec.symbol,
        underlying_symbol="TX",
        instrument_type=DerivativeInstrumentType.OPTION,
        quantity=1.0,
        multiplier=50.0,
    )
    risk_res = engine.check_position_risk(
        position=existing_pos,
        spec=spec,
        quote=quote,
        total_account_equity=100000.0,
        total_maintenance_required=0.0,
        as_of=inside_window_ts,
    )
    assert risk_res.healthy is False
    assert risk_res.pre_expiry_close_required is True
    assert any(e.event_type == "PRE_EXPIRY_CLOSE_REQUIRED" for e in risk_res.events)


def test_pricing_failure_blocks_entry_without_preventing_risk_review(option_spec: ContractSpec, base_now: datetime):
    """Pricing failure blocks entry, but risk-close review proceeds without crash."""
    engine = PaperDerivativesEngine()
    bad_quote = DerivativeQuote(
        symbol=option_spec.symbol,
        timestamp=base_now,
        bid=100.0,
        ask=90.0,  # Inverted bid/ask!
        last_price=95.0,
    )

    # 1. Entry order blocked
    entry_res = engine.execute_order(
        order_id="ord-bad-px",
        side=OrderSide.BUY,
        quantity=1.0,
        spec=option_spec,
        quote=bad_quote,
        available_cash=100000.0,
        as_of=base_now,
    )
    assert entry_res.success is False
    assert entry_res.rejection_reason == RejectionReason.PRICING_FAILURE_ENTRY_BLOCKED.value

    # 2. Risk review of existing position continues with inspection-only message
    pos = DerivativePosition(
        position_id="pos-px",
        symbol=option_spec.symbol,
        underlying_symbol="TX",
        instrument_type=DerivativeInstrumentType.OPTION,
        quantity=1.0,
        multiplier=50.0,
    )
    risk_res = engine.check_position_risk(
        position=pos,
        spec=option_spec,
        quote=bad_quote,
        total_account_equity=100000.0,
        total_maintenance_required=0.0,
        as_of=base_now,
    )
    assert risk_res.pricing_available is False
    assert "PRICING_UNAVAILABLE_INSPECTION_ONLY" in risk_res.messages


def test_production_mode_rejects_fixture_and_stale_data(option_spec: ContractSpec, base_now: datetime):
    """Production mode strictly rejects test fixtures and stale quotes."""
    engine_prod = PaperDerivativesEngine(production_mode=True, max_staleness_seconds=300.0)

    # 1. Fixture quote rejected in production
    fixture_quote = DerivativeQuote(
        symbol=option_spec.symbol,
        timestamp=base_now,
        bid=100.0,
        ask=101.0,
        is_fixture=True,
        provenance={"authority": "MAIN_CIO", "source": "unit_test"},
    )
    res_fix = engine_prod.execute_order(
        order_id="ord-fix",
        side=OrderSide.BUY,
        quantity=1.0,
        spec=option_spec,
        quote=fixture_quote,
        available_cash=50000.0,
        as_of=base_now,
    )
    assert res_fix.success is False
    assert res_fix.rejection_reason == RejectionReason.QUOTE_FIXTURE_REJECTED.value

    # 2. Stale quote rejected
    stale_quote = DerivativeQuote(
        symbol=option_spec.symbol,
        timestamp=base_now - timedelta(seconds=600),
        bid=100.0,
        ask=101.0,
        is_fixture=False,
        provenance={"authority": "MAIN_CIO", "source": "prod_feed"},
    )
    res_stale = engine_prod.execute_order(
        order_id="ord-stale",
        side=OrderSide.BUY,
        quantity=1.0,
        spec=option_spec,
        quote=stale_quote,
        available_cash=50000.0,
        as_of=base_now,
    )
    assert res_stale.success is False
    assert res_stale.rejection_reason == RejectionReason.QUOTE_STALE.value


def test_equity_margin_financing_cost_elapsed_time(base_now: datetime):
    """Verify financing cost accrual calculation based on elapsed time."""
    engine = PaperDerivativesEngine()
    # 1,000,000 borrowed at 6.5% for exactly 1 day (86,400s)
    borrowed = 1_000_000.0
    elapsed_seconds = 86400.0
    res = engine.accrue_financing_cost(
        position_id="pos-margin-1",
        symbol="2330.TW",
        borrowed_principal=borrowed,
        elapsed_seconds=elapsed_seconds,
        as_of=base_now,
    )

    expected_cost = round(1_000_000.0 * 0.065 * (86400.0 / (365.0 * 86400.0)), 4)
    assert res.financing_cost == expected_cost
    assert res.delta.cash_delta == -expected_cost
    assert res.delta.financing_cost_delta == expected_cost


def test_maintenance_margin_deficiency_emits_liquidation_required(future_spec: ContractSpec, base_now: datetime):
    """Equity breach below maintenance margin emits deterministic LIQUIDATION_REQUIRED without claiming brokerage fill."""
    engine = PaperDerivativesEngine()
    pos = DerivativePosition(
        position_id="pos-fut-breach",
        symbol=future_spec.symbol,
        underlying_symbol="TX",
        instrument_type=DerivativeInstrumentType.FUTURE,
        quantity=1.0,
        multiplier=200.0,
    )
    quote = DerivativeQuote(
        symbol=future_spec.symbol,
        timestamp=base_now,
        bid=22000.0,
        ask=22000.0,
        last_price=22000.0,
    )

    # Required maintenance is 141,000. Account equity is only 100,000
    risk_res = engine.check_position_risk(
        position=pos,
        spec=future_spec,
        quote=quote,
        total_account_equity=100000.0,
        total_maintenance_required=141000.0,
        as_of=base_now,
    )

    assert risk_res.healthy is False
    assert risk_res.liquidation_required is True
    assert risk_res.margin_deficient is True
    
    liq_events = [e for e in risk_res.events if e.event_type == "LIQUIDATION_REQUIRED"]
    assert len(liq_events) == 1
    # Verify no claim of external brokerage execution
    assert liq_events[0].payload["execution_claim"] == "NONE_SIMULATION_FLAG_ONLY"


def test_accounting_conservation_and_idempotency_replay(option_spec: ContractSpec, base_now: datetime):
    """Verify reducer idempotency deduplication and exact conservation of cash and events."""
    engine = PaperDerivativesEngine()
    quote = DerivativeQuote(
        symbol=option_spec.symbol,
        timestamp=base_now,
        bid=100.0,
        ask=100.0,
        last_price=100.0,
    )

    # 1. Execute order
    res = engine.execute_order(
        order_id="idem-1",
        side=OrderSide.BUY,
        quantity=1.0,
        spec=option_spec,
        quote=quote,
        available_cash=50000.0,
        as_of=base_now,
    )
    assert res.success is True
    event = res.event
    assert event is not None

    # Verify duplicate order execution with same idempotency key is rejected
    duplicate_res = engine.execute_order(
        order_id="idem-1",
        side=OrderSide.BUY,
        quantity=1.0,
        spec=option_spec,
        quote=quote,
        available_cash=50000.0,
        as_of=base_now,
    )
    assert duplicate_res.success is False
    assert duplicate_res.rejection_reason == "DUPLICATE_IDEMPOTENT_REQUEST"

    # 2. Test NormalizedStateReducer replay idempotency
    reducer = NormalizedStateReducer(initial_cash=100000.0)
    applied1 = reducer.apply(event)
    assert applied1 is True
    cash_after_1 = reducer.cash

    # Re-applying same event must return False and not change cash balance
    applied2 = reducer.apply(event)
    assert applied2 is False
    assert reducer.cash == cash_after_1

    # 3. Test conversion to standard EventEnvelope
    envelope = event.to_event_envelope()
    assert envelope.event_type == EventType.POSITION_UPDATED
    assert envelope.aggregate_id == option_spec.symbol
    assert envelope.payload["normalized_event_type"] == "DERIVATIVE_OPTION_OPENED"


# ============================================================================
# 12. Autonomous Cycle Contract Fields Present (Regression)
# ============================================================================

def test_autonomous_cycle_contract_fields_present():
    """Regression: verify _run_symbol CIO decision path resolves self._now()
    correctly when no CIO provider is configured.

    This test was previously failing because _run_cio_decision_path (within
    _run_symbol) referenced bare 'now' instead of self._now(), causing NameError.

    Also verifies that contract-required fields (symbol, multiplier, tick_size,
    currency) are present and correctly typed on ContractSpec.
    """
    import os
    import tempfile
    from pathlib import Path
    from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner, DYNAMIC_DESK_ID
    from cio_market_lab.engine.paper_orders import PaperExperimentSettings, PaperOrderService
    from cio_market_lab.engine.portfolio import PortfolioManager
    from cio_market_lab.engine.team_ops import TEAM_INITIAL_CAPITAL_TWD
    from cio_market_lab.events.store import EventStore
    from cio_market_lab.domain.models import Bar, Quote, Market, DecisionScope

    class FixtureAdapter:
        source_name = "fixture"

        def __init__(self, t):
            self.t = t

        def get_bars(self, symbol, start=None, end=None, timeframe="1D", limit=None):
            bars = [
                Bar(symbol=symbol, timestamp=self.t, observed_at=self.t,
                    open=100, high=110, low=95, close=105, volume=1000,
                    source="fixture", quality="good", is_stale=False),
            ]
            return bars[-limit:] if limit else bars

        def get_latest_bar(self, symbol):
            return self.get_bars(symbol)[-1]

        def get_latest_quote(self, symbol):
            return Quote(
                symbol=symbol, timestamp=self.t + timedelta(seconds=10),
                observed_at=self.t + timedelta(seconds=10),
                bid=104.0, ask=106.0, last_price=105.0,
                source="fixture_authoritative", is_stale=False, is_synthetic=False,
                quality="good",
            )

        def stream_bars(self, symbols):
            for s in symbols:
                yield self.get_latest_bar(s)

    os.environ["CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS"] = "1"
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        pm = PortfolioManager(
            initial_cash_swing=TEAM_INITIAL_CAPITAL_TWD,
            initial_cash_intraday=TEAM_INITIAL_CAPITAL_TWD,
        )
        pm.register_strategy(DYNAMIC_DESK_ID, TEAM_INITIAL_CAPITAL_TWD, unified_cash=True)
        es = EventStore(":memory:")
        po = PaperOrderService(pm, es)
        adapter = FixtureAdapter(clock[0])

        runner = AutonomousPaperRunner(
            tmp, pm, po, adapter,
            now_fn=lambda: clock[0],
            team_initial_capital=TEAM_INITIAL_CAPITAL_TWD,
        )
        runner.configure(PaperExperimentSettings(
            strategy_id=DYNAMIC_DESK_ID,
            strategy_name="Test Desk",
            enabled=True,
            universe=["2330.TW"],
            max_position_notional=500000.0,
            initial_cash=TEAM_INITIAL_CAPITAL_TWD,
        ))

        # No CIO executor -> must hit the BLOCKED path that previously raised NameError
        runner.cio_executor = None
        runner.clear_staged_packets()

        # This must NOT raise NameError: name 'now' is not defined
        result = runner.run_one_cycle(DYNAMIC_DESK_ID)
        assert result["run"]["status"] == "BLOCKED"
        assert result["run"]["reason"] == "BLOCKED_NO_CIO_DECISION_PROVIDER"

    # Verify ContractSpec has all required contract identity fields
    spec = ContractSpec(
        symbol="TX-202610",
        underlying_symbol="TX",
        instrument_type=DerivativeInstrumentType.FUTURE,
        multiplier=200.0,
        tick_size=1.0,
        initial_margin_per_contract=184000.0,
        maintenance_margin_per_contract=141000.0,
        currency="TWD",
    )
    assert spec.symbol == "TX-202610"
    assert spec.multiplier == 200.0
    assert spec.tick_size == 1.0
    assert spec.currency == "TWD"
    assert spec.initial_margin_per_contract == 184000.0
    assert spec.maintenance_margin_per_contract == 141000.0
    assert "SIMULATION_ASSUMPTION" in spec.margin_rules_label


# ============================================================================
# 13. Canonical Portfolio & Event Store Integration (No Second Ledger)
# ============================================================================

def test_canonical_portfolio_integration_no_second_ledger():
    """Verify PaperDerivativesEngine normalized events integrate into PortfolioManager directly."""
    from cio_market_lab.domain.models import DecisionScope
    from cio_market_lab.engine.portfolio import PortfolioManager
    from cio_market_lab.events.store import EventStore
    from cio_market_lab.engine.paper_derivatives import (
        PaperDerivativesEngine,
        ContractSpec,
        DerivativeQuote,
        DerivativeInstrumentType,
        apply_derivative_event_to_portfolio,
    )

    initial_cash = 2_000_000.0
    pm = PortfolioManager(initial_cash_swing=initial_cash, initial_cash_intraday=initial_cash)
    strategy_id = "test-desk"
    pm.register_strategy(strategy_id, initial_cash, unified_cash=True)
    es = EventStore(":memory:")

    engine = PaperDerivativesEngine(production_mode=False)
    now = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)

    spec = ContractSpec(
        symbol="TXO-202610-22000-C",
        underlying_symbol="TX",
        instrument_type=DerivativeInstrumentType.OPTION,
        option_right=OptionRight.CALL,
        strike=22000.0,
        expiry=now + timedelta(days=20),
        multiplier=50.0,
        tick_size=1.0,
    )
    quote = DerivativeQuote(
        symbol="TXO-202610-22000-C",
        timestamp=now,
        bid=120.0,
        ask=122.0,
        last_price=121.0,
        is_stale=False,
        is_fixture=False,
    )

    # 1. Buy 2 contracts
    res_buy = engine.attempt_execution(
        order_id="ord-deriv-01",
        spec=spec,
        quote=quote,
        side=OrderSide.BUY,
        quantity=2.0,
        available_cash=initial_cash,
        as_of=now,
    )
    assert res_buy.success
    assert res_buy.event is not None

    # Apply through canonical path
    applied = apply_derivative_event_to_portfolio(
        res_buy.event, pm, strategy_id, DecisionScope.SWING, es
    )
    assert applied is True

    # Cash debited from the single canonical cash account
    ledger = pm.get_strategy_ledger(strategy_id, DecisionScope.SWING)
    assert ledger.cash < initial_cash
    expected_debit = (2.0 * 122.0 * 50.0) + res_buy.fee + res_buy.tax
    assert round(ledger.cash, 2) == round(initial_cash - expected_debit, 2)

    # Position in canonical ledger
    assert "TXO-202610-22000-C" in ledger.positions
    pos = ledger.positions["TXO-202610-22000-C"]
    assert pos.quantity == 2.0
    assert pos.multiplier == 50.0

    # Idempotent replay: applying same event again is a no-op
    applied_dup = apply_derivative_event_to_portfolio(
        res_buy.event, pm, strategy_id, DecisionScope.SWING, es
    )
    assert applied_dup is False
    assert round(ledger.cash, 2) == round(initial_cash - expected_debit, 2)

    # 2. Sell to close 2 contracts at higher bid (130.0)
    quote_exit = DerivativeQuote(
        symbol="TXO-202610-22000-C",
        timestamp=now + timedelta(hours=2),
        bid=130.0,
        ask=132.0,
        last_price=131.0,
        is_stale=False,
        is_fixture=False,
    )
    res_sell = engine.attempt_execution(
        order_id="ord-deriv-02",
        spec=spec,
        quote=quote_exit,
        side=OrderSide.SELL,
        quantity=2.0,
        available_cash=ledger.cash,
        existing_position=res_buy.position,
        as_of=now + timedelta(hours=2),
    )
    assert res_sell.success
    applied_sell = apply_derivative_event_to_portfolio(
        res_sell.event, pm, strategy_id, DecisionScope.SWING, es
    )
    assert applied_sell is True

    # Realized PnL credited and position closed
    assert ledger.realized_pnl > 0.0
    assert ledger.positions["TXO-202610-22000-C"].quantity == 0.0

    # Events present in canonical EventStore
    events = es.get_events(event_type=EventType.POSITION_UPDATED)
    assert len(events) >= 2


# ============================================================================
# 14. Synthetic Stress Scenario Isolation
# ============================================================================

def test_synthetic_stress_scenario_separate_from_market_pnl():
    """Synthetic stress tests must project hypothetical outcomes without mutating market PnL."""
    from cio_market_lab.engine.paper_derivatives import (
        PaperDerivativesEngine,
        ContractSpec,
        DerivativePosition,
        DerivativeInstrumentType,
    )
    engine = PaperDerivativesEngine()
    spec = ContractSpec(
        symbol="TX-202610",
        underlying_symbol="TX",
        instrument_type=DerivativeInstrumentType.FUTURE,
        multiplier=200.0,
        tick_size=1.0,
    )
    pos = DerivativePosition(
        position_id="pos-tx-01",
        symbol="TX-202610",
        underlying_symbol="TX",
        instrument_type=DerivativeInstrumentType.FUTURE,
        quantity=1.0,
        multiplier=200.0,
        average_entry_price=22000.0,
        last_settlement_price=22000.0,
        current_price=22000.0,
        market_value=4400000.0,
        unrealized_pnl=0.0,
    )

    stress_up = engine.simulate_synthetic_stress_scenario(
        position=pos,
        spec=spec,
        scenario_name="BULL_SHOCK_5PCT",
        price_shock_pct=0.05,
        base_underlying_price=22000.0,
    )
    assert stress_up.is_synthetic is True
    assert stress_up.separate_from_market_pnl is True
    assert stress_up.simulated_pnl > 0.0
    assert stress_up.simulated_pnl == (22000.0 * 1.05 - 22000.0) * 200.0

    # Market PnL and position remain completely unchanged
    assert pos.market_value == 4400000.0
    assert pos.unrealized_pnl == 0.0


# ============================================================================
# 15. Autonomous Runner Derivative Execution Acceptance
# ============================================================================

def test_runner_derivative_execution_with_and_without_caller_params(tmp_path):
    """Runner stays UNAVAILABLE without genuine caller spec/quote; executes through canonical path when supplied."""
    import os
    from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner, DYNAMIC_DESK_ID
    from cio_market_lab.engine.paper_orders import PaperExperimentSettings, PaperOrderService
    from cio_market_lab.engine.portfolio import PortfolioManager
    from cio_market_lab.engine.team_ops import TEAM_INITIAL_CAPITAL_TWD
    from cio_market_lab.events.store import EventStore
    from cio_market_lab.engine.cio_packet import sign_cio_packet
    from cio_market_lab.domain.models import CIODecisionPacket, CIOProvenance, DecisionScope, Bar, Quote

    class DummyAdapter:
        source_name = "dummy"
        def __init__(self, t): self.t = t
        def get_bars(self, symbol, start=None, end=None, timeframe="1D", limit=None):
            return [Bar(symbol=symbol, timestamp=self.t, observed_at=self.t, open=100, high=105, low=95, close=100, volume=100, source="fixture", quality="good", is_stale=False)]
        def get_latest_bar(self, symbol): return self.get_bars(symbol)[-1]
        def get_latest_quote(self, symbol): return Quote(symbol=symbol, timestamp=self.t, observed_at=self.t, bid=99, ask=101, last_price=100, source="fixture", quality="good", is_stale=False)

    os.environ["CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS"] = "1"
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    pm = PortfolioManager(initial_cash_swing=TEAM_INITIAL_CAPITAL_TWD, initial_cash_intraday=TEAM_INITIAL_CAPITAL_TWD)
    pm.register_strategy(DYNAMIC_DESK_ID, TEAM_INITIAL_CAPITAL_TWD, unified_cash=True)
    es = EventStore(":memory:")
    po = PaperOrderService(pm, es)
    adapter = DummyAdapter(clock[0])
    runner = AutonomousPaperRunner(tmp_path, pm, po, adapter, now_fn=lambda: clock[0],
                                   isolated_derivative_fixture_mode=True)
    runner.configure(PaperExperimentSettings(
        strategy_id=DYNAMIC_DESK_ID, strategy_name="Test Desk", enabled=True,
        universe=["TX-202610"], max_position_notional=500000.0, initial_cash=TEAM_INITIAL_CAPITAL_TWD,
        base_currency="TWD",
    ))

    # NEGATIVE: Packet without genuine caller contract spec & quote -> fails closed with CAPABILITY_UNAVAILABLE
    packet_bare = CIODecisionPacket(
        case_id="bare-deriv-01",
        as_of=clock[0],
        expiry=clock[0] + timedelta(hours=4),
        thesis="TX future breakout attempt",
        selected_instrument="TX-202610",
        action="BUY",
        quantity=1.0,
        conditions={"instrument_type": "FUTURE"},
        provenance=CIOProvenance(authority="MAIN_CIO"),
        is_fixture=True,
    )
    sign_cio_packet(packet_bare)
    dec_bare = runner.submit_cio_packet(packet_bare)
    assert dec_bare.action == "NO_TRADE"
    assert "CAPABILITY_UNAVAILABLE" in dec_bare.reason

    # POSITIVE: Packet WITH genuine caller contract spec & quote -> executes through canonical path
    packet_valid = CIODecisionPacket(
        case_id="valid-deriv-02",
        as_of=clock[0],
        expiry=clock[0] + timedelta(hours=4),
        thesis="TX future breakout with genuine spec & quote",
        selected_instrument="TX-202610",
        action="BUY",
        quantity=1.0,
        provenance=CIOProvenance(authority="MAIN_CIO"),
        is_fixture=True,
        conditions={
            "contract_spec": {
                "symbol": "TX-202610",
                "underlying_symbol": "TX",
                "instrument_type": "FUTURE",
                "multiplier": 10.0,
                "tick_size": 1.0,
                "initial_margin_per_contract": 184000.0,
                "maintenance_margin_per_contract": 141000.0,
                "expiry": (clock[0] + timedelta(days=20)).isoformat(),
            },
            "derivative_quote": {
                "symbol": "TX-202610",
                "timestamp": clock[0].isoformat(),
                "bid": 22000.0,
                "ask": 22002.0,
                "last_price": 22001.0,
                "is_stale": False,
                "is_fixture": True,
                "provenance": {"authority": "MAIN_CIO", "source": "simulated_router"},
            },
        },
    )
    sign_cio_packet(packet_valid)
    dec_valid = runner.submit_cio_packet(packet_valid)
    assert dec_valid.action == "BUY_FILLED"
    assert dec_valid.terminal_status == "TERMINAL_FILLED"

    # Canonical cash balance debited and position recorded
    ledger = pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING)
    assert "TX-202610" in ledger.positions
    assert ledger.positions["TX-202610"].quantity == 1.0

