"""Comprehensive regression tests for canonical Team Ops refactor.

Covers:
1. Duplicate consolidation: exactly one canonical position per (market, symbol, currency) with strategy attribution metadata.
2. Freshness from timestamps: durable quote contract, no fabricated bid/ask from daily close.
3. Stale/synthetic rejection: autonomous runner blocks stale/synthetic data and exposes execution assumptions.
4. NAV reconciliation & Nonzero mark P&L: exact math, nonzero mark P&L, explicit NAV_UNAVAILABLE on missing/stale/synthetic marks.
5. Owner / Read-only convergence: port 8765 read-only mutation block, port 21322 owner mutation, shared snapshot & version.
6. Persisted settings: /api/paper/experiments returns persisted settings across processes instead of {}.
7. Adaptive posture monotonicity: non-increasing risk budget across exposures/regimes, fail-closed on data degradation.
8. Paper-only guards: paper_only=True, broker_connected=False, autonomous_capital_decisions=False.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest
from fastapi.testclient import TestClient

from cio_market_lab.api.app import create_app
from cio_market_lab.data.base import MarketDataAdapter
from cio_market_lab.domain.models import (
    Bar,
    DecisionScope,
    Fill,
    Market,
    Order,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
    Quote,
)
from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner
from cio_market_lab.engine.paper_orders import (
    PaperDataContext,
    PaperExperimentSettings,
    PaperOrderRequest,
    PaperOrderService,
)
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.engine.team_ops import (
    CanonicalPositionRow,
    DurableQuoteSnapshot,
    TeamOpsSnapshotBuilder,
    compute_team_posture,
    load_canonical_snapshot,
    save_canonical_snapshot,
)
from cio_market_lab.events.store import EventStore


class MockAdapter(MarketDataAdapter):
    def __init__(self, bars_map=None, quotes_map=None):
        self.bars_map = bars_map or {}
        self.quotes_map = quotes_map or {}

    @property
    def source_name(self):
        return "mock-adapter"

    def get_bars(self, symbol, start=None, end=None, timeframe="1D", limit=None):
        bars = self.bars_map.get(symbol, [])
        return bars[-limit:] if limit else bars

    def stream_bars(self, symbols):
        for s in symbols:
            b = self.get_latest_bar(s)
            if b:
                yield b

    def get_latest_bar(self, symbol):
        bars = self.bars_map.get(symbol, [])
        return bars[-1] if bars else None

    def get_latest_quote(self, symbol):
        return self.quotes_map.get(symbol)


# =====================================================================
# 1. Duplicate Consolidation Tests
# =====================================================================

def test_duplicate_consolidation_into_canonical_positions():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    # Strategy 1 (momentum) bought 10 AAPL at 150
    f1 = Fill(
        fill_id="f1", order_id="o1", symbol="AAPL", bucket=DecisionScope.SWING,
        side=OrderSide.BUY, quantity=10.0, fill_price=150.0, timestamp=now - timedelta(minutes=10),
    )
    # Strategy 2 (balanced) bought 5 AAPL at 160
    f2 = Fill(
        fill_id="f2", order_id="o2", symbol="AAPL", bucket=DecisionScope.SWING,
        side=OrderSide.BUY, quantity=5.0, fill_price=160.0, timestamp=now - timedelta(minutes=5),
    )
    # Strategy 2 also bought 20 2330.TW at 1000
    f3 = Fill(
        fill_id="f3", order_id="o3", symbol="2330.TW", bucket=DecisionScope.SWING,
        side=OrderSide.BUY, quantity=20.0, fill_price=1000.0, timestamp=now - timedelta(minutes=2),
    )

    quotes = {
        "AAPL": DurableQuoteSnapshot(
            symbol="AAPL", market=Market.US, source="test", observed_at=now,
            last_price=170.0, quality="good", is_stale=False,
        ),
        "2330.TW": DurableQuoteSnapshot(
            symbol="2330.TW", market=Market.TW, source="test", observed_at=now,
            last_price=1020.0, quality="good", is_stale=False,
        ),
    }

    strategy_fills = {
        "momentum": [f1],
        "balanced": [f2, f3],
    }
    strategy_names = {
        "momentum": "Momentum Strategy",
        "balanced": "Balanced Growth",
    }

    rows, warnings = TeamOpsSnapshotBuilder.consolidate_positions(
        fills=[f1, f2, f3],
        quotes=quotes,
        strategy_fills=strategy_fills,
        strategy_names=strategy_names,
    )

    # Exactly 2 canonical position rows (one for AAPL, one for 2330.TW)
    assert len(rows) == 2
    aapl_row = next(r for r in rows if r.symbol == "AAPL")
    tw_row = next(r for r in rows if r.symbol == "2330.TW")

    assert aapl_row.market == Market.US
    assert aapl_row.currency == "USD"
    assert aapl_row.quantity == 15.0
    # Cost basis = 10 * 150 + 5 * 160 = 2300, avg entry = 2300 / 15 = 153.3333
    assert aapl_row.cost_basis == 2300.0
    assert abs(aapl_row.average_entry_price - 153.3333) < 0.01
    assert aapl_row.current_price == 170.0
    assert aapl_row.market_value == 15.0 * 170.0  # 2550.0
    assert aapl_row.unrealized_pnl == 250.0  # 2550 - 2300

    # Strategy attribution metadata preserves per-strategy breakdown
    assert len(aapl_row.strategy_attribution) == 2
    assert aapl_row.strategy_attribution["momentum"]["quantity"] == 10.0
    assert aapl_row.strategy_attribution["momentum"]["unrealized_pnl"] == 200.0  # (170 - 150) * 10
    assert aapl_row.strategy_attribution["balanced"]["quantity"] == 5.0
    assert aapl_row.strategy_attribution["balanced"]["unrealized_pnl"] == 50.0   # (170 - 160) * 5


# =====================================================================
# 2. Freshness from Timestamps & Durable Quote Contract Tests
# =====================================================================

def test_durable_quote_contract_never_fabricates_bid_ask():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    bar = Bar(
        symbol="AAPL", timestamp=now - timedelta(hours=1), observed_at=now,
        open=150, high=155, low=149, close=152, volume=1000,
        source="yahoo_delayed", delay_seconds=900.0, quality="delayed", is_stale=False,
    )
    adapter = MockAdapter(bars_map={"AAPL": [bar]})
    pm = PortfolioManager(initial_cash_swing=1000, initial_cash_intraday=1000)
    service = PaperOrderService(pm, EventStore(":memory:"))
    runner = AutonomousPaperRunner(Path("/tmp"), pm, service, adapter, now_fn=lambda: now)

    quote = runner.get_durable_quote("AAPL")
    assert quote.symbol == "AAPL"
    assert quote.last_price == 152.0
    assert quote.bid is None  # Never fabricated from daily close!
    assert quote.ask is None  # Never fabricated from daily close!
    assert quote.fabrication_guard is True
    assert quote.observed_at == now
    assert quote.bar_time == bar.timestamp
    assert quote.age_seconds == 0.0 or quote.age_seconds >= 0.0


# =====================================================================
# 3. Stale / Synthetic Rejection & Execution Semantics Tests
# =====================================================================

def test_runner_blocks_synthetic_and_stale_data(tmp_path):
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    synthetic_bar = Bar(
        symbol="SYNTH", timestamp=now, observed_at=now, open=100, high=105, low=99,
        close=104, volume=1000, source="synthetic_fixture", quality="synthetic_fixture", is_stale=False,
    )
    stale_bar = Bar(
        symbol="STALE", timestamp=now - timedelta(days=5), observed_at=now - timedelta(days=5),
        open=100, high=105, low=99, close=104, volume=1000, source="yahoo_delayed",
        delay_seconds=400000.0, quality="delayed", is_stale=True,
    )

    adapter = MockAdapter(bars_map={"SYNTH": [synthetic_bar], "STALE": [stale_bar]})
    pm = PortfolioManager(initial_cash_swing=10000, initial_cash_intraday=10000)
    service = PaperOrderService(pm, EventStore(":memory:"))
    runner = AutonomousPaperRunner(tmp_path, pm, service, adapter, now_fn=lambda: now)

    runner.configure(PaperExperimentSettings(
        strategy_id="synth-test", enabled=True, universe=["SYNTH"], initial_cash=10000,
    ))
    runner.configure(PaperExperimentSettings(
        strategy_id="stale-test", enabled=True, universe=["STALE"], initial_cash=10000,
    ))

    res_synth = runner.run_one_cycle("synth-test")
    res_stale = runner.run_one_cycle("stale-test")

    assert res_synth["decisions"][0]["action"] == "NO_TRADE"
    assert "STALE_OR_SYNTHETIC" in res_synth["decisions"][0]["reason"]
    assert res_stale["decisions"][0]["action"] == "NO_TRADE"
    assert "STALE_OR_SYNTHETIC" in res_stale["decisions"][0]["reason"]
    assert len(pm.get_strategy_portfolio("synth-test", DecisionScope.SWING).fills) == 0


def test_runner_fills_include_fees_tax_slippage_and_assumptions(tmp_path):
    now = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
    bars = [
        Bar(symbol="AAPL", timestamp=now - timedelta(days=2), observed_at=now, open=100, high=101, low=99, close=100, volume=1000, source="real_feed", quality="good"),
        Bar(symbol="AAPL", timestamp=now - timedelta(days=1), observed_at=now, open=101, high=105, low=100, close=104, volume=1500, source="real_feed", quality="good"),
    ]
    # Later TEST_ONLY book stays inside the signal-sized notional cap after slippage.
    later_quote = Quote(
        symbol="AAPL", timestamp=now - timedelta(seconds=1), observed_at=now,
        bid=103.8, ask=103.9, last_price=103.9, source="real_feed", quality="good", is_stale=False, is_synthetic=False,
    )
    adapter = MockAdapter(bars_map={"AAPL": bars}, quotes_map={"AAPL": later_quote})
    pm = PortfolioManager(initial_cash_swing=50000, initial_cash_intraday=50000)
    service = PaperOrderService(pm, EventStore(":memory:"), now_fn=lambda: now)
    runner = AutonomousPaperRunner(tmp_path, pm, service, adapter, now_fn=lambda: now, require_cio_provider=False)
    adapter.is_fixture = True
    runner.allow_test_only_fx = True  # TEST_ONLY fixture authorization, never production
    runner.allow_fixture_quotes = True  # Isolated test adapter: explicitly authorize TEST_ONLY quote evidence.
    runner.configure(PaperExperimentSettings(
        strategy_id="fee-test", enabled=True, universe=["AAPL"], initial_cash=50000, max_position_notional=10000,
    ))

    cycle = runner.run_one_cycle("fee-test")
    assert cycle["run"]["orders_count"] == 1
    portfolio = pm.get_strategy_portfolio("fee-test", DecisionScope.SWING)
    assert len(portfolio.fills) == 1
    fill = portfolio.fills[0]

    # Verify execution assumptions and modeled costs
    assert fill.fee > 0
    assert fill.slippage > 0
    assert fill.assumptions["execution"] == "local_paper_only"
    assert fill.assumptions["slippage_bps"] == 5.0
    assert fill.assumptions["timing_assumption"] == "authoritative_later_quote_slippage_adjusted"
    assert fill.fill_price > later_quote.ask  # Slippage is applied to the executable ask.
    assert fill.quantity * fill.fill_price <= 10000
    assert fill.quote_verification == "BOOK_BOUND_TEST_ONLY"


# =====================================================================
# 4. NAV Reconciliation & Nonzero Mark P&L Tests
# =====================================================================

def test_nav_reconciliation_and_nonzero_mark_pnl(tmp_path):
    now = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
    pm = PortfolioManager(initial_cash_swing=100000, initial_cash_intraday=100000)
    service = PaperOrderService(pm, EventStore(":memory:"), now_fn=lambda: now)
    bars = [
        Bar(symbol="AAPL", timestamp=now - timedelta(days=2), observed_at=now, open=100, high=101, low=99, close=100, volume=1000, source="real", quality="good"),
        Bar(symbol="AAPL", timestamp=now - timedelta(days=1), observed_at=now, open=101, high=105, low=100, close=104, volume=1500, source="real", quality="good"),
    ]
    # Later TEST_ONLY book stays inside the signal-sized notional cap after slippage.
    later_quote = Quote(
        symbol="AAPL", timestamp=now - timedelta(seconds=1), observed_at=now,
        bid=103.8, ask=103.9, last_price=103.9, source="real", quality="good", is_stale=False, is_synthetic=False,
    )
    adapter = MockAdapter(bars_map={"AAPL": bars}, quotes_map={"AAPL": later_quote})
    runner = AutonomousPaperRunner(tmp_path, pm, service, adapter, now_fn=lambda: now, require_cio_provider=False)
    adapter.is_fixture = True
    runner.allow_test_only_fx = True  # TEST_ONLY fixture authorization, never production
    runner.allow_fixture_quotes = True  # Isolated test adapter: explicitly authorize TEST_ONLY quote evidence.
    runner.configured_fx_rates = {"USD": 1.0}  # Explicit parity assumption in this unit fixture.
    runner.configure(PaperExperimentSettings(
        strategy_id="nav-strat", enabled=True, universe=["AAPL"], initial_cash=100000, max_position_notional=10000,
        base_currency="USD",
    ))
    first_cycle = runner.run_one_cycle("nav-strat")
    assert first_cycle["run"]["orders_count"] == 1
    assert first_cycle["run"]["fills_count"] == 1

    # Now mark the position with a higher price: 120.0
    marked_quote = DurableQuoteSnapshot(
        symbol="AAPL", market=Market.US, source="real", observed_at=now,
        last_price=120.0, quality="good", is_stale=False,
    )
    runner._durable_quotes["AAPL"] = marked_quote
    adapter.bars_map["AAPL"].append(
        Bar(symbol="AAPL", timestamp=now, observed_at=now, open=118, high=122, low=118, close=120, volume=2000, source="real", quality="good")
    )

    snapshot = runner.generate_canonical_team_ops(now)
    assert snapshot["nav_status"] == "OK"
    assert snapshot["equity"] is not None
    # Check unrealized_pnl from the portfolio section (may be zero if fill happened at different price)
    portfolio_unrealized = runner.portfolio_manager.get_strategy_portfolio("nav-strat", DecisionScope.SWING).unrealized_pnl
    assert portfolio_unrealized is not None
    assert portfolio_unrealized > 0  # NON-ZERO MARK P&L!
    assert len(snapshot["canonical_positions"]) == 1
    pos = snapshot["canonical_positions"][0]
    assert pos["current_price"] == 120.0
    assert pos["unrealized_pnl"] > 0

    # NAV Reconciliation invariant:
    # Team initial capital + total PnL == Team Equity
    assert abs((snapshot["initial_capital"] + snapshot["total_pnl"]) - snapshot["equity"]) < 0.01


def test_nav_unavailable_when_mark_is_stale_or_synthetic():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    fill = Fill(
        fill_id="f1", order_id="o1", symbol="AAPL", bucket=DecisionScope.SWING,
        side=OrderSide.BUY, quantity=10.0, fill_price=150.0, timestamp=now,
    )
    # 1. Stale quote
    stale_quotes = {
        "AAPL": DurableQuoteSnapshot(
            symbol="AAPL", market=Market.US, source="real", observed_at=now - timedelta(days=10),
            age_seconds=864000.0, last_price=160.0, quality="delayed", is_stale=True,
        )
    }
    positions, _ = TeamOpsSnapshotBuilder.consolidate_positions([fill], stale_quotes)
    status, eq, un_pnl, tot_pnl, ret_pct, warnings = TeamOpsSnapshotBuilder.evaluate_nav(
        cash_twd=10000.0, initial_capital_twd=50000.0, positions=positions, quotes=stale_quotes,
    )
    assert status == "NAV_UNAVAILABLE"
    assert eq is None
    assert un_pnl is None
    assert any("STALE_MARK" in w for w in warnings)

    # 2. Synthetic quote
    synthetic_quotes = {
        "AAPL": DurableQuoteSnapshot(
            symbol="AAPL", market=Market.US, source="synthetic_fixture", observed_at=now,
            last_price=160.0, quality="synthetic_fixture", is_stale=False, is_synthetic=True,
        )
    }
    positions, _ = TeamOpsSnapshotBuilder.consolidate_positions([fill], synthetic_quotes)
    status, eq, un_pnl, tot_pnl, ret_pct, warnings = TeamOpsSnapshotBuilder.evaluate_nav(
        cash_twd=10000.0, initial_capital_twd=50000.0, positions=positions, quotes=synthetic_quotes,
    )
    assert status == "NAV_UNAVAILABLE"
    assert eq is None
    assert any("SYNTHETIC_MARK" in w for w in warnings)


# =====================================================================
# 5. Owner / Read-Only Convergence & Mutation Guard Tests
# =====================================================================

def test_port_8765_readonly_blocks_runner_mutation(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    client = TestClient(app)

    # Set as read-only port 8765
    monkeypatch.setenv("CIO_PORT", "8765")
    monkeypatch.setenv("CIO_AUTONOMOUS_RUNNER_OWNER", "0")

    # Mutation endpoints must be blocked with 403 Forbidden
    res_start = client.post("/api/paper/experiments/momentum/start")
    assert res_start.status_code == 403
    assert "READONLY" in res_start.json()["detail"]

    res_stop = client.post("/api/paper/experiments/momentum/stop")
    assert res_stop.status_code == 403

    res_cycle = client.post("/api/paper/experiments/momentum/run-one-cycle")
    assert res_cycle.status_code == 403

    res_put = client.put("/api/paper/experiments/momentum", json={
        "strategy_id": "momentum", "enabled": True, "universe": ["AAPL"],
    })
    assert res_put.status_code == 403


def test_owner_and_readonly_ports_read_same_canonical_snapshot(tmp_path, monkeypatch):
    # Owner app on port 21322
    monkeypatch.setenv("CIO_PORT", "21322")
    monkeypatch.setenv("CIO_AUTONOMOUS_RUNNER_OWNER", "1")
    owner_app = create_app(tmp_path)
    owner_client = TestClient(owner_app)

    # Configure experiment and generate team ops snapshot
    owner_client.put("/api/paper/experiments/test-strat", json={
        "strategy_id": "test-strat", "enabled": True, "universe": ["AAPL"], "initial_cash": 120000,
    })
    # The writer explicitly produces a snapshot; GET must only read it.
    owner_app.state.app_state.runner.generate_canonical_team_ops()
    owner_snap = owner_client.get("/api/paper/team-ops").json()
    # Public Team Ops exposes only the frontend contract; version is internal.
    runtime_dir = tmp_path / "data" / "runtime"
    saved = load_canonical_snapshot(runtime_dir)
    assert saved is not None
    assert saved["version"] >= 1
    saved_version = saved["version"]

    # Read-only app on port 8765 sharing same runtime_dir
    monkeypatch.setenv("CIO_PORT", "8765")
    monkeypatch.setenv("CIO_AUTONOMOUS_RUNNER_OWNER", "0")
    ro_app = create_app(tmp_path)
    ro_client = TestClient(ro_app)

    ro_snap = ro_client.get("/api/paper/team-ops").json()
    saved_after = load_canonical_snapshot(runtime_dir)
    assert saved_after is not None
    assert saved_after["version"] == saved_version
    assert ro_snap == owner_snap


# =====================================================================
# 6. Persisted Settings across Processes Tests
# =====================================================================

def test_api_paper_experiments_returns_persisted_settings(tmp_path, monkeypatch):
    # Owner process writes settings
    monkeypatch.setenv("CIO_AUTONOMOUS_RUNNER_OWNER", "1")
    monkeypatch.setenv("CIO_PORT", "21322")
    app1 = create_app(tmp_path)
    c1 = TestClient(app1, base_url="http://127.0.0.1:21322")
    res = c1.put("/api/paper/experiments/alpha", json={
        "strategy_id": "alpha", "strategy_name": "Alpha Strategy", "enabled": True,
        "universe": ["AAPL"], "initial_cash": 50000,
    })
    assert res.status_code == 200

    # Read-only process started afterwards
    monkeypatch.setenv("CIO_AUTONOMOUS_RUNNER_OWNER", "0")
    monkeypatch.setenv("CIO_PORT", "8765")
    app2 = create_app(tmp_path)
    c2 = TestClient(app2, base_url="http://127.0.0.1:8765")

    experiments = c2.get("/api/paper/experiments").json()
    assert isinstance(experiments, list)
    assert len(experiments) >= 1
    assert any(e["strategy_id"] == "alpha" for e in experiments)
    alpha = next(e for e in experiments if e["strategy_id"] == "alpha")
    assert alpha["initial_cash"] == 50000.0


# =====================================================================
# 7. Adaptive Posture Monotonicity Tests
# =====================================================================

def test_team_posture_monotonicity():
    # 1. Monotonicity under rising exposure in calm regime
    p_low = compute_team_posture("TRENDING_BULL", "TRENDING_BULL", "FRESH", 0.2, 0.2)
    p_med = compute_team_posture("TRENDING_BULL", "TRENDING_BULL", "FRESH", 0.5, 0.5)
    p_high = compute_team_posture("TRENDING_BULL", "TRENDING_BULL", "FRESH", 0.8, 0.8)
    p_max = compute_team_posture("TRENDING_BULL", "TRENDING_BULL", "FRESH", 0.95, 0.95)

    assert p_low.risk_budget_multiplier >= p_med.risk_budget_multiplier
    assert p_med.risk_budget_multiplier >= p_high.risk_budget_multiplier
    assert p_high.risk_budget_multiplier >= p_max.risk_budget_multiplier
    assert p_max.risk_budget_multiplier == 0.0

    # 2. Monotonicity across market regime degradation (calm vs volatile)
    p_calm = compute_team_posture("TRENDING_BULL", "TRENDING_BULL", "FRESH", 0.2, 0.2)
    p_vol = compute_team_posture("HIGH_VOLATILITY", "TRENDING_BULL", "FRESH", 0.2, 0.2)
    assert p_calm.risk_budget_multiplier >= p_vol.risk_budget_multiplier

    # 3. Fail closed on data quality degradation
    for degraded in ("STALE", "SYNTHETIC", "MISSING", "FAILED_CLOSED"):
        posture = compute_team_posture("TRENDING_BULL", "TRENDING_BULL", degraded, 0.1, 0.1)
        assert posture.posture == "DEFENSIVE_HALT"
        assert posture.risk_budget_multiplier == 0.0
        assert posture.allow_new_entries is False
        assert posture.fail_closed is True


# =====================================================================
# 8. Paper-Only Guards Tests
# =====================================================================

def test_paper_only_guards_and_noncanonical_legacy_competition(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)

    # Team Ops endpoint
    ops = client.get("/api/paper/team-ops").json()
    assert ops["safety"]["paper_only"] is True
    assert ops["safety"]["broker_connected"] is False
    assert ops["safety"]["autonomous_capital_decisions"] is False

    # Legacy competition endpoint
    comp = client.get("/api/paper/competition").json()
    assert comp["paper_only"] is True
    assert comp["broker_connected"] is False
    assert comp["autonomous_capital_decisions"] is False
    assert comp["is_canonical"] is False
    assert comp["canonical_reference"] == "/api/paper/team-ops"


# =====================================================================
# 9. Required Regression Coverage from REVIEW_REPAIR.md
# =====================================================================

def test_regression_unknown_or_no_port_defaults_read_only(tmp_path, monkeypatch):
    """1. Unknown/no-port defaults read-only and blocks all mutations and chat."""
    monkeypatch.delenv("CIO_PORT", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://testserver")

    # Order mutation must fail closed with 403
    order_res = client.post("/api/paper/orders", json={
        "symbol": "AAPL", "market": "US", "bucket": "swing", "side": "BUY", "order_type": "MARKET",
        "quantity": 1, "origin": "MANUAL", "reason": "test-no-port",
    })
    assert order_res.status_code == 403
    assert "READONLY" in order_res.json()["detail"]

    # Chat must fail closed with 403
    chat_res = client.post("/api/chat", json={"message": "hello"})
    assert chat_res.status_code == 403
    assert "READONLY" in chat_res.json()["detail"]


def test_regression_port_8765_rejects_mutation_and_chat_and_does_not_start_runner(tmp_path, monkeypatch):
    """2. Port 8765 rejects mutation and chat, and does not start/write runner state."""
    monkeypatch.setenv("CIO_PORT", "8765")
    monkeypatch.setenv("CIO_AUTONOMOUS_RUNNER_OWNER", "0")
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    # Mutation blocked
    assert client.post("/api/paper/orders", json={"symbol": "AAPL", "market": "US", "bucket": "swing", "side": "BUY", "order_type": "MARKET", "quantity": 1, "origin": "MANUAL", "reason": "r"}).status_code == 403
    assert client.post("/api/chat", json={"message": "ping"}).status_code == 403
    assert client.post("/api/research/intake", json={"url": "https://example.com", "title": "t", "hypothesis": "h"}).status_code == 403
    assert client.post("/api/paper/kill-switch", json={"enabled": True}).status_code == 403
    assert client.put("/api/paper/risk-limits", json={}).status_code == 403

    # Runner was not started
    assert app.state.app_state.runner._active == {}
    runtime_dir = tmp_path / "data" / "runtime"
    assert not (runtime_dir / "runs.jsonl").exists()


def test_regression_read_only_missing_snapshot_does_not_write(tmp_path, monkeypatch):
    """3. Read-only missing snapshot does not write to filesystem and returns unavailable contract."""
    monkeypatch.setenv("CIO_PORT", "8765")
    monkeypatch.setenv("CIO_AUTONOMOUS_RUNNER_OWNER", "0")
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    snapshot_file = tmp_path / "data" / "runtime" / "canonical_team_ops.json"
    assert not snapshot_file.exists()

    resp = client.get("/api/paper/team-ops")
    assert resp.status_code == 200
    data = resp.json()
    assert data["portfolio"]["nav_status"] in {"SNAPSHOT_UNAVAILABLE", "NAV_UNAVAILABLE"}
    data_freshness_status = data["data_freshness"]["status"] if isinstance(data["data_freshness"], dict) else data["data_freshness"]
    assert data_freshness_status == "unavailable"
    # Verification: Filesystem was NOT mutated
    assert not snapshot_file.exists()


def test_regression_three_day_old_observed_at_cannot_fill(tmp_path, monkeypatch):
    """4. Three-day-old observed_at cannot fill."""
    monkeypatch.setenv("CIO_PORT", "21322")
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:21322")

    now = datetime.now(timezone.utc)
    three_days_ago = now - timedelta(days=3)
    payload = {
        "symbol": "AAPL",
        "market": "US",
        "bucket": "swing",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": 10.0,
        "origin": "MANUAL",
        "reason": "old-observed-at-test",
        "data": {
            "source": "yahoo",
            "observed_at": three_days_ago.isoformat(),
            "age_seconds": 259200.0,
            "last_price": 150.0,
            "is_stale": False,
            "is_fallback": False,
        },
    }
    resp = client.post("/api/paper/orders", json=payload)
    assert resp.status_code == 409
    assert "REJECTED_STALE_OR_FALLBACK" in resp.json()["detail"]


def test_regression_missing_mark_leaves_position_valuation_unavailable():
    """5. Missing mark leaves position valuation unavailable."""
    now = datetime.now(timezone.utc)
    fill = Fill(
        fill_id="f1", order_id="o1", symbol="AAPL", bucket=DecisionScope.SWING,
        side=OrderSide.BUY, quantity=10.0, fill_price=100.0, timestamp=now,
    )
    # Missing quote entirely
    positions, warnings = TeamOpsSnapshotBuilder.consolidate_positions([fill], {})
    assert len(positions) == 1
    pos = positions[0]
    assert pos.current_price is None
    assert pos.market_value is None
    assert pos.unrealized_pnl is None
    assert pos.unrealized_pnl_pct is None

    # Evaluate NAV must fail closed
    status, eq, un_pnl, tot_pnl, ret_pct, nav_warns = TeamOpsSnapshotBuilder.evaluate_nav(
        cash_twd=10000.0, initial_capital_twd=50000.0, positions=positions, quotes={},
    )
    assert status == "NAV_UNAVAILABLE"
    assert eq is None
    assert un_pnl is None
    assert any("MISSING_MARK" in w for w in nav_warns)


def test_regression_mixed_swing_and_intraday_nav_reconciles(tmp_path):
    """6. NAV cash/equity reconciles across mixed SWING and INTRADAY buckets without double-counting initial capital."""
    now = datetime.now(timezone.utc)
    pm = PortfolioManager(initial_cash_swing=50000, initial_cash_intraday=50000)
    service = PaperOrderService(pm, EventStore(":memory:"))
    adapter = MockAdapter()
    runner = AutonomousPaperRunner(tmp_path, pm, service, adapter, now_fn=lambda: now)
    

    # Strategy 1 in SWING
    runner.configure(PaperExperimentSettings(
        strategy_id="strat-swing", mode=DecisionScope.SWING, enabled=True,
        initial_cash=50000, universe=["AAPL"], base_currency="USD",
    ))
    # Strategy 2 in INTRADAY
    runner.configure(PaperExperimentSettings(
        strategy_id="strat-intra", mode=DecisionScope.INTRADAY, enabled=True,
        initial_cash=50000, universe=["TSLA"], base_currency="USD",
    ))

    # Apply fills to both buckets
    fill_swing = Fill(
        fill_id="f-swing", order_id="o-s", symbol="AAPL", bucket=DecisionScope.SWING,
        side=OrderSide.BUY, quantity=10.0, fill_price=100.0, timestamp=now, currency="USD",
    )
    fill_intra = Fill(
        fill_id="f-intra", order_id="o-i", symbol="TSLA", bucket=DecisionScope.INTRADAY,
        side=OrderSide.BUY, quantity=20.0, fill_price=50.0, timestamp=now, currency="USD",
    )
    pm.apply_fill(fill_swing, "strat-swing")
    pm.apply_fill(fill_intra, "strat-intra")

    # Set authoritative quotes: AAPL @ 110 (mv=1100 USD), TSLA @ 55 (mv=1100 USD)
    runner._durable_quotes["AAPL"] = DurableQuoteSnapshot(
        symbol="AAPL", market=Market.US, source="real", observed_at=now,
        last_price=110.0, quality="good", is_stale=False,
    )
    runner._durable_quotes["TSLA"] = DurableQuoteSnapshot(
        symbol="TSLA", market=Market.US, source="real", observed_at=now,
        last_price=55.0, quality="good", is_stale=False,
    )
    adapter.bars_map["AAPL"] = [Bar(symbol="AAPL", timestamp=now, observed_at=now, open=110, high=110, low=110, close=110, volume=1000, source="real", quality="good")]
    adapter.bars_map["TSLA"] = [Bar(symbol="TSLA", timestamp=now, observed_at=now, open=55, high=55, low=55, close=55, volume=1000, source="real", quality="good")]

    snapshot = runner.generate_canonical_team_ops(now)
    assert pm.get_strategy_ledger("strat-swing", DecisionScope.SWING).currency == "USD"
    assert pm.get_strategy_ledger("strat-intra", DecisionScope.INTRADAY).currency == "USD"
    assert snapshot["portfolio"]["nav_status"] == "FX_UNAVAILABLE"
    # No fake TWD total without FX; native accounts retain independent funding.
    assert snapshot["portfolio"]["cash"] is None
    assert snapshot["portfolio"]["equity"] is None
    assert snapshot["portfolio"]["unrealized_pnl"] is None
    assert pm.get_strategy_ledger("strat-swing", DecisionScope.SWING).cash == 49000
    assert pm.get_strategy_ledger("strat-intra", DecisionScope.INTRADAY).cash == 49000
    runner.allow_test_only_fx = True
    runner.configured_fx_rates = {"USD": 32.0}  # TEST_ONLY simulated conversion
    converted = runner.generate_canonical_team_ops(now)
    assert converted["portfolio"]["nav_status"] == "OK"
    assert converted["portfolio"]["initial_capital"] == 100000 * 32
    assert converted["portfolio"]["cash"] == 98000 * 32
    assert converted["portfolio"]["equity"] == 100200 * 32
    assert converted["portfolio"]["unrealized_pnl"] == 200 * 32
    assert converted["portfolio"]["fx_accounting"]["is_simulated"] is True
    assert len(converted["canonical_positions"]) == 2


def test_regression_buy_fill_tax_assumption_is_zero():
    """7. BUY fill assumptions must report zero buy-side transaction tax."""
    from cio_market_lab.engine.execution import ExecutionCostConfig
    cost_cfg = ExecutionCostConfig()

    # TW market
    assert cost_cfg.get_tax_rate(Market.TW, OrderSide.BUY) == 0.0
    assert cost_cfg.calculate_tax(Market.TW, OrderSide.BUY, 50000.0) == 0.0
    # US market
    assert cost_cfg.get_tax_rate(Market.US, OrderSide.BUY) == 0.0
    assert cost_cfg.calculate_tax(Market.US, OrderSide.BUY, 50000.0) == 0.0

    # Sell tax remains positive
    assert cost_cfg.get_tax_rate(Market.TW, OrderSide.SELL) == 0.003
    assert cost_cfg.calculate_tax(Market.TW, OrderSide.SELL, 50000.0) == 150.0


def test_regression_persisted_version_increments_after_restart(tmp_path):
    """8. Canonical snapshot version remains monotonic across restarts."""
    now = datetime.now(timezone.utc)
    pm1 = PortfolioManager(initial_cash_swing=10000, initial_cash_intraday=10000)
    runner1 = AutonomousPaperRunner(tmp_path, pm1, PaperOrderService(pm1, EventStore(":memory:")), MockAdapter(), now_fn=lambda: now)
    snap1 = runner1.generate_canonical_team_ops(now)
    assert snap1["version"] == 1

    # Restart with runner2 sharing same runtime dir
    pm2 = PortfolioManager(initial_cash_swing=10000, initial_cash_intraday=10000)
    runner2 = AutonomousPaperRunner(tmp_path, pm2, PaperOrderService(pm2, EventStore(":memory:")), MockAdapter(), now_fn=lambda: now)
    snap2 = runner2.generate_canonical_team_ops(now)
    assert snap2["version"] == 2


def test_regression_local_cors_allowlist_has_no_wildcard_with_credentials(tmp_path):
    """9. Restrict CORS to explicit local origins; never use wildcard with credentials."""
    from starlette.middleware.cors import CORSMiddleware
    app = create_app(tmp_path)

    cors_middleware = next((m for m in app.user_middleware if m.cls == CORSMiddleware), None)
    assert cors_middleware is not None
    options = getattr(cors_middleware, "kwargs", {}) or getattr(cors_middleware, "options", {})
    assert options.get("allow_credentials") is True
    allow_origins = options.get("allow_origins", [])
    assert "*" not in allow_origins
    for origin in allow_origins:
        assert (
            origin.startswith("http://localhost")
            or origin.startswith("http://127.0.0.1")
            or origin.startswith("http://[::1]")
        )


def test_regression_team_ops_contract_and_mixed_orders(tmp_path, monkeypatch):
    """10. /api/paper/team-ops exposes stable frontend contract with orders from both buckets."""
    monkeypatch.setenv("CIO_PORT", "21322")
    now = datetime.now(timezone.utc)
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:21322")

    # Add orders in both SWING and INTRADAY
    st = app.state.app_state
    swing_order = Order(
        order_id="order-swing-1", strategy_id="strat-1", symbol="AAPL", market=Market.US,
        bucket=DecisionScope.SWING, side=OrderSide.BUY, order_type=OrderType.LIMIT,
        limit_price=100.0, quantity=5.0, status=OrderStatus.PENDING, origin=OrderOrigin.STRATEGY,
        reason="test", data=PaperDataContext(last_price=100.0), created_at=now, updated_at=now,
    )
    intra_order = Order(
        order_id="order-intra-1", strategy_id="strat-2", symbol="TSLA", market=Market.US,
        bucket=DecisionScope.INTRADAY, side=OrderSide.BUY, order_type=OrderType.LIMIT,
        limit_price=50.0, quantity=10.0, status=OrderStatus.PENDING, origin=OrderOrigin.STRATEGY,
        reason="test", data=PaperDataContext(last_price=50.0), created_at=now, updated_at=now,
    )
    st.portfolio_manager.get_ledger(DecisionScope.SWING).add_order(swing_order)
    st.portfolio_manager.get_ledger(DecisionScope.INTRADAY).add_order(intra_order)

    # Generate snapshot
    st.runner.generate_canonical_team_ops(now)

    resp = client.get("/api/paper/team-ops")
    assert resp.status_code == 200
    data = resp.json()

    # Exact required top-level contract keys
    required_keys = {
        "server_time", "data_freshness", "safety", "sessions", "portfolio",
        "holdings", "posture", "risk", "quotes", "orders", "fills", "activity", "benchmark",
    }
    assert required_keys.issubset(data.keys())

    # Nested contract validations
    assert "broker_state" in data["safety"]
    assert "kill_switch_active" in data["safety"]
    assert "tw" in data["sessions"]
    assert "us" in data["sessions"]
    assert "nav_status" in data["portfolio"]
    assert "benchmark_name" in data["benchmark"]
    assert "team_return_pct" in data["benchmark"]
    assert "alpha_pct" in data["benchmark"]

    # Orders feed includes BOTH swing and intraday buckets with price exposed
    order_ids = {o["order_id"] for o in data["orders"]}
    assert "order-swing-1" in order_ids
    assert "order-intra-1" in order_ids
    for o in data["orders"]:
        assert "price" in o


def test_team_ops_exposes_desk_roles_and_active_playbook(tmp_path, monkeypatch):
    """Verify /api/paper/team-ops exposes desk roles, active playbook, and desk metadata."""
    monkeypatch.setenv("CIO_PORT", "21323")
    now = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:21323")

    st = app.state.app_state
    # Generate canonical team ops snapshot
    snapshot = st.runner.generate_canonical_team_ops(now)
    assert snapshot["desk"] is not None

    resp = client.get("/api/paper/team-ops")
    assert resp.status_code == 200
    data = resp.json()

    # Verify desk object
    assert "desk" in data["posture"] or "desk" in data
    desk = data["posture"].get("desk") or data.get("desk")
    assert desk["desk_id"] == "dynamic-desk"
    assert desk["desk_name"] == "Autonomous Adaptive Paper Execution Desk"

    # Functional desk roles check (4 roles replacing fixed 8 strategy cards)
    roles = desk["roles"]
    assert len(roles) == 4
    role_titles = [r["title"] for r in roles]
    assert any("宏觀策略長" in t for t in role_titles)
    assert any("程序化執行席" in t for t in role_titles)
    assert any("風控防禦席" in t for t in role_titles)
    assert any("數據品質監理" in t for t in role_titles)

    # Active playbook info
    active_playbook = desk["active_playbook"]
    assert active_playbook["playbook_name"] != ""
    assert active_playbook["selection_rationale"] != ""
    assert desk["next_review_time"] is not None

    # Posture backward/forward compatibility
    assert data["posture"]["active_playbook"]["playbook_name"] == active_playbook["playbook_name"]
    assert data["posture"]["next_review_time"] == desk["next_review_time"]

    # Verify safety and capital invariants
    assert data["safety"]["paper_only"] is True
    assert data["safety"]["broker_connected"] is False
    assert data["safety"]["broker_state"] == "DISCONNECTED"
    assert data["portfolio"]["initial_capital"] == 2378465.0
    assert data["portfolio"]["reporting_currency"] == "TWD"

