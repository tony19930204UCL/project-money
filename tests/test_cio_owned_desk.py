"""Comprehensive test suite for CIO-owned autonomous paper laboratory.

Verifies:
1. Positive E2E: Persisted CIO packet -> simulated event -> NAV & single canonical cash -> outcome -> next context retrieval.
2. Negative E2E: Missing decision provider produces explicit BLOCKED status (never autonomous AGY trade).
3. Negative E2E: Scripted or worker-generated verdict masquerading as CIO is rejected (provenance violation).
4. Negative E2E: Stale and expired CIO packets are rejected (freshness violation).
5. Negative E2E: Duplicate case_id is rejected (idempotency violation).
6. Negative E2E: Unsupported derivative orders report exact capability gaps rather than fake support.
7. Single canonical TWD cash pool invariant: holding horizon is metadata, not separate 55/45 compartments.
8. Hermes runtime contract & unavailable executor states.

Defect-Specific Acceptance Tests:
9. Defect 1: AutonomousPaperRunner require_cio_provider defaults True.
10. Defect 2: Startup executor registration on app AppState.
11. Defect 3: Self-asserted MAIN_CIO string without cryptographic signature or receipt is rejected.
12. Defect 4: Bridge prompt includes complete CIODecisionPacket JSON schema.
13. Defect 5: Provider and model are pinned in contract, executor, and command.
14. Defect 6: Horizon aliases (SWING vs INTRADAY) never double-apply fill cash debits; reconcile_canonical_cash deduplicates.
15. Restart Reconciliation: Single cash and NAV reconcile through restart without double-counting fills.
16. Learning Retrieval: Context retrieval strictly includes real outcomes, filtering out fabricated/un-executed lessons.
17. Autonomy Claim: Autonomy is not claimed without configured executor and readback receipt.
"""

from tests.fixture_next_quote import submit_after_new_fixture_quote
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import pytest
from fastapi.testclient import TestClient

from cio_market_lab.api.app import create_app
from cio_market_lab.data.base import MarketDataAdapter
from cio_market_lab.domain.events import EventEnvelope, EventType
from cio_market_lab.domain.models import (
    Bar,
    CIODecisionContextRequest,
    CIODecisionPacket,
    CIOExecutionReceipt,
    CIOProvenance,
    DecisionScope,
    Fill,
    Market,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
    Quote,
)
from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner, DYNAMIC_DESK_ID
from cio_market_lab.engine.cio_packet import (
    compute_packet_signature,
    sign_cio_packet,
    validate_cio_packet,
)
from cio_market_lab.engine.decision_learning import CIODecisionLearningStore, CIODecisionRecord
from cio_market_lab.engine.capabilities import get_derivative_capabilities_report
from cio_market_lab.engine.paper_orders import (
    PaperDataContext,
    PaperExperimentSettings,
    PaperOrderRequest,
    PaperOrderService,
)
from cio_market_lab.engine.portfolio import CanonicalCashAccount, Ledger, PortfolioManager
from cio_market_lab.engine.team_ops import TEAM_INITIAL_CAPITAL_TWD, DurableQuoteSnapshot
from cio_market_lab.events.store import EventStore
from cio_market_lab.integrations.hermes_chat import (
    DEFAULT_PINNED_MODEL_ID,
    DEFAULT_PINNED_PROVIDER_ID,
    HermesCIODecisionExecutor,
    get_hermes_runtime_contract,
)


class MockCIOMarketAdapter(MarketDataAdapter):
    """Adapter supplying deterministic bars and authoritative later quotes."""

    def __init__(self, current_time: datetime):
        self.current_time = current_time
        self.quote_time = current_time
        self.quote_price = 1000.0
        self.stale_quote = False
        self.synthetic_quote = False

    @property
    def source_name(self) -> str:
        return "mock_cio_adapter"

    def get_bars(self, symbol: str, start=None, end=None, timeframe="1D", limit=None) -> List[Bar]:
        now = self.current_time
        bars = [
            Bar(symbol=symbol, timestamp=now - timedelta(days=2), observed_at=now, open=980, high=990, low=975, close=985, volume=1000, source="fixture", quality="good", is_stale=False),
            Bar(symbol=symbol, timestamp=now - timedelta(days=1), observed_at=now, open=985, high=1000, low=980, close=995, volume=1200, source="fixture", quality="good", is_stale=False),
            Bar(symbol=symbol, timestamp=now - timedelta(minutes=1), observed_at=now, open=995, high=1005, low=990, close=1000, volume=1500, source="fixture", quality="good", is_stale=False),
        ]
        return bars[-limit:] if limit else bars

    def stream_bars(self, symbols: List[str]):
        for s in symbols:
            yield self.get_latest_bar(s)

    def get_latest_bar(self, symbol: str) -> Optional[Bar]:
        return self.get_bars(symbol)[-1]

    def get_latest_quote(self, symbol: str) -> Optional[Quote]:
        return Quote(bid_size=1000, ask_size=1000,
            quote_id=f"TEST_ONLY_{symbol}_{(self.quote_time).isoformat()}",
            session="ODD_LOT" if (symbol).endswith(".TW") else "REGULAR",
            source_capabilities={"source": "fixture_authoritative" if not self.synthetic_quote else "synthetic", "two_sided_book": True,
                "size_backed": True, "exchange_session_attested": True,
                "entitlement_evidence_id": "TEST_ONLY_FIXTURE_ODD_LOT",
                "entitlement_status": "TEST_ONLY", "odd_lot_book": True,
                "supported_sessions": ["REGULAR", "ODD_LOT"]},
            
            symbol=symbol,
            timestamp=self.quote_time,
            observed_at=self.quote_time,
            bid=self.quote_price - 1.0,
            ask=self.quote_price + 1.0,
            last_price=self.quote_price,
            source="fixture_authoritative" if not self.synthetic_quote else "synthetic",
            is_stale=self.stale_quote,
            is_synthetic=self.synthetic_quote,
            quality="good" if not (self.stale_quote or self.synthetic_quote) else "stale",
        )


def build_test_runner(tmp_path: Path, clock: List[datetime], native_currency: str = "TWD") -> Tuple[AutonomousPaperRunner, PaperOrderService, PortfolioManager, MockCIOMarketAdapter]:
    import os
    os.environ["CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS"] = "1"
    pm = PortfolioManager(
        initial_cash_swing=TEAM_INITIAL_CAPITAL_TWD,
        initial_cash_intraday=TEAM_INITIAL_CAPITAL_TWD,
    )
    native_currency = native_currency.upper()
    test_native_funding = 100_000.0 if native_currency == "USD" else TEAM_INITIAL_CAPITAL_TWD
    pm.register_strategy(DYNAMIC_DESK_ID, test_native_funding, unified_cash=True, currency=native_currency)
    es = EventStore(":memory:")
    po = PaperOrderService(pm, es)
    adapter = MockCIOMarketAdapter(clock[0])
    runner = AutonomousPaperRunner(
        tmp_path,
        pm,
        po,
        adapter,
        now_fn=lambda: clock[0],
        team_initial_capital=test_native_funding,
    )
    runner.allow_test_only_fx = True  # TEST_ONLY fixture authorization, never production
    runner.allow_fixture_quotes = True  # Explicitly isolated test runtime; production stays fail-closed.
    runner.configure(PaperExperimentSettings(
        strategy_id=DYNAMIC_DESK_ID,
        strategy_name="Autonomous Paper Execution Desk",
        enabled=True,
        universe=["NVDA", "AAPL", "ES", "TX", "SPY"] if native_currency == "USD" else ["2330.TW", "2317.TW", "1101.TW", "2454.TW", "2303.TW", "2881.TW", "1301.TW", "2603.TW", "1216.TW"],
        base_currency=native_currency,
        max_position_notional=500000.0,
        initial_cash=test_native_funding,
    ))
    return runner, po, pm, adapter


# ============================================================================
# 1. Positive E2E: CIO Packet -> Order -> Fill -> Outcome -> Next Context
# ============================================================================

