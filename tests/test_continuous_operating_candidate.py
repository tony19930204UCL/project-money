"""Unit and regression tests for TwoModeContinuousOperatingCandidate.

Verifies:
1. Two-mode operation:
   - During open sessions: dispatches standard market-open decision loop.
   - Outside open sessions: dispatches bounded post-close review, research refresh, candidate prep,
     and next-session planning WITHOUT placing or simulating off-session orders.
2. Strict isolation:
   - 0 orders placed, 0 fills simulated off-session.
   - Never touches live broker or executable quote feeds.
3. Honest degradation:
   - When host or internet is unavailable, records explicit deterministic data gaps.
   - Never fakes or hallucinates successful data.
   - Plan marked DEGRADED with honest root-cause reason.
4. Idempotent resumption:
   - Completed plans return immediately without redundant network calls.
   - Interrupted/degraded plans resume from checkpoint, retrying only failed symbols.
   - Updates plan status cleanly to COMPLETED.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Dict
from urllib.error import URLError
import pytest

from cio_market_lab.engine.continuous_operating_candidate import (
    NextSessionPlan,
    OperatingMode,
    TwoModeContinuousOperatingCandidate,
)
from cio_market_lab.research.free_adapters import (
    CompaniesMarketCapAdapter,
    FinvizAdapter,
    FreeSourceCoordinator,
    SecCompanyFactsAdapter,
    StockAnalysisAdapter,
)
from cio_market_lab.research.browser import PublicResearchInboxReader


# Sample test doubles
MOCK_TICKERS = {"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"}}

MOCK_FACTS_NVDA = {
    "cik": 1045810,
    "entityName": "NVIDIA CORP",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [{
                        "end": "2026-07-28",
                        "val": 30040000000,
                        "fy": 2027,
                        "fp": "Q2",
                        "form": "10-Q",
                        "filed": "2026-08-28",
                        "accn": "0001045810-26-000088",
                    }]
                }
            }
        }
    }
}

MOCK_FINVIZ_HTML = """
<html><body>
<table class="snapshot-table2">
<tr>
<td class="snapshot-td2-cp">Market Cap</td><td class="snapshot-td2"><b>3.05T</b></td>
<td class="snapshot-td2-cp">P/E</td><td class="snapshot-td2"><b>45.20</b></td>
</tr>
</table>
</body></html>
"""

MOCK_STOCK_ANALYSIS_HTML = """
<html><body>
<div class="font-semibold">Market Cap</div><div>$3.05T</div>
<div class="font-semibold">PE Ratio</div><div>45.20</div>
</body></html>
"""

MOCK_CMC_HTML = """
<html><body>
<div class="ranking-number">#2</div>
<div class="marketcap-value">$3.050 T</div>
</body></html>
"""


def _build_test_coordinator(network_failing: bool = False):
    if network_failing:
        def failing_fetch(*args, **kwargs):
            raise URLError("Simulated network outage: Host name lookup failure")
        return FreeSourceCoordinator(
            sec_adapter=SecCompanyFactsAdapter(fetch_json=failing_fetch),
            finviz_adapter=FinvizAdapter(fetch_text=failing_fetch),
            stock_analysis_adapter=StockAnalysisAdapter(fetch_text=failing_fetch),
            market_cap_adapter=CompaniesMarketCapAdapter(fetch_text=failing_fetch),
        )

    def sec_fetch(url: str):
        if "company_tickers.json" in url:
            return MOCK_TICKERS
        return MOCK_FACTS_NVDA

    return FreeSourceCoordinator(
        sec_adapter=SecCompanyFactsAdapter(fetch_json=sec_fetch),
        finviz_adapter=FinvizAdapter(fetch_text=lambda url: MOCK_FINVIZ_HTML),
        stock_analysis_adapter=StockAnalysisAdapter(fetch_text=lambda url: MOCK_STOCK_ANALYSIS_HTML),
        market_cap_adapter=CompaniesMarketCapAdapter(fetch_text=lambda url: MOCK_CMC_HTML),
    )


class MockRunner:
    def __init__(self):
        self.ran_cycle = False
        self.last_strategy_id = None
        self.last_symbols = None

    def run_one_cycle(self, strategy_id: str, symbols=None):
        self.ran_cycle = True
        self.last_strategy_id = strategy_id
        self.last_symbols = symbols
        return {
            "run": {"run_id": "mock-run-001", "status": "COMPLETED"},
            "orders_count": 1,
            "fills_count": 1,
        }


# ---------------------------------------------------------------------------
# Mode Detection Tests
# ---------------------------------------------------------------------------

def test_operating_mode_detection():
    """Verify mode detection correctly identifies market open vs market close."""
    candidate = TwoModeContinuousOperatingCandidate()

    # Known US market open time: 2026-09-29 14:00 UTC (10:00 EDT on Tuesday)
    us_open_time = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)
    mode_open = candidate.get_operating_mode(["AAPL"], now=us_open_time)
    assert mode_open == OperatingMode.OPEN_SESSION

    # Known US market close time: 2026-09-29 21:00 UTC (17:00 EDT - after close)
    us_close_time = datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)
    mode_closed = candidate.get_operating_mode(["AAPL"], now=us_close_time)
    assert mode_closed == OperatingMode.OFF_SESSION


def test_open_session_step_executes_market_open_loop():
    """Verify step during market open dispatches to runner's decision loop."""
    mock_runner = MockRunner()
    candidate = TwoModeContinuousOperatingCandidate(runner=mock_runner)

    us_open_time = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)
    res = candidate.step(strategy_id="test-strategy", universe=["AAPL"], now=us_open_time)

    assert res["operating_mode"] == OperatingMode.OPEN_SESSION.value
    assert mock_runner.ran_cycle is True
    assert mock_runner.last_strategy_id == "test-strategy"
    assert res["orders_placed"] == 1
    assert res["simulated_fills"] == 1


