"""Freshness regressions: historical research cannot authorize stale fills."""
from datetime import datetime, timedelta, timezone

import pytest

from cio_market_lab.data import tw_official
from cio_market_lab.data.tw_official import _iso_date
from cio_market_lab.domain.models import Bar, Quote
from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner
from cio_market_lab.engine.paper_orders import PaperOrderService
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.events.store import EventStore
from cio_market_lab.research.browser import PublicResearchInboxReader
from cio_market_lab.research.official import OfficialResearchProducer, TW_REVENUE_URL, SEC_TICKERS_URL
from tests.test_runtime_continuity import MockMarketAdapter


def test_invalid_official_report_date_never_becomes_today():
    with pytest.raises(ValueError, match="INVALID_OFFICIAL_REPORT_DATE"):
        _iso_date("n/a")
    with pytest.raises(ValueError):
        _iso_date("1150230")
    assert _iso_date("1150924") == "2026-09-24"

@pytest.mark.parametrize("report_date", ["", "n/a", "1150230"])
def test_official_index_invalid_report_date_is_unavailable(monkeypatch, report_date):
    monkeypatch.setattr(tw_official, "quote_rows", lambda: {
        "2330": {"code": "2330", "market": "TWSE", "date": report_date,
                 "close": 100.0, "change": 1.0, "amount": 1000}
    })
    monkeypatch.setattr(tw_official, "profile_rows", lambda: {
        "2330": {"industry": "24", "shares": 1000}
    })
    result = tw_official.index_components_snapshot()
    assert result["refresh_state"] == "unavailable"
    assert result["date"] is None and result["reference_date"] is None
    assert result["entries"] == [] and result["info"] == []
    assert result["reason"]

def test_read_only_context_without_snapshot_never_regenerates_nav_or_fetches(tmp_path, monkeypatch):
    now = datetime(2026, 9, 29, 1, 0, tzinfo=timezone.utc)
    adapter = MockMarketAdapter(now)
    pm = PortfolioManager(initial_cash_swing=10000, initial_cash_intraday=10000)
    runner = AutonomousPaperRunner(tmp_path, pm, PaperOrderService(pm, EventStore(":memory:")), adapter,
                                   now_fn=lambda: now, require_cio_provider=False, is_read_only=True)
    def forbidden(*args, **kwargs):
        pytest.fail("read-only context regenerated NAV or fetched external data")
    monkeypatch.setattr(runner, "generate_canonical_team_ops", forbidden)
    monkeypatch.setattr(runner, "get_durable_quote", forbidden)
    monkeypatch.setattr(adapter, "get_latest_quote", forbidden)
    snapshot = runner.get_canonical_team_ops(is_read_only=True)
    assert snapshot["status"] == "unavailable"
    assert snapshot["portfolio"]["nav"] is None
    context = runner.build_decision_context_request(symbols=["2330.TW"], read_only=True)
    assert context.canonical_portfolio["nav"] is None
    assert context.verified_quotes == {}
    monkeypatch.setattr(runner, "get_canonical_team_ops", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="SNAPSHOT_UNAVAILABLE"):
        runner.build_decision_context_request(symbols=["2330.TW"], read_only=True)

def test_runner_freshness_missing_and_future_timestamps_fail_closed(tmp_path):
    now = datetime(2026, 9, 29, 1, 0, tzinfo=timezone.utc)
    adapter = MockMarketAdapter(now)
    pm = PortfolioManager(initial_cash_swing=10000, initial_cash_intraday=10000)
    runner = AutonomousPaperRunner(tmp_path, pm, PaperOrderService(pm, EventStore(":memory:")), adapter,
                                   now_fn=lambda: now, require_cio_provider=False)
    bar = Bar(symbol="AAPL", timestamp=now - timedelta(days=1), observed_at=now,
              open=100, high=101, low=99, close=100, volume=1000, source="real")
    assert not runner._is_fresh(None)
    for changed in ({"timestamp": None}, {"observed_at": None},
                    {"timestamp": now + timedelta(seconds=1)},
                    {"observed_at": now + timedelta(seconds=1)}):
        assert not runner._is_fresh(bar.model_copy(update=changed))


def test_analysis_bar_observation_is_distinct_from_executable_quote(tmp_path):
    now = datetime(2026, 9, 29, 1, 0, tzinfo=timezone.utc)
    adapter = MockMarketAdapter(now)
    pm = PortfolioManager(initial_cash_swing=10000, initial_cash_intraday=10000)
    runner = AutonomousPaperRunner(tmp_path, pm, PaperOrderService(pm, EventStore(":memory:")), adapter,
                                   now_fn=lambda: now, require_cio_provider=False)
    bar = Bar(symbol="AAPL", timestamp=now - timedelta(days=1), observed_at=now,
              open=100, high=101, low=99, close=100, volume=1000, source="real", quality="good")
    assert runner._is_fresh(bar, intraday=False)
    assert not runner._is_fresh(bar, intraday=True)
    adapter.quotes["AAPL"] = Quote(symbol="AAPL", timestamp=now - timedelta(days=1) + timedelta(seconds=1),
                                    observed_at=now, last_price=100, source="real", quality="good")
    assert runner._find_eligible_later_quote("AAPL", bar) is None
    adapter.quotes["AAPL"] = adapter.quotes["AAPL"].model_copy(update={"timestamp": now - timedelta(minutes=1)})
    # Freshness alone cannot authorize a real-source quote without attested BBO.
    assert runner._find_eligible_later_quote("AAPL", bar) is None
    runner.allow_fixture_quotes = True
    adapter.quotes["AAPL"] = adapter.quotes["AAPL"].model_copy(update={"source": "fixture", "quality": "fixture"})
    assert runner._find_eligible_later_quote("AAPL", bar) is not None
    adapter.quotes["AAPL"] = adapter.quotes["AAPL"].model_copy(update={"quality": "delayed_chart_close_proxy"})
    assert runner._find_eligible_later_quote("AAPL", bar) is None


def test_old_official_company_facts_are_not_fresh_catalysts(tmp_path):
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)

    def fetch(url):
        if url == TW_REVENUE_URL:
            return [{"公司代號": "2330", "出表日期": "1150801", "資料年月": "11507", "營業收入-當月營收": "123,456"}]
        if url == SEC_TICKERS_URL:
            return {"0": {"ticker": "NVDA", "cik_str": 1045810}}
        return {"tickers": ["NVDA"], "filings": {"recent": {"form": ["10-Q"],
                "filingDate": ["2026-08-01"], "accessionNumber": ["0001045810-26-000001"]}}}

    reader = PublicResearchInboxReader(inbox_dir=tmp_path)
    result = OfficialResearchProducer(fetch_json=fetch).acquire(["2330.TW", "NVDA"], reader, now)
    assert len(result["accepted"]) == 2, result
    items, gaps = reader.get_verified_research_for_symbols(["2330.TW", "NVDA"], now=now)
    assert not gaps
    for item in items:
        assert item["research_scope"] == "historical_company_facts_not_catalyst"
        assert item["published_at"] < "2026-09-22"
        assert item["verification_status"] == "verified"