def test_positive_e2e_cio_packet_lifecycle_and_learning_retrieval(tmp_path):
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]  # Monday market hours
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock)

    cio_packet = CIODecisionPacket(
        case_id="cio-case-20260928-001",
        as_of=clock[0],
        evidence=["research://tw-semis-demand", "quote://2330.TW-1000"],
        thesis="TSMC leading-edge 2nm capacity expansion confirmed; accumulation phase.",
        selected_instrument="2330.TW",
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=50.0,
        conditions={**({"max_slippage_bps": 20}), "allow_odd_lot": True},
        risk_assessment={"downside_buffer": 0.05, "thesis_invalidation": "close < 960"},
        alternatives_considered=[{"symbol": "2317.TW", "reason": "Lower beta than TSMC in semi breakout"}],
        expiry=clock[0] + timedelta(hours=8),
        confidence=0.88,
        strategy_version="dynamic-desk-cio-20260928",
        provenance=CIOProvenance(
            authority="MAIN_CIO",
            actor_role="CHIEF_INVESTMENT_OFFICER",
            signer_id="main-cio-key",
            source="external_packet",
        ),
        is_fixture=True,
    )
    sign_cio_packet(cio_packet, signer_id="main-cio-key")

    decision = submit_after_new_fixture_quote(runner, clock, cio_packet)
    assert decision.action == "BUY_FILLED"
    assert decision.terminal_status == "TERMINAL_FILLED"
    assert decision.quantity == 50.0
    assert "TSMC leading-edge" in decision.reason

    # Verify single canonical cash pool debited
    swing_ledger = pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING)
    intraday_ledger = pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.INTRADAY)
    assert swing_ledger.cash == intraday_ledger.cash
    assert swing_ledger.cash < TEAM_INITIAL_CAPITAL_TWD
    assert round(swing_ledger.cash + 50.0 * 1000.0, -2) >= round(TEAM_INITIAL_CAPITAL_TWD - 2000, -2)

    # Verify position is recorded with holding_horizon metadata
    assert "2330.TW" in swing_ledger.positions
    pos = swing_ledger.positions["2330.TW"]
    assert pos.quantity == 50.0

    # Verify pre-decision beliefs and fills were persisted into learning store
    rec = runner.learning_store._records.get(cio_packet.case_id)
    assert rec is not None
    assert rec.status == "FILLED"
    assert rec.order_id is not None
    assert rec.costs is not None
    assert rec.costs["total_cost"] > 0

    # 2. Record an outcome and lesson for this CIO case at T=0
    t0_outcome = clock[0]
    runner.learning_store.record_outcome(
        case_id=cio_packet.case_id,
        outcome_dict={"exit_price": 1050.0, "realized_pnl": 2450.0, "return_pct": 5.0, "as_of": t0_outcome.isoformat()},
        attribution_dict={"alpha": 3.2, "market_beta": 1.8},
        lessons=["High conviction semi entry on capacity expansion yielded 5.0% return; keep disciplined stop."],
        as_of=t0_outcome,
    )

    # 3. Decision context requested at exact outcome timestamp (T=0): must NOT admit lesson
    context_at_t0 = runner.build_decision_context_request(symbols=["2330.TW"])
    admitted_at_t0 = [l for l in context_at_t0.prior_lessons if l.get("case_id") == cio_packet.case_id]
    assert len(admitted_at_t0) == 0, "Future leakage: lesson was admitted by a decision context timestamped concurrently with the outcome"

    # Also record a future outcome for another hypothetical case at T+6h
    t_future = t0_outcome + timedelta(hours=6)
    runner.learning_store.record_outcome(
        case_id="cio-future-case-002",
        outcome_dict={"exit_price": 1100.0, "realized_pnl": 5000.0, "return_pct": 10.0, "as_of": t_future.isoformat()},
        attribution_dict={"alpha": 4.0, "market_beta": 1.0},
        lessons=["Future lesson that should never be admitted before T+6h."],
        as_of=t_future,
    )

    # 4. Subsequent Decision Context Retrieval at T=4h (after T=0 outcome, before T=6h outcome)
    clock[0] = clock[0] + timedelta(hours=4)
    next_context = runner.build_decision_context_request(symbols=["2330.TW"])

    assert next_context.universe == ["2330.TW"]
    # T=0 outcome lesson IS admitted
    admitted_t0 = [l for l in next_context.prior_lessons if l.get("case_id") == cio_packet.case_id]
    assert len(admitted_t0) >= 1
    assert "High conviction semi entry" in admitted_t0[0]["takeaway"]

    # T=6h future outcome lesson and outcome stay strictly EXCLUDED at T=4h
    admitted_future = [l for l in next_context.prior_lessons if l.get("case_id") == "cio-future-case-002"]
    assert len(admitted_future) == 0, "Future leakage: lesson from T+6h admitted at T+4h"
    outcomes_future = [o for o in next_context.past_outcomes if o.get("case_id") == "cio-future-case-002"]
    assert len(outcomes_future) == 0, "Future leakage: past_outcomes admitted an outcome that has not yet occurred"

    assert len(next_context.past_outcomes) >= 1
    assert next_context.past_outcomes[0]["outcome"]["realized_pnl"] == 2450.0
    assert len(next_context.rejected_opportunities) >= 1
    assert next_context.rejected_opportunities[0]["opportunity"]["symbol"] == "2317.TW"


# ============================================================================
# 2. Negative E2E: Missing Decision Provider -> Explicit BLOCKED Status
# ============================================================================

def test_negative_missing_decision_provider_produces_explicit_blocked_status(tmp_path):
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock)

    assert runner.cio_executor is None
    runner.clear_staged_packets()

    result = runner.run_one_cycle(DYNAMIC_DESK_ID)

    assert result["run"]["status"] == "BLOCKED"
    assert result["run"]["reason"] == "BLOCKED_NO_CIO_DECISION_PROVIDER"
    assert result["run"]["orders_count"] == 0
    assert result["run"]["fills_count"] == 0
    assert result["autonomous_capital_decisions"] is False
    assert result["autonomous_paper_execution"] is False
    assert result["decision_owner"] == "MAIN_CIO"
    assert result["worker_role"] == "ENGINEERING_WORKER_ONLY"

    for d in result["decisions"]:
        assert d["action"] == "NO_TRADE"
        assert "BLOCKED_NO_CIO_DECISION_PROVIDER" in d["reason"]
        assert d["terminal_status"] == "TERMINAL_RISK_BLOCK"

    assert orders.all_orders() == []


# ============================================================================
# 3. Negative E2E: Scripted / Worker Verdict Masquerading as CIO is Rejected
# ============================================================================

def test_negative_scripted_worker_verdict_fails_provenance_validation(tmp_path):
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock)

    fake_packet = CIODecisionPacket(
        case_id="worker-fake-001",
        as_of=clock[0],
        thesis="Worker calculated moving average crossover signal.",
        selected_instrument="2330.TW",
        action="BUY",
        quantity=10.0,
        expiry=clock[0] + timedelta(hours=1),
        confidence=0.5,
        provenance=CIOProvenance(
            authority="ENGINEERING_WORKER",
            actor_role="ENGINEERING_WORKER",
            source="worker_script",
        ),
    )

    decision = runner.submit_cio_packet(fake_packet)
    assert decision.action == "NO_TRADE"
    assert "PROVENANCE_VIOLATION" in decision.reason
    assert decision.terminal_status == "TERMINAL_RISK_BLOCK"
    assert orders.all_orders() == []


# ============================================================================
# 4. Negative E2E: Stale and Expired CIO Packets Are Rejected
# ============================================================================

def test_negative_stale_and_expired_cio_packets_rejected(tmp_path):
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock)

    expired_packet = CIODecisionPacket(
        case_id="cio-expired-001",
        as_of=clock[0] - timedelta(hours=5),
        expiry=clock[0] - timedelta(hours=1),
        thesis="Breakout thesis",
        selected_instrument="2330.TW",
        action="BUY",
        quantity=10.0,
        provenance=CIOProvenance(authority="MAIN_CIO"),
    )
    sign_cio_packet(expired_packet)
    decision_exp = runner.submit_cio_packet(expired_packet)
    assert decision_exp.action == "NO_TRADE"
    assert "PACKET_EXPIRED" in decision_exp.reason

    stale_packet = CIODecisionPacket(
        case_id="cio-stale-001",
        as_of=clock[0] - timedelta(days=5),
        expiry=clock[0] + timedelta(hours=10),
        thesis="Stale thesis",
        selected_instrument="2330.TW",
        action="BUY",
        quantity=10.0,
        provenance=CIOProvenance(authority="MAIN_CIO"),
    )
    sign_cio_packet(stale_packet)
    decision_stale = runner.submit_cio_packet(stale_packet)
    assert decision_stale.action == "NO_TRADE"
    assert "PACKET_STALE" in decision_stale.reason


# ============================================================================
# 5. Negative E2E: Duplicate case_id (Idempotency) Is Rejected
# ============================================================================

def test_negative_duplicate_case_id_rejected_for_idempotency(tmp_path):
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock)

    packet = CIODecisionPacket(
        case_id="cio-idempotency-test",
        as_of=clock[0],
        expiry=clock[0] + timedelta(hours=2),
        thesis="Valid thesis",
        selected_instrument="2330.TW",
        action="BUY",
        quantity=10.0,
        provenance=CIOProvenance(authority="MAIN_CIO"),
        conditions={"allow_odd_lot": True}, is_fixture=True,
    )
    sign_cio_packet(packet)

    d1 = submit_after_new_fixture_quote(runner, clock, packet)
    assert d1.action == "BUY_FILLED"

    d2 = submit_after_new_fixture_quote(runner, clock, packet)
    assert d2.action == "NO_TRADE"
    assert "DUPLICATE_CASE_ID" in d2.reason
    assert d2.terminal_status == "TERMINAL_RISK_BLOCK"


# ============================================================================
# 6. Negative E2E: Derivative Capabilities Gaps Reported Rather than Faked
# ============================================================================

def test_negative_derivative_capabilities_advertised_unavailable_with_exact_gaps(tmp_path):
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock)

    futures_packet = CIODecisionPacket(
        case_id="cio-futures-test",
        as_of=clock[0],
        expiry=clock[0] + timedelta(hours=2),
        thesis="Hedge beta with index futures",
        selected_instrument="TX00",
        action="BUY",
        quantity=1.0,
        conditions={"instrument_type": "FUTURE"},
        provenance=CIOProvenance(authority="MAIN_CIO"),
    )
    sign_cio_packet(futures_packet)
    d_fut = runner.submit_cio_packet(futures_packet)
    assert d_fut.action == "NO_TRADE"
    assert "CAPABILITY_UNAVAILABLE:FUTURES" in d_fut.reason
    assert "NO_FUTURES_FEED" in d_fut.reason

    report = get_derivative_capabilities_report()
    assert report.policy == "PERMITTED_FOR_PAPER_RESEARCH"
    assert report.capabilities["CASH_EQUITY"].status == "AVAILABLE"
    assert report.capabilities["SPOT_ETF"].status == "AVAILABLE"
    assert report.capabilities["LONG_PREMIUM_OPTIONS"].status == "UNAVAILABLE"
    assert report.capabilities["FUTURES"].status == "UNAVAILABLE"
    assert report.capabilities["MARGIN_LEVERAGE"].status == "UNAVAILABLE"
    assert len(report.capabilities["LONG_PREMIUM_OPTIONS"].exact_gaps) >= 3


# ============================================================================
# 7. One Canonical TWD Cash Pool Invariant
# ============================================================================

def test_one_canonical_twd_cash_pool_invariant(tmp_path):
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock)

    initial_cash = TEAM_INITIAL_CAPITAL_TWD

    p1 = CIODecisionPacket(
        case_id="cio-pool-swing-1",
        as_of=clock[0],
        expiry=clock[0] + timedelta(hours=2),
        thesis="Swing purchase",
        selected_instrument="2330.TW",
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=20.0,
        provenance=CIOProvenance(authority="MAIN_CIO"),
        conditions={"allow_odd_lot": True}, is_fixture=True,
    )
    sign_cio_packet(p1)
    d1 = submit_after_new_fixture_quote(runner, clock, p1)
    assert d1.action == "BUY_FILLED"

    swing_l = pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING)
    intra_l = pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.INTRADAY)
    assert swing_l.cash == intra_l.cash
    cash_after_swing = swing_l.cash
    assert cash_after_swing < initial_cash

    p2 = CIODecisionPacket(
        case_id="cio-pool-intraday-1",
        as_of=clock[0],
        expiry=clock[0] + timedelta(hours=2),
        thesis="Intraday scalping opportunity",
        selected_instrument="2330.TW",
        action="BUY",
        holding_horizon=DecisionScope.INTRADAY,
        quantity=10.0,
        provenance=CIOProvenance(authority="MAIN_CIO"),
        conditions={"allow_odd_lot": True}, is_fixture=True,
    )
    sign_cio_packet(p2)
    d2 = submit_after_new_fixture_quote(runner, clock, p2)
    assert d2.action == "BUY_FILLED"

    assert swing_l.cash == intra_l.cash
    assert swing_l.cash < cash_after_swing

    reconciled_cash = pm.reconcile_canonical_cash(DYNAMIC_DESK_ID)
    assert abs(reconciled_cash - swing_l.cash) < 0.01