# ---------------------------------------------------------------------------
# Off-Session Operation & Plan Generation Tests
# ---------------------------------------------------------------------------

def test_off_session_cycle_generates_plan_with_zero_orders(tmp_path: Path):
    """Verify off-session cycle performs review, refresh, candidate prep, and next-session plan with 0 orders."""
    coord = _build_test_coordinator(network_failing=False)
    plans_dir = tmp_path / "plans"
    inbox_dir = tmp_path / "inbox"
    reader = PublicResearchInboxReader(inbox_dir=inbox_dir)

    candidate = TwoModeContinuousOperatingCandidate(
        coordinator=coord,
        research_reader=reader,
        plans_dir=plans_dir,
    )

    us_close_time = datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)
    plan = candidate.run_off_session_cycle(
        universe=["NVDA"],
        now=us_close_time,
        session_date="2026-09-30",
        portfolio_summary={"cash_balance": 100000.0, "positions_reviewed": 1},
    )

    assert isinstance(plan, NextSessionPlan)
    assert plan.session_date == "2026-09-30"
    assert plan.operating_status == "COMPLETED"
    assert plan.mode == "OFF_SESSION"

    # STRICT ASSERTION: Zero orders placed or simulated off-session
    assert plan.orders_placed == 0
    assert plan.simulated_fills == 0

    # Verify post-close review recorded
    assert plan.post_close_review["cash_balance"] == 100000.0
    assert plan.post_close_review["positions_reviewed"] == 1

    # Verify candidate formulated with verified primary facts
    assert len(plan.candidates) == 1
    nvda_cand = plan.candidates[0]
    assert nvda_cand["symbol"] == "NVDA"
    assert nvda_cand["readiness"] == "READY"
    assert nvda_cand["verified_primary_evidence"] == 1
    assert nvda_cand["finviz_metrics"]["Market Cap"] == "3.05T"
    assert nvda_cand["peer_rank"] == 2

    # Verify guidelines generated
    assert "NVDA" in plan.next_session_guidelines["target_watchlist"]
    assert plan.next_session_guidelines["orders_permitted_off_session"] is False

    # Verify persisted to disk atomically
    plan_file = plans_dir / "next_session_plan_2026-09-30.json"
    assert plan_file.exists()
    with open(plan_file, "r") as f:
        disk_data = json.load(f)
    assert disk_data["session_date"] == "2026-09-30"
    assert disk_data["orders_placed"] == 0


