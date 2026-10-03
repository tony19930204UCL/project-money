from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from cio_market_lab.api.app import create_app
from cio_market_lab.data.base import MarketDataAdapter
from cio_market_lab.domain.events import EventType
from cio_market_lab.domain.models import Bar, Quote, CIODecisionPacket, CIOProvenance, DecisionScope
from cio_market_lab.engine.automation_policy import LLMPolicyBoundary
from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner
from cio_market_lab.engine.cio_packet import sign_cio_packet
from cio_market_lab.engine.paper_orders import PaperExperimentSettings
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.events.store import EventStore
from cio_market_lab.engine.paper_orders import PaperOrderService


class FixtureAdapter(MarketDataAdapter):
    def __init__(self, stale=False):
        self.stale = stale
        self.requests = []

    @property
    def source_name(self):
        return "fixture"

    def get_bars(self, symbol, start=None, end=None, timeframe="1D", limit=None):
        self.requests.append({"symbol": symbol, "timeframe": timeframe, "limit": limit})
        now = datetime.now(timezone.utc)
        bars = [
            Bar(symbol=symbol, timestamp=now - timedelta(days=2), observed_at=now, open=100, high=101, low=99, close=100, volume=1000, source="fixture", quality="good", is_stale=self.stale),
            Bar(symbol=symbol, timestamp=now - timedelta(days=1), observed_at=now, open=101, high=102, low=100, close=101, volume=1100, source="fixture", quality="good", is_stale=self.stale),
            Bar(symbol=symbol, timestamp=now - timedelta(seconds=2), observed_at=now, open=102, high=104, low=101, close=103, volume=1200, source="fixture", quality="good", is_stale=self.stale),
        ]
        return bars[-limit:] if limit is not None else bars

    def stream_bars(self, symbols):
        for symbol in symbols:
            yield self.get_bars(symbol)[-1]

    def get_latest_bar(self, symbol):
        return self.get_bars(symbol)[-1]

    def get_latest_quote(self, symbol):
        bar = self.get_latest_bar(symbol)
        return Quote(symbol=symbol, timestamp=bar.timestamp + timedelta(seconds=1), observed_at=bar.observed_at,
                     bid=101, ask=102, bid_size=100, ask_size=100, last_price=bar.close,
                     source=bar.source, is_stale=bar.is_stale, quote_id=f"fixture-book-{bar.timestamp.isoformat()}",
                     source_capabilities={"source": "fixture", "two_sided_book": True, "size_backed": True,
                                          "exchange_session_attested": True, "entitlement_status": "TEST_ONLY",
                                          "entitlement_evidence_id": "constructed-test-only"})


class BreakoutFixtureAdapter(FixtureAdapter):
    def __init__(self):
        super().__init__()
        self.quote_timestamp = datetime(2026, 9, 24, 5, 0, tzinfo=timezone.utc)

    def get_bars(self, symbol, start=None, end=None, timeframe="1D", limit=None):
        now = datetime(2026, 9, 24, 5, 0, tzinfo=timezone.utc)
        bars = [
            Bar(symbol=symbol, timestamp=now - timedelta(minutes=45), observed_at=now, open=100, high=101, low=99, close=100, volume=1000, source="fixture", quality="good"),
            Bar(symbol=symbol, timestamp=now - timedelta(minutes=30), observed_at=now, open=100, high=102, low=100, close=101, volume=1000, source="fixture", quality="good"),
            Bar(symbol=symbol, timestamp=now - timedelta(minutes=15), observed_at=now, open=101, high=103, low=101, close=102, volume=1000, source="fixture", quality="good"),
            Bar(symbol=symbol, timestamp=now - timedelta(seconds=2), observed_at=now, open=102, high=106, low=102, close=105, volume=1200, source="fixture", quality="good"),
        ]
        return bars[-limit:] if limit is not None else bars

    def get_latest_quote(self, symbol):
        return Quote(
            symbol=symbol,
            timestamp=self.quote_timestamp,
            observed_at=self.quote_timestamp,
            bid=104,
            ask=104,
            bid_size=100,
            ask_size=100,
            last_price=105,
            source="fixture",
            is_stale=False,
            quote_id=f"fixture-breakout-{self.quote_timestamp.isoformat()}",
            source_capabilities={"source": "fixture", "two_sided_book": True, "size_backed": True,
                                 "exchange_session_attested": True, "entitlement_status": "TEST_ONLY",
                                 "entitlement_evidence_id": "constructed-test-only"},
        )