# ============================================================================
# 8. Hermes Runtime Contract & Executor Unavailable State
# ============================================================================

def test_hermes_runtime_contract_and_unavailable_executor():
    contract = get_hermes_runtime_contract()
    assert contract.authority == "MAIN_CIO"
    assert contract.worker_role == "ENGINEERING_WORKER_ONLY"
    assert "CIODecisionPacket" in contract.output_contract["required_schema"]
    assert "provenance" in contract.output_contract["required_fields"]
    assert "NO_CONFIGURED_EXECUTOR" in contract.unavailable_states
    assert "HERMES_CLI_NOT_FOUND" in contract.unavailable_states

    executor = HermesCIODecisionExecutor(workspace_root="/tmp/nonexistent")
    if not executor.is_available():
        with pytest.raises(RuntimeError) as exc_info:
            executor.request_decision(CIODecisionContextRequest(request_id="test", timestamp=datetime.now(timezone.utc)))
        assert "EXECUTOR_UNAVAILABLE" in str(exc_info.value)


# ============================================================================
# 9. Defect 1: AutonomousPaperRunner require_cio_provider defaults True
# ============================================================================

def test_defect_require_cio_provider_defaults_true(tmp_path):
    pm = PortfolioManager(initial_cash_swing=10000, initial_cash_intraday=10000)
    service = PaperOrderService(pm, EventStore(":memory:"))
    adapter = MockCIOMarketAdapter(datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc))

    # Instantiation without specifying require_cio_provider must default to True
    runner = AutonomousPaperRunner(tmp_path, pm, service, adapter)
    assert runner.require_cio_provider is True


# ============================================================================
# 10. Defect 2: Startup Executor Registration on App AppState
# ============================================================================

def test_defect_startup_executor_registered():
    app = create_app()
    st = app.state.app_state

    # Verify that a startup CIO executor is registered on the app runner
    assert st.cio_executor is not None
    assert st.runner.cio_executor is not None
    assert isinstance(st.runner.cio_executor, HermesCIODecisionExecutor)

    client = TestClient(app)
    resp = client.get("/api/paper/cio/runtime-contract")
    assert resp.status_code == 200
    data = resp.json()
    assert data["executor_registered"] is True
    assert "executor_available" in data


# ============================================================================
# 11. Defect 3: Self-asserted MAIN_CIO string without auth signature is rejected
# ============================================================================

def test_defect_self_asserted_main_cio_string_rejected_without_auth(tmp_path):
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock)

    # 1. Bare self-asserted string without cryptographic signature or receipt
    bare_packet = CIODecisionPacket(
        case_id="unauth-cio-001",
        as_of=clock[0],
        expiry=clock[0] + timedelta(hours=2),
        thesis="Self asserted without key",
        selected_instrument="2330.TW",
        action="BUY",
        quantity=10.0,
        provenance=CIOProvenance(
            authority="MAIN_CIO",
            actor_role="CHIEF_INVESTMENT_OFFICER",
            signer_id="main-cio",
            signature=None,
            receipt_id=None,
        ),
    )
    decision1 = submit_after_new_fixture_quote(runner, clock, bare_packet)
    assert decision1.action == "NO_TRADE"
    assert "PROVENANCE_AUTHENTICATION_FAILED" in decision1.reason

    # 2. Forged/invalid signature
    forged_packet = CIODecisionPacket(
        case_id="forged-cio-002",
        as_of=clock[0],
        expiry=clock[0] + timedelta(hours=2),
        thesis="Forged signature",
        selected_instrument="2330.TW",
        action="BUY",
        quantity=10.0,
        provenance=CIOProvenance(
            authority="MAIN_CIO",
            actor_role="CHIEF_INVESTMENT_OFFICER",
            signer_id="main-cio",
            signature="badf00d" * 8,
        ),
    )
    decision2 = submit_after_new_fixture_quote(runner, clock, forged_packet)
    assert decision2.action == "NO_TRADE"
    assert "PROVENANCE_AUTHENTICATION_FAILED" in decision2.reason

    # 3. Authentically signed packet succeeds
    valid_packet = CIODecisionPacket(
        case_id="valid-cio-003",
        as_of=clock[0],
        expiry=clock[0] + timedelta(hours=2),
        thesis="Authentic cryptographic signature",
        selected_instrument="2330.TW",
        action="BUY",
        quantity=10.0,
        provenance=CIOProvenance(authority="MAIN_CIO"),
        conditions={"allow_odd_lot": True}, is_fixture=True,
    )
    sign_cio_packet(valid_packet, signer_id="main-cio")
    decision3 = submit_after_new_fixture_quote(runner, clock, valid_packet)
    assert decision3.action == "BUY_FILLED"


# ============================================================================
# 12. Defect 4: Bridge prompt includes complete CIODecisionPacket JSON schema
# ============================================================================

def test_defect_bridge_prompt_contains_full_output_schema():
    contract = get_hermes_runtime_contract()
    full_schema = contract.output_contract.get("full_schema", {})
    assert full_schema is not None
    assert "properties" in full_schema
    assert "case_id" in full_schema["properties"]
    assert "holding_horizon" in full_schema["properties"]
    assert "thesis" in full_schema["properties"]
    assert "provenance" in full_schema["properties"]


# ============================================================================
# 13. Defect 5: Provider and Model are Pinned
# ============================================================================

def test_defect_pinned_provider_and_model():
    contract = get_hermes_runtime_contract()
    assert contract.pinned_provider == DEFAULT_PINNED_PROVIDER_ID
    assert contract.pinned_model == DEFAULT_PINNED_MODEL_ID
    assert contract.provider_id == DEFAULT_PINNED_PROVIDER_ID
    assert contract.model_id == DEFAULT_PINNED_MODEL_ID

    executor = HermesCIODecisionExecutor()
    assert executor.provider_id == DEFAULT_PINNED_PROVIDER_ID
    assert executor.model_id == DEFAULT_PINNED_MODEL_ID


# ============================================================================
# 14. Defect 6: Horizon aliases never double-apply fill cash debits
# ============================================================================

def test_defect_no_double_apply_fills_across_horizon_aliases():
    # Dedicated test verifying that holding horizons SWING and INTRADAY
    # share the same CanonicalCashAccount and never double-apply the same fill.
    cash_account = CanonicalCashAccount(initial_cash=1_000_000.0)
    swing_ledger = Ledger(DecisionScope.SWING, 1_000_000.0, cash_account=cash_account)
    intraday_ledger = Ledger(DecisionScope.INTRADAY, 1_000_000.0, cash_account=cash_account)

    fill = Fill(
        fill_id="fill-test-horizon-1",
        order_id="order-1",
        symbol="2330.TW",
        bucket=DecisionScope.SWING,
        side=OrderSide.BUY,
        quantity=10.0,
        fill_price=1000.0,
        fee=14.25,
        tax=0.0,
        slippage=5.0,
        timestamp=datetime.now(timezone.utc),
    )
    total_cost = 10.0 * 1000.0 + 14.25 + 5.0

    # Apply to SWING
    applied_swing = swing_ledger.apply_fill(fill)
    assert applied_swing is True
    assert round(cash_account.cash, 2) == round(1_000_000.0 - total_cost, 2)

    # Replay or mirror same fill across horizon alias INTRADAY
    applied_intraday = intraday_ledger.apply_fill(fill)
    # Cash account must NOT double debit
    assert round(cash_account.cash, 2) == round(1_000_000.0 - total_cost, 2)
    assert swing_ledger.cash == intraday_ledger.cash

    # Test duplicate on swing ledger is rejected idempotently
    re_applied = swing_ledger.apply_fill(fill)
    assert re_applied is False
    assert round(cash_account.cash, 2) == round(1_000_000.0 - total_cost, 2)


# ============================================================================
# 15. Restart Reconciliation: Single Cash and NAV Reconcile Through Restart
# ============================================================================

def test_defect_reconcile_single_cash_and_nav_through_restart(tmp_path):
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    runner1, orders1, pm1, adapter1 = build_test_runner(tmp_path, clock)

    # Submit an authenticated trade
    packet = CIODecisionPacket(
        case_id="restart-trade-001",
        as_of=clock[0],
        expiry=clock[0] + timedelta(hours=2),
        thesis="Pre-restart position entry",
        selected_instrument="2330.TW",
        action="BUY",
        quantity=25.0,
        provenance=CIOProvenance(authority="MAIN_CIO"),
        conditions={"allow_odd_lot": True}, is_fixture=True,
    )
    sign_cio_packet(packet)
    d = submit_after_new_fixture_quote(runner1, clock, packet)
    assert d.action == "BUY_FILLED"

    # Pre-restart state
    pre_cash = pm1.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING).cash
    pre_nav = pm1.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING).equity
    assert pre_cash < TEAM_INITIAL_CAPITAL_TWD
    assert pre_nav is not None

    # Persist
    runner1._persist_portfolios()

    # Restart runner on same directory with a new PortfolioManager instance
    pm2 = PortfolioManager(
        initial_cash_swing=TEAM_INITIAL_CAPITAL_TWD,
        initial_cash_intraday=TEAM_INITIAL_CAPITAL_TWD,
    )
    pm2.register_strategy(DYNAMIC_DESK_ID, TEAM_INITIAL_CAPITAL_TWD, unified_cash=True)
    orders2 = PaperOrderService(pm2, EventStore(":memory:"))
    runner2 = AutonomousPaperRunner(
        tmp_path,
        pm2,
        orders2,
        adapter1,
        now_fn=lambda: clock[0],
        team_initial_capital=TEAM_INITIAL_CAPITAL_TWD,
    )

    # Restored state
    post_cash = pm2.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING).cash
    post_nav = pm2.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING).equity

    # Cash and NAV must match exactly across restart
    assert round(post_cash, 2) == round(pre_cash, 2)
    assert round(post_nav, 2) == round(pre_nav, 2)

    # Reconciled canonical cash matches
    reconciled = pm2.reconcile_canonical_cash(DYNAMIC_DESK_ID)
    assert round(reconciled, 2) == round(post_cash, 2)


