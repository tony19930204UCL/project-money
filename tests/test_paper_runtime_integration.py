"""Isolated paper runtime integration harness using labeled fixtures.

Verifies:
1. Complete paper lifecycle:
   decision -> paper pending -> paper fill -> single ledger NAV -> outcome attribution -> lesson retrieval in next decision.
2. Invariant: No hindsight or same-bar fills (quotes at or before bar timestamp leave order pending).
3. Invariant: No experiment clock or baseline reset across cycles or state restarts.
4. Invariant: Single canonical TWD cash pool shared across holding horizons (SWING and INTRADAY).
5. Invariant: Fail closed on unconfigured or unsafe model (hermes-3-llama-3.1-8b prohibited from masquerading as Main CIO).
"""
from __future__ import annotations

from tests.fixture_next_quote import submit_after_new_fixture_quote

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import pytest

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
    UNSAFE_HERMES_DEFAULT_MODEL,
    HermesCIODecisionExecutor,
    get_hermes_runtime_contract,
    run_hermes_cli_chat,
)
from cio_market_lab.integrations.runtime_evidence import (
    RuntimeEvidenceAdapter,
    RuntimeEvidenceRecord,
    RuntimeEvidenceStreamJsonEmitter,
    EVIDENCE_STRENGTH_LOCAL_RUNTIME,
    ALLOWLISTED_SOURCE_FIELDS,
)
from tests.hermes_test_doubles import (
    ExplicitFakeAgent,
    dummy_packet_dict,
    mock_injected_transport_for_test,
    failing_injected_transport_for_test,
)


class ControllableMarketAdapter(MarketDataAdapter):
    """Adapter with deterministic control over bar timestamps, quote timestamps, freshness, and quality."""

    def __init__(self, current_time: datetime):
        self.current_time = current_time
        self._bars: Dict[str, List[Bar]] = {}
        self._quotes: Dict[str, Quote] = {}

    @property
    def source_name(self) -> str:
        return "controllable_paper_fixture_adapter"

    def set_bar(self, symbol: str, bar_time: datetime, open_p: float, high_p: float, low_p: float, close_p: float, volume: float = 1000):
        bar = Bar(
            symbol=symbol,
            timestamp=bar_time,
            observed_at=bar_time,
            open=open_p,
            high=high_p,
            low=low_p,
            close=close_p,
            volume=volume,
            source="fixture_authoritative",
            quality="good",
            is_stale=False,
        )
        self._bars[symbol] = [bar]

    def set_quote(
        self,
        symbol: str,
        quote_time: datetime,
        price: float,
        is_stale: bool = False,
        is_synthetic: bool = False,
        source: str = "fixture_authoritative",
        quality: str = "good",
    ):
        self._quotes[symbol] = Quote(bid_size=1000, ask_size=1000,
            quote_id=f"TEST_ONLY_{symbol}_{(quote_time).isoformat()}",
            session="ODD_LOT" if (symbol).endswith(".TW") else "REGULAR",
            source_capabilities={"source": source, "two_sided_book": True,
                "size_backed": True, "exchange_session_attested": True,
                "entitlement_evidence_id": "TEST_ONLY_FIXTURE_ODD_LOT",
                "entitlement_status": "TEST_ONLY", "odd_lot_book": True,
                "supported_sessions": ["REGULAR", "ODD_LOT"]},
            
            symbol=symbol,
            timestamp=quote_time,
            observed_at=quote_time,
            last_price=price,
            bid=price - 0.5,
            ask=price + 0.5,
            is_stale=is_stale,
            is_synthetic=is_synthetic,
            source=source,
            quality=quality,
            delay_seconds=0.0,
        )

    def get_bars(self, symbol: str, start=None, end=None, timeframe="1D", limit=None) -> List[Bar]:
        return self._bars.get(symbol, [])

    def get_latest_bar(self, symbol: str) -> Optional[Bar]:
        bars = self._bars.get(symbol, [])
        return bars[-1] if bars else None

    def get_latest_quote(self, symbol: str) -> Optional[Quote]:
        return self._quotes.get(symbol)

    def stream_bars(self, symbols: List[str]):
        for s in symbols:
            yield self.get_latest_bar(s)


def build_harness_environment(tmp_path: Path, clock_ref: List[datetime]) -> Tuple[AutonomousPaperRunner, PaperOrderService, PortfolioManager, ControllableMarketAdapter]:
    os.environ["CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS"] = "1"
    pm = PortfolioManager(
        initial_cash_swing=TEAM_INITIAL_CAPITAL_TWD,
        initial_cash_intraday=TEAM_INITIAL_CAPITAL_TWD,
    )
    pm.register_strategy(DYNAMIC_DESK_ID, TEAM_INITIAL_CAPITAL_TWD, unified_cash=True)
    event_store = EventStore(":memory:")
    orders = PaperOrderService(pm, event_store)
    adapter = ControllableMarketAdapter(clock_ref[0])

    runner = AutonomousPaperRunner(
        tmp_path,
        pm,
        orders,
        adapter,
        now_fn=lambda: clock_ref[0],
        team_initial_capital=TEAM_INITIAL_CAPITAL_TWD,
        require_cio_provider=True,
    )

    runner.allow_fixture_quotes = True  # Explicit isolated runtime only.
    settings = PaperExperimentSettings(
        strategy_id=DYNAMIC_DESK_ID,
        strategy_name="Dynamic Desk Harness",
        enabled=True,
        mode=DecisionScope.SWING,
        universe=["2330.TW"],
        base_currency="TWD",
        initial_cash=TEAM_INITIAL_CAPITAL_TWD,
        max_position_notional=500000.0,
        max_daily_loss=50000.0,
    )
    runner.configure(settings)
    return runner, orders, pm, adapter


# ============================================================================
# 1. Full E2E Chain: Decision -> Pending -> Fill -> Single Ledger NAV -> Attribution -> Lesson
# ============================================================================

def test_isolated_paper_integration_lifecycle_with_labeled_fixtures(tmp_path):
    t0 = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)  # Monday 10:00 AM TST
    clock = [t0]
    runner, orders, pm, adapter = build_harness_environment(tmp_path, clock)

    symbol = "2330.TW"
    bar_price = 1000.0
    adapter.set_bar(symbol, t0, 995.0, 1005.0, 990.0, bar_price)

    # 1. Same-bar quote at t0 -> Must result in BUY_PENDING (no same-bar fill, no hindsight fill)
    adapter.set_quote(symbol, t0, 1000.0)

    packet1 = CIODecisionPacket(
        case_id="cio-harness-case-pending-001",
        as_of=t0,
        evidence=["research://semi-cycle-expansion", "quote://2330.TW-1000"],
        thesis="TSMC capacity expansion entering high utilization; initiate paper swing accumulation.",
        selected_instrument=symbol,
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=30.0,
        conditions={**({"max_slippage_bps": 20}), "allow_odd_lot": True},
        risk_assessment={"downside_buffer": 0.05, "thesis_invalidation": "close < 950"},
        alternatives_considered=[{"symbol": "2317.TW", "reason": "Lower beta, lower operating leverage"}],
        expiry=t0 + timedelta(hours=8),
        confidence=0.85,
        strategy_version="dynamic-desk-harness-v1",
        provenance=CIOProvenance(
            authority="MAIN_CIO",
            actor_role="CHIEF_INVESTMENT_OFFICER",
            signer_id="main-cio-key",
            source="external_packet",
        ),
        is_fixture=True,
    )
    sign_cio_packet(packet1, signer_id="main-cio-key")

    # Step A: Submit decision with same-bar quote
    d1 = runner.submit_cio_packet(packet1)
    assert d1.action == "BUY_PENDING"
    assert d1.terminal_status == "NON_TERMINAL"
    assert d1.inputs["order_status"] == "PENDING"
    assert d1.inputs["fill_pending_reason"] == "WAITING_FOR_LATER_AUTHORITATIVE_QUOTE"
    assert d1.order_id is not None

    # Verify no fills applied yet
    swing_ledger = pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING)
    intra_ledger = pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.INTRADAY)
    assert len(swing_ledger.fills) == 0
    assert swing_ledger.cash == TEAM_INITIAL_CAPITAL_TWD
    assert intra_ledger.cash == TEAM_INITIAL_CAPITAL_TWD

    # Step B: Authoritative quote arrives at t1 > t0 (e.g. 5 minutes later)
    t1 = t0 + timedelta(minutes=5)
    clock[0] = t1
    later_price = 1002.0
    adapter.set_quote(symbol, t1, later_price)

    # Submit decision with authoritative later quote
    packet2 = CIODecisionPacket(
        case_id="cio-harness-case-filled-002",
        as_of=t1,
        evidence=["research://semi-cycle-expansion", "quote://2330.TW-1002"],
        thesis="TSMC breakout confirmed with later quote; execute buy.",
        selected_instrument=symbol,
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=30.0,
        conditions={**({"max_slippage_bps": 20}), "allow_odd_lot": True},
        risk_assessment={"downside_buffer": 0.05, "thesis_invalidation": "close < 950"},
        alternatives_considered=[{"symbol": "2317.TW", "reason": "Lower beta"}],
        expiry=t1 + timedelta(hours=8),
        confidence=0.88,
        strategy_version="dynamic-desk-harness-v1",
        provenance=CIOProvenance(
            authority="MAIN_CIO",
            actor_role="CHIEF_INVESTMENT_OFFICER",
            signer_id="main-cio-key",
            source="external_packet",
        ),
        is_fixture=True,
    )
    sign_cio_packet(packet2, signer_id="main-cio-key")

    orders.cancel(d1.order_id)  # retire unfilled first case before the replacement case
    d2 = submit_after_new_fixture_quote(runner, clock, packet2)
    assert d2.action == "BUY_FILLED"
    assert d2.terminal_status == "TERMINAL_FILLED"
    assert d2.price is not None
    assert d2.price >= later_price  # Slippage accounted for

    # Step C: Single ledger NAV verification
    reconciled_cash = pm.reconcile_canonical_cash(DYNAMIC_DESK_ID)
    assert swing_ledger.cash == intra_ledger.cash
    assert abs(reconciled_cash - swing_ledger.cash) < 0.01
    assert swing_ledger.cash < TEAM_INITIAL_CAPITAL_TWD

    # Check positions and NAV calculation
    assert symbol in swing_ledger.positions
    pos = swing_ledger.positions[symbol]
    assert pos.quantity == 30.0
    snap = runner.generate_canonical_team_ops(t1)
    nav = snap["portfolio"]["equity"]
    assert nav is not None and nav > 0
    # Single cash pool invariant: cash + position value equals total nav
    expected_nav = swing_ledger.cash + pos.quantity * later_price
    assert abs(nav - expected_nav) < 500.0  # within slippage & transaction costs

    # Step D: Outcome Attribution
    t2 = t1 + timedelta(days=2)
    clock[0] = t2
    exit_price = 1040.0
    realized_gain = (exit_price - d2.price) * pos.quantity
    outcome_dict = {
        "exit_price": exit_price,
        "realized_pnl": round(realized_gain, 2),
        "return_pct": round((exit_price / d2.price - 1.0) * 100.0, 2),
        "holding_period_hours": 48.0,
    }
    attribution_dict = {
        "alpha_bps": 250.0,
        "market_beta_bps": 120.0,
        "thesis_validation": "Capacity expansion thesis validated by earnings update",
    }
    lesson_record = runner.learning_store.record_outcome(
        case_id=packet2.case_id,
        outcome_dict=outcome_dict,
        attribution_dict=attribution_dict,
        lessons=["Leading-edge semi expansion accumulation achieved positive alpha after 48h holding."],
    )
    assert lesson_record is not None
    assert lesson_record.case_id == packet2.case_id

    # Step E: Lesson retrieval in subsequent decision context
    t3 = t2 + timedelta(hours=1)
    clock[0] = t3
    ctx_req = runner.build_decision_context_request(symbols=[symbol])
    assert len(ctx_req.prior_lessons) >= 1
    found_lesson = next((l for l in ctx_req.prior_lessons if l["case_id"] == packet2.case_id), None)
    assert found_lesson is not None
    assert "positive alpha after 48h holding" in found_lesson["takeaway"]
    assert len(ctx_req.past_outcomes) >= 1
    assert ctx_req.past_outcomes[0]["case_id"] == packet2.case_id
    assert ctx_req.past_outcomes[0]["outcome"]["realized_pnl"] == round(realized_gain, 2)


