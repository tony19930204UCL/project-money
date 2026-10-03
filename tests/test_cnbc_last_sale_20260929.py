"""CNBC public Nasdaq last-sale adapter: captured field shape and fail-closed variants."""
from datetime import datetime, timedelta, timezone

import pytest

from cio_market_lab.data.cnbc import CnbcNasdaqAdapter, parse_nasdaq_last_sale
from cio_market_lab.data.market_data import CompositeMarketDataAdapter
from cio_market_lab.domain.models import Bar
from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner
from cio_market_lab.engine.paper_orders import PaperOrderService
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.events.store import EventStore
from tests.test_runtime_continuity import MockMarketAdapter

OBSERVED = datetime(2026, 9, 28, 20, 0, 15, tzinfo=timezone.utc)


def payload(symbol="NVDA", **changed):
    # Fields observed in public read-only GET on 2026-09-29; prices are
    # test-only fixtures, never presented as live market observations.
    row = {"symbol": symbol, "last": "228.86", "last_time": "2026-09-28T16:00:00.000-0400",
           "source": "Last NASDAQ LS, VOL From CTA", "realTime": "true"}
    row.update(changed)
    return {"FormattedQuoteResult": {"FormattedQuote": [row]}}


def test_observed_nasdaq_last_sale_is_a_trade_not_a_candle():
    quote = parse_nasdaq_last_sale("NVDA", payload(), OBSERVED)
    assert quote.last_price == 228.86
    assert quote.timestamp == datetime(2026, 9, 28, 20, tzinfo=timezone.utc)
    assert quote.source == "cnbc_nasdaq_last_sale" and not quote.is_synthetic
    assert quote.bid is None and quote.ask is None and quote.last_size == 0
    assert not quote.is_stale


@pytest.mark.parametrize("changed", [
    {"last": "-"}, {"last": "nan"}, {"last": "0"},
    {"last_time": ""}, {"last_time": "Sep 28, 2026"},
    {"last_time": "2026-09-28T16:00:00"},
    {"last_time": "2026-09-29T16:00:00.000-0400"},
    {"source": "Last NYSE LS"}, {"source": "CHART CLOSE"},
    {"realTime": "false"}, {"symbol": "AAPL"},
])
def test_missing_or_unverified_sale_is_rejected(changed):
    with pytest.raises(ValueError, match="CNBC_LAST_SALE"):
        parse_nasdaq_last_sale("NVDA", payload(**changed), OBSERVED)


def test_closed_session_sale_stales_without_age_guard_relaxation():
    quote = parse_nasdaq_last_sale("NVDA", payload(), OBSERVED + timedelta(hours=6))
    assert quote.is_stale
    assert quote.timestamp == datetime(2026, 9, 28, 20, tzinfo=timezone.utc)


def test_last_sale_fits_existing_runner_guard_only_when_fresh_and_later(tmp_path):
    adapter = MockMarketAdapter(OBSERVED)
    portfolio = PortfolioManager(initial_cash_swing=10000, initial_cash_intraday=10000)
    runner = AutonomousPaperRunner(tmp_path, portfolio, PaperOrderService(portfolio, EventStore(":memory:")),
                                   adapter, now_fn=lambda: OBSERVED, require_cio_provider=False)
    bar = Bar(symbol="NVDA", timestamp=OBSERVED - timedelta(minutes=16), observed_at=OBSERVED,
              open=228, high=229, low=227, close=228, volume=100, source="yahoo_delayed")
    adapter.quotes["NVDA"] = parse_nasdaq_last_sale("NVDA", payload(), OBSERVED)
    assert runner._find_eligible_later_quote("NVDA", bar) == adapter.quotes["NVDA"]
    adapter.quotes["NVDA"] = parse_nasdaq_last_sale("NVDA", payload(), OBSERVED + timedelta(hours=6))
    assert runner._find_eligible_later_quote("NVDA", bar) is None
    adapter.quotes["NVDA"] = parse_nasdaq_last_sale("NVDA", payload(), OBSERVED).model_copy(
        update={"quality": "delayed_chart_close_proxy"})
    assert runner._find_eligible_later_quote("NVDA", bar) is None


def test_default_live_composite_routes_us_to_last_sale_and_tw_to_mis():
    adapter = CompositeMarketDataAdapter(offline_mode=False)
    assert isinstance(adapter.us_adapter, CnbcNasdaqAdapter)
    assert adapter._select_adapter("NVDA") is adapter.us_adapter
    assert adapter._select_adapter("2330.TW") is adapter.tw_adapter


def test_unknown_nasdaq_source_cannot_fall_back_to_chart_proxy(monkeypatch):
    class Reply:
        def raise_for_status(self):
            return None

        def json(self):
            return payload(source="CHART CLOSE")

    monkeypatch.setattr("cio_market_lab.data.cnbc.requests.get", lambda *a, **kw: Reply())
    adapter = CnbcNasdaqAdapter(offline_mode=False)
    assert adapter.get_latest_quote("NVDA") is None
    assert adapter.last_fetch_mode == "live_last_sale_unavailable"
