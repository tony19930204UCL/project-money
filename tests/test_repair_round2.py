"""Test suite for Round 2 backend repairs.

Covers:
1. Read-only mutation boundary (preview does not mutate event store, parameterized 403 on port 8765)
2. Local-only CORS (evil.example rejected, explicit local dev origins accepted)
3. Exact public TeamOpsSnapshot contract (exact keys, nested types, no legacy raw fields)
4. Consolidated NAV/cash (single capital pool TWD 2,378,465, mixed-bucket fixture, no double counting)
5. Missing authoritative marks (no fill price fallback, NAV_UNAVAILABLE, cost basis preserved)
6. No same-bar lookahead fills (same timestamp does not fill, later quote fills, stale/synthetic does not fill)
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict
import pytest
from fastapi.testclient import TestClient

from cio_market_lab.api.app import create_app
from cio_market_lab.data.base import MarketDataAdapter
from cio_market_lab.domain.events import EventEnvelope, EventType
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
    TEAM_INITIAL_CAPITAL_TWD,
    DurableQuoteSnapshot,
    TeamOpsSnapshotBuilder,
)
from cio_market_lab.events.store import EventStore


# ============================================================================
# 1. Read-only mutation boundary tests
# ============================================================================

def test_preview_is_pure_calculation_and_does_not_mutate_event_store_on_readonly_port(tmp_path, monkeypatch):
    monkeypatch.setenv("CIO_PORT", "8765")
    monkeypatch.setenv("CIO_AUTONOMOUS_RUNNER_OWNER", "0")
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    st = app.state.app_state
    initial_event_count = st.event_store.count()
    assert initial_event_count == 0

    preview_payload = {
        "symbol": "AAPL",
        "market": "US",
        "bucket": "swing",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": 10.0,
        "origin": "MANUAL",
        "reason": "preview-test",
        "data": {
            "source": "yahoo",
            "last_price": 150.0,
            "age_seconds": 10.0,
            "is_stale": False,
            "is_fallback": False,
        },
    }

    resp = client.post("/api/paper/orders/preview", json=preview_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("APPROVED", "REJECTED")

    # Event count must remain unchanged
    assert st.event_store.count() == initial_event_count

    # No files in runtime directory should be created
    runtime_dir = tmp_path / "data" / "runtime"
    if runtime_dir.exists():
        assert list(runtime_dir.iterdir()) == []


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("POST", "/api/strategies/volatility_contraction/activate", {"authority": "Main CIO"}),
        ("POST", "/api/strategies/volatility_contraction/pause", {"authority": "Main CIO"}),
        ("POST", "/api/paper/orders", {"symbol": "AAPL", "market": "US", "bucket": "swing", "side": "BUY", "order_type": "MARKET", "quantity": 1.0, "origin": "MANUAL", "reason": "r"}),
        ("POST", "/api/paper/orders/ord-123/cancel", {}),
        ("POST", "/api/paper/orders/ord-123/cancel-replace", {"symbol": "AAPL", "market": "US", "bucket": "swing", "side": "BUY", "order_type": "MARKET", "quantity": 1.0, "origin": "MANUAL", "reason": "r"}),
        ("POST", "/api/paper/kill-switch", {"enabled": True, "reason": "halt"}),
        ("PUT", "/api/paper/risk-limits", {"max_position_notional": 50000.0}),
        ("PUT", "/api/paper/experiments/test-strat", {"strategy_id": "test-strat", "enabled": True}),
        ("POST", "/api/paper/experiments/test-strat/start", {}),
        ("POST", "/api/paper/experiments/test-strat/stop", {}),
        ("POST", "/api/paper/experiments/test-strat/run-one-cycle", {}),
        ("POST", "/api/chat", {"message": "ping"}),
        ("POST", "/api/research/intake", {"url": "https://example.com", "title": "t"}),
        ("POST", "/api/v1/order/place_order", {"contract": {"code": "2330"}, "order": {"action": "Buy", "price": 100, "quantity": 1}}),
        ("POST", "/api/v1/order/cancel_order", {"trade_id": "t-1"}),
        ("POST", "/api/v1/order/update_price", {"trade_id": "t-1", "price": 105.0}),
        ("POST", "/api/v1/order/update_qty", {"trade_id": "t-1", "quantity": 2.0}),
        ("POST", "/api/v1/order/place_comboorder", {"combo_contract": {"code": "TXF"}, "order": {"price": 100, "quantity": 1}}),
        ("POST", "/api/v1/order/cancel_comboorder", {"trade_id": "c-1"}),
        ("POST", "/api/v1/watchlist", {"name": "w", "contracts": []}),
        ("PUT", "/api/v1/watchlist/w-1", {"contracts": []}),
        ("POST", "/api/v1/watchlist/w-1/contracts", {"contracts": []}),
        ("DELETE", "/api/v1/watchlist/w-1/contracts", {"contracts": []}),
        ("DELETE", "/api/v1/watchlist/w-1", {}),
        ("POST", "/api/v1/stream/subscribe", {"code": "2330"}),
        ("POST", "/api/v1/stream/unsubscribe", {"code": "2330"}),
        ("POST", "/api/v1/stream/subscribe/ticks", {"code": "2330"}),
        ("POST", "/api/v1/order/reserve_stock", {}),
        ("POST", "/api/v1/order/reserve_earmarking", {}),
    ],
)
def test_all_mutation_routes_reject_on_readonly_port_8765(tmp_path, monkeypatch, method, path, payload):
    monkeypatch.setenv("CIO_PORT", "8765")
    monkeypatch.setenv("CIO_AUTONOMOUS_RUNNER_OWNER", "0")
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    resp = client.request(method, path, json=payload)
    assert resp.status_code == 403, f"{method} {path} returned {resp.status_code}, expected 403"
    detail = resp.json().get("detail", "")
    assert "MUTATION_FORBIDDEN_ON_READONLY_PORT" in str(detail) or "read-only" in str(detail).lower()


# ============================================================================
# 2. Local-only CORS tests
# ============================================================================

def test_cors_rejects_evil_origin_and_accepts_valid_local_origins(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "CIO_CORS_ORIGINS",
        "https://evil.example,http://localhost:3001,http://127.0.0.1:8081,*,http://user:pass@localhost:3000,http://evil.example.com",
    )
    app = create_app(tmp_path)
    client = TestClient(app)

    # 1. Evil origin must NOT receive allow-origin or credentials
    evil_resp = client.options(
        "/api/health",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert evil_resp.headers.get("access-control-allow-origin") != "https://evil.example"

    evil_get = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert evil_get.headers.get("access-control-allow-origin") != "https://evil.example"

    # 2. Local dev origins MUST receive allow-origin and allow-credentials
    for local_orig in ("http://localhost:3001", "http://127.0.0.1:8081"):
        loc_resp = client.options(
            "/api/health",
            headers={
                "Origin": local_orig,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert loc_resp.headers.get("access-control-allow-origin") == local_orig
        assert loc_resp.headers.get("access-control-allow-credentials") == "true"

        loc_get = client.get("/api/health", headers={"Origin": local_orig})
        assert loc_get.headers.get("access-control-allow-origin") == local_orig
        assert loc_get.headers.get("access-control-allow-credentials") == "true"


# ============================================================================
# 3. Exact public TeamOpsSnapshot contract tests
# ============================================================================

def test_team_ops_public_contract_exact_keys_and_types(tmp_path, monkeypatch):
    monkeypatch.setenv("CIO_PORT", "21322")
    monkeypatch.setenv("CIO_AUTONOMOUS_RUNNER_OWNER", "1")
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:21322")

    st = app.state.app_state
    st.runner.generate_canonical_team_ops(now)

    resp = client.get("/api/paper/team-ops")
    assert resp.status_code == 200
    data = resp.json()

    # Exact allowed top-level keys
    allowed_top_keys = {
        "server_time", "data_freshness", "safety", "sessions", "portfolio",
        "holdings", "posture", "risk", "quotes", "orders", "fills", "activity", "benchmark",
        "integrity_warnings", "legacy_leaderboard",
    }
    required_top_keys = {
        "server_time", "data_freshness", "safety", "sessions", "portfolio",
        "holdings", "posture", "risk", "quotes", "orders", "fills", "activity", "benchmark",
    }
    actual_keys = set(data.keys())
    assert required_top_keys.issubset(actual_keys)
    assert actual_keys.issubset(allowed_top_keys), f"Disallowed top-level keys found: {actual_keys - allowed_top_keys}"

    # No legacy raw keys at top level
    legacy_keys = {"timestamp", "version", "cash", "equity", "initial_capital", "realized_pnl", "unrealized_pnl", "total_pnl", "return_pct", "nav_status", "nav", "canonical_positions", "regime_and_posture", "regime", "data_status"}
    assert not (actual_keys & legacy_keys), f"Legacy keys present at top level: {actual_keys & legacy_keys}"

    # data_freshness is a string
    assert data["data_freshness"] in ("fresh", "stale", "unavailable")

    # sessions.tw/us
    for code in ("tw", "us"):
        s = data["sessions"][code]
        assert set(s.keys()) >= {"market", "status", "session_label", "timezone", "server_time"}

    # portfolio
    p = data["portfolio"]
    assert set(p.keys()) >= {"reporting_currency", "equity", "nav_status", "cash", "realized_pnl", "unrealized_pnl", "return_pct", "initial_cash", "as_of"}
    assert p["reporting_currency"] == "TWD"

    # posture
    posture = data["posture"]
    assert "posture_label" in posture
    assert "updated_at" in posture
    for reg_key in ("tw_regime", "us_regime"):
        reg = posture[reg_key]
        assert set(reg.keys()) >= {"market", "regime", "trend", "volatility", "updated_at"}

    # risk
    risk = data["risk"]
    for k in ("kill_switch_active", "kill_switch_armed", "budget_usage_pct", "max_drawdown_pct", "drawdown_limit_pct", "daily_loss_limit", "current_daily_loss", "breaches"):
        assert k in risk
    assert isinstance(risk["breaches"], list)

    # quotes is an array
    assert isinstance(data["quotes"], list)


def test_team_ops_readonly_missing_snapshot_satisfies_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("CIO_PORT", "8765")
    monkeypatch.setenv("CIO_AUTONOMOUS_RUNNER_OWNER", "0")
    app = create_app(tmp_path)
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    resp = client.get("/api/paper/team-ops")
    assert resp.status_code == 200
    data = resp.json()

    allowed_top_keys = {
        "server_time", "data_freshness", "safety", "sessions", "portfolio",
        "holdings", "posture", "risk", "quotes", "orders", "fills", "activity", "benchmark",
        "integrity_warnings", "legacy_leaderboard",
    }
    assert set(data.keys()).issubset(allowed_top_keys)
    assert data["data_freshness"] == "unavailable"
    assert data["portfolio"]["nav_status"] == "SNAPSHOT_UNAVAILABLE"
    assert data["portfolio"]["cash"] is None
    assert data["portfolio"]["initial_cash"] is None
    assert data["portfolio"]["equity"] is None
    assert data["portfolio"]["realized_pnl"] is None
    assert isinstance(data["quotes"], list)
    assert data["risk"]["breaches"] == []
    assert "integrity_warnings" in data
    assert any("SNAPSHOT_UNAVAILABLE" in w for w in data["integrity_warnings"])


# ============================================================================
# 4. Consolidated NAV/cash tests
# ============================================================================

class MockLookaheadAdapter(MarketDataAdapter):
    def __init__(self, bars=None, quote=None):
        self._bars = bars or []
        self._quote = quote

    @property
    def source_name(self):
        return "mock"

    def get_bars(self, symbol, start=None, end=None, timeframe="1D", limit=None):
        return self._bars[-limit:] if limit else self._bars

    def stream_bars(self, symbols):
        yield from self._bars

    def get_latest_bar(self, symbol):
        return self._bars[-1] if self._bars else None

    def get_latest_quote(self, symbol):
        return self._quote


def test_consolidated_nav_and_cash_mixed_bucket_fixture(tmp_path):
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    pm = PortfolioManager(
        initial_cash_swing=TEAM_INITIAL_CAPITAL_TWD,
        initial_cash_intraday=TEAM_INITIAL_CAPITAL_TWD,
    )
    es = EventStore(":memory:")
    po = PaperOrderService(pm, es)

    # TEST_ONLY native funding: USD 10,000 + TWD 2,058,465 report as
    # TWD 2,378,465 under the explicitly simulated 32 TWD/USD assumption.
    po.configure_experiment(PaperExperimentSettings(
        strategy_id="native-usd", enabled=True, universe=["AAPL"],
        base_currency="USD", initial_cash=10_000, unified_cash=True,
    ))
    po.configure_experiment(PaperExperimentSettings(
        strategy_id="native-twd", enabled=True, universe=["2330.TW"],
        base_currency="TWD", initial_cash=TEAM_INITIAL_CAPITAL_TWD - 320_000,
        unified_cash=True,
    ))
    # Mixed-bucket execution occurs on separate native cash accounts.
    # 1. SWING BUY 10 AAPL at USD 150: costs stay USD; FX is reporting only.
    f_swing = Fill(
        fill_id="fill-s1", order_id="ord-s1", symbol="AAPL", bucket=DecisionScope.SWING,
        side=OrderSide.BUY, quantity=10.0, fill_price=150.0, fee=1.0, tax=0.0, slippage=0.5,
        timestamp=now - timedelta(minutes=30), assumptions={}, currency="USD",
    )
    pm.apply_fill(f_swing, strategy_id="native-usd")

    # 2. INTRADAY BUY 20 2330.TW at 1000.0 (fee=28.5, tax=0.0, slip=10.0) TWD => 20000 + costs
    f_intra_buy = Fill(
        fill_id="fill-i1", order_id="ord-i1", symbol="2330.TW", bucket=DecisionScope.INTRADAY,
        side=OrderSide.BUY, quantity=20.0, fill_price=1000.0, fee=28.5, tax=0.0, slippage=10.0,
        timestamp=now - timedelta(minutes=20), assumptions={},
    )
    pm.apply_fill(f_intra_buy, strategy_id="native-twd")

    # 3. INTRADAY SELL 10 2330.TW at 1050.0 (fee=15.0, tax=31.5, slip=5.0) TWD
    f_intra_sell = Fill(
        fill_id="fill-i2", order_id="ord-i2", symbol="2330.TW", bucket=DecisionScope.INTRADAY,
        side=OrderSide.SELL, quantity=10.0, fill_price=1050.0, fee=15.0, tax=31.5, slippage=5.0,
        timestamp=now - timedelta(minutes=10), assumptions={},
    )
    pm.apply_fill(f_intra_sell, strategy_id="native-twd")

    adapter = MockLookaheadAdapter(bars=[])
    runner = AutonomousPaperRunner(tmp_path, pm, po, adapter, now_fn=lambda: now)
    runner.allow_test_only_fx = True  # Explicit TEST_ONLY assumption authorization
    runner.configured_fx_rates = {"USD": 32.0}  # Explicit simulated accounting assumption.

    # Set authoritative quotes for both open positions:
    # AAPL = 160.0 USD, 2330.TW = 1100.0 TWD
    runner._durable_quotes["AAPL"] = DurableQuoteSnapshot(
        symbol="AAPL", market=Market.US, source="real", observed_at=now,
        last_price=160.0, quality="good", is_stale=False, is_synthetic=False,
    )
    runner._durable_quotes["2330.TW"] = DurableQuoteSnapshot(
        symbol="2330.TW", market=Market.TW, source="real", observed_at=now,
        last_price=1100.0, quality="good", is_stale=False, is_synthetic=False,
    )

    snapshot = runner.generate_canonical_team_ops(now)
    assert snapshot["portfolio"]["initial_cash"] == 2_378_465.0
    assert snapshot["portfolio"]["nav_status"] == "OK"

    cash = snapshot["portfolio"]["cash"]
    equity = snapshot["portfolio"]["equity"]

    # Calculate marked positions market value in TWD
    # AAPL: 10 * 160 * 32 = 51200
    # 2330.TW: 10 * 1100 = 11000
    # Total marked MV = 62200
    positions_mv = sum(h["market_value"] * (32.0 if h["currency"] == "USD" else 1.0) for h in snapshot["holdings"])
    assert round(positions_mv, 2) == 62200.0

    # Invariant: cash + marked positions == equity
    assert abs((cash + positions_mv) - equity) < 0.01

    # Invariant: initial_capital + total_pnl == equity (no double-counted capital)
    assert abs((snapshot["portfolio"]["initial_cash"] + (snapshot["portfolio"]["realized_pnl"] or 0.0) + (snapshot["portfolio"]["unrealized_pnl"] or 0.0)) - equity) < 0.01


# ============================================================================
# 5. Missing authoritative marks tests
# ============================================================================

def test_missing_authoritative_mark_leaves_valuation_unavailable_and_no_fill_price_fallback():
    store = EventStore(":memory:")
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)

    # 1. Append fill event without preceding or subsequent quote / bar
    store.append(EventEnvelope(
        event_type=EventType.ORDER_FILLED,
        timestamp=now,
        aggregate_id="ord-1",
        payload={
            "fill_id": "f-1",
            "order_id": "ord-1",
            "symbol": "2330.TW",
            "bucket": "swing",
            "side": "BUY",
            "quantity": 10.0,
            "fill_price": 950.0,
            "fee": 13.5,
            "tax": 0.0,
            "slippage": 5.0,
            "timestamp": now.isoformat(),
        },
    ))

    # Reconstruct portfolio from event store
    reconstructed = store.reconstruct_portfolio(DecisionScope.SWING, initial_cash=1_000_000.0)
    pos = reconstructed.positions["2330.TW"]

    # Cost basis preserved
    assert pos.quantity == 10.0
    assert pos.average_entry_price == 950.0

    # Valuation MUST NOT fall back to fill price 950.0!
    assert pos.current_price is None
    assert pos.market_value is None
    assert pos.unrealized_pnl is None

    # Portfolio equity and unrealized pnl are unavailable
    assert reconstructed.equity is None
    assert reconstructed.unrealized_pnl is None
    assert getattr(reconstructed, "nav_status", "NAV_UNAVAILABLE") == "NAV_UNAVAILABLE"


# ============================================================================
# 6. No same-bar lookahead fills tests
# ============================================================================


def test_order_does_not_fill_on_same_bar_timestamp(tmp_path):
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    bars = [
        Bar(symbol="AAPL", timestamp=now - timedelta(days=2), observed_at=now, open=100, high=101, low=99, close=100, volume=1000, source="real", quality="good"),
        Bar(symbol="AAPL", timestamp=now - timedelta(days=1), observed_at=now, open=101, high=105, low=100, close=104, volume=1500, source="real", quality="good"),
    ]
    # Quote with SAME timestamp as signal bar (now - 1 day)
    same_ts_quote = Quote(
        symbol="AAPL", timestamp=bars[-1].timestamp, observed_at=bars[-1].observed_at,
        bid=104.0, ask=104.5, last_price=104.0, source="real", quality="good", is_stale=False, is_synthetic=False,
    )
    adapter = MockLookaheadAdapter(bars=bars, quote=same_ts_quote)
    pm = PortfolioManager(initial_cash_swing=50000, initial_cash_intraday=50000)
    service = PaperOrderService(pm, EventStore(":memory:"))
    runner = AutonomousPaperRunner(tmp_path, pm, service, adapter, now_fn=lambda: now, require_cio_provider=False)
    runner.configure(PaperExperimentSettings(
        strategy_id="same-bar-strat", enabled=True, universe=["AAPL"], initial_cash=50000, max_position_notional=10000,
    ))

    cycle = runner.run_one_cycle("same-bar-strat")
    # Order was created, but NOT filled on same timestamp
    assert cycle["run"]["orders_count"] == 1
    assert cycle["run"]["fills_count"] == 0
    portfolio = pm.get_strategy_portfolio("same-bar-strat", DecisionScope.SWING)
    assert len(portfolio.fills) == 0


def test_order_fills_on_strictly_later_eligible_quote(tmp_path, monkeypatch):
    now = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
    bar_time = now - timedelta(days=1)
    bars = [
        Bar(symbol="AAPL", timestamp=now - timedelta(days=2), observed_at=now, open=100, high=101, low=99, close=100, volume=1000, source="real", quality="good"),
        Bar(symbol="AAPL", timestamp=bar_time, observed_at=now, open=101, high=105, low=100, close=104, volume=1500, source="real", quality="good"),
    ]
    # Later TEST_ONLY book stays inside the signal-sized notional cap after slippage.
    later_quote = Quote(
        symbol="AAPL", timestamp=now - timedelta(seconds=1), observed_at=now,
        bid=103.8, ask=103.9, last_price=103.9, source="real", quality="good", is_stale=False, is_synthetic=False,
    )
    adapter = MockLookaheadAdapter(bars=bars, quote=later_quote)
    pm = PortfolioManager(initial_cash_swing=50000, initial_cash_intraday=50000)
    service = PaperOrderService(pm, EventStore(":memory:"), now_fn=lambda: now)
    runner = AutonomousPaperRunner(tmp_path, pm, service, adapter, now_fn=lambda: now, require_cio_provider=False)
    adapter.is_fixture = True
    runner.allow_fixture_quotes = True
    runner.configure(PaperExperimentSettings(
        strategy_id="later-fill-strat", enabled=True, universe=["AAPL"], initial_cash=50000, max_position_notional=10000,
    ))

    cycle = runner.run_one_cycle("later-fill-strat")
    assert cycle["run"]["orders_count"] == 1
    assert cycle["run"]["fills_count"] == 1
    portfolio = pm.get_strategy_portfolio("later-fill-strat", DecisionScope.SWING)
    assert len(portfolio.fills) == 1
    fill = portfolio.fills[0]
    assert fill.fill_price > later_quote.ask  # Slippage is applied to the executable ask.
    assert fill.quantity * fill.fill_price <= 10000
    assert fill.quote_verification == "BOOK_BOUND_TEST_ONLY"
    assert "same_bar" not in fill.assumptions.get("timing_assumption", "")


def test_order_does_not_fill_on_later_stale_or_synthetic_quote(tmp_path):
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    bar_time = now - timedelta(days=1)
    bars = [
        Bar(symbol="AAPL", timestamp=now - timedelta(days=2), observed_at=now, open=100, high=101, low=99, close=100, volume=1000, source="real", quality="good"),
        Bar(symbol="AAPL", timestamp=bar_time, observed_at=now, open=101, high=105, low=100, close=104, volume=1500, source="real", quality="good"),
    ]
    # 1. Later but stale quote
    stale_quote = Quote(
        symbol="AAPL", timestamp=bar_time + timedelta(seconds=1), observed_at=now - timedelta(days=5),
        bid=104.8, ask=105.2, last_price=105.0, source="real", quality="delayed", is_stale=True, is_synthetic=False,
    )
    adapter = MockLookaheadAdapter(bars=bars, quote=stale_quote)
    pm = PortfolioManager(initial_cash_swing=50000, initial_cash_intraday=50000)
    service = PaperOrderService(pm, EventStore(":memory:"))
    runner = AutonomousPaperRunner(tmp_path, pm, service, adapter, now_fn=lambda: now, require_cio_provider=False)
    runner.configure(PaperExperimentSettings(
        strategy_id="stale-quote-strat", enabled=True, universe=["AAPL"], initial_cash=50000, max_position_notional=10000,
    ))
    cycle = runner.run_one_cycle("stale-quote-strat")
    assert cycle["run"]["fills_count"] == 0

    # 2. Later but synthetic quote
    synth_quote = Quote(
        symbol="AAPL", timestamp=bar_time + timedelta(seconds=1), observed_at=now,
        bid=104.8, ask=105.2, last_price=105.0, source="synthetic_fallback", quality="synthetic", is_stale=False, is_synthetic=True,
    )
    runner.market_adapter._quote = synth_quote
    cycle2 = runner.run_one_cycle("stale-quote-strat")
    assert cycle2["run"]["fills_count"] == 0