# ============================================================================
# 2. Invariant: No Experiment Clock or Baseline Reset Across Restarts
# ============================================================================

def test_experiment_clock_and_baseline_immutability_on_restart(tmp_path):
    t0 = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)
    clock = [t0]
    runner, orders, pm, adapter = build_harness_environment(tmp_path, clock)

    original_settings = runner.paper_orders.experiment_for(DYNAMIC_DESK_ID)
    original_initial_cash = original_settings.initial_cash

    # Run cycle and apply a fill
    symbol = "2330.TW"
    t1 = t0 + timedelta(minutes=10)
    adapter.set_bar(symbol, t1, 990.0, 1000.0, 985.0, 1000.0)
    t_quote = t1 + timedelta(seconds=10)
    clock[0] = t_quote
    adapter.set_quote(symbol, t_quote, 1001.0)

    packet = CIODecisionPacket(
        case_id="cio-immutability-001",
        as_of=t1,
        thesis="Immutability check",
        selected_instrument=symbol,
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=10.0,
        expiry=t1 + timedelta(hours=4),
        provenance=CIOProvenance(authority="MAIN_CIO"),
        conditions={"allow_odd_lot": True}, is_fixture=True,
    )
    sign_cio_packet(packet)
    d = submit_after_new_fixture_quote(runner, clock, packet)
    assert d.action == "BUY_FILLED", f"Expected BUY_FILLED but got {d.action}: {d.reason}"

    cash_before_restart = pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING).cash

    # Simulate restart by instantiating new runner pointing to same runtime_dir
    clock[0] = t1 + timedelta(hours=2)
    new_pm = PortfolioManager(
        initial_cash_swing=TEAM_INITIAL_CAPITAL_TWD,
        initial_cash_intraday=TEAM_INITIAL_CAPITAL_TWD,
    )
    new_orders = PaperOrderService(new_pm, EventStore(":memory:"))
    new_runner = AutonomousPaperRunner(
        tmp_path,
        new_pm,
        new_orders,
        adapter,
        now_fn=lambda: clock[0],
        team_initial_capital=TEAM_INITIAL_CAPITAL_TWD,
        require_cio_provider=True,
    )

    # Verify settings immutability
    reloaded_settings = new_orders.experiment_for(DYNAMIC_DESK_ID)
    assert reloaded_settings.initial_cash == original_initial_cash

    # Verify cash balance and fills not double debited
    new_cash = new_pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING).cash
    assert abs(new_cash - cash_before_restart) < 0.01

    # Verify idempotency: duplicate packet re-submission is rejected
    d_dup = new_runner.submit_cio_packet(packet)
    assert d_dup.action == "NO_TRADE"
    assert "DUPLICATE_CASE_ID" in d_dup.reason


# ============================================================================
# 3. Invariant: Fail Closed on Unconfigured or Masquerading Hermes Model
# ============================================================================

def test_hermes_bridge_fails_closed_without_masquerading(monkeypatch):
    # 1. Default unsafe model hermes-3-llama-3.1-8b is prohibited
    monkeypatch.setenv("CIO_MODEL_ID", UNSAFE_HERMES_DEFAULT_MODEL)
    executor_unsafe = HermesCIODecisionExecutor(model_id=UNSAFE_HERMES_DEFAULT_MODEL)
    assert executor_unsafe.is_available() is False
    with pytest.raises(RuntimeError) as exc_info:
        executor_unsafe.request_decision(
            CIODecisionContextRequest(request_id="req-test", timestamp=datetime.now(timezone.utc))
        )
    assert "must not masquerade as Main CIO" in str(exc_info.value)

    # 2. Unconfigured / empty model fails closed
    monkeypatch.setenv("CIO_MODEL_ID", "")
    executor_empty = HermesCIODecisionExecutor(model_id="")
    assert executor_empty.is_available() is False
    with pytest.raises(RuntimeError) as exc_empty:
        executor_empty.request_decision(
            CIODecisionContextRequest(request_id="req-empty", timestamp=datetime.now(timezone.utc))
        )
    assert "Unsafe or unconfigured CIO model" in str(exc_empty.value)

    # 3. Direct run_hermes_cli_chat raises ValueError on unsafe default model
    with pytest.raises(ValueError) as exc_chat:
        run_hermes_cli_chat("hello", model=UNSAFE_HERMES_DEFAULT_MODEL)
    assert "must not masquerade as Main CIO" in str(exc_chat.value)


# ============================================================================
# 4. Hermes Provenance Readback Verification & Isolated Paper Mandate Tests
# ============================================================================

def _dummy_valid_cio_packet_dict() -> Dict[str, Any]:
    return {
        "case_id": "case-provenance-test-001",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "evidence": ["fixture://provenance-test"],
        "thesis": "Positive readback verification test case.",
        "selected_instrument": "2330.TW",
        "action": "BUY",
        "holding_horizon": "swing",
        "quantity": 10.0,
        "conditions": {},
        "risk_assessment": {},
        "alternatives_considered": [],
        "expiry": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
        "confidence": 0.9,
        "strategy_version": "test-v1",
        "provenance": {
            "authority": "MAIN_CIO",
            "actor_role": "CHIEF_INVESTMENT_OFFICER",
            "signer_id": "hermes-bridge-cio",
            "source": "hermes_bridge",
        },
        "is_fixture": True,
    }


def test_hermes_readback_fails_on_provider_mismatch(monkeypatch):
    """Negative test: fail closed on inference provider mismatch."""
    executor = HermesCIODecisionExecutor(provider_id="openai-codex", model_id="gpt-6-astra")

    def mock_run_chat(message, **kwargs):
        return {
            "transport": "hermes_cli",
            "transport_identifier": "hermes-cli-cio-bridge",
            "status": "completed",
            "response": json.dumps(_dummy_valid_cio_packet_dict()),
            "session_id": "test-session",
            "runtime_metadata": {
                "resolved_provider": "anthropic",  # Mismatch!
                "resolved_model": "gpt-6-astra",
            },
        }

    monkeypatch.setattr("cio_market_lab.integrations.hermes_chat.run_hermes_cli_chat", mock_run_chat)
    ctx = CIODecisionContextRequest(request_id="req-mismatch", timestamp=datetime.now(timezone.utc))

    with pytest.raises(RuntimeError) as exc_info:
        executor.request_decision(ctx)
    assert "Provider mismatch" in str(exc_info.value)
    assert executor.last_receipt is None, "No verified receipt may be claimed on provider mismatch"


def test_hermes_readback_fails_on_transport_masquerading_as_provider(monkeypatch):
    """Negative test: transport identifier must not masquerade as actual inference provider."""
    executor = HermesCIODecisionExecutor(provider_id="openai-codex", model_id="gpt-6-astra")

    def mock_run_chat(message, **kwargs):
        return {
            "transport": "hermes_cli",
            "transport_identifier": "hermes-cli-cio-bridge",
            "status": "completed",
            "response": json.dumps(_dummy_valid_cio_packet_dict()),
            "session_id": "test-session",
            "runtime_metadata": {
                "resolved_provider": "hermes-cli-cio-bridge",  # Transport masquerading!
                "resolved_model": "gpt-6-astra",
            },
        }

    monkeypatch.setattr("cio_market_lab.integrations.hermes_chat.run_hermes_cli_chat", mock_run_chat)
    ctx = CIODecisionContextRequest(request_id="req-transport", timestamp=datetime.now(timezone.utc))

    with pytest.raises(RuntimeError) as exc_info:
        executor.request_decision(ctx)
    assert "cannot masquerade as actual inference provider" in str(exc_info.value)
    assert executor.last_receipt is None


def test_hermes_readback_fails_on_missing_resolved_model(monkeypatch):
    """Negative test: fail closed on missing resolved model in runtime metadata."""
    executor = HermesCIODecisionExecutor(provider_id="openai-codex", model_id="gpt-6-astra")

    def mock_run_chat(message, **kwargs):
        return {
            "transport": "hermes_cli",
            "transport_identifier": "hermes-cli-cio-bridge",
            "status": "completed",
            "response": json.dumps(_dummy_valid_cio_packet_dict()),
            "session_id": "test-session",
            "runtime_metadata": {
                "resolved_provider": "openai-codex",
                "resolved_model": "",  # Missing!
            },
        }

    monkeypatch.setattr("cio_market_lab.integrations.hermes_chat.run_hermes_cli_chat", mock_run_chat)
    ctx = CIODecisionContextRequest(request_id="req-no-model", timestamp=datetime.now(timezone.utc))

    with pytest.raises(RuntimeError) as exc_info:
        executor.request_decision(ctx)
    assert "Missing resolved model" in str(exc_info.value)
    assert executor.last_receipt is None