def make_runner(tmp_path, adapter):
    pm = PortfolioManager(initial_cash_swing=10_000, initial_cash_intraday=10_000)
    es = EventStore(":memory:")
    po = PaperOrderService(pm, es)
    # Legacy strategy tests (strategy_id != "dynamic-desk") need the local
    # signal path.  require_cio_provider=False lets non-desk strategies
    # execute locally while the dynamic-desk always remains CIO-owned
    # via the strategy_id == DYNAMIC_DESK_ID check in _run_symbol.
    runner = AutonomousPaperRunner(tmp_path, pm, po, adapter, require_cio_provider=False)
    runner.allow_fixture_quotes = True
    return runner, po, pm


def stage_isolated_cio_buy(runner):
    """Inject fixture decision; missing runtime provider still fails closed."""
    now = datetime.now(timezone.utc)
    packet = CIODecisionPacket(
        case_id="fixture-playbook-buy", as_of=now, thesis="Fixture risk veto probe",
        selected_instrument="AAPL", action="BUY", holding_horizon=DecisionScope.SWING,
        quantity=1, expiry=now + timedelta(hours=1), is_fixture=True,
        provenance=CIOProvenance(authority="MAIN_CIO", source="isolated_test"),
    )
    runner.stage_cio_packet(sign_cio_packet(packet, signer_id="fixture-test-signer"))


def test_stale_data_is_explicit_no_trade(tmp_path):
    runner, orders, _ = make_runner(tmp_path, FixtureAdapter(stale=True))
    runner.configure(PaperExperimentSettings(strategy_id="s", enabled=True, universe=["AAPL"], max_position_notional=500))
    result = runner.run_one_cycle("s")
    assert result["decisions"][0]["action"] == "NO_TRADE"
    assert result["decisions"][0]["reason"] == "NO_TRADE_STALE_OR_SYNTHETIC_DATA"
    assert orders.all_orders() == []


def test_closed_market_latest_close_remains_valid_for_valuation_only(tmp_path):
    pm = PortfolioManager(initial_cash_swing=10_000, initial_cash_intraday=10_000)
    es = EventStore(":memory:")
    po = PaperOrderService(pm, es)
    saturday = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    runner = AutonomousPaperRunner(
        tmp_path,
        pm,
        po,
        FixtureAdapter(stale=True),
        now_fn=lambda: saturday,
    )

    quote = runner.get_durable_quote("2330.TW")

    assert quote is not None
    assert quote.is_stale is False
    assert quote.last_price == 103


def test_fresh_fixture_generates_bounded_local_fill(tmp_path):
    runner, orders, pm = make_runner(tmp_path, FixtureAdapter())
    runner.configure(PaperExperimentSettings(strategy_id="s", enabled=True, universe=["AAPL", "MSFT"], max_position_notional=206, max_open_positions=1))
    result = runner.run_one_cycle("s")
    assert result["run"]["fills_count"] == 1
    assert result["decisions"][0]["quantity"] == 2
    assert result["decisions"][1]["reason"] == "NO_TRADE_MAX_OPEN_POSITIONS"
    assert len(orders.all_orders()) == 1
    assert pm.get_strategy_portfolio("s", "swing").cash < 300_000
    assert pm.get_portfolio("swing").cash == 10_000  # USD never mirrored into TWD