# ============================================================================
# 16. Learning Retrieval: Real Outcomes Only, Filtering Fabricated Lessons
# ============================================================================

def test_defect_learning_retrieval_real_outcomes_only(tmp_path):
    store = CIODecisionLearningStore(tmp_path)

    # 1. Un-executed decision (no fill, no realized outcome)
    unexecuted_packet = CIODecisionPacket(
        case_id="unexecuted-001",
        expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        thesis="Unexecuted speculative thesis",
        selected_instrument="2330.TW",
        action="HOLD",
        quantity=0.0,
    )
    store.record_decision(unexecuted_packet, {}, {})

    # 2. Executed decision with real fill and real outcome
    executed_packet = CIODecisionPacket(
        case_id="executed-002",
        expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        thesis="Executed breakout thesis",
        selected_instrument="2330.TW",
        action="BUY",
        quantity=10.0,
    )
    store.record_decision(executed_packet, {}, {})
    store.record_fill(
        case_id="executed-002",
        order_id="order-2",
        fill_dict={"fill_price": 1000.0, "quantity": 10.0},
        costs_dict={"total_cost": 25.0},
    )
    store.record_outcome(
        case_id="executed-002",
        outcome_dict={"exit_price": 1050.0, "realized_pnl": 475.0},
        attribution_dict={"alpha": 2.0},
        lessons=["Real verified outcome on TSMC entry"],
    )

    # Retrieval must strictly return ONLY the executed case with real outcome
    lessons = store.retrieve_context_lessons(symbol="2330.TW")
    assert len(lessons) == 1
    assert lessons[0]["case_id"] == "executed-002"
    assert "Real verified outcome" in lessons[0]["takeaway"]

    outcomes = store.retrieve_past_outcomes(symbol="2330.TW")
    assert len(outcomes) == 1
    assert outcomes[0]["case_id"] == "executed-002"
    assert outcomes[0]["outcome"]["realized_pnl"] == 475.0


# ============================================================================
# 17. Autonomy Claim: Not Claimed Without Configured Executor & Readback Receipt
# ============================================================================

def test_defect_autonomy_not_claimed_without_configured_executor_and_readback_receipt(tmp_path):
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock)

    # 1. No executor -> must not claim autonomy
    assert runner.cio_executor is None
    cycle1 = runner.run_one_cycle(DYNAMIC_DESK_ID)
    assert cycle1["autonomous_paper_execution"] is False
    assert cycle1["executor_configured"] is False
    assert cycle1["readback_receipt_present"] is False

    # 2. Mock executor configured with valid decision and readback receipt
    class MockValidCIOExecutor:
        def __init__(self):
            self.last_receipt = None

        def is_available(self) -> bool:
            return True

        def request_decision(self, ctx_req) -> CIODecisionPacket:
            p = CIODecisionPacket(
                case_id=f"auto-{ctx_req.request_id[:8]}",
                as_of=clock[0],
                expiry=clock[0] + timedelta(hours=2),
                thesis="Autonomous CIO verdict",
                selected_instrument="2330.TW",
                action="BUY",
                quantity=10.0,
                provenance=CIOProvenance(authority="MAIN_CIO"),
                conditions={"allow_odd_lot": True}, is_fixture=True,
            )
            receipt = CIOExecutionReceipt(
                receipt_id=f"rcpt-{ctx_req.request_id[:8]}",
                case_id=p.case_id,
                session_id="cio-market-lab",
                provider_id=DEFAULT_PINNED_PROVIDER_ID,
                model_id=DEFAULT_PINNED_MODEL_ID,
                raw_prompt_hash="prompt-hash",
                raw_response_hash="resp-hash",
                readback_verified=True,
                authority="MAIN_CIO",
            )
            self.last_receipt = receipt
            sign_cio_packet(p, signer_id="main-cio", receipt_id=receipt.receipt_id)
            return p

    mock_exec = MockValidCIOExecutor()
    runner.set_cio_executor(mock_exec)

    cycle2 = runner.run_one_cycle(DYNAMIC_DESK_ID)
    assert cycle2["run"]["status"] == "COMPLETED"
    assert cycle2["autonomous_paper_execution"] is True
    assert cycle2["executor_configured"] is True
    assert cycle2["readback_receipt_present"] is True


# ============================================================================
# 18. Dynamic Desk Fail-Closed Even When require_cio_provider Disabled
# ============================================================================

def test_dynamic_desk_fail_closed_when_require_cio_provider_disabled(tmp_path):
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock)

    # Explicitly disable require_cio_provider on runner
    runner.require_cio_provider = False
    assert runner.require_cio_provider is False
    assert runner.cio_executor is None
    runner.clear_staged_packets()

    # Dynamic desk MUST NOT fall back to scripted breakout trading
    result = runner.run_one_cycle(DYNAMIC_DESK_ID)

    assert result["run"]["status"] == "BLOCKED"
    assert result["run"]["reason"] == "BLOCKED_NO_CIO_DECISION_PROVIDER"
    assert result["run"]["orders_count"] == 0
    assert result["run"]["fills_count"] == 0
    assert result["autonomous_paper_execution"] is False

    for d in result["decisions"]:
        assert d["action"] == "NO_TRADE"
        assert "BLOCKED_NO_CIO_DECISION_PROVIDER" in d["reason"]
        assert d["terminal_status"] == "TERMINAL_RISK_BLOCK"

    assert orders.all_orders() == []


# ============================================================================
# 19. Executor Startup Wiring via Runner Constructor
# ============================================================================

def test_executor_startup_wiring_via_constructor(tmp_path):
    class MockUnavailableExecutor:
        def is_available(self) -> bool:
            return False

    mock_exec = MockUnavailableExecutor()
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]

    pm = PortfolioManager(
        initial_cash_swing=TEAM_INITIAL_CAPITAL_TWD,
        initial_cash_intraday=TEAM_INITIAL_CAPITAL_TWD,
    )
    pm.register_strategy(DYNAMIC_DESK_ID, TEAM_INITIAL_CAPITAL_TWD, unified_cash=True)
    es = EventStore(":memory:")
    po = PaperOrderService(pm, es)
    adapter = MockCIOMarketAdapter(clock[0])

    # Direct constructor registration
    runner = AutonomousPaperRunner(
        tmp_path,
        pm,
        po,
        adapter,
        now_fn=lambda: clock[0],
        team_initial_capital=TEAM_INITIAL_CAPITAL_TWD,
        cio_executor=mock_exec,
    )
    assert runner.cio_executor is mock_exec

    runner.configure(PaperExperimentSettings(
        strategy_id=DYNAMIC_DESK_ID,
        strategy_name="Autonomous Paper Execution Desk",
        enabled=True,
        universe=["2330.TW"],
        max_position_notional=500000.0,
        initial_cash=TEAM_INITIAL_CAPITAL_TWD,
    ))

    # Runner execution with unavailable startup executor must explicitly block
    result = runner.run_one_cycle(DYNAMIC_DESK_ID)
    assert result["run"]["status"] == "BLOCKED"
    assert "BLOCKED_CIO_EXECUTOR_UNAVAILABLE" in result["run"]["reason"]
    assert result["run"]["orders_count"] == 0
    assert result["run"]["fills_count"] == 0
    for d in result["decisions"]:
        assert d["action"] == "NO_TRADE"
        assert "BLOCKED_CIO_EXECUTOR_UNAVAILABLE" in d["reason"]
        assert d["terminal_status"] == "TERMINAL_RISK_BLOCK"


# ============================================================================
# 20. REGRESSION: Temporal Eligibility – Lessons Consumed Only After Outcome
# ============================================================================

def test_regression_lessons_consumed_only_by_decisions_after_outcome_timestamp(tmp_path):
    """Positive regression: lessons must only be retrievable by decisions
    whose as_of timestamp is strictly AFTER the lesson's created_at.

    Prevents future leakage where a lesson from outcome X feeds a decision
    that was actually timestamped BEFORE X occurred.
    """
    store = CIODecisionLearningStore(tmp_path)

    # Simulate a decision at T=0
    t0 = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)
    packet = CIODecisionPacket(
        case_id="temporal-001",
        as_of=t0,
        expiry=t0 + timedelta(hours=8),
        thesis="Temporal test entry",
        selected_instrument="2330.TW",
        action="BUY",
        quantity=10.0,
    )
    store.record_decision(packet, {}, {})
    store.record_fill(
        case_id="temporal-001",
        order_id="order-temp-1",
        fill_dict={"fill_price": 1000.0, "quantity": 10.0},
        costs_dict={"total_cost": 20.0},
    )

    # Outcome at T=2h: lesson should get created_at = T+2h
    t_outcome = t0 + timedelta(hours=2)
    store.record_outcome(
        case_id="temporal-001",
        outcome_dict={"exit_price": 1050.0, "realized_pnl": 480.0},
        lessons=["Lesson from temporal test outcome"],
        as_of=t_outcome,
    )

    # Verify lesson was created with the correct timestamp
    lessons_all = store._lessons
    assert len(lessons_all) == 1
    lesson = lessons_all[0]
    lesson_ts = lesson.created_at if lesson.created_at.tzinfo else lesson.created_at.replace(tzinfo=timezone.utc)
    assert lesson_ts == t_outcome

    # POSITIVE: Decision at T=3h (after outcome) SHOULD see the lesson
    t_after = t0 + timedelta(hours=3)
    retrieved_after = store.retrieve_context_lessons(symbol="2330.TW", as_of=t_after, limit=10)
    assert len(retrieved_after) >= 1
    assert retrieved_after[0]["case_id"] == "temporal-001"

    # NEGATIVE: Decision at T=1h (before outcome) must NOT see the lesson
    t_before = t0 + timedelta(hours=1)
    retrieved_before = store.retrieve_context_lessons(symbol="2330.TW", as_of=t_before, limit=10)
    assert len(retrieved_before) == 0, (
        f"Future leakage: lesson from outcome at {t_outcome.isoformat()} was visible to "
        f"decision at {t_before.isoformat()}"
    )

    # NEGATIVE: Decision at exactly the outcome time must NOT see the lesson
    # (strictly as_of > created_at, not >=)
    retrieved_exact = store.retrieve_context_lessons(symbol="2330.TW", as_of=t_outcome, limit=10)
    assert len(retrieved_exact) == 0, (
        f"Decision at exactly outcome time {t_outcome.isoformat()} must not see lesson created at same time"
    )

    # Also verify retrieve_past_outcomes respects strict temporal boundary
    outcomes_after = store.retrieve_past_outcomes(symbol="2330.TW", as_of=t_after, limit=10)
    assert len(outcomes_after) >= 1
    outcomes_before = store.retrieve_past_outcomes(symbol="2330.TW", as_of=t_before, limit=10)
    assert len(outcomes_before) == 0, "Past outcomes admitted outcome before it occurred"
    outcomes_exact = store.retrieve_past_outcomes(symbol="2330.TW", as_of=t_outcome, limit=10)
    assert len(outcomes_exact) == 0, "Past outcomes admitted outcome concurrent with decision"