def test_hermes_readback_fails_on_missing_resolved_provider(monkeypatch):
    """Negative test: fail closed on missing resolved provider in runtime metadata."""
    executor = HermesCIODecisionExecutor(provider_id="openai-codex", model_id="gpt-6-astra")

    def mock_run_chat(message, **kwargs):
        return {
            "transport": "hermes_cli",
            "transport_identifier": "hermes-cli-cio-bridge",
            "status": "completed",
            "response": json.dumps(_dummy_valid_cio_packet_dict()),
            "session_id": "test-session",
            "runtime_metadata": {
                "resolved_model": "gpt-6-astra",
                # missing resolved_provider!
            },
        }

    monkeypatch.setattr("cio_market_lab.integrations.hermes_chat.run_hermes_cli_chat", mock_run_chat)
    ctx = CIODecisionContextRequest(request_id="req-no-provider", timestamp=datetime.now(timezone.utc))

    with pytest.raises(RuntimeError) as exc_info:
        executor.request_decision(ctx)
    assert "Missing resolved provider" in str(exc_info.value)
    assert executor.last_receipt is None


def test_hermes_readback_fails_on_malformed_metadata(monkeypatch):
    """Negative test: fail closed on malformed runtime metadata."""
    executor = HermesCIODecisionExecutor(provider_id="openai-codex", model_id="gpt-6-astra")

    def mock_run_chat(message, **kwargs):
        return {
            "transport": "hermes_cli",
            "transport_identifier": "hermes-cli-cio-bridge",
            "status": "completed",
            "response": json.dumps(_dummy_valid_cio_packet_dict()),
            "session_id": "test-session",
            "runtime_metadata": "not-a-dict",  # Corrupt metadata!
        }

    monkeypatch.setattr("cio_market_lab.integrations.hermes_chat.run_hermes_cli_chat", mock_run_chat)
    ctx = CIODecisionContextRequest(request_id="req-corrupt", timestamp=datetime.now(timezone.utc))

    with pytest.raises(RuntimeError) as exc_info:
        executor.request_decision(ctx)
    assert "MALFORMED_RUNTIME_METADATA" in str(exc_info.value)
    assert executor.last_receipt is None


def test_hermes_readback_fails_on_absent_evidence(monkeypatch):
    """Negative test: no verification claim without evidence."""
    executor = HermesCIODecisionExecutor(provider_id="openai-codex", model_id="gpt-6-astra")

    def mock_run_chat(message, **kwargs):
        return {
            "transport": "hermes_cli",
            "transport_identifier": "hermes-cli-cio-bridge",
            "status": "completed",
            "response": json.dumps(_dummy_valid_cio_packet_dict()),
            "session_id": "test-session",
            "runtime_metadata": {},  # Completely empty evidence!
        }

    monkeypatch.setattr("cio_market_lab.integrations.hermes_chat.run_hermes_cli_chat", mock_run_chat)
    ctx = CIODecisionContextRequest(request_id="req-no-evidence", timestamp=datetime.now(timezone.utc))

    with pytest.raises(RuntimeError) as exc_info:
        executor.request_decision(ctx)
    assert "READBACK_VERIFICATION_FAILED" in str(exc_info.value)
    assert executor.last_receipt is None


def test_hermes_readback_success_with_verified_receipt(monkeypatch):
    """Positive test: authenticated runtime readback succeeds and records verified receipt."""
    executor = HermesCIODecisionExecutor(provider_id="openai-codex", model_id="gpt-6-astra")

    captured_kwargs = {}

    def mock_run_chat(message, **kwargs):
        captured_kwargs.update(kwargs)
        captured_kwargs["message"] = message
        return {
            "transport": "hermes_cli",
            "transport_identifier": "hermes-cli-cio-bridge",
            "status": "completed",
            "response": json.dumps(_dummy_valid_cio_packet_dict()),
            "session_id": "test-session",
            "runtime_metadata": {
                "resolved_provider": "openai-codex",
                "resolved_model": "gpt-6-astra",
            },
        }

    monkeypatch.setattr("cio_market_lab.integrations.hermes_chat.run_hermes_cli_chat", mock_run_chat)
    ctx = CIODecisionContextRequest(request_id="req-success", timestamp=datetime.now(timezone.utc))

    packet = executor.request_decision(ctx)
    assert packet is not None
    # Case identity is owned by the authenticated request/session, not model text.
    import hashlib
    expected_case = 'case-' + hashlib.sha256((executor.session_id + '\0' + ctx.request_id).encode()).hexdigest()[:24]
    assert packet.case_id == expected_case
    assert packet.case_id != "case-provenance-test-001"
    assert packet.provenance.authority == "MAIN_CIO"
    assert packet.provenance.signer_id == "hermes-bridge-cio"
    assert packet.provenance.receipt_id is not None

    # Verify captured prompt contains isolated Paper-Only mandate
    prompt_text = captured_kwargs["message"]
    assert "PROJECT MONEY PAPER-ONLY MANDATE" in prompt_text
    assert "Autonomous one-month terminal NAV growth on isolated paper simulation ledger" in prompt_text
    assert "NO client real holdings" in prompt_text
    assert "NO broker connection" in prompt_text
    assert "NO real money" in prompt_text
    assert "Antigravity (AGY) engineering only" in prompt_text
    assert "NOT a live-account mandate" in prompt_text

    # Verify pin propagation
    assert captured_kwargs["provider"] == "openai-codex"
    assert captured_kwargs["model"] == "gpt-6-astra"
    assert captured_kwargs["enforce_cio_pin"] is True

    # Verify execution receipt
    receipt = executor.last_receipt
    assert receipt is not None
    assert receipt.receipt_id == packet.provenance.receipt_id
    assert receipt.readback_verified is True
    assert receipt.provider_id == "openai-codex"
    assert receipt.model_id == "gpt-6-astra"
    assert receipt.transport == "hermes-cli-cio-bridge"
    assert receipt.requested_provider == "openai-codex"
    assert receipt.requested_model == "gpt-6-astra"


def _mock_hermes_popen(monkeypatch, captured_command, stdout_jsonl):
    class FakeChild:
        returncode = 0

        def communicate(self, input=None, timeout=None):
            assert input
            assert timeout == 240
            return stdout_jsonl, ""

    def fake_popen(cmd, **kwargs):
        captured_command.extend(cmd)
        assert kwargs["start_new_session"] is True
        return FakeChild()

    monkeypatch.setattr("cio_market_lab.integrations.hermes_chat.subprocess.Popen", fake_popen)


def test_cli_pin_flag_propagation_and_stream_json(monkeypatch):
    """Verify that run_hermes_cli_chat constructs CLI invocation with --provider and --model flags."""
    captured_command = []
    stdout_jsonl = (
        json.dumps({"type": "system", "subtype": "init", "model": "gpt-6-astra", "session_id": "test-session"}) + "\n"
        + json.dumps({"type": "result", "session_id": "test-session", "text": "OK"}) + "\n"
    )
    monkeypatch.setattr("cio_market_lab.integrations.hermes_chat._hermes_executable", lambda: "/mock/bin/hermes")
    _mock_hermes_popen(monkeypatch, captured_command, stdout_jsonl)

    res = run_hermes_cli_chat(
        "test query",
        session_id="test-session",
        provider="openai-codex",
        model="gpt-6-astra",
        enforce_cio_pin=True,
        escalation_reason="high_consequence",
    )
    assert "--provider" in captured_command
    idx_p = captured_command.index("--provider")
    assert captured_command[idx_p + 1] == "openai-codex"

    assert "--model" in captured_command
    idx_m = captured_command.index("--model")
    assert captured_command[idx_m + 1] == "gpt-6-astra"

    assert "--format" in captured_command
    idx_f = captured_command.index("--format")
    assert captured_command[idx_f + 1] == "stream-json"

    # Enforce packet-only reasoning flags: zero tools, no personal memories/context
    assert "--toolsets" in captured_command
    idx_t = captured_command.index("--toolsets")
    assert captured_command[idx_t + 1] == "none"

    assert "--ignore-rules" in captured_command
    assert "--oneshot" in captured_command
    assert "--session-id" in captured_command
    idx_s = captured_command.index("--session-id")
    assert captured_command[idx_s + 1] == "test-session"
    assert "--continue" not in captured_command
    assert "--create-if-missing" not in captured_command

    assert res["status"] == "completed"
    assert res["response"] == "OK"
    assert res["resolved_model"] == "gpt-6-astra"


def test_general_chat_compatibility(monkeypatch):
    """Verify compatible general chat bridge behavior without mandatory CIO pin enforcement."""
    captured_command = []
    stdout_jsonl = json.dumps({"type": "result", "session_id": "general-session", "text": "Hello user!"}) + "\n"
    monkeypatch.setattr("cio_market_lab.integrations.hermes_chat._hermes_executable", lambda: "/mock/bin/hermes")
    _mock_hermes_popen(monkeypatch, captured_command, stdout_jsonl)

    # General chat without provider/model override and enforce_cio_pin=False
    res = run_hermes_cli_chat("hello", session_id="general-session")
    assert "--provider" not in captured_command
    assert "--model" not in captured_command
    assert res["status"] == "completed"
    assert res["response"] == "Hello user!"


def test_negative_pre_auth_init_rejected():
    """Negative test: pre-auth init events emitted before authentication cannot serve as runtime evidence."""
    adapter = RuntimeEvidenceAdapter(pinned_provider="openai-codex", pinned_model="gpt-6-astra")

    # 1. Direct verify rejection when event is pre-auth init
    pre_auth_metadata = {
        "type": "system",
        "subtype": "init",
        "model": "gpt-6-astra",
        "is_pre_auth_init": True,
    }
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(pre_auth_metadata, response_text="some response")
    assert "READBACK_VERIFICATION_FAILED" in str(exc_info.value)
    assert "Pre-auth init" in str(exc_info.value)

    # 2. Rejection via HermesCIODecisionExecutor
    executor = HermesCIODecisionExecutor(provider_id="openai-codex", model_id="gpt-6-astra")
    ctx = CIODecisionContextRequest(request_id="req-pre-auth", timestamp=datetime.now(timezone.utc))

    def mock_run_chat(message, **kwargs):
        return {
            "transport": "hermes_cli",
            "transport_identifier": "hermes-cli-cio-bridge",
            "status": "completed",
            "response": json.dumps(_dummy_valid_cio_packet_dict()),
            "session_id": "test-session",
            "runtime_metadata": pre_auth_metadata,
        }

    mp = pytest.MonkeyPatch()
    mp.setattr("cio_market_lab.integrations.hermes_chat.run_hermes_cli_chat", mock_run_chat)
    try:
        with pytest.raises(RuntimeError) as exc_info:
            executor.request_decision(ctx)
        assert "READBACK_VERIFICATION_FAILED" in str(exc_info.value)
        assert executor.last_receipt is None
    finally:
        mp.undo()


