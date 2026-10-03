from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from cio_market_lab.api.app import create_app
from cio_market_lab.data.base import MarketDataAdapter
from cio_market_lab.domain.models import (
    Bar,
    DecisionScope,
    Market,
    OrderOrigin,
    OrderSide,
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
from cio_market_lab.events.store import EventStore


class CompetitionFixtureAdapter(MarketDataAdapter):
    @property
    def source_name(self):
        return "competition-fixture"

    def get_bars(self, symbol, start=None, end=None, timeframe="1D", limit=None):
        now = datetime.now(timezone.utc)
        bars = [
            Bar(symbol=symbol, timestamp=now - timedelta(seconds=4), observed_at=now, open=99, high=101, low=98,
                close=100, volume=900, source="fixture", quality="good"),
            Bar(symbol=symbol, timestamp=now - timedelta(seconds=2), observed_at=now, open=100, high=104, low=99,
                close=103, volume=1000, source="fixture", quality="good"),
        ]
        return bars[-limit:] if limit else bars

    def stream_bars(self, symbols):
        for symbol in symbols:
            yield self.get_bars(symbol)[-1]

    def get_latest_bar(self, symbol):
        return self.get_bars(symbol)[-1]

    def get_latest_quote(self, symbol):
        bar = self.get_latest_bar(symbol)
        return Quote(symbol=symbol, timestamp=bar.timestamp + timedelta(seconds=1), observed_at=bar.observed_at,
                     bid=102, ask=104, last_price=bar.close, source=bar.source)


def test_strategy_ledgers_are_independent_and_marked_to_market(tmp_path):
    pm = PortfolioManager(initial_cash_swing=1000, initial_cash_intraday=1000)
    service = PaperOrderService(pm, EventStore(":memory:"))
    runner = AutonomousPaperRunner(tmp_path, pm, service, CompetitionFixtureAdapter(), require_cio_provider=False)
    runner.allow_fixture_quotes = True
    runner.configure(PaperExperimentSettings(
        strategy_id="momentum", strategy_name="Momentum", style="aggressive_momentum",
        initial_cash=1000, enabled=True, universe=["AAPL"], max_position_notional=500,
    ))
    runner.configure(PaperExperimentSettings(
        strategy_id="defensive", strategy_name="Defensive", style="defensive_cash_etf",
        initial_cash=2000, enabled=True, universe=["SGOV"], max_position_notional=500,
    ))

    runner.run_one_cycle("momentum")
    now_mkt = datetime.now(timezone.utc)
    runner.portfolio_manager.update_mark_to_market(Bar(
        symbol="AAPL", timestamp=now_mkt,
        observed_at=now_mkt, open=103,
        high=111, low=102, close=110, volume=1200, source="fixture", quality="good",
    ))
    momentum = pm.get_strategy_portfolio("momentum", "swing")
    defensive = pm.get_strategy_portfolio("defensive", "swing")

    assert momentum.initial_cash == 1000
    assert momentum.equity > momentum.initial_cash
    assert len(momentum.fills) == 1
    assert defensive.initial_cash == 2000
    assert defensive.cash == 2000
    assert defensive.fills == []
    assert pm.get_strategy_portfolio("momentum", "swing").equity > 1000


def test_competition_persists_strategy_portfolios_and_settings(tmp_path):
    pm = PortfolioManager(initial_cash_swing=1000, initial_cash_intraday=1000)
    service = PaperOrderService(pm, EventStore(":memory:"))
    runner = AutonomousPaperRunner(tmp_path, pm, service, CompetitionFixtureAdapter(), require_cio_provider=False)
    runner.allow_fixture_quotes = True
    settings = PaperExperimentSettings(
        strategy_id="persisted", strategy_name="Persisted", style="balanced_growth",
        initial_cash=1500, enabled=True, universe=["AAPL"], max_position_notional=500,
    )
    runner.configure(settings)
    runner.run_one_cycle("persisted")

    pm2 = PortfolioManager(initial_cash_swing=1000, initial_cash_intraday=1000)
    service2 = PaperOrderService(pm2, EventStore(":memory:"))
    restarted = AutonomousPaperRunner(tmp_path, pm2, service2, CompetitionFixtureAdapter(), require_cio_provider=False)
    restored = pm2.get_strategy_portfolio("persisted", "swing")

    assert restarted.status("persisted")["enabled"] is True
    assert restored.initial_cash == 1500
    assert len(restored.fills) == 1
    summary = restarted.competition_summary(datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc))
    assert summary["competition"]["days_remaining"] == 30
    # Legacy native ledger lacks USD typing; never relabel TWD as USD.
    assert summary["total"]["initial_cash"] is None
    assert summary["leaderboard"][0]["reporting_status"] == "FX_UNAVAILABLE"
    assert summary["total"]["currency"] == "TWD"
    assert summary["leaderboard"][0]["strategy_id"] == "persisted"