def test_regression_future_outcomes_excluded_from_decision_context(tmp_path):
    """Negative regression: outcomes that have not yet occurred in the decision
    timeline must never leak into earlier decision contexts.

    Tests the strict temporal boundary: a lesson recorded at T+6h must not
    appear in a context request at T+4h even if wall-clock time is later.
    """
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock)

    # 1. Submit a CIO buy at T=0
    buy_packet = CIODecisionPacket(
        case_id="future-excl-buy-001",
        as_of=clock[0],
        expiry=clock[0] + timedelta(hours=8),
        thesis="Future exclusion test buy",
        selected_instrument="2330.TW",
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=20.0,
        provenance=CIOProvenance(authority="MAIN_CIO"),
        conditions={"allow_odd_lot": True}, is_fixture=True,
    )
    sign_cio_packet(buy_packet)
    d1 = submit_after_new_fixture_quote(runner, clock, buy_packet)
    assert d1.action == "BUY_FILLED"

    # 2. Record outcome at T+6h (simulating future close)
    t_outcome = clock[0] + timedelta(hours=6)
    runner.learning_store.record_outcome(
        case_id="future-excl-buy-001",
        outcome_dict={"exit_price": 1100.0, "realized_pnl": 1900.0, "return_pct": 10.0},
        lessons=["Future outcome that should NOT be visible at T+4h"],
        as_of=t_outcome,
    )

    # 3. Build decision context at T+4h (BEFORE the outcome)
    clock[0] = clock[0] + timedelta(hours=4)
    ctx = runner.build_decision_context_request(symbols=["2330.TW"])

    # Verify: the future lesson must NOT appear
    future_lessons = [
        l for l in ctx.prior_lessons
        if l.get("case_id") == "future-excl-buy-001"
    ]
    assert len(future_lessons) == 0, (
        f"Future leakage: outcome from T+6h visible in context at T+4h. "
        f"Leaked lessons: {future_lessons}"
    )

    # 4. Build decision context at T+7h (AFTER the outcome): lesson SHOULD appear
    clock[0] = clock[0] + timedelta(hours=3)  # now at T+7h
    ctx_after = runner.build_decision_context_request(symbols=["2330.TW"])
    after_lessons = [
        l for l in ctx_after.prior_lessons
        if l.get("case_id") == "future-excl-buy-001"
    ]
    assert len(after_lessons) >= 1, (
        f"Expected lesson from T+6h to be visible at T+7h, but got none"
    )


def test_regression_autonomous_cycle_now_variable_resolved(tmp_path):
    """Regression: _run_cio_decision_path (inside _run_symbol) must not raise
    NameError for undefined local variable 'now' when no CIO provider is configured.

    Previously, the BLOCKED_NO_CIO_DECISION_PROVIDER path referenced bare 'now'
    instead of self._now(), causing NameError at runtime.
    """
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock)

    # Ensure no CIO executor
    runner.cio_executor = None
    runner.clear_staged_packets()

    # This must NOT raise NameError
    result = runner.run_one_cycle(DYNAMIC_DESK_ID)

    assert result["run"]["status"] == "BLOCKED"
    assert result["run"]["reason"] == "BLOCKED_NO_CIO_DECISION_PROVIDER"
    for d in result["decisions"]:
        assert d["action"] == "NO_TRADE"
        assert "BLOCKED_NO_CIO_DECISION_PROVIDER" in d["reason"]
        assert d["terminal_status"] == "TERMINAL_RISK_BLOCK"


# ============================================================================
# 22. REGRESSION: Public Evidence Reader Verification & Gap Audits
# ============================================================================

def test_research_reader_rejects_fixtures_and_synthetic_data(tmp_path):
    """Production research inbox reader must reject test fixtures and synthetic data."""
    from cio_market_lab.research.browser import (
        PublicResearchInboxReader,
        validate_and_sanitize_evidence,
        FakeBrowserResearchAdapter,
    )
    reader = PublicResearchInboxReader(inbox_dir=tmp_path)
    now = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)

    # 1. Explicit fixture item
    item_fixture = {
        "id": "item-001",
        "symbol": "2330.TW",
        "source_url": "https://mops.twse.com.tw/filing",
        "is_fixture": True,
        "verification_status": "verified",
        "verified_facts": ["Revenue increased 20% YoY"],
        "observed_at": now.isoformat(),
    }
    ok, err = reader.add_evidence(item_fixture, now=now)
    assert not ok
    assert "REJECTED_FIXTURE" in err

    # 2. Fake source mode / adapter sample seed
    fake_adapter = FakeBrowserResearchAdapter()
    for sample in fake_adapter.list_inbox():
        ok, err = reader.add_evidence(sample, now=now)
        assert not ok, f"Sample item {sample.id} should have been rejected as fixture/unverified"
        assert "REJECTED_FIXTURE" in err or "REJECTED_UNVERIFIED_NARRATIVE" in err


def test_research_reader_rejects_stale_and_future_evidence(tmp_path):
    """Reader must reject stale evidence (> max_age) and future timestamps (observed_at > now)."""
    from cio_market_lab.research.browser import PublicResearchInboxReader
    reader = PublicResearchInboxReader(inbox_dir=tmp_path, max_age_seconds=86400)
    now = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)

    # Future evidence: observed_at 2 hours in the future
    future_item = {
        "id": "item-future",
        "symbol": "2330.TW",
        "source_url": "https://mops.twse.com.tw/future",
        "verification_status": "verified",
        "verified_facts": ["Future forecast"],
        "observed_at": (now + timedelta(hours=2)).isoformat(),
    }
    ok, err = reader.add_evidence(future_item, now=now)
    assert not ok
    assert "REJECTED_FUTURE_TIMESTAMP" in err

    # Stale evidence: observed_at 2 days ago
    stale_item = {
        "id": "item-stale",
        "symbol": "2330.TW",
        "source_url": "https://mops.twse.com.tw/stale",
        "verification_status": "verified",
        "verified_facts": ["Historical fact"],
        "observed_at": (now - timedelta(days=2)).isoformat(),
    }
    ok, err = reader.add_evidence(stale_item, now=now)
    assert not ok
    assert "REJECTED_STALE_EVIDENCE" in err


def test_research_reader_rejects_missing_url_symbol_facts_and_unverified_narrative(tmp_path):
    """Reader rejects items missing URL, symbol, verified facts, or unverified status."""
    from cio_market_lab.research.browser import PublicResearchInboxReader
    reader = PublicResearchInboxReader(inbox_dir=tmp_path)
    now = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)

    # Missing URL
    ok, err = reader.add_evidence({
        "symbol": "2330.TW",
        "verification_status": "verified",
        "verified_facts": ["Fact"],
        "observed_at": now.isoformat(),
    }, now=now)
    assert not ok
    assert "REJECTED_INVALID_URL" in err

    # Missing symbol
    ok, err = reader.add_evidence({
        "source_url": "https://mops.twse.com.tw/filing",
        "verification_status": "verified",
        "verified_facts": ["Fact"],
        "observed_at": now.isoformat(),
    }, now=now)
    assert not ok
    assert "REJECTED_MISSING_SYMBOL" in err

    # Empty verified facts
    ok, err = reader.add_evidence({
        "symbol": "2330.TW",
        "source_url": "https://mops.twse.com.tw/filing",
        "verification_status": "verified",
        "verified_facts": [],
        "observed_at": now.isoformat(),
    }, now=now)
    assert not ok
    assert "REJECTED_NO_VERIFIED_FACTS" in err

    # Unverified narrative status
    ok, err = reader.add_evidence({
        "symbol": "2330.TW",
        "source_url": "https://mops.twse.com.tw/filing",
        "verification_status": "community_narrative",
        "verified_facts": ["Rumor on retail forum"],
        "observed_at": now.isoformat(),
    }, now=now)
    assert not ok
    assert "REJECTED_UNVERIFIED_NARRATIVE" in err


def test_research_reader_does_not_default_to_official_filing(tmp_path):
    """Source tier must not default missing/unknown provenance to official_filing."""
    from cio_market_lab.research.browser import PublicResearchInboxReader
    reader = PublicResearchInboxReader(inbox_dir=tmp_path)
    now = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)

    # Valid evidence without source_tier and without official provenance
    item = {
        "id": "item-news",
        "symbol": "2330.TW",
        "source_url": "https://news.cnyes.com/news/id/12345",
        "verification_status": "verified",
        "verified_facts": ["TSMC reports record quarterly margins"],
        "observed_at": now.isoformat(),
    }
    ok, err = reader.add_evidence(item, now=now)
    assert ok
    staged = reader._staged_evidence.get("item-news")
    assert staged is not None
    assert staged.source_tier != "official_filing"
    assert staged.source_tier == "unknown_tier"


def test_research_reader_emits_explicit_gap_rather_than_fabricated_research(tmp_path):
    """When a symbol lacks verified research, an explicit research gap is emitted."""
    from cio_market_lab.research.browser import PublicResearchInboxReader
    reader = PublicResearchInboxReader(inbox_dir=tmp_path)
    now = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)

    verified, gaps = reader.get_verified_research_for_symbols(["2330.TW", "AAPL"], now=now)
    assert len(verified) == 0
    assert len(gaps) == 2

    gap_tw = next(g for g in gaps if g["symbol"] == "2330.TW")
    assert gap_tw["gap_status"] == "EXPLICIT_RESEARCH_GAP"
    assert gap_tw["reason"] == "NO_VERIFIED_RESEARCH"
    assert "official_filings" in gap_tw["required_evidence"]