def test_intraday_llm_veto_boundary_and_forced_flatten_are_persisted(tmp_path):
    pm = PortfolioManager(initial_cash_swing=10_000, initial_cash_intraday=10_000)
    es = EventStore(":memory:")
    po = PaperOrderService(pm, es)
    clock = [datetime(2026, 9, 24, 5, 0, tzinfo=timezone.utc)]
    adapter = BreakoutFixtureAdapter()
    runner = AutonomousPaperRunner(
        tmp_path,
        pm,
        po,
        adapter,
        now_fn=lambda: clock[0],
        llm_policy=LLMPolicyBoundary(lambda _: {"verdict": "ALLOW", "reason": "fixture permits entry"}),
        require_cio_provider=False,
    )
    runner.allow_fixture_quotes = True
    runner.configure(PaperExperimentSettings(strategy_id="intraday-safe", enabled=True, universe=["2330.TW"], mode="intraday", max_position_notional=525))

    entry = runner.run_one_cycle("intraday-safe")
    assert entry["decisions"][0]["action"] == "BUY_FILLED"
    entry_fill = pm.get_strategy_portfolio("intraday-safe", "intraday").fills[0]
    assert entry_fill.quote_verification == "BOOK_BOUND_TEST_ONLY"
    assert entry_fill.fill_price > entry_fill.consumed_quote.ask
    assert entry_fill.consumed_quote.ask != adapter.get_latest_quote("2330.TW").last_price
    assert len(es.get_events(event_type=EventType.LLM_REVIEW_RECORDED)) == 1

    # If no same-bar fill, the position may still be pending; advance clock
    # and run again so the forced-flatten path can execute.
    if entry["decisions"][0]["action"] == "BUY_PENDING":
        # Supply a later quote so the BUY fills on the next cycle
        clock[0] = datetime(2026, 9, 24, 5, 1, tzinfo=timezone.utc)
        adapter.quote_timestamp = clock[0]
        entry2 = runner.run_one_cycle("intraday-safe")
        # After the second cycle the BUY should have filled
        portfolio = pm.get_strategy_portfolio("intraday-safe", "intraday")
        assert len(portfolio.fills) >= 1

    clock[0] = datetime(2026, 9, 24, 5, 26, tzinfo=timezone.utc)
    adapter.quote_timestamp = clock[0] - timedelta(seconds=1)
    exit_run = runner.run_one_cycle("intraday-safe")
    assert exit_run["decisions"][0]["action"] == "SELL_FILLED"
    if exit_run["decisions"][0]["action"] == "SELL_FILLED":
        exit_fill = pm.get_strategy_portfolio("intraday-safe", "intraday").fills[-1]
        assert exit_fill.quote_verification == "BOOK_BOUND_TEST_ONLY"
        assert exit_fill.fill_price < exit_fill.consumed_quote.bid
        assert exit_fill.consumed_quote.bid != adapter.get_latest_quote("2330.TW").last_price
        assert exit_run["decisions"][0]["reason"] == "INTRADAY_SESSION_FLATTEN"
        assert len(es.get_events(event_type=EventType.AUTOMATION_EXIT_TRIGGERED)) >= 1
        assert pm.get_strategy_portfolio("intraday-safe", "intraday").positions["2330.TW"].quantity == 0


def test_runner_uses_intraday_bars_for_intraday_and_daily_bars_for_swing(tmp_path):
    adapter = FixtureAdapter()
    runner, _, _ = make_runner(tmp_path, adapter)
    runner.configure(PaperExperimentSettings(strategy_id="i", enabled=True, universe=["AAPL"], mode="intraday"))
    runner.configure(PaperExperimentSettings(strategy_id="s", enabled=True, universe=["AAPL"], mode="swing"))

    intraday = runner.run_one_cycle("i")
    swing = runner.run_one_cycle("s")

    # Intraday uses 1D (15m bars), swing uses 1M (daily bars).
    # Quote-refresh requests may interleave; locate signal-bar requests by timeframe.
    intraday_reqs = [r for r in adapter.requests if r["timeframe"] == "1D"]
    swing_reqs = [r for r in adapter.requests if r["timeframe"] == "1M"]
    assert len(intraday_reqs) >= 1
    assert intraday_reqs[0] == {"symbol": "AAPL", "timeframe": "1D", "limit": 32}
    assert len(swing_reqs) >= 1
    assert swing_reqs[0] == {"symbol": "AAPL", "timeframe": "1M", "limit": 8}
    assert intraday["decisions"][0]["inputs"]["analysis_interval"] == "15m"
    assert swing["decisions"][0]["inputs"]["analysis_interval"] == "1d"