def test_competition_endpoint_exposes_mixed_market_team_contract(tmp_path):
    app = create_app(tmp_path)
    for strategy_id, name, style in [
        ("aggressive", "Aggressive Momentum", "aggressive_momentum"),
        ("high-beta", "Concentrated High Beta", "concentrated_high_beta"),
        ("balanced", "Balanced Growth", "balanced_growth"),
        ("defensive", "Defensive Cash / ETF", "defensive_cash_etf"),
    ]:
        app.state.app_state.runner.configure(PaperExperimentSettings(
            strategy_id=strategy_id, strategy_name=name, style=style, enabled=True,
            universe=["AAPL"], initial_cash=100000,
        ))
    app.state.app_state.runner.configure(PaperExperimentSettings(
        strategy_id="tw-balanced", strategy_name="TW Balanced", style="balanced_growth",
        market="TW", base_currency="TWD", reporting_currency="TWD", fx_to_reporting=1.0,
        enabled=True, universe=["0050.TW"], initial_cash=250000,
    ))

    with TestClient(app) as client:
        payload = client.get("/api/paper/competition").json()

    assert payload["paper_only"] is True
    assert payload["broker_connected"] is False
    assert payload["autonomous_capital_decisions"] is False
    assert payload["competition"]["start"] == "2026-09-26T00:00:00+00:00"
    assert payload["competition"]["end"] == "2026-10-26T00:00:00+00:00"
    assert len(payload["leaderboard"]) == 5
    assert {item["style"] for item in payload["leaderboard"]} == {
        "aggressive_momentum", "concentrated_high_beta", "balanced_growth", "defensive_cash_etf"
    }
    assert {item["market"] for item in payload["leaderboard"]} == {"US", "TW"}
    assert payload["total"]["currency"] == "TWD"
    assert payload["competition"]["name"] == "TW + US Autonomous Paper Team Competition"


def test_competition_rejects_unattributed_manual_cross_currency_rate(tmp_path):
    pm = PortfolioManager(initial_cash_swing=1000, initial_cash_intraday=1000)
    service = PaperOrderService(pm, EventStore(":memory:"))
    runner = AutonomousPaperRunner(tmp_path, pm, service, CompetitionFixtureAdapter(), require_cio_provider=False)
    runner.configure(PaperExperimentSettings(
        strategy_id="us", initial_cash=100, enabled=True, universe=["AAPL"],
        base_currency="USD", fx_to_reporting=32,
    ))
    runner.configure(PaperExperimentSettings(
        strategy_id="tw", market="TW", initial_cash=3200, enabled=True, universe=["0050.TW"],
        base_currency="TWD", fx_to_reporting=1,
    ))
    summary = runner.competition_summary(datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc))
    assert summary["total"]["initial_cash"] is None
    assert summary["total"]["equity"] is None
    rows = {r["strategy_id"]: r for r in summary["leaderboard"]}
    assert rows["us"]["reporting_status"] == "FX_UNAVAILABLE"
    assert rows["us"]["initial_cash_reporting"] is None
    assert rows["us"]["fx_to_reporting"] is None
    assert rows["tw"]["initial_cash_reporting"] == 3200
    assert rows["tw"]["reporting_status"] == "OK"
    assert {row["base_currency"] for row in summary["leaderboard"]} == {"USD", "TWD"}


def test_swing_competition_accepts_delayed_daily_bar_within_explicit_limit():
    pm = PortfolioManager(initial_cash_swing=1000, initial_cash_intraday=1000)
    service = PaperOrderService(pm, EventStore(":memory:"))
    service.configure_experiment(PaperExperimentSettings(
        strategy_id="daily-swing",
        enabled=True,
        universe=["AAPL"],
        base_currency="USD",
        max_position_notional=500,
        max_data_age_seconds=172_800,
    ))
    preview = service.preview(PaperOrderRequest(
        symbol="AAPL",
        market=Market.US,
        bucket=DecisionScope.SWING,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=1,
        origin=OrderOrigin.STRATEGY,
        strategy_id="daily-swing",
        reason="daily close signal",
        data=PaperDataContext(
            source="yahoo_delayed",
            age_seconds=80_000,
            last_price=100,
            is_stale=False,
            is_fallback=False,
        ),
    ))

    assert preview["status"] == "APPROVED"
    assert preview["risk_decision"]["data_status"] == "FRESH_NON_FALLBACK"