def test_deterministic_e2e_research_inbox_to_cio_packet_and_learning_lifecycle(tmp_path):
    """Deterministic end-to-end verification of:
    1. Research inbox staging and verified evidence ingestion.
    2. Decision context request construction with verified research, explicit gaps, and FX accounting.
    3. Authoritative CIO packet execution debited against single cash pool.
    4. Decision journal and pre-decision beliefs recorded in learning store.
    5. Outcome evaluation and lesson extraction with strict timestamp preservation.
    6. Retrieval into subsequent decision context with NO concurrent or future leakage.
    """
    clock = [datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock, native_currency="USD")
    adapter.quote_price = 230.0

    runner.configure(PaperExperimentSettings(
        strategy_id=DYNAMIC_DESK_ID,
        strategy_name="Autonomous Paper Execution Desk",
        enabled=True,
        universe=["NVDA", "TSLA"],
        base_currency="USD",
        max_position_notional=500000.0,
        initial_cash=100_000.0,  # TEST_ONLY USD-native desk funding
        fx_to_reporting=1.0,
        fx_rates={"USD": 32.0},
    ))

    # 1. Stage verified research for NVDA into runner's research inbox
    inbox_dir = runner.runtime_dir / "research_inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    evidence_item = {
        "research_id": "nvda-repurchase-20260928-official",
        "symbol": "NVDA",
        "source_url": "https://nvidianews.nvidia.com/news/nvidia-announces-a-150-billion-share-repurchase-authorization-increase",
        "source_tier": "official_company_ir",
        "observed_at": (clock[0] - timedelta(hours=1)).isoformat(),
        "published_at": "2026-09-28",
        "is_fixture": False,
        "verification_status": "verified",
        "verified_facts": [
            "NVIDIA announced a USD150 billion increase to its share repurchase authorization.",
            "Company reports total remaining authorization of USD235 billion.",
            "Company expects execution through fiscal year 2028.",
        ],
        "research_scope": "event_input_only_not_order",
        "limitations": [
            "No execution pace guaranteed.",
            "No valuation or price evidence in this release; no buy inference from authorization alone.",
        ],
    }
    with (inbox_dir / "nvda_ir.json").open("w", encoding="utf-8") as f:
        json.dump(evidence_item, f)

    # 2. Build decision context request for [NVDA, TSLA] at T0
    ctx_req = runner.build_decision_context_request(symbols=["NVDA", "TSLA"])

    # Verify verified_research injected into CIO context for NVDA
    assert len(ctx_req.verified_research) == 1
    assert ctx_req.verified_research[0]["research_id"] == "nvda-repurchase-20260928-official"
    assert ctx_req.verified_research[0]["symbol"] == "NVDA"

    # Verify TSLA has explicit research gap (no fabricated research)
    assert len(ctx_req.research_gaps) == 1
    assert ctx_req.research_gaps[0]["symbol"] == "TSLA"
    assert ctx_req.research_gaps[0]["gap_status"] == "EXPLICIT_RESEARCH_GAP"

    # Verify FX accounting and sizing guidance provided to CIO
    assert "USD" in ctx_req.fx_rates
    assert ctx_req.fx_rates["USD"] == 32.0
    assert ctx_req.fx_accounting["reporting_currency"] == "TWD"
    assert ctx_req.fx_accounting["usd_twd_rate"] == 32.0
    assert ctx_req.fx_accounting["source_label"] == "configured_assumption"
    assert ctx_req.fx_accounting["is_simulated"] is True
    assert ctx_req.fx_accounting["sizing_guidance"]["max_position_notional_twd"] == 500000.0
    assert ctx_req.fx_accounting["sizing_guidance"]["max_position_notional_usd"] == 15625.0
    assert ctx_req.tactical_risk_limits["max_position_notional_usd"] == 15625.0

    # 3. Create authoritative CIO decision packet referencing this research evidence
    cio_packet = CIODecisionPacket(
        case_id="cio-case-nvda-20260928-001",
        as_of=clock[0],
        evidence=["nvda-repurchase-20260928-official"],
        thesis="Verified capital return announcement supports accumulation within USD sizing boundary.",
        selected_instrument="NVDA",
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=20.0,
        conditions={**({"max_slippage_bps": 20}), "allow_odd_lot": True},
        risk_assessment={"downside_buffer": 0.05, "thesis_invalidation": "close < 210"},
        alternatives_considered=[{"symbol": "TSLA", "reason": "No verified issuer research available"}],
        expiry=clock[0] + timedelta(hours=8),
        confidence=0.91,
        strategy_version="dynamic-desk-cio-20260928",
        provenance=CIOProvenance(
            authority="MAIN_CIO",
            actor_role="CHIEF_INVESTMENT_OFFICER",
            signer_id="main-cio-key",
            source="external_packet",
        ),
        is_fixture=True,
    )
    sign_cio_packet(cio_packet, signer_id="main-cio-key")

    # 4. Submit CIO packet -> verify execution against single cash pool
    decision = submit_after_new_fixture_quote(runner, clock, cio_packet)
    assert decision.action == "BUY_FILLED"
    assert decision.terminal_status == "TERMINAL_FILLED"
    assert decision.quantity == 20.0

    # Verify journaled pre-decision beliefs and fill in learning store
    rec = runner.learning_store.get_record(cio_packet.case_id)
    assert rec is not None
    assert rec.status == "FILLED"
    assert rec.fill is not None
    assert rec.costs is not None
    assert rec.pre_decision_portfolio is not None
    assert len(rec.rejected_opportunities) == 1
    assert rec.rejected_opportunities[0]["symbol"] == "TSLA"

    # 5. Evaluate subsequent outcome at T_outcome = T0 + 2h
    t_outcome = clock[0] + timedelta(hours=2)
    outcome_payload = {
        "exit_price": 242.0,
        "realized_pnl": 240.0,
        "return_pct": 5.22,
        "as_of": t_outcome.isoformat(),
    }
    lesson_record = runner.learning_store.record_outcome(
        case_id=cio_packet.case_id,
        outcome_dict=outcome_payload,
        attribution_dict={"alpha": 3.1, "market_beta": 2.12},
        lessons=["Verified IR share repurchase authorization established firm support for NVDA swing entry."],
        as_of=t_outcome,
    )
    assert lesson_record is not None
    assert lesson_record.created_at == t_outcome

    # 6. Query context at exact outcome time T_outcome: must NOT admit lesson (concurrent exclusion)
    ctx_concurrent = runner.build_decision_context_request(symbols=["NVDA"])
    assert len([l for l in ctx_concurrent.prior_lessons if l.get("case_id") == cio_packet.case_id]) == 0

    # 7. Record a future outcome at T_future = T_outcome + 4h
    t_future = t_outcome + timedelta(hours=4)
    runner.learning_store.record_outcome(
        case_id="cio-future-case-hypothetical",
        outcome_dict={"exit_price": 300.0, "realized_pnl": 1200.0, "as_of": t_future.isoformat()},
        attribution_dict={"alpha": 5.0},
        lessons=["Hypothetical future lesson."],
        as_of=t_future,
    )

    # 8. Query context at T_next = T_outcome + 1h (after T_outcome, before T_future)
    clock[0] = t_outcome + timedelta(hours=1)
    ctx_next = runner.build_decision_context_request(symbols=["NVDA"])

    # T_outcome lesson IS admitted
    admitted_lessons = [l for l in ctx_next.prior_lessons if l.get("case_id") == cio_packet.case_id]
    assert len(admitted_lessons) == 1
    assert "Verified IR share repurchase" in admitted_lessons[0]["takeaway"]

    # T_future lesson is strictly EXCLUDED (no future leakage)
    assert len([l for l in ctx_next.prior_lessons if l.get("case_id") == "cio-future-case-hypothetical"]) == 0
    assert not any(o.get("case_id") == "cio-future-case-hypothetical" for o in ctx_next.past_outcomes)

    # Past outcomes includes the real NVDA outcome
    assert any(o.get("case_id") == cio_packet.case_id for o in ctx_next.past_outcomes)


def test_negative_missing_fx_source_produces_explicit_sizing_gap_and_no_silent_32(tmp_path):
    """Negative test: When no evidenced USD/TWD rate exists, missing source must remain missing.

    Concrete invariant checks:
    1. NEVER silently defaults to 32.0.
    2. usd_twd_rate is None (or not in rates).
    3. USD sizing cannot be authorized (max_position_notional_usd is None).
    4. Explicit sizing gap is produced for USD instruments.
    5. Source is labeled 'missing', not 'canonical_team_ops'.
    """
    clock = [datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock, native_currency="USD")

    # Configure runner WITHOUT any USD FX rate or experiment
    runner.configure(PaperExperimentSettings(
        strategy_id=DYNAMIC_DESK_ID,
        strategy_name="Autonomous Paper Execution Desk",
        enabled=True,
        universe=["NVDA"],
        base_currency="USD",
        max_position_notional=500000.0,
        initial_cash=100_000.0,
        fx_to_reporting=1.0,
    ))

    ctx_req = runner.build_decision_context_request(symbols=["NVDA", "2330.TW"])

    # 1. No silent 32 fallback: USD must NOT be 32.0
    assert "USD" not in ctx_req.fx_rates
    assert ctx_req.fx_accounting["usd_twd_rate"] is None
    assert ctx_req.tactical_risk_limits["fx_rate_usd_twd"] is None

    # 2. Source is explicitly missing, NOT claimed as canonical_team_ops
    assert ctx_req.fx_accounting["source"] == "missing"
    assert ctx_req.fx_accounting["source_label"] == "missing"
    assert ctx_req.fx_accounting["is_simulated"] is False

    # 3. USD position sizing is NOT authorized (None, never silently 15625.0)
    assert ctx_req.fx_accounting["sizing_guidance"]["max_position_notional_usd"] is None
    assert ctx_req.fx_accounting["sizing_guidance"]["max_daily_loss_usd"] is None
    assert ctx_req.tactical_risk_limits["max_position_notional_usd"] is None
    assert ctx_req.tactical_risk_limits["max_daily_loss_usd"] is None

    # 4. TWD sizing is preserved
    assert ctx_req.fx_accounting["sizing_guidance"]["max_position_notional_twd"] == 500000.0

    # 5. Explicit sizing gap is produced for NVDA (USD instrument)
    usd_gaps = [g for g in ctx_req.sizing_gaps if g["symbol"] == "NVDA"]
    assert len(usd_gaps) == 1
    assert usd_gaps[0]["gap_status"] == "EXPLICIT_SIZING_GAP"
    assert usd_gaps[0]["currency"] == "USD"
    assert "Missing evidenced USD/TWD valuation FX source" in usd_gaps[0]["reason"]

    # 6. TW instrument (2330.TW) has no sizing gap
    tw_gaps = [g for g in ctx_req.sizing_gaps if g["symbol"] == "2330.TW"]
    assert len(tw_gaps) == 0