def test_intraday_schedule_is_exchange_open_only(tmp_path):
    runner, _, _ = make_runner(tmp_path, FixtureAdapter())
    settings = PaperExperimentSettings(
        strategy_id="intraday-session-gate",
        enabled=True,
        universe=["AAPL"],
        mode="intraday",
        cadence_seconds=900,
    )
    us_open = datetime(2026, 9, 23, 14, 0, tzinfo=timezone.utc)
    us_symbols, _ = runner._scheduled_symbols(settings, us_open)
    assert us_symbols == ["AAPL"]

    both_closed = datetime(2026, 9, 23, 23, 0, tzinfo=timezone.utc)
    closed_symbols, _ = runner._scheduled_symbols(settings, both_closed)
    assert closed_symbols == []


def test_swing_schedule_runs_once_after_each_market_close(tmp_path):
    runner, _, _ = make_runner(tmp_path, FixtureAdapter())
    settings = PaperExperimentSettings(
        strategy_id="swing-session-gate",
        enabled=True,
        universe=["AAPL"],
        mode="swing",
        cadence_seconds=3600,
    )
    after_close = datetime(2026, 9, 23, 20, 15, tzinfo=timezone.utc)
    symbols, slots = runner._scheduled_symbols(settings, after_close)
    assert symbols == ["AAPL"]
    assert slots == {"swing-session-gate|AAPL": "XNYS:2026-09-23"}

    runner._scheduled_slots.update(slots)
    symbols_again, slots_again = runner._scheduled_symbols(settings, after_close)
    assert symbols_again == []
    assert slots_again == {}


def test_settings_and_portfolios_recover_after_restart(tmp_path):
    runner, _, _ = make_runner(tmp_path, FixtureAdapter())
    runner.configure(PaperExperimentSettings(strategy_id="s", enabled=True, universe=["AAPL"], max_position_notional=500))
    runner.run_one_cycle("s")
    restarted, orders, pm = make_runner(tmp_path, FixtureAdapter())
    assert restarted.status("s")["enabled"] is True
    assert len(restarted.history("s")["runs"]) == 1
    assert len(pm.get_strategy_portfolio("s", "swing").fills) == 1
    assert pm.get_strategy_portfolio("s", "swing").currency == "USD"
    assert len(pm.get_portfolio("swing").fills) == 0
    assert len(orders.all_orders()) == 1


def test_api_runner_lifecycle_and_health_invariants(tmp_path, monkeypatch):
    monkeypatch.setenv("CIO_PORT", "21322")
    app = create_app(tmp_path)
    app.state.app_state.market_adapter = FixtureAdapter()
    client = TestClient(app, base_url="http://127.0.0.1:21322")
    settings = {"strategy_id": "s", "enabled": True, "universe": ["AAPL"], "mode": "swing", "cadence_seconds": 60, "max_position_notional": 500, "max_daily_loss": 100, "max_open_positions": 1}
    assert client.put("/api/paper/experiments/s", json=settings).status_code == 200
    status = client.get("/api/health").json()
    assert (status["paper_only"], status["broker_connected"], status["autonomous_capital_decisions"]) == (True, False, False)
    assert client.post("/api/paper/experiments/s/start").status_code == 200
    assert client.post("/api/paper/experiments/s/stop").json()["running"] is False
    cycle = client.post("/api/paper/experiments/s/run-one-cycle").json()
    assert cycle["paper_only"] is True
    history = client.get("/api/paper/experiments/s/history").json()
    assert len(history["runs"]) >= 1


def test_enabled_experiment_auto_resumes_on_service_start(tmp_path, monkeypatch):
    monkeypatch.setenv("CIO_PORT", "21322")
    monkeypatch.setenv("CIO_AUTONOMOUS_RUNNER_OWNER", "1")
    seed_app = create_app(tmp_path)
    seed_app.state.app_state.runner.configure(
        PaperExperimentSettings(
            strategy_id="resume-me",
            enabled=True,
            universe=["AAPL"],
            mode="swing",
            cadence_seconds=3600,
            max_position_notional=500,
            max_daily_loss=1000,
            max_open_positions=2,
        )
    )

    resumed_app = create_app(tmp_path)
    resumed_app.state.app_state.runner.market_adapter = FixtureAdapter()
    with TestClient(resumed_app, base_url="http://127.0.0.1:21322") as client:
        status = client.get("/api/paper/experiments/resume-me/status").json()
        assert status["running"] is True
        assert status["paper_only"] is True
        assert status["broker_connected"] is False
        client.post("/api/paper/experiments/resume-me/stop")