def test_negative_prefix_inferred_identity_rejected(monkeypatch):
    """Negative test: provider identity inferred from model prefix (e.g. 'openai-codex/gpt-6-astra') is rejected."""
    adapter = RuntimeEvidenceAdapter(pinned_provider="openai-codex", pinned_model="gpt-6-astra")

    # Prefix-inferred metadata explicitly rejected
    prefix_metadata = {
        "resolved_model": "gpt-6-astra",
        "prefix_inferred": True,
    }
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(prefix_metadata, response_text="test")
    assert "Prefix-inferred identity rejected" in str(exc_info.value)

    # Missing provider where model carries prefix must NOT be split into provider
    executor = HermesCIODecisionExecutor(provider_id="openai-codex", model_id="gpt-6-astra")
    ctx = CIODecisionContextRequest(request_id="req-prefix", timestamp=datetime.now(timezone.utc))

    def mock_run_chat(message, **kwargs):
        return {
            "transport": "hermes_cli",
            "transport_identifier": "hermes-cli-cio-bridge",
            "status": "completed",
            "response": json.dumps(_dummy_valid_cio_packet_dict()),
            "session_id": "test-session",
            "runtime_metadata": {
                "resolved_model": "openai-codex/gpt-6-astra",
                # Note: No resolved_provider! Old bridge would have split and inferred "openai-codex".
            },
        }

    monkeypatch.setattr("cio_market_lab.integrations.hermes_chat.run_hermes_cli_chat", mock_run_chat)
    with pytest.raises(RuntimeError) as exc_info:
        executor.request_decision(ctx)
    assert "Missing resolved provider" in str(exc_info.value)
    assert executor.last_receipt is None


def test_negative_missing_metadata_rejected():
    """Negative test: missing provider, missing model, or non-dict metadata is rejected."""
    adapter = RuntimeEvidenceAdapter(pinned_provider="openai-codex", pinned_model="gpt-6-astra")

    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence("invalid-string", response_text="ok")
    assert "MALFORMED_RUNTIME_METADATA" in str(exc_info.value)

    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence({"resolved_model": "gpt-6-astra"}, response_text="ok")
    assert "Missing resolved provider" in str(exc_info.value)

    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence({"resolved_provider": "openai-codex"}, response_text="ok")
    assert "Missing resolved model" in str(exc_info.value)


def test_negative_mismatch_and_fallback_masquerading_rejected():
    """Negative test: provider mismatch, model mismatch, and fallback masquerading are rejected."""
    adapter = RuntimeEvidenceAdapter(pinned_provider="openai-codex", pinned_model="gpt-6-astra")

    # Provider mismatch
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(
            {"resolved_provider": "anthropic", "resolved_model": "gpt-6-astra"},
            response_text="ok",
        )
    assert "Provider mismatch" in str(exc_info.value)

    # Model mismatch
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(
            {"resolved_provider": "openai-codex", "resolved_model": "claude-3-sonnet"},
            response_text="ok",
        )
    assert "Model mismatch" in str(exc_info.value)

    # Fallback masquerading (fallback_active flag)
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(
            {
                "resolved_provider": "openai-codex",
                "resolved_model": "gpt-6-astra",
                "fallback_active": True,
            },
            response_text="ok",
        )
    assert "Fallback masquerading rejected" in str(exc_info.value)

    # Fallback masquerading (primary runtime diverged from pin)
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(
            {
                "resolved_provider": "openai-codex",
                "resolved_model": "gpt-6-astra",
                "primary_runtime": {"provider": "backup-provider", "model": "backup-model"},
            },
            response_text="ok",
        )
    assert "Primary provider 'backup-provider' mismatches pinned provider" in str(exc_info.value)


def test_negative_failed_response_rejected():
    """Negative test: failed exit code, error flag, or empty response text is rejected."""
    adapter = RuntimeEvidenceAdapter(pinned_provider="openai-codex", pinned_model="gpt-6-astra")
    valid_meta = {"resolved_provider": "openai-codex", "resolved_model": "gpt-6-astra"}

    # Non-zero exit code
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(valid_meta, response_text="Valid text", exit_code=1)
    assert "non-zero exit code" in str(exc_info.value)

    # Reported error
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence({**valid_meta, "error": "Rate limit exceeded"}, response_text="Valid text")
    assert "reported error" in str(exc_info.value)

    # Empty response text
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(valid_meta, response_text="")
    assert "Missing or empty final response" in str(exc_info.value)


class FakeAgentFixture:
    """Fake Hermes agent fixture explicitly labeled for integration testing."""
    is_fixture: bool = True

    def __init__(self, provider: str = "openai-codex", model: str = "gpt-6-astra"):
        self.provider = provider
        self.model = model
        self.requested_provider = provider
        self.base_url = "https://api.openai.com/v1"
        self._primary_runtime = {
            "provider": provider,
            "model": model,
            "requested_provider": provider,
            "base_url": "https://api.openai.com/v1",
            "api_mode": "responses",
        }
        self.stream_delta_callback = None
        self.tool_progress_callback = None


def test_installed_emitter_adapter_integration_with_fake_agent_fixture(monkeypatch):
    """Integration test: installed-emitter wrapper with fake agent explicitly labeled fixture."""
    captured_stream = []

    def mock_emit_line(obj):
        captured_stream.append(obj)

    # 1. Instantiate emitter instrumentation wrapper
    emitter = RuntimeEvidenceStreamJsonEmitter(
        model="gpt-6-astra",
        session_id="test-fixture-session",
        emit_fn=mock_emit_line,
    )
    # Pre-auth init event was emitted at construction
    assert len(captured_stream) == 1
    assert captured_stream[0]["type"] == "system"
    assert captured_stream[0]["subtype"] == "init"
    assert captured_stream[0]["is_pre_auth_init"] is True

    # 2. Attach initialized fake agent fixture
    fake_agent = FakeAgentFixture(provider="openai-codex", model="gpt-6-astra")
    emitter.attach(fake_agent)

    # Post-auth runtime metadata event emitted
    assert len(captured_stream) == 2
    meta_event = captured_stream[1]
    assert meta_event["type"] == "runtime_metadata"
    assert meta_event["evidence_strength"] == EVIDENCE_STRENGTH_LOCAL_RUNTIME
    assert meta_event["resolved_provider"] == "openai-codex"
    assert meta_event["resolved_model"] == "gpt-6-astra"
    assert meta_event["is_fixture"] is True
    assert meta_event["is_pre_auth_init"] is False

    # 3. Simulate turn delta and completion
    emitter.on_text_delta("Generating decision...")
    valid_packet = _dummy_valid_cio_packet_dict()
    valid_packet["is_fixture"] = True
    exit_code = emitter.emit_result({"final_response": json.dumps(valid_packet)}, exit_code=0)
    assert exit_code == 0

    # 4. Integrate with HermesCIODecisionExecutor (with allow_fixture=True for fixture test)
    executor = HermesCIODecisionExecutor(provider_id="openai-codex", model_id="gpt-6-astra", allow_fixture=True)

    def mock_run_chat(message, **kwargs):
        events = captured_stream
        res_meta = {}
        resp = ""
        for ev in events:
            if ev.get("type") == "runtime_metadata":
                res_meta.update(ev)
            elif ev.get("type") == "result":
                resp = ev.get("text", "")
        return {
            "transport": "hermes_cli",
            "transport_identifier": "hermes-cli-cio-bridge",
            "status": "completed",
            "response": resp,
            "session_id": "test-fixture-session",
            "returncode": 0,
            "runtime_metadata": res_meta,
            "resolved_provider": res_meta.get("resolved_provider"),
            "resolved_model": res_meta.get("resolved_model"),
            "evidence_strength": res_meta.get("evidence_strength", "local-runtime"),
            "is_fixture": True,
            "auth_verified": True,
            "is_success_response": True,
        }

    monkeypatch.setattr("cio_market_lab.integrations.hermes_chat.run_hermes_cli_chat", mock_run_chat)
    ctx = CIODecisionContextRequest(request_id="req-fixture-integration", timestamp=datetime.now(timezone.utc))

    packet = executor.request_decision(ctx)
    assert packet is not None
    # Case identity is owned by the authenticated request/session, not model text.
    import hashlib
    expected_case = 'case-' + hashlib.sha256((executor.session_id + '\0' + ctx.request_id).encode()).hexdigest()[:24]
    assert packet.case_id == expected_case
    assert packet.case_id != "case-provenance-test-001"
    assert packet.provenance.authority == "MAIN_CIO"
    assert packet.provenance.signer_id == "hermes-bridge-cio"
    assert packet.is_fixture is True

    # Verify receipt has local-runtime evidence strength
    receipt = executor.last_receipt
    assert receipt is not None
    assert receipt.readback_verified is True
    assert receipt.evidence_strength == "local-runtime"
    assert receipt.provider_id == "openai-codex"
    assert receipt.model_id == "gpt-6-astra"


def test_negative_sentinel_secret_absent():
    """Negative test: agent _primary_runtime containing secrets must strictly allowlist provider/model/api_mode ONLY."""
    sentinel_secret = "SENTINEL_SECRET_TOKEN_DO_NOT_LEAK_999"
    sentinel_header = "Bearer SENTINEL_AUTH_KEY_XYZ"

    class AgentWithSecrets:
        provider = "openai-codex"
        model = "gpt-6-astra"
        requested_provider = "openai-codex"
        base_url = "https://api.openai.com/v1"
        is_fixture = True
        stream_delta_callback = None
        tool_progress_callback = None
        _primary_runtime = {
            "provider": "openai-codex",
            "model": "gpt-6-astra",
            "api_mode": "responses",
            "api_key": sentinel_secret,
            "token": sentinel_secret,
            "headers": {"Authorization": sentinel_header},
            "credentials": {"secret": sentinel_secret},
        }

    agent = AgentWithSecrets()
    adapter = RuntimeEvidenceAdapter(pinned_provider="openai-codex", pinned_model="gpt-6-astra")
    evidence = adapter.extract_from_agent(agent)

    # 1. Primary runtime strictly contains allowlisted nonsecret keys ONLY
    assert set(evidence.primary_runtime.keys()) == {"provider", "model", "api_mode"}
    assert evidence.primary_runtime["provider"] == "openai-codex"
    assert evidence.primary_runtime["model"] == "gpt-6-astra"
    assert evidence.primary_runtime["api_mode"] == "responses"

    # 2. Sentinel secret is strictly absent anywhere in evidence dict/json
    evidence_json = evidence.model_dump_json()
    assert sentinel_secret not in evidence_json
    assert sentinel_header not in evidence_json
    assert "api_key" not in evidence.primary_runtime
    assert "token" not in evidence.primary_runtime
    assert "headers" not in evidence.primary_runtime

    # 3. Sentinel secret is strictly absent in emitter events
    captured = []
    emitter = RuntimeEvidenceStreamJsonEmitter(
        model="gpt-6-astra",
        session_id="test-sentinel-session",
        emit_fn=lambda obj: captured.append(obj),
    )
    emitter.attach(agent)
    emitter.emit_result({"final_response": "ok"}, exit_code=0)

    all_emitted_str = json.dumps(captured)
    assert sentinel_secret not in all_emitted_str
    assert sentinel_header not in all_emitted_str


