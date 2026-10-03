"""Acceptance tests for CIO Market Lab API Single Writer Candidate.

Verifies:
1. Real subprocess writer contention API-vs-CLI (mutual exclusion without nested deadlock).
2. Restart preservation (events, learning, cash preserved; no cash reset to initial capital).
3. Read-only instance leaves runtime bytes unchanged (identical sha256, no seeding, all mutations 403).
4. Default production contains zero invented performance/signals; fixture mode explicitly marked.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Dict
from fastapi.testclient import TestClient
import pytest

from datetime import datetime, timezone
from cio_market_lab.api.app import AppState, create_app
from cio_market_lab.domain.events import EventEnvelope, EventType
from cio_market_lab.domain.models import DecisionScope, Fill, Market, OrderOrigin, OrderSide, OrderType
from cio_market_lab.engine.continuation import ContinuationLock, ContinuationLockAcquisitionError
from cio_market_lab.engine.paper_orders import (
    PaperDataContext,
    PaperExperimentSettings,
    PaperOrderRequest,
)


def _hash_directory(dir_path: Path) -> Dict[str, str]:
    """Calculate sha256 hash for every file in directory tree."""
    hashes = {}
    if not dir_path.exists():
        return hashes
    for p in sorted(dir_path.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(dir_path))
            hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return hashes


# ============================================================================
# 1. Real Subprocess Writer Contention API-vs-CLI
# ============================================================================

def test_writer_contention_api_vs_cli(tmp_path: Path):
    """Verify that a CLI subprocess holding the writer lock prevents API mutation routes

    from mutating the same runtime, returning HTTP 423 RUNTIME_LOCKED without crashing.
    """
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_file = runtime_dir / "continuation.lock"

    # Start a background subprocess holding the writer lock for 4 seconds
    lock_script = f"""
import fcntl, time
with open(r"{lock_file}", "a+") as f:
    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    time.sleep(3.0)