def test_dynamic_desk_migration_from_legacy_eight_strategies(tmp_path):
    # Setup legacy eight strategy settings on disk
    runtime_dir = tmp_path / "data" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    legacy_file = runtime_dir / "experiment_settings.json"
    legacy_settings = {
        "aggressive-momentum-us": {"strategy_id": "aggressive-momentum-us", "universe": ["TSLA", "NVDA"], "initial_cash": 9364.92, "fx_to_reporting": 31.747, "enabled": True},
        "concentrated-high-beta-us": {"strategy_id": "concentrated-high-beta-us", "universe": ["TSLA", "PLTR"], "initial_cash": 9364.92, "fx_to_reporting": 31.747, "enabled": True},
        "balanced-growth-us": {"strategy_id": "balanced-growth-us", "universe": ["MSFT", "AMZN"], "initial_cash": 9364.92, "fx_to_reporting": 31.747, "enabled": True},
        "defensive-cash-etf-us": {"strategy_id": "defensive-cash-etf-us", "universe": ["SGOV", "BIL"], "initial_cash": 9364.92, "fx_to_reporting": 31.747, "enabled": True},
        "aggressive-momentum-tw": {"strategy_id": "aggressive-momentum-tw", "universe": ["1519.TW", "2383.TW"], "initial_cash": 297308.125, "fx_to_reporting": 1.0, "enabled": True},
        "concentrated-high-beta-tw": {"strategy_id": "concentrated-high-beta-tw", "universe": ["1519.TW", "3017.TW"], "initial_cash": 297308.125, "fx_to_reporting": 1.0, "enabled": True},
        "balanced-growth-tw": {"strategy_id": "balanced-growth-tw", "universe": ["0050.TW", "2330.TW"], "initial_cash": 297308.125, "fx_to_reporting": 1.0, "enabled": True},
        "defensive-income-tw": {"strategy_id": "defensive-income-tw", "universe": ["00919.TW", "00878.TW"], "initial_cash": 297308.125, "fx_to_reporting": 1.0, "enabled": True},
    }
    import json
    legacy_file.write_text(json.dumps(legacy_settings), encoding="utf-8")

    runner, orders, pm = make_runner(tmp_path, FixtureAdapter())
    # Independent native desks survive restart; merging would relabel cash.
    assert set(runner.paper_orders.experiments) == set(legacy_settings)
    assert len(runner.paper_orders.experiments) == 8
    for sid, legacy in legacy_settings.items():
        desk = runner.paper_orders.experiments[sid]
        native = "TWD" if sid.endswith("-tw") else "USD"
        assert desk.strategy_id == sid
        assert desk.initial_cash == legacy["initial_cash"]
        assert desk.base_currency == native
        assert desk.reporting_currency == "TWD"
        assert desk.universe == legacy["universe"]
        assert desk.enabled is True
        assert pm.get_strategy_portfolio(sid, "swing").currency == native
        assert pm.get_strategy_portfolio(sid, "swing").initial_cash == legacy["initial_cash"]
    assert "2330.TW" in runner.paper_orders.experiments["balanced-growth-tw"].universe
    assert "TSLA" in runner.paper_orders.experiments["aggressive-momentum-us"].universe
    assert "0050.TW" in runner.paper_orders.experiments["balanced-growth-tw"].universe
    assert "MSFT" in runner.paper_orders.experiments["balanced-growth-us"].universe
    assert json.loads(legacy_file.read_text()) == legacy_settings