def test_negative_pre_auth_only_stream_with_provider_rejected():
    """Negative test: stream containing ONLY pre-auth init with provider must be rejected (auth_verified=False)."""
    adapter = RuntimeEvidenceAdapter(pinned_provider="openai-codex", pinned_model="gpt-6-astra")

    # Stream parser or caller passes metadata from pre-auth init event
    pre_auth_meta = {
        "type": "system",
        "subtype": "init",
        "provider": "openai-codex",
        "model": "gpt-6-astra",
        "is_pre_auth_init": True,
        "auth_verified": False,
    }
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(pre_auth_meta, response_text="Valid text")
    assert "Pre-auth init record rejected" in str(exc_info.value) or "auth_verified is False" in str(exc_info.value)

    # Unverified auth flag
    unverified_meta = {
        "resolved_provider": "openai-codex",
        "resolved_model": "gpt-6-astra",
        "auth_verified": False,
    }
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(unverified_meta, response_text="Valid text")
    assert "auth_verified=False" in str(exc_info.value)


def test_negative_production_executor_rejects_fixture():
    """Negative test: production executor (allow_fixture=False) strictly rejects fixture=True."""
    executor = HermesCIODecisionExecutor(provider_id="openai-codex", model_id="gpt-6-astra", escalation_reason="thesis_conflict", allow_fixture=False)
    assert executor.allow_fixture is False

    meta = {
        "resolved_provider": "openai-codex",
        "resolved_model": "gpt-6-astra",
        "is_fixture": True,
        "auth_verified": True,
        "is_success_response": True,
    }
    with pytest.raises(RuntimeError) as exc_info:
        executor.runtime_adapter.verify_runtime_evidence(meta, response_text="Valid response", allow_fixture=False)
    assert "Fixture runtime evidence rejected" in str(exc_info.value)


def test_negative_production_executor_rejects_is_success_response_false():
    """Negative test: is_success_response=False must be rejected."""
    adapter = RuntimeEvidenceAdapter(pinned_provider="openai-codex", pinned_model="gpt-6-astra")
    meta = {
        "resolved_provider": "openai-codex",
        "resolved_model": "gpt-6-astra",
        "auth_verified": True,
        "is_success_response": False,
    }
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(meta, response_text="Valid response")
    assert "is_success_response=False" in str(exc_info.value)


def test_negative_production_executor_rejects_metadata_nonzero_returncode():
    """Negative test: metadata reporting non-zero exit_code or returncode must be rejected."""
    adapter = RuntimeEvidenceAdapter(pinned_provider="openai-codex", pinned_model="gpt-6-astra")
    meta = {
        "resolved_provider": "openai-codex",
        "resolved_model": "gpt-6-astra",
        "auth_verified": True,
        "is_success_response": True,
        "exit_code": 1,
    }
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(meta, response_text="Valid response", exit_code=0)
    assert "nonzero exit code" in str(exc_info.value).lower()


def test_negative_production_executor_rejects_result_error_or_failed_when_subprocess_zero():
    """Negative test: result error/failed must be rejected even when subprocess return code is zero."""
    adapter = RuntimeEvidenceAdapter(pinned_provider="openai-codex", pinned_model="gpt-6-astra")
    meta = {
        "resolved_provider": "openai-codex",
        "resolved_model": "gpt-6-astra",
        "auth_verified": True,
        "is_success_response": True,
        "failed": True,
    }
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(meta, response_text="Valid response", exit_code=0)
    assert "Runtime reported error/failure" in str(exc_info.value)

    meta_err = {
        "resolved_provider": "openai-codex",
        "resolved_model": "gpt-6-astra",
        "auth_verified": True,
        "is_success_response": True,
        "error": "Upstream rate limit exceeded",
    }
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(meta_err, response_text="Valid response", exit_code=0)
    assert "Runtime reported error/failure" in str(exc_info.value)


def test_negative_mid_turn_fallback_detected_at_terminal():
    """Negative test: re-extracting runtime at terminal response catches mid-turn fallback."""
    class FallbackAgent:
        provider = "openai-codex"
        model = "gpt-6-astra"
        requested_provider = "openai-codex"
        base_url = "https://api.openai.com/v1"
        is_fixture = True
        stream_delta_callback = None
        tool_progress_callback = None
        _primary_runtime = {"provider": "openai-codex", "model": "gpt-6-astra", "api_mode": "responses"}

    agent = FallbackAgent()
    events = []
    emitter = RuntimeEvidenceStreamJsonEmitter(
        model="gpt-6-astra",
        session_id="test-midturn-session",
        emit_fn=lambda obj: events.append(obj),
    )
    # Initial attach snapshot
    emitter.attach(agent)
    assert emitter.runtime_evidence.resolved_provider == "openai-codex"

    # Simulate mid-turn fallback occurring on agent
    agent.provider = "backup-provider"
    agent.model = "backup-model"

    # Terminal response re-extraction
    exit_code = emitter.emit_result({"final_response": "done"}, exit_code=0)
    assert exit_code == 0

    # Terminal metadata event emitted with fallback_active=True
    terminal_events = [e for e in events if e.get("type") == "runtime_metadata" and e.get("subtype") == "terminal"]
    assert len(terminal_events) == 1
    assert terminal_events[0]["fallback_active"] is True
    assert terminal_events[0]["resolved_provider"] == "backup-provider"

    # Adapter verification must detect fallback or pin mismatch and reject
    adapter = RuntimeEvidenceAdapter(pinned_provider="openai-codex", pinned_model="gpt-6-astra")
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(terminal_events[0], response_text="done", allow_fixture=True)
    assert any(
        msg in str(exc_info.value)
        for msg in (
            "Fallback masquerading rejected",
            "Provider mismatch",
            "Authentication not verified",
        )
    )


def test_actual_subprocess_bootstrap_path_with_fake_agent():
    """Integration test: explicit dependency injection with fake agent transport fixture."""
    ctx = CIODecisionContextRequest(request_id="req-subproc-test", timestamp=datetime.now(timezone.utc))

    # Test explicit dependency injection via transport parameter
    result = run_hermes_cli_chat(
        "Generate decision for 2330.TW",
        session_id="subproc-bootstrap-test",
        model="gpt-6-astra",
        provider="openai-codex",
        enforce_cio_pin=True,
        transport=mock_injected_transport_for_test,
    )

    assert result["transport"] == "hermes_cli"
    assert result["returncode"] == 0
    assert result["resolved_provider"] == "openai-codex"
    assert result["resolved_model"] == "gpt-6-astra"
    assert result["evidence_strength"] == "local-runtime"
    # Every injected result is marked fixture
    assert result["is_fixture"] is True
    assert result["auth_verified"] is True
    assert result["is_success_response"] is True

    # Events must contain init, runtime_metadata, and result
    event_types = [e.get("type") for e in result["events"]]
    assert "system" in event_types
    assert "runtime_metadata" in event_types
    assert "result" in event_types

    # Verified with HermesCIODecisionExecutor (allow_fixture=True)
    executor = HermesCIODecisionExecutor(
        provider_id="openai-codex",
        model_id="gpt-6-astra",
        allow_fixture=True,
        transport=mock_injected_transport_for_test,
    )
    packet = executor.request_decision(ctx)
    assert packet is not None
    import hashlib
    expected_case = 'case-' + hashlib.sha256((executor.session_id + '\0' + ctx.request_id).encode()).hexdigest()[:24]
    assert packet.case_id == expected_case
    assert packet.case_id != "case-injected-test-001"
    assert packet.is_fixture is True
    assert executor.last_receipt is not None
    assert executor.last_receipt.readback_verified is True
    assert executor.last_receipt.provider_id == "openai-codex"
    assert executor.last_receipt.model_id == "gpt-6-astra"


def test_negative_failed_bootstrap_stub_rejected():
    """Negative fixture test: Old bootstrap stub (missing terminal result / returns 0 without conversation call) is rejected."""
    adapter = RuntimeEvidenceAdapter(pinned_provider="openai-codex", pinned_model="gpt-6-astra")

    # Simulate events from the old failed stub: init event emitted, but NO terminal result event
    stub_meta = {
        "resolved_provider": "openai-codex",
        "resolved_model": "gpt-6-astra",
        "is_pre_auth_init": False,
        "auth_verified": True,
        "is_success_response": False,
    }
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(stub_meta, response_text="", allow_fixture=False)
    assert any(
        msg in str(exc_info.value)
        for msg in (
            "Response marked unsuccessful",
            "Missing or empty final response text",
        )
    )


def test_explicit_dependency_injection_marked_fixture_and_rejected_by_production():
    """Integration test: Explicit dependency injection transport.

    Proves:
    1. Original input reaches the genuine conversation method / transport.
    2. Real returned text reaches the terminal result.
    3. Every injected result is marked fixture=True.
    4. Production receipt verifier strictly rejects injected fixture when allow_fixture=False.
    5. Production executor strictly rejects injected fixture when is_production=True.
    """
    test_query = "Analyze real production breakout setup for 2330.TW swing trading"
    result = run_hermes_cli_chat(
        test_query,
        session_id="injected-transport-test",
        model="gpt-6-astra",
        provider="openai-codex",
        enforce_cio_pin=True,
        transport=mock_injected_transport_for_test,
    )

    # 1. Successful execution
    assert result["transport"] == "hermes_cli"
    assert result["returncode"] == 0
    assert result["resolved_provider"] == "openai-codex"
    assert result["resolved_model"] == "gpt-6-astra"
    assert result["evidence_strength"] == "local-runtime"
    # Injected result MUST be marked fixture=True
    assert result["is_fixture"] is True
    assert result["auth_verified"] is True
    assert result["status"] == "completed"

    # 2. Proves original input reached conversation method and real returned text reached terminal result
    response_data = json.loads(result["response"])
    assert response_data["query_echo"] == test_query
    assert test_query in response_data["thesis"]
    assert response_data["selected_instrument"] == "2330.TW"
    assert response_data["action"] == "BUY"

    # 3. Production verifier (allow_fixture=False) strictly REJECTS the injected fixture!
    adapter = RuntimeEvidenceAdapter(pinned_provider="openai-codex", pinned_model="gpt-6-astra")
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(
            result["runtime_metadata"],
            response_text=result["response"],
            allow_fixture=False,
        )
    assert "Fixture runtime evidence rejected by production executor" in str(exc_info.value)

    # 4. Production executor (is_production=True) strictly REJECTS the injected fixture!
    ctx = CIODecisionContextRequest(request_id="req-injected-prod-test", timestamp=datetime.now(timezone.utc))
    prod_executor = HermesCIODecisionExecutor(
        provider_id="openai-codex",
        model_id="gpt-6-astra",
        escalation_reason="thesis_conflict",
        allow_fixture=False,
        is_production=True,
        transport=mock_injected_transport_for_test,
    )
    with pytest.raises(RuntimeError) as exc_info2:
        prod_executor.request_decision(ctx)
    err2 = str(exc_info2.value)
    assert any(
        msg in err2
        for msg in (
            "PRODUCTION_EXECUTOR_REJECTED_FIXTURE",
            "Fixture runtime evidence rejected by production executor",
        )
    )


