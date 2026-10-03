"""Test suite for Runtime Continuity, Public Market-Data Probe, and Continuation Entrypoint.

Verifies:
1. Public market-data probe:
   - CLI flags and argument parsing.
   - Preservation of exact source timestamps and calculation of data age.
   - Honest session freshness classification across open, weekend, pre-market, and after-hours regimes.
   - Next market session timestamps and release condition disclosure.
   - Explicit is_fixture=True labeling when using fixture transport.
   - TwOfficialAdapter and CompositeMarketDataAdapter wiring and fallback.
2. Local continuation entrypoint:
   - Idempotent file lock preventing concurrent process execution.
   - Checkpoints persisted atomically after each cycle.
   - Bounded retries for transient runner errors resulting in HALTED_MAX_RETRIES.
   - Kill switch enforcement resulting in HALTED_KILL_SWITCH.
   - Explicit terminal states (COMPLETED, BLOCKED, INTERRUPTED, HALTED_MAX_RETRIES, HALTED_KILL_SWITCH, HALTED_LOCK_FAILED).
   - Multi-cycle paper simulation.
   - Interruption recovery restoring single canonical cash and deduplicating fills without double-counting.
   - Learning records from cycle N consumed in decision context of cycle N+1.
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional
import pytest

from cio_market_lab.data.base import MarketDataAdapter
from cio_market_lab.data.probe import (
    CalendarUncertaintyError,
    classify_data_freshness,
    classify_market_session,
    execute_public_market_data_probe,
    probe_single_symbol,
)
from cio_market_lab.data.tw_official import TwOfficialAdapter
from cio_market_lab.data.market_data import CompositeMarketDataAdapter
from cio_market_lab.data.yahoo import YahooAdapter
from cio_market_lab.domain.models import (
    Bar,
    CIODecisionPacket,
    CIOProvenance,
    DecisionScope,
    Market,
    Position,
    Quote,
)
from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner, DYNAMIC_DESK_ID
from cio_market_lab.engine.cio_packet import sign_cio_packet
from cio_market_lab.engine.continuation import (
    ContinuationCheckpoint,
    ContinuationLock,
    ContinuationLockAcquisitionError,
    ContinuationRunner,
    CorruptCheckpointError,
    StrategyMismatchError,
)
from cio_market_lab.engine.paper_orders import PaperExperimentSettings, PaperOrderService
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.events.store import EventStore


class MockMarketAdapter(MarketDataAdapter):
    """Deterministic adapter for continuity testing."""

    def __init__(self, base_time: datetime):
        self.base_time = base_time
        self.bars: Dict[str, List[Bar]] = {}
        self.quotes: Dict[str, Quote] = {}

    @property
    def source_name(self) -> str:
        return "mock_continuity_adapter"

    def set_bar(self, symbol: str, dt: datetime, o: float, h: float, l: float, c: float, v: float = 1000.0) -> None:
        self.bars.setdefault(symbol, []).append(
            Bar(
                symbol=symbol,
                timestamp=dt,
                observed_at=dt,
                open=o,
                high=h,
                low=l,
                close=c,
                volume=v,
                source="fixture_mock_continuity",
                quality="good",
                is_stale=False,
            )
        )

    def set_quote(self, symbol: str, dt: datetime, price: float) -> None:
        self.quotes[symbol] = Quote(bid_size=1000, ask_size=1000,
            quote_id=f"TEST_ONLY_{symbol}_{(dt).isoformat()}",
            session="ODD_LOT" if (symbol).endswith(".TW") else "REGULAR",
            source_capabilities={"source": "fixture_mock_continuity", "two_sided_book": True,
                "size_backed": True, "exchange_session_attested": True,
                "entitlement_evidence_id": "TEST_ONLY_FIXTURE_ODD_LOT",
                "entitlement_status": "TEST_ONLY", "odd_lot_book": True,
                "supported_sessions": ["REGULAR", "ODD_LOT"]},
            
            symbol=symbol,
            timestamp=dt,
            observed_at=dt,
            bid=price - 1.0,
            ask=price + 1.0,
            last_price=price,
            source="fixture_mock_continuity",
            quality="good",
            is_stale=False,
            is_synthetic=False,
        )

    def get_bars(self, symbol: str, start=None, end=None, timeframe="1D", limit=None) -> List[Bar]:
        b = self.bars.get(symbol, [])
        return b[-limit:] if limit is not None else b

    def get_latest_bar(self, symbol: str) -> Optional[Bar]:
        b = self.bars.get(symbol, [])
        return b[-1] if b else None

    def get_latest_quote(self, symbol: str) -> Optional[Quote]:
        return self.quotes.get(symbol)

    def stream_bars(self, symbols: List[str]):
        for s in symbols:
            b = self.get_latest_bar(s)
            if b:
                yield b


def build_harness(tmp_path: Path, clock: List[datetime]) -> tuple[AutonomousPaperRunner, PaperOrderService, PortfolioManager, MockMarketAdapter]:
    pm = PortfolioManager(initial_cash_swing=1_000_000.0, initial_cash_intraday=1_000_000.0)
    pm.register_strategy(DYNAMIC_DESK_ID, 1_000_000.0, unified_cash=True)
    es = EventStore(":memory:")
    po = PaperOrderService(pm, es)
    adapter = MockMarketAdapter(clock[0])
    runner = AutonomousPaperRunner(
        root=tmp_path,
        portfolio_manager=pm,
        paper_orders=po,
        market_adapter=adapter,
        now_fn=lambda: clock[0],
        require_cio_provider=True,
    )
    runner.allow_fixture_quotes = True  # Explicit isolated fixture opt-in.
    runner.configure(
        PaperExperimentSettings(
            strategy_id=DYNAMIC_DESK_ID,
            enabled=True,
            universe=["2330.TW"],
            initial_cash=1_000_000.0,
            max_position_notional=500_000.0,
        )
    )
    return runner, po, pm, adapter


# =========================================================================
# 1. Public Market-Data Probe Tests
# =========================================================================

def test_probe_session_classification_taiwan_and_us():
    """Verify honest session classification for TW and US markets."""
    # Tuesday 08:30 UTC = Tuesday 16:30 CST (after hours for TW regular session)
    t_tw_after = datetime(2026, 9, 29, 8, 30, tzinfo=timezone.utc)
    tw_session = classify_market_session("2330.TW", t_tw_after)
    assert tw_session["market"] == "TW"
    assert tw_session["session_state"] == "CLOSED_AFTER_HOURS"
    assert "09:00:00" in tw_session["release_condition"]

    # Tuesday 00:30 UTC = Tuesday 08:30 CST (pre-market for TW regular session)
    t_tw_pre = datetime(2026, 9, 29, 0, 30, tzinfo=timezone.utc)
    tw_session_pre = classify_market_session("2330.TW", t_tw_pre)
    assert tw_session_pre["session_state"] == "CLOSED_PRE_MARKET"

    # Saturday 12:00 UTC (weekend)
    t_weekend = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    us_session = classify_market_session("AAPL", t_weekend)
    assert us_session["market"] == "US"
    assert us_session["session_state"] == "CLOSED_WEEKEND"
    assert "09:30:00 EDT" in us_session["release_condition"]


def test_probe_freshness_classification_logic():
    """Verify classification of data freshness across session states."""
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    # Synthetic fixture is always marked SYNTHETIC_FIXTURE
    assert classify_data_freshness(None, {"market_open_now": True, "session_state": "REGULAR_OPEN"}, now, is_fixture=True) == "SYNTHETIC_FIXTURE"

    # Missing bar is UNAVAILABLE
    assert classify_data_freshness(None, {"market_open_now": True, "session_state": "REGULAR_OPEN"}, now, is_fixture=False) == "UNAVAILABLE"

    # Market open with fresh bar (< 30 min)
    bar_fresh = Bar(
        symbol="2330.TW", timestamp=now - timedelta(minutes=10), observed_at=now,
        open=1000, high=1005, low=995, close=1000, volume=1000, source="test",
    )
    assert classify_data_freshness(bar_fresh, {"market_open_now": True, "session_state": "REGULAR_OPEN"}, now) == "FRESH_INTRA_SESSION"

    # Weekend with Friday bar (2 days old) -> STALE_OFF_SESSION_EXPECTED
    bar_friday = Bar(
        symbol="2330.TW", timestamp=now - timedelta(days=2), observed_at=now,
        open=1000, high=1005, low=995, close=1000, volume=1000, source="test",
    )
    assert classify_data_freshness(bar_friday, {"market_open_now": False, "session_state": "CLOSED_WEEKEND"}, now) == "STALE_OFF_SESSION_EXPECTED"


def test_probe_single_symbol_preserves_timestamps_and_fixture_label():
    """Verify probe_single_symbol preserves timestamps and accurately flags test fixtures."""
    now = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)
    res = probe_single_symbol("2330.TW", now=now, test_fixture=True)

    assert res["is_fixture"] is True
    assert res["source"] == "fixture_probe"
    assert res["freshness_classification"] == "SYNTHETIC_FIXTURE"
    assert "bar_timestamp" in res and res["bar_timestamp"] is not None
    assert "observed_at" in res and res["observed_at"] == now.isoformat()
    assert res["data_age_seconds"] is not None
    assert "next_session" in res
    assert "release_condition" in res["next_session"]


def test_execute_public_market_data_probe_structure():
    """Verify overall public probe report structure."""
    now = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)
    report = execute_public_market_data_probe(
        symbols=["2330.TW", "AAPL"],
        test_fixture=True,
        now=now,
    )
    assert report["read_only"] is True
    assert report["broker_connected"] is False
    assert report["credentials_loaded"] is False
    assert len(report["results"]) == 2
    assert "future_market_session_gaps" in report
    assert "TW" in report["future_market_session_gaps"]
    assert "US" in report["future_market_session_gaps"]


def test_tw_official_adapter_and_composite_wiring():
    """Verify TwOfficialAdapter and CompositeMarketDataAdapter wiring in offline mode."""
    tw_adapter = TwOfficialAdapter(offline_mode=True)
    assert tw_adapter.source_name == "twse_tpex_openapi"
    assert tw_adapter.get_bars("2330.TW") == []
    assert tw_adapter.get_latest_quote("2330.TW") is None

    composite = CompositeMarketDataAdapter(offline_mode=True)
    assert composite.source_name == "composite_public"
    # Routes TW to tw_adapter
    assert composite._select_adapter("2330.TW") is composite.tw_adapter
    # Routes US to us_adapter
    assert composite._select_adapter("AAPL") is composite.us_adapter


# =========================================================================
# 2. Local Continuation Entrypoint Tests
# =========================================================================

def test_continuation_lock_mutual_exclusion(tmp_path: Path):
    """Verify ContinuationLock prevents concurrent execution."""
    lock_file = tmp_path / "runtime" / "continuation.lock"
    lock1 = ContinuationLock(lock_file)
    lock2 = ContinuationLock(lock_file)

    assert lock1.acquire() is True
    assert lock1.is_locked is True

    # Second lock attempt must fail
    with pytest.raises(ContinuationLockAcquisitionError):
        lock2.acquire()

    lock1.release()
    assert lock1.is_locked is False

    # Now lock2 can acquire
    assert lock2.acquire() is True
    lock2.release()


def test_continuation_checkpoint_save_and_load(tmp_path: Path):
    """Verify atomic checkpoint persistence and load."""
    clock = [datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)]
    runner, _, _, _ = build_harness(tmp_path, clock)
    cont = ContinuationRunner(runner, runtime_dir=tmp_path / "runtime")

    assert cont.load_checkpoint() is None

    chk = ContinuationCheckpoint(
        strategy_id=DYNAMIC_DESK_ID,
        cycle_index=1,
        target_cycles=3,
        last_run_id="run-001",
        terminal_state="IN_PROGRESS",
        canonical_cash=980_000.0,
        portfolio_equity=1_000_000.0,
        open_positions=1,
        retries_attempted=0,
        runs=["run-001"],
    )
    cont.save_checkpoint(chk)

    loaded = cont.load_checkpoint()
    assert loaded is not None
    assert loaded.cycle_index == 1
    assert loaded.target_cycles == 3
    assert loaded.canonical_cash == 980_000.0
    assert loaded.last_run_id == "run-001"


def test_continuation_kill_switch_terminal_state(tmp_path: Path):
    """Verify continuation runner immediately halts with HALTED_KILL_SWITCH when kill switch is on."""
    clock = [datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)]
    runner, orders, _, _ = build_harness(tmp_path, clock)
    orders.set_kill_switch(True, "Emergency test stop")

    cont = ContinuationRunner(runner, runtime_dir=tmp_path / "runtime")
    result = cont.run(max_cycles=3)

    assert result["status"] == "HALTED_KILL_SWITCH"
    assert result["terminal_state"] == "HALTED_KILL_SWITCH"
    assert result["completed_cycles"] == 0

    chk = cont.load_checkpoint()
    assert chk is not None
    assert chk.terminal_state == "HALTED_KILL_SWITCH"


def test_continuation_bounded_retries_on_transient_failure(tmp_path: Path, monkeypatch):
    """Verify continuation runner retries bounded number of times before setting HALTED_MAX_RETRIES."""
    clock = [datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)]
    runner, _, _, _ = build_harness(tmp_path, clock)

    call_count = [0]
    def _failing_run_cycle(*args, **kwargs):
        call_count[0] += 1
        raise RuntimeError("Simulated transient market data crash")

    monkeypatch.setattr(runner, "run_one_cycle", _failing_run_cycle)

    cont = ContinuationRunner(runner, max_retries=3, runtime_dir=tmp_path / "runtime")
    result = cont.run(max_cycles=1)

    assert result["status"] == "HALTED_MAX_RETRIES"
    assert result["terminal_state"] == "HALTED_MAX_RETRIES"
    assert call_count[0] == 3

    chk = cont.load_checkpoint()
    assert chk is not None
    assert chk.terminal_state == "HALTED_MAX_RETRIES"
    assert chk.retries_attempted == 3


def test_continuation_blocked_terminal_state(tmp_path: Path):
    """Verify that when no CIO provider is configured, runner reports explicit BLOCKED terminal state."""
    clock = [datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)]
    runner, _, _, adapter = build_harness(tmp_path, clock)
    adapter.set_bar("2330.TW", clock[0], 995, 1005, 990, 1000)
    adapter.set_quote("2330.TW", clock[0], 1000)

    cont = ContinuationRunner(runner, runtime_dir=tmp_path / "runtime")
    result = cont.run(max_cycles=1, symbols=["2330.TW"])

    assert result["status"] == "BLOCKED"
    assert result["terminal_state"] == "BLOCKED"
    assert "BLOCKED_NO_CIO_DECISION_PROVIDER" in result["reason"]

    chk = cont.load_checkpoint()
    assert chk is not None
    assert chk.terminal_state == "BLOCKED"


def test_continuation_multi_cycle_interruption_recovery_and_learning_consumption(tmp_path: Path):
    """Comprehensive test verifying:
    1. Multi-cycle paper simulation.
    2. Interruption recovery: resumption does not double-count fills or cash debits.
    3. Persistent single ledger: single canonical cash account preserved.
    4. Learning records: lesson created in Cycle 1 is consumed in decision context of Cycle 2.
    """
    t0 = datetime(2026, 9, 28, 9, 30, tzinfo=timezone.utc)
    clock = [t0]
    runner, orders, pm, adapter = build_harness(tmp_path, clock)

    symbol = "2330.TW"
    adapter.set_bar(symbol, t0, 995, 1005, 990, 1000)
    adapter.set_quote(symbol, t0, 1000)

    # Stage BUY packet for Cycle 1
    pkt_buy = CIODecisionPacket(
        case_id="case-continuity-buy-001",
        as_of=t0,
        evidence=["quote://2330.TW-1000"],
        thesis="Cycle 1 breakout long",
        selected_instrument=symbol,
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=10.0,
        expiry=t0 + timedelta(hours=4),
        confidence=0.88,
        strategy_version="dynamic-desk-v2",
        provenance=CIOProvenance(authority="MAIN_CIO", signer_id="main-cio-key", source="external_packet"),
        conditions={"allow_odd_lot": True}, is_fixture=True,
    )
    sign_cio_packet(pkt_buy, signer_id="main-cio-key")
    runner.stage_cio_packet(pkt_buy)

    cont = ContinuationRunner(runner, runtime_dir=tmp_path / "runtime")

    # Run Cycle 1
    res1 = cont.run(max_cycles=1, symbols=[symbol])
    assert res1["status"] == "COMPLETED"
    assert res1["completed_cycles"] == 1

    # Buy was submitted as PENDING (waiting for authoritative later quote)
    # Provide authoritative later quote and fill
    t1 = t0 + timedelta(minutes=5)
    clock[0] = t1
    adapter.set_quote(symbol, t1, 1000.0)
    d_fill = runner.submit_cio_packet(pkt_buy)
    assert d_fill.action == "BUY_FILLED"

    desk_ledger = pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING)
    cash_after_buy = desk_ledger.cash
    assert cash_after_buy < 1_000_000.0  # Cash debited

    # Close trade and record outcome in learning store
    t_close = t1 + timedelta(days=1)
    clock[0] = t_close
    adapter.set_bar(symbol, t_close, 1045, 1055, 1040, 1050)
    adapter.set_quote(symbol, t_close, 1050)

    pkt_close = CIODecisionPacket(
        case_id="case-continuity-close-001",
        as_of=t_close,
        evidence=["quote://2330.TW-1050"],
        thesis="Cycle 1 profit take",
        selected_instrument=symbol,
        action="SELL",
        holding_horizon=DecisionScope.SWING,
        quantity=10.0,
        conditions={**({"target_case_id": pkt_buy.case_id}), "allow_odd_lot": True},
        expiry=t_close + timedelta(hours=4),
        confidence=0.92,
        strategy_version="dynamic-desk-v2",
        provenance=CIOProvenance(authority="MAIN_CIO", signer_id="main-cio-key", source="external_packet"),
        is_fixture=True,
    )
    sign_cio_packet(pkt_close, signer_id="main-cio-key")
    d_close_pending = runner.submit_cio_packet(pkt_close)
    assert d_close_pending.action == "SELL_PENDING"

    t_close_fill = t_close + timedelta(minutes=5)
    clock[0] = t_close_fill
    adapter.set_quote(symbol, t_close_fill, 1050.0)
    d_close = runner.submit_cio_packet(pkt_close)
    assert d_close.action == "SELL_FILLED"

    # Outcome recorded, lesson produced in learning store
    lessons = runner.learning_store._lessons
    assert len(lessons) == 1
    assert lessons[0].symbol == symbol
    assert lessons[0].outcome_details["realized_pnl"] > 0

    cash_after_close = desk_ledger.cash
    assert cash_after_close > cash_after_buy

    # -----------------------------------------------------------------
    # SIMULATE INTERRUPTION AND RECOVERY (Restart fresh instance)
    # -----------------------------------------------------------------
    # Recreate portfolio manager & runner pointing to the same runtime directory
    new_pm = PortfolioManager(initial_cash_swing=1_000_000.0, initial_cash_intraday=1_000_000.0)
    new_pm.register_strategy(DYNAMIC_DESK_ID, 1_000_000.0, unified_cash=True)
    new_po = PaperOrderService(new_pm, EventStore(":memory:"))
    restarted_runner = AutonomousPaperRunner(
        root=tmp_path,
        portfolio_manager=new_pm,
        paper_orders=new_po,
        market_adapter=adapter,
        now_fn=lambda: clock[0],
        require_cio_provider=True,
    )
    restarted_runner.allow_fixture_quotes = True
    restarted_runner.configure(
        PaperExperimentSettings(
            strategy_id=DYNAMIC_DESK_ID,
            enabled=True,
            universe=["2330.TW"],
            initial_cash=1_000_000.0,
            max_position_notional=500_000.0,
        )
    )

    # Verify single canonical cash preserved exactly after restart
    restarted_desk_ledger = new_pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING)
    assert abs(restarted_desk_ledger.cash - cash_after_close) < 1e-4

    # Verify learning store reloaded exactly 1 lesson
    assert len(restarted_runner.learning_store._lessons) == 1

    # -----------------------------------------------------------------
    # CYCLE 2: DEMONSTRATE LEARNING CONSUMPTION IN SUBSEQUENT CONTEXT
    # -----------------------------------------------------------------
    # In Cycle 2, context request must contain the lesson from Cycle 1
    clock[0] = t_close_fill + timedelta(seconds=1)  # Strictly later than recorded outcome.
    ctx_req = restarted_runner.build_decision_context_request(symbols=[symbol])
    assert len(ctx_req.prior_lessons) >= 1
    prior_lesson = ctx_req.prior_lessons[0]
    assert prior_lesson["case_id"] == pkt_buy.case_id
    assert prior_lesson["symbol"] == symbol
    assert "realized_pnl" in prior_lesson["outcome_details"]

    # Now stage Cycle 2 packet referencing prior lesson
    t2 = t_close_fill + timedelta(days=1)
    clock[0] = t2
    adapter.set_bar(symbol, t2, 1055, 1065, 1050, 1060)
    adapter.set_quote(symbol, t2, 1060)

    pkt_cycle2 = CIODecisionPacket(
        case_id="case-continuity-buy-002",
        as_of=t2,
        evidence=["quote://2330.TW-1060", f"lesson://{pkt_buy.case_id}"],
        thesis="Cycle 2 buy informed by Cycle 1 lesson",
        selected_instrument=symbol,
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=5.0,
        expiry=t2 + timedelta(hours=4),
        confidence=0.90,
        strategy_version="dynamic-desk-v2",
        provenance=CIOProvenance(authority="MAIN_CIO", signer_id="main-cio-key", source="external_packet"),
        conditions={"allow_odd_lot": True}, is_fixture=True,
    )
    sign_cio_packet(pkt_cycle2, signer_id="main-cio-key")
    restarted_runner.stage_cio_packet(pkt_cycle2)

    # Continue run Cycle 2 with ContinuationRunner on restarted runtime
    restarted_cont = ContinuationRunner(restarted_runner, runtime_dir=tmp_path / "runtime")
    res2 = restarted_cont.run(max_cycles=1, symbols=[symbol])
    assert res2["status"] == "COMPLETED"
    assert res2["completed_cycles"] == 2  # Resumed from 1 to 2

    # Checkpoint records cycle_index = 2
    final_chk = restarted_cont.load_checkpoint()
    assert final_chk is not None
    assert final_chk.cycle_index == 2
    assert final_chk.terminal_state == "COMPLETED"


def test_probe_cli_subprocess_invocation(tmp_path: Path):
    """Verify probe CLI runs cleanly via subprocess and produces valid JSON."""
    import subprocess
    import sys

    out_file = tmp_path / "probe_output.json"
    cmd = [
        sys.executable,
        "-m",
        "cio_market_lab.data.probe",
        "--symbols",
        "2330.TW,AAPL",
        "--test-fixture",
        "--output",
        str(out_file),
    ]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"Probe CLI failed: {proc.stderr}"
    assert out_file.exists()

    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["authority"] == "MAIN_CIO"
    assert payload["read_only"] is True
    assert payload["is_fixture"] is True
    assert len(payload["results"]) == 2
    assert payload["results"][0]["symbol"] == "2330.TW"
    assert payload["results"][0]["freshness_classification"] == "SYNTHETIC_FIXTURE"
    assert payload["results"][1]["symbol"] == "AAPL"
    assert "future_market_session_gaps" in payload
    assert "release_condition" in payload["future_market_session_gaps"]["TW"]


def test_probe_us_timezone_and_dst():
    """Verify America/New_York DST handling for US market sessions."""
    # Summer: 2026-07-15 12:00 UTC (EDT = UTC-4)
    dt_summer = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    res_summer = classify_market_session("AAPL", dt_summer)
    assert res_summer["market"] == "US"
    assert "09:30:00 EDT" in res_summer["release_condition"]
    assert "13:30:00 UTC" in res_summer["release_condition"]
    assert "-04:00" in res_summer["local_time"]

    # Winter: 2026-12-15 12:00 UTC (EST = UTC-5)
    dt_winter = datetime(2026, 12, 15, 12, 0, tzinfo=timezone.utc)
    res_winter = classify_market_session("AAPL", dt_winter)
    assert res_winter["market"] == "US"
    assert "09:30:00 EST" in res_winter["release_condition"]
    assert "14:30:00 UTC" in res_winter["release_condition"]
    assert "-05:00" in res_winter["local_time"]


def test_probe_holiday_fixtures_and_no_past_opens():
    """Verify exchange-calendar holiday detection and guaranteed non-past next_open."""
    # Taiwan Mid-Autumn Festival: 2026-09-28
    dt_tw_holiday = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)
    tw_res = classify_market_session("2330.TW", dt_tw_holiday)
    assert tw_res["session_state"] == "CLOSED_HOLIDAY"
    # Must skip holiday and point to next regular session (2026-09-29 01:00 UTC)
    assert tw_res["next_market_open_iso"] == "2026-09-29T01:00:00+00:00"
    next_open_dt = datetime.fromisoformat(tw_res["next_market_open_iso"])
    assert next_open_dt > dt_tw_holiday

    # US Independence Day observed: 2026-07-03
    dt_us_holiday = datetime(2026, 7, 3, 14, 0, tzinfo=timezone.utc)
    us_res = classify_market_session("AAPL", dt_us_holiday)
    assert us_res["session_state"] == "CLOSED_HOLIDAY"
    # Skips to Monday 2026-07-06 13:30 UTC
    assert us_res["next_market_open_iso"] == "2026-07-06T13:30:00+00:00"
    assert datetime.fromisoformat(us_res["next_market_open_iso"]) > dt_us_holiday


def test_probe_calendar_uncertainty_fails_closed():
    """Verify that unknown exchange suffixes or invalid symbols fail closed."""
    with pytest.raises(CalendarUncertaintyError):
        classify_market_session("VOD.L", datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc))

    with pytest.raises(CalendarUncertaintyError):
        classify_market_session("", datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc))


def test_probe_eod_capability_and_freshness():
    """Verify that EOD adapter does not promise fresh ticks and is properly classified."""
    tw_adapter = TwOfficialAdapter(offline_mode=True)
    assert getattr(tw_adapter, "is_eod", False) is True
    assert getattr(tw_adapter, "supports_intraday_ticks", True) is False

    now = datetime(2026, 9, 29, 3, 0, tzinfo=timezone.utc)  # Market open (11:00 CST)
    open_session = {"market_open_now": True, "session_state": "REGULAR_OPEN"}

    # An EOD bar during active market session cannot be classified as fresh intra-session
    bar_eod = Bar(
        symbol="2330.TW", timestamp=now - timedelta(minutes=5), observed_at=now,
        open=1000, high=1010, low=995, close=1005, volume=10000,
        source="twse_tpex_openapi", quality="official_eod",
    )
    freshness = classify_data_freshness(bar_eod, open_session, now, is_eod=True)
    assert freshness == "STALE_INTRA_SESSION"

    # EOD release condition does not promise live ticks upon open
    eod_session = classify_market_session("2330.TW", now, is_eod=True)
    assert "provides EOD settled quotes" in eod_session["release_condition"]
    assert "does not provide live intraday ticks" in eod_session["release_condition"]


def test_probe_future_timestamp_fails_freshness():
    """Verify that future timestamps are rejected and fail freshness."""
    now = datetime(2026, 9, 29, 3, 0, tzinfo=timezone.utc)
    future_bar = Bar(
        symbol="AAPL", timestamp=now + timedelta(minutes=15), observed_at=now,
        open=200, high=205, low=198, close=202, volume=5000, source="yahoo",
    )
    res = classify_data_freshness(future_bar, {"market_open_now": True, "session_state": "REGULAR_OPEN"}, now)
    assert res == "FUTURE_TIMESTAMP_INVALID"


def test_continuation_corrupt_checkpoint_fails_closed(tmp_path: Path):
    """Verify continuation runner fails closed on corrupt checkpoint without resetting."""
    clock = [datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)]
    runner, _, _, _ = build_harness(tmp_path, clock)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    chk_file = runtime_dir / "continuation_checkpoint.json"
    chk_file.write_text("{corrupt: json content", encoding="utf-8")

    cont = ContinuationRunner(runner, runtime_dir=runtime_dir)

    # Direct load raises CorruptCheckpointError
    with pytest.raises(CorruptCheckpointError):
        cont.load_checkpoint()

    # run() halts in ERROR state rather than silently resetting cycle_index to 0
    res = cont.run(max_cycles=3)
    assert res["status"] == "ERROR"
    assert res["terminal_state"] == "ERROR"
    assert "Corrupt checkpoint" in res["error"]
    assert res["completed_cycles"] == 0


def test_continuation_strategy_mismatch_fails_closed(tmp_path: Path):
    """Verify continuation runner fails closed when checkpoint strategy_id does not match."""
    clock = [datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)]
    runner, _, _, _ = build_harness(tmp_path, clock)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    other_chk = ContinuationCheckpoint(
        strategy_id="other-strategy",
        cycle_index=2,
        target_cycles=5,
        canonical_cash=500_000.0,
        portfolio_equity=500_000.0,
        open_positions=0,
    )
    chk_file = runtime_dir / "continuation_checkpoint.json"
    chk_file.write_text(json.dumps(other_chk.model_dump(mode="json")), encoding="utf-8")

    cont = ContinuationRunner(runner, strategy_id=DYNAMIC_DESK_ID, runtime_dir=runtime_dir)

    # Direct load raises StrategyMismatchError
    with pytest.raises(StrategyMismatchError):
        cont.load_checkpoint()

    # run() halts in ERROR state rather than silently adopting or resetting
    res = cont.run(max_cycles=3)
    assert res["status"] == "ERROR"
    assert res["terminal_state"] == "ERROR"
    assert "strategy mismatch" in res["error"].lower()


def test_portfolio_shared_canonical_cash_not_duplicated_in_equity():
    """Verify shared canonical cash is counted exactly once in total equity across buckets."""
    pm = PortfolioManager(initial_cash_swing=1_000_000.0, initial_cash_intraday=1_000_000.0)
    pm.register_strategy("multi-bucket-desk", 1_000_000.0, unified_cash=True)

    swing_ledger = pm.get_strategy_ledger("multi-bucket-desk", DecisionScope.SWING)
    intraday_ledger = pm.get_strategy_ledger("multi-bucket-desk", DecisionScope.INTRADAY)

    # Verify both ledgers point to the exact same shared cash account
    assert swing_ledger._cash_account is intraday_ledger._cash_account
    assert swing_ledger.cash == 1_000_000.0
    assert intraday_ledger.cash == 1_000_000.0

    # Add position to SWING
    swing_pos = Position(symbol="2330.TW", bucket=DecisionScope.SWING, quantity=10, average_entry_price=1000)
    swing_pos.current_price = 1050.0
    swing_pos.market_value = 10_500.0
    swing_ledger.positions["2330.TW"] = swing_pos

    # Add position to INTRADAY
    intra_pos = Position(symbol="AAPL", bucket=DecisionScope.INTRADAY, quantity=20, average_entry_price=200)
    intra_pos.current_price = 210.0
    intra_pos.market_value = 4_200.0
    intraday_ledger.positions["AAPL"] = intra_pos

    # Total equity across both buckets MUST be cash (1_000_000) + market_values (14_700) = 1_014_700
    # It must NEVER be 2 * cash (2_000_000) + 14_700
    equity = pm.get_strategy_equity("multi-bucket-desk", [DecisionScope.SWING, DecisionScope.INTRADAY])
    assert equity == 1_014_700.0


def test_continuation_cli_subprocess_and_recovery(tmp_path: Path):
    """Verify bounded executable CLI for ContinuationRunner via subprocess, including restart recovery."""
    import subprocess
    import sys

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    out_file = tmp_path / "continuation_out.json"

    env = dict(os.environ)
    env["CIO_MARKET_LAB_RUNTIME_DIR"] = str(runtime_dir)
    env["CIO_MARKET_LAB_OFFLINE"] = "1"
    env["PYTHONPATH"] = "."

    cmd = [
        sys.executable,
        "-m",
        "cio_market_lab.engine.continuation",
        "--max-cycles",
        "1",
        "--runtime-dir",
        str(runtime_dir),
        "--output",
        str(out_file),
    ]

    # Run Cycle 1 via subprocess
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, f"Continuation CLI failed: {proc.stderr}\nStdout: {proc.stdout}"
    assert out_file.exists()

    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["terminal_state"] in ("COMPLETED", "BLOCKED")
    assert data["checkpoint"] is not None
    assert data["checkpoint"]["cycle_index"] >= 1

    # Run Cycle 2 via subprocess on the same runtime_dir (interruption/subprocess restart recovery)
    proc2 = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert proc2.returncode == 0, f"Continuation CLI cycle 2 failed: {proc2.stderr}"
    data2 = json.loads(out_file.read_text(encoding="utf-8"))
    assert data2["checkpoint"]["cycle_index"] == data["checkpoint"]["cycle_index"] + 1