def test_desk_playbook_selection_rules(tmp_path):
    runner, _, _ = make_runner(tmp_path, FixtureAdapter())
    now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)

    # 1. Stale or degraded data forces CAPITAL_PRESERVATION
    p_stale = runner.select_active_playbook(
        tw_regime="TRENDING_BULL", us_regime="TRENDING_BULL", data_quality="STALE",
        tw_open=True, us_open=True, gross_exposure=0.10, momentum=0.03, liquidity="HIGH", now=now,
    )
    assert p_stale["playbook_id"] == "CAPITAL_PRESERVATION"
    assert p_stale["risk_multiplier"] == 0.0
    assert p_stale["allow_new_entries"] is False
    assert "STALE" in p_stale["selection_rationale"]
    assert "next_review_time" in p_stale

    # 2. Exposure ceiling (>=85%) forces CAPITAL_PRESERVATION
    p_exp = runner.select_active_playbook(
        tw_regime="TRENDING_BULL", us_regime="TRENDING_BULL", data_quality="FRESH",
        tw_open=True, us_open=True, gross_exposure=0.88, momentum=0.03, liquidity="HIGH", now=now,
    )
    assert p_exp["playbook_id"] == "CAPITAL_PRESERVATION"
    assert p_exp["risk_multiplier"] == 0.0
    assert "88.0%" in p_exp["selection_rationale"]

    # 3. High volatility + elevated exposure forces CAPITAL_PRESERVATION
    p_vol_high = runner.select_active_playbook(
        tw_regime="HIGH_VOLATILITY", us_regime="TRENDING_BULL", data_quality="FRESH",
        tw_open=True, us_open=True, gross_exposure=0.45, momentum=0.01, liquidity="HIGH", now=now,
    )
    assert p_vol_high["playbook_id"] == "CAPITAL_PRESERVATION"
    assert p_vol_high["risk_multiplier"] == 0.0

    # 4. High volatility + low exposure selects DEFENSIVE_INCOME
    p_vol_low = runner.select_active_playbook(
        tw_regime="HIGH_VOLATILITY", us_regime="TRENDING_BULL", data_quality="FRESH",
        tw_open=True, us_open=True, gross_exposure=0.15, momentum=0.01, liquidity="HIGH", now=now,
    )
    assert p_vol_low["playbook_id"] == "DEFENSIVE_INCOME"
    assert p_vol_low["risk_multiplier"] == 0.25

    # 5. Active session + surging momentum + low exposure selects CONCENTRATED_HIGH_BETA
    p_beta = runner.select_active_playbook(
        tw_regime="TRENDING_BULL", us_regime="TRENDING_BULL", data_quality="FRESH",
        tw_open=True, us_open=True, gross_exposure=0.15, momentum=0.035, liquidity="HIGH", now=now,
    )
    assert p_beta["playbook_id"] == "CONCENTRATED_HIGH_BETA"
    assert p_beta["risk_multiplier"] == 1.0
    assert p_beta["allow_new_entries"] is True

    # 6. Active session + strong momentum selects AGGRESSIVE_MOMENTUM
    p_mom = runner.select_active_playbook(
        tw_regime="TRENDING_BULL", us_regime="TRENDING_BULL", data_quality="FRESH",
        tw_open=True, us_open=True, gross_exposure=0.25, momentum=0.020, liquidity="HIGH", now=now,
    )
    assert p_mom["playbook_id"] == "AGGRESSIVE_MOMENTUM"
    assert p_mom["risk_multiplier"] == 1.0

    # 7. Normal market condition selects BALANCED_GROWTH
    p_norm = runner.select_active_playbook(
        tw_regime="TRENDING_BULL", us_regime="TRENDING_BULL", data_quality="FRESH",
        tw_open=True, us_open=True, gross_exposure=0.40, momentum=0.005, liquidity="NORMAL", now=now,
    )
    assert p_norm["playbook_id"] == "BALANCED_GROWTH"
    assert p_norm["risk_multiplier"] == 0.60

    # 8. All sessions closed selects SESSION_STANDBY
    p_closed = runner.select_active_playbook(
        tw_regime="CLOSED", us_regime="CLOSED", data_quality="FRESH",
        tw_open=False, us_open=False, gross_exposure=0.20, momentum=0.01, liquidity="NORMAL", now=now,
    )
    assert p_closed["playbook_id"] == "SESSION_STANDBY"
    assert p_closed["risk_multiplier"] == 0.0