def test_real_bootstrap_production_path_failure_propagates_nonzero_status():
    """Integration test: Failure propagates nonzero status."""
    with pytest.raises(RuntimeError) as exc_info:
        run_hermes_cli_chat(
            "Test failing query",
            session_id="real-bootstrap-fail-test",
            model="gpt-6-astra",
            provider="openai-codex",
            enforce_cio_pin=True,
            transport=failing_injected_transport_for_test,
        )
    err_str = str(exc_info.value)
    assert any(
        msg in err_str
        for msg in (
            "nonzero exit code",
            "Simulated injected transport execution failure",
            "READBACK_VERIFICATION_FAILED",
        )
    )


def test_negative_sentinel_auth_paths_never_accessed_or_linked(tmp_path):
    """Negative security test: Sentinel auth/config/.env paths are never accessed, copied, or linked."""
    sentinel_dir = tmp_path / "sentinel_hermes_home"
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    sentinel_auth = sentinel_dir / "auth.json"
    sentinel_config = sentinel_dir / "config.yaml"
    sentinel_env = sentinel_dir / ".env"

    sentinel_auth.write_text('{"sentinel_token": "FORBIDDEN_LEAK_CHECK"}', encoding="utf-8")
    sentinel_config.write_text("sentinel_secret: DO_NOT_READ", encoding="utf-8")
    sentinel_env.write_text("SENTINEL_API_KEY=NEVER_LOAD", encoding="utf-8")

    # Record initial file states
    auth_mtime = sentinel_auth.stat().st_mtime
    config_mtime = sentinel_config.stat().st_mtime
    env_mtime = sentinel_env.stat().st_mtime

    # Run executor with mock transport
    ctx = CIODecisionContextRequest(request_id="req-sentinel-test", timestamp=datetime.now(timezone.utc))
    executor = HermesCIODecisionExecutor(
        provider_id="openai-codex",
        model_id="gpt-6-astra",
        allow_fixture=True,
        transport=mock_injected_transport_for_test,
    )
    packet = executor.request_decision(ctx)
    assert packet is not None

    # Verify sentinel files were NOT modified
    assert sentinel_auth.stat().st_mtime == auth_mtime
    assert sentinel_config.stat().st_mtime == config_mtime
    assert sentinel_env.stat().st_mtime == env_mtime

    # Verify NO symlinks pointing to any sentinel file exist in project or temp directories
    project_root = Path(__file__).resolve().parent.parent
    for check_dir in [project_root / "artifacts", tmp_path]:
        for p in check_dir.rglob("*"):
            if p.is_symlink():
                target = str(p.resolve())
                assert str(sentinel_dir) not in target, f"Forbidden symlink pointing to sentinel: {p} -> {target}"


def test_negative_bootstrap_rejects_unsupported_flags():
    """Negative test: Project bootstrap explicitly rejects unsupported CLI flags rather than silently ignoring."""
    from cio_market_lab.integrations.cio_hermes_bootstrap import run_chat_command

    # Passing unsupported flag must return exit code 1
    ret = run_chat_command(["--unsupported-flag-xyz", "--query", "hello"])
    assert ret == 1

    # Passing invalid source must return exit code 1
    ret_source = run_chat_command(["--source", "invalid_source_123", "--query", "hello"])
    assert ret_source == 1

    # Passing non-positive max_turns must return exit code 1
    ret_turns = run_chat_command(["--max-turns", "0", "--query", "hello"])
    assert ret_turns == 1

    # Passing non-positive run_budget must return exit code 1
    ret_budget = run_chat_command(["--run-budget", "-5", "--query", "hello"])
    assert ret_budget == 1

    # Unsupported flag combination: --oneshot with --continue
    ret_oneshot_cont = run_chat_command(["--oneshot", "--continue", "sess-1", "--query", "hello"])
    assert ret_oneshot_cont == 1

    # Unsupported flag combination: --oneshot with --create-if-missing
    ret_oneshot_create = run_chat_command(["--oneshot", "--create-if-missing", "--query", "hello"])
    assert ret_oneshot_create == 1

    # Unsupported flag combination: --create-if-missing without --continue
    ret_create_no_cont = run_chat_command(["--create-if-missing", "--query", "hello"])
    assert ret_create_no_cont == 1


def test_negative_pytest_current_test_not_popped():
    """Negative security test: PYTEST_CURRENT_TEST must never be popped by production code."""
    import os
    assert "PYTEST_CURRENT_TEST" in os.environ
    orig_val = os.environ["PYTEST_CURRENT_TEST"]

    # Call run_hermes_cli_chat with injected transport
    run_hermes_cli_chat(
        "Check test env isolation",
        session_id="pytest-current-test-check",
        model="gpt-6-astra",
        provider="openai-codex",
        transport=mock_injected_transport_for_test,
    )
    # Ensure PYTEST_CURRENT_TEST was not popped or cleared
    assert "PYTEST_CURRENT_TEST" in os.environ
    assert os.environ["PYTEST_CURRENT_TEST"] == orig_val


def test_negative_staged_source_never_imported_in_production():
    """Negative isolation test: staged sources under artifacts/hermes_source_readonly must NEVER be on sys.path.
    Missing installed CLI must fail explicitly without fallback."""
    import sys
    from pathlib import Path
    from cio_market_lab.integrations.cio_hermes_bootstrap import run_chat_command

    project_root = Path(__file__).resolve().parent.parent
    staged_src = project_root / "artifacts" / "hermes_source_readonly"

    # Staged source must NEVER be added to sys.path
    assert str(staged_src) not in sys.path

    # Missing installed CLI in production path must fail explicitly without falling back
    ret = run_chat_command(["--query", "test staged source isolation"])
    assert ret == 1


def test_negative_socket_prohibition_blocks_external_networks():
    """Negative isolation test: socket prohibition blocks external network while preserving local loopback/ASGI."""
    import socket

    # External IP connection must be blocked at socket layer
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError) as exc_info:
            s.connect(("8.8.8.8", 53))
        assert "BLOCKED: External network connection prohibited" in str(exc_info.value)
    finally:
        s.close()

    # External hostname connection via create_connection must be blocked
    with pytest.raises(RuntimeError) as exc_info2:
        socket.create_connection(("api.openai.com", 443), timeout=0.1)
    assert "BLOCKED: External network connection prohibited" in str(exc_info2.value)

    # Local loopback connection is permitted past the blocker (will raise ConnectionRefusedError if no listener, not BLOCKED)
    s_local = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            s_local.connect(("127.0.0.1", 49151))
        except (ConnectionRefusedError, OSError) as e:
            # OS error expected since no server is listening, but NOT isolation BLOCKED RuntimeError!
            assert "BLOCKED" not in str(e)
    finally:
        s_local.close()


def _offline_bootstrap_subprocess_env(tmp_path: Path) -> Dict[str, str]:
    """Run fixture bootstrap without installed Hermes updates, home or credentials."""
    import sys

    agent_path = tmp_path / "fixture_agent"
    agent_path.mkdir()
    (agent_path / "hermes_bootstrap.py").write_text(
        "is_fixture = True\n", encoding="utf-8"
    )
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        "HERMES_HOME": str(hermes_home),
        "HERMES_AGENT_PATH": str(agent_path),
        "HERMES_PYTHON": sys.executable,
        "CIO_PRODUCTION_EXECUTOR": "0",
    }


def test_actual_subprocess_bootstrap_fixture_transport_and_rejection(tmp_path):
    """Exercise bootstrap in actual subprocess with explicitly labeled fixture transport and prove rejection.
    No more success claims based solely on in-process doubles."""
    import subprocess
    import sys

    # 1. Run actual subprocess invoking project bootstrap with --test-fixture-transport
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "cio_market_lab.integrations.cio_hermes_bootstrap",
            "chat",
            "--test-fixture-transport",
            "--format",
            "stream-json",
            "--query",
            "Analyze swing trading breakout for 2330.TW",
            "--provider",
            "openai-codex",
            "--model",
            "gpt-6-astra",
        ],
        capture_output=True,
        text=True,
        env=_offline_bootstrap_subprocess_env(tmp_path),
        timeout=20,
    )

    assert proc.returncode == 0, f"Subprocess failed with code {proc.returncode}: {proc.stderr}"

    # 2. Parse stream-json events from actual subprocess stdout (robust against auxiliary warnings)
    events = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip() and line.strip().startswith("{")]
    event_types = [e.get("type") for e in events]
    assert "system" in event_types
    assert "runtime_metadata" in event_types
    assert "result" in event_types

    meta_event = next(e for e in events if e.get("type") == "runtime_metadata")
    assert meta_event["resolved_provider"] == "openai-codex"
    assert meta_event["resolved_model"] == "gpt-6-astra"
    assert meta_event["evidence_strength"] == "local-runtime"
    # Subprocess fixture transport MUST be explicitly labeled fixture
    assert meta_event["is_fixture"] is True
    assert meta_event["auth_verified"] is True

    result_event = next(e for e in events if e.get("type") == "result")
    final_resp_data = json.loads(result_event["text"])
    assert final_resp_data["is_fixture"] is True
    assert final_resp_data["symbol"] == "2330.TW"

    # 3. Prove production verifier strictly rejects the subprocess fixture output when allow_fixture=False
    adapter = RuntimeEvidenceAdapter(pinned_provider="openai-codex", pinned_model="gpt-6-astra")
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(
            meta_event,
            response_text=result_event["text"],
            allow_fixture=False,
        )
    assert "Fixture runtime evidence rejected by production executor" in str(exc_info.value)


def test_actual_subprocess_fixture_transport_prohibited_in_production(tmp_path):
    """Prove fixture transport is unavailable and strictly rejected inside subprocess when CIO_PRODUCTION_EXECUTOR=1."""
    import subprocess
    import sys
    prod_env = _offline_bootstrap_subprocess_env(tmp_path)
    prod_env["CIO_PRODUCTION_EXECUTOR"] = "1"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "cio_market_lab.integrations.cio_hermes_bootstrap",
            "chat",
            "--test-fixture-transport",
            "--format",
            "stream-json",
            "--query",
            "Analyze breakout for 2330.TW",
        ],
        capture_output=True,
        text=True,
        env=prod_env,
        timeout=20,
    )

    # Subprocess must fail with non-zero exit code
    assert proc.returncode == 1
    combined_output = proc.stdout + proc.stderr
    assert "Fixture transport is strictly prohibited and unavailable in production" in combined_output