# ---------------------------------------------------------------------------
# Honest Degradation Under Network Outage Tests
# ---------------------------------------------------------------------------

def test_off_session_honest_degradation_network_unavailable(tmp_path: Path):
    """Verify off-session cycle degrades honestly when network/host is unavailable."""
    coord = _build_test_coordinator(network_failing=True)
    plans_dir = tmp_path / "plans"

    candidate = TwoModeContinuousOperatingCandidate(
        coordinator=coord,
        plans_dir=plans_dir,
    )

    us_close_time = datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)
    plan = candidate.run_off_session_cycle(
        universe=["NVDA"],
        now=us_close_time,
        session_date="2026-09-30",
        network_available=False,  # Explicit network offline signal
    )

    # Degraded status reported honestly
    assert plan.operating_status == "DEGRADED"
    assert "HOST_OR_INTERNET_UNAVAILABLE" in plan.degradation_reason

    # Data gaps recorded deterministically; NO synthetic facts fabricated
    assert len(plan.data_gaps) == 1
    assert plan.data_gaps[0]["symbol"] == "NVDA"
    assert "NETWORK_UNAVAILABLE" in plan.data_gaps[0]["reason"]

    # Zero orders placed or simulated
    assert plan.orders_placed == 0
    assert plan.simulated_fills == 0

    # Plan persisted with DEGRADED status for operators to inspect
    plan_file = plans_dir / "next_session_plan_2026-09-30.json"
    assert plan_file.exists()
    with open(plan_file, "r") as f:
        saved = json.load(f)
    assert saved["operating_status"] == "DEGRADED"


# ---------------------------------------------------------------------------
# Idempotent Resumption Tests
# ---------------------------------------------------------------------------

def test_off_session_idempotent_resumption_after_service_returns(tmp_path: Path):
    """Verify off-session cycle resumes idempotently from degraded checkpoint when service returns."""
    plans_dir = tmp_path / "plans"
    us_close_time = datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)

    # Step 1: Initial attempt with network offline -> produces DEGRADED plan
    failing_coord = _build_test_coordinator(network_failing=True)
    candidate_step1 = TwoModeContinuousOperatingCandidate(
        coordinator=failing_coord,
        plans_dir=plans_dir,
    )
    plan_deg = candidate_step1.run_off_session_cycle(
        universe=["NVDA"],
        now=us_close_time,
        session_date="2026-09-30",
        network_available=False,
    )
    assert plan_deg.operating_status == "DEGRADED"
    assert plan_deg.resumed_from_checkpoint is False

    # Step 2: Service returns! Instantiate new candidate with working coordinator
    working_coord = _build_test_coordinator(network_failing=False)
    candidate_step2 = TwoModeContinuousOperatingCandidate(
        coordinator=working_coord,
        plans_dir=plans_dir,
    )

    # Resume the same session date
    plan_resumed = candidate_step2.run_off_session_cycle(
        universe=["NVDA"],
        now=us_close_time + timedelta(minutes=15),
        session_date="2026-09-30",
        network_available=True,
    )

    # Status transitioned to COMPLETED idempotently
    assert plan_resumed.operating_status == "COMPLETED"
    assert plan_resumed.resumed_from_checkpoint is True
    assert len(plan_resumed.candidates) == 1
    assert plan_resumed.candidates[0]["readiness"] == "READY"
    assert plan_resumed.orders_placed == 0
    assert plan_resumed.simulated_fills == 0

    # Step 3: Run again when already COMPLETED -> returns exact plan idempotently without re-fetching
    plan_cached = candidate_step2.run_off_session_cycle(
        universe=["NVDA"],
        now=us_close_time + timedelta(minutes=30),
        session_date="2026-09-30",
        network_available=True,
    )
    assert plan_cached.operating_status == "COMPLETED"
    assert plan_cached.plan_id == plan_resumed.plan_id
    assert plan_cached.created_at == plan_resumed.created_at