"""
    proc = subprocess.Popen([sys.executable, "-c", lock_script])
    # Give subprocess a moment to acquire the lock
    time.sleep(0.5)

    try:
        # Create owner app pointing to the same runtime_dir
        app = create_app(
            workspace_root=tmp_path,
            runtime_dir=runtime_dir,
            is_read_only=False,
        )
        client = TestClient(app, base_url="http://127.0.0.1:21322")

        # Attempt mutating operations while lock is held
        resp_exp = client.put("/api/paper/experiments/test-strat", json={
            "strategy_id": "test-strat",
            "enabled": True,
            "universe": ["2330.TW"],
        })
        assert resp_exp.status_code == 423
        assert "RUNTIME_LOCKED" in resp_exp.json()["detail"]

        resp_kill = client.post("/api/paper/kill-switch", json={
            "enabled": True,
            "reason": "contention test",
        })
        assert resp_kill.status_code == 423
        assert "RUNTIME_LOCKED" in resp_kill.json()["detail"]

        resp_risk = client.put("/api/paper/risk-limits", json={
            "max_daily_drawdown_pct": 0.05,
            "max_position_weight": 0.20,
            "max_gross_exposure": 1.0,
            "max_unhedged_exposure": 0.5,
            "daily_loss_limit_twd": 50000.0,
            "per_trade_risk_limit_twd": 10000.0,
        })
        assert resp_risk.status_code == 423
        assert "RUNTIME_LOCKED" in resp_risk.json()["detail"]
    finally:
        proc.wait(timeout=5.0)

    # Now that the lock is released, API mutations succeed
    resp_exp_after = client.put("/api/paper/experiments/test-strat", json={
        "strategy_id": "test-strat",
        "enabled": True,
        "universe": ["2330.TW"],
    })
    assert resp_exp_after.status_code == 200
    assert resp_exp_after.json()["strategy_id"] == "test-strat"


# ============================================================================
# 2. Restart Preservation (Events, Learning, Cash Preserved; No Cash Reset)
# ============================================================================

def test_restart_preservation_canonical_runtime(tmp_path: Path):
    """Verify that restarting the service preserves events, learning decisions,

    experiment configurations, and cash balance without resetting to default capital.
    """
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    # Session 1: Create owner app, execute an order that alters cash, save settings
    app1 = create_app(
        workspace_root=tmp_path,
        runtime_dir=runtime_dir,
        is_read_only=False,
    )
    st1 = app1.state.app_state

    # Configure experiment
    st1.runner.configure(PaperExperimentSettings(
        strategy_id="restart-preservation-strat",
        enabled=True,
        universe=["2330.TW"],
        initial_cash=500000.0,
    ))

    # Place and fill a paper order
    req = PaperOrderRequest(
        symbol="2330.TW",
        market="TW",
        bucket=DecisionScope.SWING,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100.0,
        origin=OrderOrigin.MANUAL,
        reason="restart-preservation-test",
        data=PaperDataContext(
            source="local_replay",
            last_price=1000.0,
            is_stale=False,
            is_fallback=False,
        ),
    )
    order = st1.paper_orders.submit(req)
    assert order is not None

    fill = Fill(
        fill_id="fill-test-001",
        order_id=order.order_id,
        symbol="2330.TW",
        bucket=DecisionScope.SWING,
        side=OrderSide.BUY,
        quantity=100.0,
        fill_price=1000.0,
        timestamp=datetime.now(timezone.utc),
        cash_flow=-100000.0,
    )
    initial_cash = st1.portfolio_manager.get_portfolio(DecisionScope.SWING).cash
    st1.portfolio_manager.apply_fill(fill)
    st1.event_store.append(EventEnvelope(
        event_type=EventType.ORDER_FILLED,
        aggregate_id=fill.fill_id,
        payload=fill.model_dump(mode="json"),
    ))

    cash_after_fill = st1.portfolio_manager.get_portfolio(DecisionScope.SWING).cash
    assert cash_after_fill < initial_cash

    # Append a decision record in decision learning store
    from datetime import timedelta
    from cio_market_lab.domain.models import CIODecisionPacket, CIOProvenance
    pkt = CIODecisionPacket(
        case_id="case-restart-001",
        as_of=datetime.now(timezone.utc),
        evidence=["research://test"],
        thesis="Restart preservation test thesis",
        selected_instrument="2330.TW",
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=100.0,
        conditions={},
        risk_assessment={},
        alternatives_considered=[],
        expiry=datetime.now(timezone.utc) + timedelta(hours=8),
        confidence=0.9,
        strategy_version="test-v1",
        provenance=CIOProvenance(
            authority="MAIN_CIO",
            actor_role="CHIEF_INVESTMENT_OFFICER",
            signer_id="main-cio-key",
            source="external_packet",
        ),
        is_fixture=True,
    )
    st1.runner.learning_store.record_decision(
        packet=pkt,
        pre_decision_portfolio={},
        pre_decision_quotes={},
    )

    # Persist portfolio state
    st1.runner._persist_portfolios()

    event_count_before = st1.event_store.count()
    assert event_count_before > 0

    # Shutdown Session 1
    st1.runner.shutdown()

    # Session 2: Fresh restart using the EXACT SAME runtime_dir
    app2 = create_app(
        workspace_root=tmp_path,
        runtime_dir=runtime_dir,
        is_read_only=False,
    )
    st2 = app2.state.app_state

    # 1. Events preserved
    event_count_after = st2.event_store.count()
    assert event_count_after == event_count_before

    # 2. Cash balance preserved (NOT reset to TEAM_INITIAL_CAPITAL_TWD 1,000,000)
    assert st2.portfolio_manager.get_portfolio(DecisionScope.SWING).cash == cash_after_fill

    # 3. Learning store records preserved
    rec = st2.runner.learning_store.get_record("case-restart-001")
    assert rec is not None
    assert rec.case_id == "case-restart-001"

    # 4. Experiment settings preserved
    experiments = st2.paper_orders.list_experiments()
    assert any(e.strategy_id == "restart-preservation-strat" for e in experiments)

    # 5. Post-restart cycle executes safely without cash reset
    cycle_res = st2.runner.run_one_cycle("restart-preservation-strat")
    assert cycle_res["run"]["strategy_id"] == "restart-preservation-strat"
    assert cycle_res["run"]["status"] in {"BLOCKED", "COMPLETED"}
    assert st2.portfolio_manager.get_portfolio(DecisionScope.SWING).cash == cash_after_fill

    st2.runner.shutdown()


# ============================================================================
# 3. Read-Only Instance Leaves Runtime Bytes Unchanged (Zero Mutation)
# ============================================================================

def test_readonly_runtime_bytes_unchanged(tmp_path: Path):
    """Verify that a read-only instance (port 8765) leaves all files in runtime_dir

    completely unchanged (identical sha256 checksums), does not seed on startup,
    and rejects all mutation routes with HTTP 403.
    """
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    # Seed some valid baseline state with an owner app
    app_owner = create_app(
        workspace_root=tmp_path,
        runtime_dir=runtime_dir,
        is_read_only=False,
    )
    st_owner = app_owner.state.app_state
    st_owner.runner.configure(PaperExperimentSettings(
        strategy_id="baseline-strat",
        enabled=True,
        universe=["AAPL"],
    ))
    st_owner.runner._persist_portfolios()
    st_owner.runner.shutdown()

    # Capture pristine file hashes of the runtime directory
    before_hashes = _hash_directory(runtime_dir)
    assert len(before_hashes) > 0

    # Start read-only app instance on port 8765
    app_ro = create_app(
        workspace_root=tmp_path,
        runtime_dir=runtime_dir,
        is_read_only=True,
    )
    client_ro = TestClient(app_ro, base_url="http://127.0.0.1:8765")

    # 1. Read operations succeed
    res_watch = client_ro.get("/api/watchlists")
    assert res_watch.status_code == 200

    res_strat = client_ro.get("/api/strategies")
    assert res_strat.status_code == 200

    res_port = client_ro.get("/api/portfolios")
    assert res_port.status_code == 200

    res_team = client_ro.get("/api/paper/team-ops")
    assert res_team.status_code == 200

    res_exp = client_ro.get("/api/paper/experiments")
    assert res_exp.status_code == 200

    # 2. Mutation operations are strictly rejected with 403
    res_mut_order = client_ro.post("/api/paper/orders", json={
        "symbol": "AAPL",
        "market": "US",
        "bucket": "swing",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": 10,
        "origin": "MANUAL",
        "reason": "ro-test",
    })
    assert res_mut_order.status_code == 403
    assert "READONLY" in res_mut_order.json()["detail"]

    res_mut_kill = client_ro.post("/api/paper/kill-switch", json={"enabled": True})
    assert res_mut_kill.status_code == 403

    res_mut_risk = client_ro.put("/api/paper/risk-limits", json={})
    assert res_mut_risk.status_code == 403

    res_mut_put_exp = client_ro.put("/api/paper/experiments/new-strat", json={
        "strategy_id": "new-strat",
        "enabled": True,
        "universe": ["AAPL"],
    })
    assert res_mut_put_exp.status_code == 403

    res_mut_chat = client_ro.post("/api/chat", json={"message": "hello"})
    assert res_mut_chat.status_code == 403

    # Verify sha256 checksums of every file in runtime_dir are strictly identical
    after_hashes = _hash_directory(runtime_dir)
    assert after_hashes == before_hashes


# ============================================================================
# 4. Default Production: No Invented Performance or Synthetic Signals
# ============================================================================

def test_default_production_no_invented_performance(tmp_path: Path, monkeypatch):
    """Verify that in default production mode (no fixture flags set):

    1. signals_log contains 0 invented signals.
    2. experiments_log contains 0 invented/synthetic completed experiments.
    3. When explicit fixture mode is enabled, items are marked with is_fixture=True.
    """
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    # Ensure no fixture environment variables are present
    monkeypatch.delenv("CIO_FIXTURE_MODE", raising=False)
    monkeypatch.delenv("CIO_ENABLE_FIXTURES", raising=False)
    monkeypatch.delenv("CIO_ENABLE_DEMO_FIXTURES", raising=False)

    # 1. Default Production Instance
    app_prod = create_app(
        workspace_root=tmp_path,
        runtime_dir=runtime_dir,
        is_read_only=False,
    )
    st_prod = app_prod.state.app_state

    # Verify zero invented signals or experiments in default production
    assert len(st_prod.signals_log) == 0, f"Expected 0 signals in production, found {len(st_prod.signals_log)}"
    assert len(st_prod.experiments_log) == 0, f"Expected 0 experiments in production, found {len(st_prod.experiments_log)}"

    client_prod = TestClient(app_prod, base_url="http://127.0.0.1:21322")
    res_signals = client_prod.get("/api/signals").json()
    assert len(res_signals) == 0

    res_overview = client_prod.get("/api/overview").json()
    assert res_overview["recent_signals_count"] == 0

    st_prod.runner.shutdown()

    # 2. Explicit Fixture Mode Instance
    app_fix = create_app(
        workspace_root=tmp_path,
        runtime_dir=runtime_dir,
        is_read_only=False,
        fixture_mode=True,
    )
    st_fix = app_fix.state.app_state

    # Fixture mode populates seed data
    assert len(st_fix.signals_log) > 0
    assert len(st_fix.experiments_log) > 0

    # Every item must be explicitly labeled with is_fixture=True
    for sig in st_fix.signals_log:
        assert sig.get("is_fixture") is True, f"Signal {sig.get('signal_id')} missing is_fixture flag"

    for exp in st_fix.experiments_log:
        assert exp.get("is_fixture") is True, f"Experiment {exp.get('experiment_id')} missing is_fixture flag"

    st_fix.runner.shutdown()