def test_offline_paper_pipeline_integration_exercise(tmp_path):
    """Offline paper-pipeline integration exercise using existing runner API.
    Flow: decision packet -> same-case pending-to-filled progression -> ledger/NAV ->
    simulated close fill -> derived realized PnL/costs & attribution -> recorded lesson
    retrieval in next decision context.
    All synthetic inputs explicitly fixture, never actual market receipts; strategy unaltered."""
    t0 = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)
    clock = [t0]
    runner, orders, pm, adapter = build_harness_environment(tmp_path, clock)

    symbol = "2330.TW"
    price_t0 = 1000.0
    adapter.set_bar(symbol, t0, 995.0, 1005.0, 990.0, price_t0)
    adapter.set_quote(symbol, t0, price_t0)

    # Step A: Decision packet submitted with same-bar quote -> enters pending
    pending_packet = CIODecisionPacket(
        case_id="case-offline-exercise-roundtrip-001",
        as_of=t0,
        evidence=["research://semi-cycle-expansion", "quote://2330.TW-1000"],
        thesis="Synthetic swing breakout setup awaiting later authoritative quote",
        selected_instrument=symbol,
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=20.0,
        conditions={**({"max_slippage_bps": 20}), "allow_odd_lot": True},
        risk_assessment={"downside_buffer": 0.05, "thesis_invalidation": "close < 960"},
        alternatives_considered=[{"symbol": "2454.TW", "reason": "Higher relative beta"}],
        expiry=t0 + timedelta(hours=8),
        confidence=0.88,
        strategy_version="dynamic-desk-harness-v1",
        provenance=CIOProvenance(
            authority="MAIN_CIO",
            actor_role="CHIEF_INVESTMENT_OFFICER",
            signer_id="main-cio-key",
            source="external_packet",
        ),
        is_fixture=True,
    )
    sign_cio_packet(pending_packet, signer_id="main-cio-key")
    d_pending = runner.submit_cio_packet(pending_packet)
    assert d_pending.action == "BUY_PENDING"
    assert d_pending.terminal_status == "NON_TERMINAL"
    assert d_pending.inputs["fill_pending_reason"] == "WAITING_FOR_LATER_AUTHORITATIVE_QUOTE"
    assert d_pending.order_id is not None

    # Verify no fills applied yet and cash unchanged
    swing_ledger = pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING)
    initial_cash = swing_ledger.cash
    assert len(swing_ledger.fills) == 0
    assert initial_cash == TEAM_INITIAL_CAPITAL_TWD

    # Step B: Authoritative later quote arrives at t1 > t0 -> SAME case progresses to fill
    t1 = t0 + timedelta(minutes=5)
    clock[0] = t1
    later_price = 1002.0
    adapter.set_quote(symbol, t1, later_price)

    # Do not force a new case just to fill previous pending action: submit SAME pending_packet
    decision_fill = runner.submit_cio_packet(pending_packet)
    assert decision_fill.action == "BUY_FILLED"
    assert decision_fill.terminal_status == "TERMINAL_FILLED"
    assert decision_fill.price is not None
    assert decision_fill.price >= later_price
    assert decision_fill.order_id == d_pending.order_id
    assert len(swing_ledger.fills) == 1
    fill_record = swing_ledger.fills[0]
    assert fill_record.side == OrderSide.BUY
    assert fill_record.quantity == 20.0
    post_buy_cash = swing_ledger.cash
    assert post_buy_cash < initial_cash

    # Replay idempotency: resubmitting same packet produces NO duplicate fills or altered cash
    d_replay = runner.submit_cio_packet(pending_packet)
    assert d_replay.action == "NO_TRADE"
    assert "DUPLICATE_CASE_ID" in d_replay.reason
    assert len(swing_ledger.fills) == 1
    assert swing_ledger.cash == post_buy_cash

    # Restart idempotency: reloading runner from disk maintains cash, positions, and rejects duplicate
    new_pm = PortfolioManager(
        initial_cash_swing=TEAM_INITIAL_CAPITAL_TWD,
        initial_cash_intraday=TEAM_INITIAL_CAPITAL_TWD,
    )
    new_orders = PaperOrderService(new_pm, EventStore(":memory:"))
    reloaded_runner = AutonomousPaperRunner(
        tmp_path,
        new_pm,
        new_orders,
        adapter,
        now_fn=lambda: clock[0],
        team_initial_capital=TEAM_INITIAL_CAPITAL_TWD,
        require_cio_provider=True,
    )
    reloaded_ledger = new_pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING)
    assert abs(reloaded_ledger.cash - post_buy_cash) < 0.01
    assert symbol in reloaded_ledger.positions
    assert reloaded_ledger.positions[symbol].quantity == 20.0
    d_restart_dup = reloaded_runner.submit_cio_packet(pending_packet)
    assert d_restart_dup.action == "NO_TRADE"
    assert "DUPLICATE_CASE_ID" in d_restart_dup.reason

    # Step C: Single ledger NAV verification
    pos = swing_ledger.positions[symbol]
    assert pos.quantity == 20.0
    reconciled_cash = pm.reconcile_canonical_cash(DYNAMIC_DESK_ID)
    assert abs(reconciled_cash - swing_ledger.cash) < 0.01

    snap = runner.generate_canonical_team_ops(t1)
    nav = snap["portfolio"]["equity"]
    assert nav is not None and nav > 0
    expected_nav = swing_ledger.cash + pos.quantity * later_price
    assert abs(nav - expected_nav) < 500.0

    # Step D: Actual Closing Simulated Fill through existing runner APIs
    t2 = t1 + timedelta(days=2)
    clock[0] = t2
    adapter.set_bar(symbol, t2, 1045.0, 1055.0, 1040.0, 1048.0)
    adapter.set_quote(symbol, t2, 1048.0)

    close_packet = CIODecisionPacket(
        case_id="case-offline-exercise-close-001",
        as_of=t2,
        evidence=["research://semi-cycle-expansion", "quote://2330.TW-1048"],
        thesis="Synthetic swing profit target reached; close position.",
        selected_instrument=symbol,
        action="SELL",
        holding_horizon=DecisionScope.SWING,
        quantity=20.0,
        conditions={**({"target_case_id": pending_packet.case_id}), "allow_odd_lot": True},
        expiry=t2 + timedelta(hours=8),
        confidence=0.92,
        strategy_version="dynamic-desk-harness-v1",
        provenance=CIOProvenance(
            authority="MAIN_CIO",
            actor_role="CHIEF_INVESTMENT_OFFICER",
            signer_id="main-cio-key",
            source="external_packet",
        ),
        is_fixture=True,
    )
    sign_cio_packet(close_packet, signer_id="main-cio-key")

    # Same-bar quote at t2 -> enters SELL_PENDING
    d_close_pending = runner.submit_cio_packet(close_packet)
    assert d_close_pending.action == "SELL_PENDING"
    assert d_close_pending.terminal_status == "NON_TERMINAL"

    # Later synthetic quote at t2_fill -> completes closing fill
    t2_fill = t2 + timedelta(minutes=5)
    clock[0] = t2_fill
    exit_quote_px = 1050.0
    adapter.set_quote(symbol, t2_fill, exit_quote_px)

    decision_close = runner.submit_cio_packet(close_packet)
    assert decision_close.action == "SELL_FILLED"
    assert decision_close.terminal_status == "TERMINAL_FILLED"
    assert decision_close.price is not None
    assert decision_close.price <= exit_quote_px  # sell slippage adjusted

    # Position is now completely closed in ledger
    assert symbol in swing_ledger.positions
    assert swing_ledger.positions[symbol].quantity == 0.0
    assert len(swing_ledger.fills) == 2
    sell_fill = swing_ledger.fills[1]
    assert sell_fill.side == OrderSide.SELL
    assert sell_fill.quantity == 20.0

    # Derive realized PnL and costs directly from actual simulated fill/ledger records
    ledger_realized_pnl = round(swing_ledger.realized_pnl, 4)
    assert ledger_realized_pnl > 0.0
    derived_outcome = decision_close.inputs["outcome"]
    assert derived_outcome["exit_price"] == decision_close.price
    assert derived_outcome["realized_pnl"] == ledger_realized_pnl
    assert derived_outcome["holding_period_hours"] >= 48.0
    assert "costs" in derived_outcome
    assert derived_outcome["costs"]["total_costs"] > 0.0

    # Benchmark attribution: NO benchmark inputs provided -> UNAVAILABLE, never invented
    derived_attribution = decision_close.inputs["attribution"]
    assert derived_attribution["status"] == "UNAVAILABLE"
    assert derived_attribution["benchmark_status"] == "UNAVAILABLE"
    assert derived_attribution["alpha_bps"] is None
    assert derived_attribution["reason"] == "NO_BENCHMARK_INPUT"

    # Step E: Next Decision Context retrieves recorded outcome and lesson
    t3 = t2_fill + timedelta(hours=1)
    clock[0] = t3
    next_ctx = runner.build_decision_context_request(symbols=[symbol])
    assert len(next_ctx.prior_lessons) >= 1
    found_lesson = next((l for l in next_ctx.prior_lessons if l["case_id"] == pending_packet.case_id), None)
    assert found_lesson is not None
    assert found_lesson["outcome_details"]["realized_pnl"] == ledger_realized_pnl
    assert found_lesson["outcome_details"]["exit_price"] == decision_close.price
    assert len(next_ctx.past_outcomes) >= 1
    found_outcome = next((o for o in next_ctx.past_outcomes if o["case_id"] == pending_packet.case_id), None)
    assert found_outcome is not None
    assert found_outcome["outcome"]["realized_pnl"] == ledger_realized_pnl
    assert found_outcome["outcome"]["exit_price"] == decision_close.price