def test_desk_execution_blocks_entry_under_capital_preservation(tmp_path):
    runner, orders, _ = make_runner(tmp_path, FixtureAdapter())
    runner.configure(PaperExperimentSettings(
        strategy_id="dynamic-desk",
        strategy_name="Adaptive Desk",
        enabled=True,
        universe=["AAPL"],
        max_position_notional=500,
        initial_cash=2378465.0,
    ))
    runner._active_playbook_override = {
        "playbook_id": "CAPITAL_PRESERVATION",
        "playbook_name": "資本防禦",
        "selection_rationale": "Test capital preservation override",
        "risk_multiplier": 0.0,
        "allow_new_entries": False,
    }
    stage_isolated_cio_buy(runner)
    result = runner.run_one_cycle("dynamic-desk")
    assert result["decisions"][0]["action"] == "NO_TRADE"
    assert "PLAYBOOK_CAPITAL_PRESERVATION" in result["decisions"][0]["reason"]
    assert orders.all_orders() == []


def test_desk_execution_blocks_entry_when_allow_new_entries_false(tmp_path):
    runner, orders, _ = make_runner(tmp_path, FixtureAdapter())
    runner.configure(PaperExperimentSettings(
        strategy_id="dynamic-desk",
        strategy_name="Adaptive Desk",
        enabled=True,
        universe=["AAPL"],
        max_position_notional=500,
        initial_cash=2378465.0,
    ))
    # Test with nonzero risk_multiplier but allow_new_entries=False
    runner._active_playbook_override = {
        "playbook_id": "DEFENSIVE_INCOME",
        "playbook_name": "防禦收益",
        "selection_rationale": "High volatility defense",
        "risk_multiplier": 0.25,
        "allow_new_entries": False,
    }
    stage_isolated_cio_buy(runner)
    result = runner.run_one_cycle("dynamic-desk")
    assert result["decisions"][0]["action"] == "NO_TRADE"
    assert "PLAYBOOK_CAPITAL_PRESERVATION" in result["decisions"][0]["reason"]
    assert result["decisions"][0]["terminal_status"] == "TERMINAL_RISK_BLOCK"
    assert orders.all_orders() == []


def test_dynamic_desk_migration_is_atomic_and_idempotent(tmp_path):
    import json
    runtime_dir = tmp_path / "data" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    settings_file = runtime_dir / "experiment_settings.json"
    legacy_settings = {
        "tw-momentum-long-01": {"strategy_id": "tw-momentum-long-01", "universe": ["2330.TW"], "initial_cash": 300000.0, "enabled": True},
        "us-tech-momentum-01": {"strategy_id": "us-tech-momentum-01", "universe": ["AAPL"], "initial_cash": 300000.0, "enabled": True},
    }
    settings_file.write_text(json.dumps(legacy_settings), encoding="utf-8")

    runner, orders, pm = make_runner(tmp_path, FixtureAdapter())
    assert set(runner.paper_orders.experiments) == set(legacy_settings)
    assert len(runner.paper_orders.experiments) == 2
    assert set(pm._strategy_capital) == set(legacy_settings)
    assert pm.get_strategy_portfolio("tw-momentum-long-01", "swing").currency == "TWD"
    assert pm.get_strategy_portfolio("us-tech-momentum-01", "swing").currency == "USD"

    # Legacy source is not rewritten into a mixed/relabelled account.
    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    assert persisted == legacy_settings
    before = {sid: pm.get_strategy_portfolio(sid, "swing").cash for sid in legacy_settings}

    runner.reload_settings_if_needed()
    assert len(runner.paper_orders.experiments) == 2
    assert set(runner.paper_orders.experiments) == set(legacy_settings)
    assert set(pm._strategy_capital) == set(legacy_settings)
    assert {sid: pm.get_strategy_portfolio(sid, "swing").cash for sid in legacy_settings} == before


def test_desk_scheduling_and_safety_status(tmp_path):
    runner, _, _ = make_runner(tmp_path, FixtureAdapter())
    runner.configure(PaperExperimentSettings(
        strategy_id="dynamic-desk",
        strategy_name="Adaptive Desk",
        enabled=True,
        universe=["0050.TW"],
        base_currency="TWD",
        cadence_seconds=3600.0,
        initial_cash=2378465.0,
    ))
    st = runner.status("dynamic-desk")
    assert st["strategy_id"] == "dynamic-desk"
    assert st["paper_only"] is True
    assert st["broker_connected"] is False
    assert st["decision_schedule"] == "once_after_each_market_close"
    assert st["llm_may_place_order"] is False