def test_positive_configured_assumption_fx_matches_canonical_accounting(tmp_path):
    """Positive test: Configured simulation assumption matches canonical accounting valuation FX."""
    clock = [datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock, native_currency="USD")

    # Configure runner with explicit accounting valuation assumption: USD/TWD = 31.50
    runner.configure(PaperExperimentSettings(
        strategy_id=DYNAMIC_DESK_ID,
        strategy_name="Autonomous Paper Execution Desk",
        enabled=True,
        universe=["NVDA"],
        base_currency="USD",
        max_position_notional=500000.0,
        initial_cash=100_000.0,
        fx_to_reporting=1.0,
        fx_rates={"USD": 31.50},
    ))

    ctx_req = runner.build_decision_context_request(symbols=["NVDA", "2330.TW"])

    # 1. Decision sizing uses the exact configured rate
    assert ctx_req.fx_rates["USD"] == 31.50
    assert ctx_req.fx_accounting["usd_twd_rate"] == 31.50
    assert ctx_req.fx_accounting["source_label"] == "configured_assumption"
    assert ctx_req.fx_accounting["is_simulated"] is True

    # 2. Sizing is computed exactly from canonical rate (500000 / 31.5 = 15873.02)
    expected_notional_usd = round(500000.0 / 31.50, 2)
    expected_loss_usd = round(50000.0 / 31.50, 2)
    assert ctx_req.fx_accounting["sizing_guidance"]["max_position_notional_usd"] == expected_notional_usd
    assert ctx_req.fx_accounting["sizing_guidance"]["max_daily_loss_usd"] == expected_loss_usd
    assert ctx_req.tactical_risk_limits["max_position_notional_usd"] == expected_notional_usd
    assert ctx_req.tactical_risk_limits["max_daily_loss_usd"] == expected_loss_usd

    # 3. No sizing gaps for NVDA
    assert len([g for g in ctx_req.sizing_gaps if g["symbol"] == "NVDA"]) == 0

    # 4. Canonical accounting snapshot reconciles with the exact same rate
    snap = runner.generate_canonical_team_ops(clock[0])
    assert snap["fx_rates"]["USD"] == 31.50
    assert snap["portfolio"]["fx_accounting"]["usd_twd_rate"] == 31.50


def test_positive_market_fx_source_with_observed_timestamps(tmp_path):
    """Positive test: Authoritative market FX quote provides source, timestamps, and real sizing."""
    clock = [datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock, native_currency="USD")

    # Set authoritative market quote in adapter for USDTWD=X
    obs_time = datetime(2026, 9, 28, 13, 55, tzinfo=timezone.utc)
    market_quote = DurableQuoteSnapshot(
        symbol="USDTWD=X",
        market=Market.US,
        source="taifex_official_fx",
        observed_at=obs_time,
        bar_time=obs_time,
        last_price=31.875,
        quality="good",
        is_stale=False,
        is_synthetic=False,
    )
    runner._durable_quotes["USDTWD=X"] = market_quote

    runner.configure(PaperExperimentSettings(
        strategy_id=DYNAMIC_DESK_ID,
        strategy_name="Autonomous Paper Execution Desk",
        enabled=True,
        universe=["NVDA"],
        base_currency="USD",
        max_position_notional=500000.0,
        initial_cash=100_000.0,
        fx_to_reporting=1.0,
    ))

    ctx_req = runner.build_decision_context_request(symbols=["NVDA"])

    # 1. Market source provenance verified
    assert ctx_req.fx_rates["USD"] == 31.875
    assert ctx_req.fx_accounting["usd_twd_rate"] == 31.875
    assert ctx_req.fx_accounting["source"] == "taifex_official_fx"
    assert ctx_req.fx_accounting["source_label"] == "market"
    assert ctx_req.fx_accounting["is_simulated"] is False
    assert ctx_req.fx_accounting["observed_at"] == obs_time.isoformat()
    assert ctx_req.fx_accounting["source_timestamp"] == obs_time.isoformat()

    # 2. Sizing derived from market quote
    expected_notional_usd = round(500000.0 / 31.875, 2)
    assert ctx_req.fx_accounting["sizing_guidance"]["max_position_notional_usd"] == expected_notional_usd
    assert ctx_req.tactical_risk_limits["max_position_notional_usd"] == expected_notional_usd
    assert len(ctx_req.sizing_gaps) == 0

    # 3. Canonical accounting snapshot reconciles with the exact same market rate
    snap = runner.generate_canonical_team_ops(clock[0])
    assert snap["fx_rates"]["USD"] == 31.875
    assert snap["portfolio"]["fx_accounting"]["usd_twd_rate"] == 31.875
    assert snap["portfolio"]["fx_accounting"]["source"] == "taifex_official_fx"
    assert snap["portfolio"]["fx_accounting"]["source_label"] == "market"
    assert snap["portfolio"]["fx_accounting"]["is_simulated"] is False
    assert snap["portfolio"]["fx_accounting"]["observed_at"] == obs_time.isoformat()
    assert snap["portfolio"]["fx_accounting"]["source_timestamp"] == obs_time.isoformat()


def test_actual_usd_fill_accounting_with_missing_rate_fails_closed_and_blocks_sizing(tmp_path):
    """Missing FX rate test with actual USD fill:
    When USD fills exist and no validated/configured FX source exists,
    NAV must be explicitly unavailable (NAV_UNAVAILABLE) and risk gate blocks sizing,
    never silently defaulting cash or NAV to 32.0.
    Assert decision and NAV use the same rate (None) and source ('missing').
    """
    clock = [datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock, native_currency="USD")

    runner.configure(PaperExperimentSettings(
        strategy_id=DYNAMIC_DESK_ID,
        strategy_name="Autonomous Paper Execution Desk",
        enabled=True,
        universe=["AAPL"],
        base_currency="USD",
        max_position_notional=500000.0,
        initial_cash=100_000.0,
        fx_to_reporting=1.0,
    ))

    # Apply an actual USD fill: BUY 10 AAPL @ 150.0 USD
    fill = Fill(
        fill_id="fill-usd-missing-1",
        order_id="order-usd-missing-1",
        symbol="AAPL",
        currency="USD",
        assumptions={"test_only": True},
        bucket=DecisionScope.SWING,
        side=OrderSide.BUY,
        quantity=10.0,
        fill_price=150.0,
        fee=0.0,
        tax=0.0,
        slippage=0.0,
        timestamp=clock[0],
    )
    pm.apply_fill(fill, DYNAMIC_DESK_ID)

    # Authoritative price quote for AAPL is fresh and good
    runner._durable_quotes["AAPL"] = DurableQuoteSnapshot(
        symbol="AAPL",
        market=Market.US,
        source="polygon",
        observed_at=clock[0],
        last_price=160.0,
        quality="good",
        is_stale=False,
        is_synthetic=False,
    )

    ctx_req = runner.build_decision_context_request(symbols=["AAPL"])

    # 1. Decision context reports FX missing and risk gate blocks USD sizing
    assert "USD" not in ctx_req.fx_rates
    assert ctx_req.fx_accounting["usd_twd_rate"] is None
    assert ctx_req.fx_accounting["source"] == "missing"
    assert ctx_req.fx_accounting["source_label"] == "missing"
    assert ctx_req.fx_accounting["is_simulated"] is False
    assert ctx_req.fx_accounting["sizing_guidance"]["max_position_notional_usd"] is None
    assert ctx_req.tactical_risk_limits["max_position_notional_usd"] is None
    assert any(g["symbol"] == "AAPL" and g["gap_status"] == "EXPLICIT_SIZING_GAP" for g in ctx_req.sizing_gaps)

    # 2. Accounting snapshot fails closed with NAV_UNAVAILABLE
    snap = runner.generate_canonical_team_ops(clock[0])
    assert "USD" not in snap["fx_rates"]
    assert snap["portfolio"]["fx_accounting"]["usd_twd_rate"] is None
    assert snap["portfolio"]["fx_accounting"]["source"] == "missing"
    assert snap["portfolio"]["fx_accounting"]["source_label"] == "missing"

    # 3. Assert decision and NAV share the exact same rate/source
    assert snap["portfolio"]["fx_accounting"]["usd_twd_rate"] == ctx_req.fx_accounting["usd_twd_rate"]
    assert snap["portfolio"]["fx_accounting"]["source"] == ctx_req.fx_accounting["source"]

    # 4. Native USD debit succeeds independently; TWD reporting cash is unavailable.
    assert snap["portfolio"]["cash"] is None
    assert snap["portfolio"]["nav_status"] == "FX_UNAVAILABLE"
    assert snap["cash"] is None
    assert pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING).currency == "USD"
    assert pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING).cash == 98_500.0

    # 5. NAV and Equity are explicitly unavailable
    assert snap["portfolio"]["nav_status"] == "FX_UNAVAILABLE"
    assert snap["nav_status"] == "FX_UNAVAILABLE"
    assert snap["portfolio"]["equity"] is None
    assert snap["portfolio"]["nav"] is None
    assert snap["equity"] is None
    assert snap["nav"] is None
    assert any("MISSING_VALUATION_FX" in w for w in snap["integrity_warnings"])