def test_negative_regression_reproduces_managed_runtime_dependency_timing(tmp_path):
    """Negative regression test: hermetic subprocess reproducing managed-runtime dependency timing failure.
    Proves that importing third-party dependencies before managed bootstrap activates dependencies
    reproduces the ModuleNotFoundError seen in artifacts/main_bridge_live_probe.stderr."""
    import subprocess
    import sys

    # Hermetic setup: a dummy package simulating managed third-party dependency
    managed_pkg_dir = tmp_path / "managed_site"
    managed_pkg_dir.mkdir()
    (managed_pkg_dir / "managed_dependency_probe.py").write_text(
        "IS_ACTIVATED = True\n",
        encoding="utf-8",
    )

    # A mock hermes_bootstrap that activates the managed dependencies when imported
    agent_dir = tmp_path / "mock_agent"
    agent_dir.mkdir()
    (agent_dir / "hermes_bootstrap.py").write_text(
        f"import sys\nsys.path.insert(0, {repr(str(managed_pkg_dir))})\nBOOTSTRAP_RAN = True\n",
        encoding="utf-8",
    )

    # Entrypoint script that imports the managed dependency
    entrypoint = tmp_path / "entrypoint.py"
    entrypoint.write_text(
        "from managed_dependency_probe import IS_ACTIVATED\n"
        "print('MANAGED_DEPENDENCY_LOADED_OK')\n",
        encoding="utf-8",
    )

    # Negative test: Subprocess without bootstrap activation fails with ModuleNotFoundError
    # (reproducing main_bridge_live_probe.stderr defect)
    neg_proc = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys, runpy; sys.path.insert(0, {repr(str(agent_dir))}); runpy.run_path({repr(str(entrypoint))}, run_name='__main__')",
        ],
        capture_output=True,
        text=True,
    )
    assert neg_proc.returncode != 0
    assert "ModuleNotFoundError" in neg_proc.stderr
    assert "managed_dependency_probe" in neg_proc.stderr

    # Positive test: With bootstrap activated FIRST followed by runpy entrypoint, execution succeeds
    pos_proc = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {repr(str(agent_dir))}); import hermes_bootstrap; import runpy; runpy.run_path({repr(str(entrypoint))}, run_name='__main__')",
        ],
        capture_output=True,
        text=True,
    )
    assert pos_proc.returncode == 0
    assert "MANAGED_DEPENDENCY_LOADED_OK" in pos_proc.stdout


def test_positive_regression_production_command_builder(tmp_path, monkeypatch):
    """Positive regression test: exact production command builder produces correct invocation
    with bootstrap-first wrapper, zero tools, ignore-rules, oneshot, and fixture transport."""
    import subprocess
    import json
    from cio_market_lab.integrations.hermes_chat import build_production_chat_command
    from cio_market_lab.integrations.runtime_evidence import RuntimeEvidenceAdapter

    subprocess_env = _offline_bootstrap_subprocess_env(tmp_path)
    monkeypatch.setenv("HERMES_AGENT_PATH", subprocess_env["HERMES_AGENT_PATH"])
    monkeypatch.setenv("HERMES_PYTHON", subprocess_env["HERMES_PYTHON"])
    monkeypatch.setenv("HERMES_HOME", subprocess_env["HERMES_HOME"])
    session_id = "test-positive-command-builder"
    cmd = build_production_chat_command(
        session_id=session_id,
        workspace_root=str(tmp_path),
        provider="openai-codex",
        model="gpt-6-astra",
        query="Connectivity acceptance positive regression test",
        format_type="stream-json",
        reasoning="medium",
        max_turns=1,
        run_budget=45.0,
        toolsets="none",
        ignore_rules=True,
        oneshot=True,
        source="tool",
        test_fixture_transport=True,
    )

    # Verify command invariants
    assert "--toolsets" in cmd and "none" in cmd
    assert "--ignore-rules" in cmd
    assert "--oneshot" in cmd
    assert "--format" in cmd and "stream-json" in cmd
    assert "--session-id" in cmd and session_id in cmd
    assert "--provider" in cmd and "openai-codex" in cmd
    assert "--model" in cmd and "gpt-6-astra" in cmd
    assert "--test-fixture-transport" in cmd

    # Verify bootstrap-first activation wrapper is in the Python command
    assert "-c" in cmd
    code_arg = cmd[cmd.index("-c") + 1]
    assert "import hermes_bootstrap" in code_arg
    assert "runpy.run_path" in code_arg

    # Run subprocess with the exact built command
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=subprocess_env,
        timeout=20,
    )
    assert proc.returncode == 0, f"Subprocess failed: {proc.stderr}"

    # Parse stream-json events robustly
    events = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip() and line.strip().startswith("{")]
    event_types = [e.get("type") for e in events]
    assert "system" in event_types
    assert "runtime_metadata" in event_types
    assert "result" in event_types

    meta_event = next(e for e in events if e.get("type") == "runtime_metadata")
    assert meta_event["is_fixture"] is True
    assert meta_event["resolved_provider"] == "openai-codex"
    assert meta_event["resolved_model"] == "gpt-6-astra"

    result_event = next(e for e in events if e.get("type") == "result")
    assert result_event["exit_code"] == 0
    payload = json.loads(result_event["text"])
    assert payload["is_fixture"] is True
    assert payload["provenance"]["mode"] == "fixture_transport"

    # Strictly verify production rejection of fixture
    adapter = RuntimeEvidenceAdapter(pinned_provider="openai-codex", pinned_model="gpt-6-astra")
    with pytest.raises(RuntimeError) as exc_info:
        adapter.verify_runtime_evidence(
            meta_event,
            response_text=result_event["text"],
            allow_fixture=False,
        )
    assert "Fixture runtime evidence rejected by production executor" in str(exc_info.value)




def test_isolated_fixture_roundtrip_with_benchmark_attribution_derived(tmp_path):
    """Verify that when benchmark inputs exist, benchmark attribution (alpha_bps) is derived,
    whereas without benchmark inputs it remains UNAVAILABLE and is never invented."""
    t0 = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)
    clock = [t0]
    runner, orders, pm, adapter = build_harness_environment(tmp_path, clock)

    symbol = "2330.TW"
    price_t0 = 1000.0
    adapter.set_bar(symbol, t0, 995.0, 1005.0, 990.0, price_t0)
    adapter.set_quote(symbol, t0, price_t0)

    # 1. Open BUY packet
    buy_packet = CIODecisionPacket(
        case_id="case-benchmark-attribution-buy-001",
        as_of=t0,
        evidence=["research://semi-cycle", "quote://2330.TW-1000"],
        thesis="Swing long setup with benchmark comparison",
        selected_instrument=symbol,
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=10.0,
        expiry=t0 + timedelta(hours=8),
        confidence=0.85,
        strategy_version="dynamic-desk-harness-v1",
        provenance=CIOProvenance(authority="MAIN_CIO", signer_id="main-cio-key", source="external_packet"),
        conditions={"allow_odd_lot": True}, is_fixture=True,
    )
    sign_cio_packet(buy_packet, signer_id="main-cio-key")
    d1 = runner.submit_cio_packet(buy_packet)
    assert d1.action == "BUY_PENDING"

    # Later quote fills the buy
    t1 = t0 + timedelta(minutes=5)
    clock[0] = t1
    later_price = 1000.0
    adapter.set_quote(symbol, t1, later_price)
    d_fill = runner.submit_cio_packet(buy_packet)
    assert d_fill.action == "BUY_FILLED"

    # 2. Close SELL packet with explicit benchmark input
    t2 = t1 + timedelta(days=1)
    clock[0] = t2
    adapter.set_bar(symbol, t2, 1030.0, 1040.0, 1025.0, 1035.0)
    adapter.set_quote(symbol, t2, 1035.0)

    # Taiwan 50 benchmark returned 2.0% during the holding period
    benchmark_return_pct = 2.0
    close_packet = CIODecisionPacket(
        case_id="case-benchmark-attribution-sell-001",
        as_of=t2,
        evidence=["quote://2330.TW-1035"],
        thesis="Take profit against benchmark",
        selected_instrument=symbol,
        action="SELL",
        holding_horizon=DecisionScope.SWING,
        quantity=10.0,
        conditions={**({
            "target_case_id": buy_packet.case_id,
            "benchmark_symbol": "0050.TW",
            "benchmark_return_pct": benchmark_return_pct,
        }), "allow_odd_lot": True},
        expiry=t2 + timedelta(hours=8),
        confidence=0.88,
        strategy_version="dynamic-desk-harness-v1",
        provenance=CIOProvenance(authority="MAIN_CIO", signer_id="main-cio-key", source="external_packet"),
        is_fixture=True,
    )
    sign_cio_packet(close_packet, signer_id="main-cio-key")
    d_close_pending = runner.submit_cio_packet(close_packet)
    assert d_close_pending.action == "SELL_PENDING"

    t2_fill = t2 + timedelta(minutes=5)
    clock[0] = t2_fill
    adapter.set_quote(symbol, t2_fill, 1040.0)
    d_close = runner.submit_cio_packet(close_packet)
    assert d_close.action == "SELL_FILLED"

    # Verify benchmark attribution was derived
    attribution = d_close.inputs["attribution"]
    assert attribution["status"] == "DERIVED"
    assert attribution["benchmark_status"] == "AVAILABLE"
    assert attribution["benchmark_symbol"] == "0050.TW"
    assert attribution["benchmark_return_pct"] == benchmark_return_pct
    assert attribution["alpha_bps"] is not None

    trade_ret = d_close.inputs["outcome"]["return_pct"]
    expected_alpha = round((trade_ret - benchmark_return_pct) * 100.0, 2)
    assert abs(attribution["alpha_bps"] - expected_alpha) < 0.01

    # Verify context retrieval includes derived alpha attribution
    clock[0] = t2_fill + timedelta(seconds=1)  # Lessons become eligible only after the outcome.
    ctx = runner.build_decision_context_request(symbols=[symbol])
    lesson = next((l for l in ctx.prior_lessons if l["case_id"] == buy_packet.case_id), None)
    assert lesson is not None
    assert lesson["attribution"]["status"] == "DERIVED"
    assert lesson["attribution"]["alpha_bps"] == expected_alpha










def test_paper_cio_route_defaults_to_six_sol_and_escalates_only_explicitly():
    from cio_market_lab.integrations.hermes_chat import resolve_cio_route

    default = resolve_cio_route(provider_id="openai-codex")
    assert default == {"provider_id": "openai-codex", "model_id": "gpt-6.1-sol", "route": "default", "escalation_reason": ""}
    for reason in ("high_consequence", "thesis_conflict", "multiasset_complexity"):
        route = resolve_cio_route(escalation_reason=reason, provider_id="openai-codex")
        assert route["model_id"] == "gpt-6-astra"
        assert route["route"] == "exceptional_escalation"
        assert route["escalation_reason"] == reason


def test_paper_cio_route_rejects_implicit_or_unknown_escalation_and_preserves_session():
    from cio_market_lab.integrations.hermes_chat import HermesCIODecisionExecutor

    executor = HermesCIODecisionExecutor(session_id="project-money-main-cio")
    assert executor.model_id == "gpt-6.1-sol"
    assert executor.session_id == "project-money-main-cio"
    with pytest.raises(ValueError, match="Unsupported CIO escalation"):
        HermesCIODecisionExecutor(escalation_reason="quota_exhausted")
    with pytest.raises(ValueError, match="conflicts"):
        HermesCIODecisionExecutor(model_id="gpt-6.1-sol", escalation_reason="thesis_conflict")