def test_actual_usd_fill_accounting_with_configured_assumption_rate(tmp_path):
    """Configured assumption FX test with actual USD fill:
    When explicit simulation rate is configured (labeled configured_assumption),
    both decision and NAV accounting share the exact same rate.
    Fills are converted into cash and positions marked using that rate.
    """
    clock = [datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock, native_currency="USD")

    # Configure runner with explicit assumption USD/TWD = 31.50
    runner.configure(PaperExperimentSettings(
        strategy_id=DYNAMIC_DESK_ID,
        strategy_name="Autonomous Paper Execution Desk",
        enabled=True,
        universe=["AAPL"],
        max_position_notional=500000.0,
        initial_cash=100_000.0,
        base_currency="USD",
        fx_to_reporting=1.0,
        fx_rates={"USD": 31.50},
    ))

    # Apply an actual USD fill: BUY 10 AAPL @ 150.0 USD
    fill = Fill(
        fill_id="fill-usd-cfg-1",
        order_id="order-usd-cfg-1",
        symbol="AAPL",
        currency="USD",
        assumptions={"test_only": True},
        bucket=DecisionScope.SWING,
        side=OrderSide.BUY,
        quantity=10.0,
        fill_price=150.0,
        fee=0.0,
        tax=0.0,
        slippage=0.0,
        timestamp=clock[0],
    )
    pm.apply_fill(fill, DYNAMIC_DESK_ID)

    # Authoritative quote for AAPL @ 160.0 USD
    runner._durable_quotes["AAPL"] = DurableQuoteSnapshot(
        symbol="AAPL",
        market=Market.US,
        source="polygon",
        observed_at=clock[0],
        last_price=160.0,
        quality="good",
        is_stale=False,
        is_synthetic=False,
    )

    ctx_req = runner.build_decision_context_request(symbols=["AAPL"])

    # 1. Decision context uses configured assumption
    assert ctx_req.fx_rates["USD"] == 31.50
    assert ctx_req.fx_accounting["usd_twd_rate"] == 31.50
    assert ctx_req.fx_accounting["source"] == "configured_assumption"
    assert ctx_req.fx_accounting["source_label"] == "configured_assumption"
    assert ctx_req.fx_accounting["is_simulated"] is True
    expected_notional_usd = round(500000.0 / 31.50, 2)
    assert ctx_req.fx_accounting["sizing_guidance"]["max_position_notional_usd"] == expected_notional_usd
    assert ctx_req.tactical_risk_limits["max_position_notional_usd"] == expected_notional_usd

    # 2. Accounting snapshot reconciles using the exact same configured rate
    snap = runner.generate_canonical_team_ops(clock[0])
    assert snap["fx_rates"]["USD"] == 31.50
    assert snap["portfolio"]["fx_accounting"]["usd_twd_rate"] == 31.50
    assert snap["portfolio"]["fx_accounting"]["source"] == "configured_assumption"
    assert snap["portfolio"]["fx_accounting"]["source_label"] == "configured_assumption"
    assert snap["portfolio"]["fx_accounting"]["is_simulated"] is True

    # 3. Assert decision and NAV share the exact same rate/source
    assert snap["portfolio"]["fx_accounting"]["usd_twd_rate"] == ctx_req.fx_accounting["usd_twd_rate"] == 31.50
    assert snap["portfolio"]["fx_accounting"]["source"] == ctx_req.fx_accounting["source"] == "configured_assumption"

    # 4. Native cash debits USD 1,500 (not FX-multiplied); report separately in TWD.
    native_ledger = pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING)
    assert native_ledger.currency == "USD"
    assert native_ledger.cash == 98_500.0
    assert native_ledger.positions["AAPL"].currency == "USD"

    # 5. Reporting NAV converts native cash and market value with labeled FX.
    assert snap["portfolio"]["cash"] == 98_500.0 * 31.50
    assert snap["portfolio"]["equity"] == round((98_500.0 + 1_600.0) * 31.50, 4)
    assert snap["portfolio"]["nav"] == snap["portfolio"]["equity"]


def test_actual_usd_fill_accounting_with_market_rate(tmp_path):
    """Market FX rate test with actual USD fill:
    When authoritative market FX quote exists, both decision and NAV accounting
    share the exact same market rate and provenance.
    """
    clock = [datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock, native_currency="USD")

    obs_time = datetime(2026, 9, 28, 13, 58, tzinfo=timezone.utc)
    runner._durable_quotes["USDTWD=X"] = DurableQuoteSnapshot(
        symbol="USDTWD=X",
        market=Market.US,
        source="taifex_official_fx",
        observed_at=obs_time,
        bar_time=obs_time,
        last_price=31.875,
        quality="good",
        is_stale=False,
        is_synthetic=False,
    )

    runner.configure(PaperExperimentSettings(
        strategy_id=DYNAMIC_DESK_ID,
        strategy_name="Autonomous Paper Execution Desk",
        enabled=True,
        universe=["AAPL"],
        max_position_notional=500000.0,
        initial_cash=100_000.0,
        base_currency="USD",
        fx_to_reporting=1.0,
    ))

    # Apply an actual USD fill: BUY 10 AAPL @ 150.0 USD
    fill = Fill(
        fill_id="fill-usd-mkt-1",
        order_id="order-usd-mkt-1",
        symbol="AAPL",
        currency="USD",
        assumptions={"test_only": True},
        bucket=DecisionScope.SWING,
        side=OrderSide.BUY,
        quantity=10.0,
        fill_price=150.0,
        fee=0.0,
        tax=0.0,
        slippage=0.0,
        timestamp=clock[0],
    )
    pm.apply_fill(fill, DYNAMIC_DESK_ID)

    runner._durable_quotes["AAPL"] = DurableQuoteSnapshot(
        symbol="AAPL",
        market=Market.US,
        source="polygon",
        observed_at=clock[0],
        last_price=160.0,
        quality="good",
        is_stale=False,
        is_synthetic=False,
    )

    ctx_req = runner.build_decision_context_request(symbols=["AAPL"])

    # 1. Decision context uses market rate
    assert ctx_req.fx_rates["USD"] == 31.875
    assert ctx_req.fx_accounting["usd_twd_rate"] == 31.875
    assert ctx_req.fx_accounting["source"] == "taifex_official_fx"
    assert ctx_req.fx_accounting["source_label"] == "market"
    assert ctx_req.fx_accounting["is_simulated"] is False
    expected_notional_usd = round(500000.0 / 31.875, 2)
    assert ctx_req.fx_accounting["sizing_guidance"]["max_position_notional_usd"] == expected_notional_usd

    # 2. Accounting snapshot reconciles using the exact same market rate
    snap = runner.generate_canonical_team_ops(clock[0])
    assert snap["fx_rates"]["USD"] == 31.875
    assert snap["portfolio"]["fx_accounting"]["usd_twd_rate"] == 31.875
    assert snap["portfolio"]["fx_accounting"]["source"] == "taifex_official_fx"
    assert snap["portfolio"]["fx_accounting"]["source_label"] == "market"
    assert snap["portfolio"]["fx_accounting"]["is_simulated"] is False

    # 3. Assert decision and NAV share the exact same rate/source
    assert snap["portfolio"]["fx_accounting"]["usd_twd_rate"] == ctx_req.fx_accounting["usd_twd_rate"] == 31.875
    assert snap["portfolio"]["fx_accounting"]["source"] == ctx_req.fx_accounting["source"] == "taifex_official_fx"

    # 4. Native cash debits USD 1,500; reporting conversion is separate.
    native_ledger = pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING)
    assert native_ledger.currency == "USD"
    assert native_ledger.cash == 98_500.0
    assert native_ledger.positions["AAPL"].currency == "USD"

    # 5. Reporting NAV converts native amounts using observed market FX.
    assert snap["portfolio"]["cash"] == 98_500.0 * 31.875
    assert snap["portfolio"]["equity"] == round((98_500.0 + 1_600.0) * 31.875, 4)
    assert snap["portfolio"]["nav"] == snap["portfolio"]["equity"]


def test_actual_twd_fill_accounting_remains_valid_when_usd_fx_missing(tmp_path):
    """TWD-only cash remains valid test:
    When TWD fills exist, cash accounting and NAV evaluation remain valid
    even when USD FX is completely missing.
    """
    clock = [datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock)

    runner.configure(PaperExperimentSettings(
        strategy_id=DYNAMIC_DESK_ID,
        strategy_name="Autonomous Paper Execution Desk",
        enabled=True,
        universe=["2330.TW"],
        max_position_notional=500000.0,
        initial_cash=TEAM_INITIAL_CAPITAL_TWD,
        base_currency="TWD",
        fx_to_reporting=1.0,
    ))

    # Apply a TWD fill: BUY 10 2330.TW @ 1000.0 TWD
    fill = Fill(
        fill_id="fill-twd-1",
        order_id="order-twd-1",
        symbol="2330.TW",
        bucket=DecisionScope.SWING,
        side=OrderSide.BUY,
        quantity=10.0,
        fill_price=1000.0,
        fee=0.0,
        tax=0.0,
        slippage=0.0,
        timestamp=clock[0],
    )
    pm.apply_fill(fill, DYNAMIC_DESK_ID)

    runner._durable_quotes["2330.TW"] = DurableQuoteSnapshot(
        symbol="2330.TW",
        market=Market.TW,
        source="twse",
        observed_at=clock[0],
        last_price=1050.0,
        quality="good",
        is_stale=False,
        is_synthetic=False,
    )

    ctx_req = runner.build_decision_context_request(symbols=["2330.TW", "AAPL"])

    # 1. Decision context: TWD instrument has no gap, USD instrument AAPL has sizing gap
    twd_gaps = [g for g in ctx_req.sizing_gaps if g["symbol"] == "2330.TW"]
    assert len(twd_gaps) == 0
    usd_gaps = [g for g in ctx_req.sizing_gaps if g["symbol"] == "AAPL"]
    assert len(usd_gaps) == 1
    assert usd_gaps[0]["gap_status"] == "EXPLICIT_SIZING_GAP"
    assert ctx_req.fx_accounting["sizing_guidance"]["max_position_notional_twd"] == 500000.0

    # 2. Accounting snapshot: TWD-only cash remains valid and NAV evaluates to OK
    snap = runner.generate_canonical_team_ops(clock[0])
    expected_cash = round(TEAM_INITIAL_CAPITAL_TWD - (10.0 * 1000.0), 4)
    assert snap["portfolio"]["cash"] == expected_cash
    assert snap["portfolio"]["nav_status"] == "OK"
    assert snap["portfolio"]["unrealized_pnl"] == round(10.0 * (1050.0 - 1000.0), 4)
    expected_equity = round(expected_cash + (10.0 * 1050.0), 4)
    assert snap["portfolio"]["equity"] == expected_equity
    assert snap["portfolio"]["nav"] == expected_equity



