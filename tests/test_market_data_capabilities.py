from datetime import date

from fastapi.testclient import TestClient

from cio_market_lab.api.app import create_app
from cio_market_lab.data.yahoo import YahooAdapter, history_interval_for_range


def offline_client() -> TestClient:
    app = create_app()
    app.state.app_state.market_adapter = YahooAdapter(offline_mode=True)
    return TestClient(app)


def test_history_interval_respects_yahoo_intraday_retention():
    today = date(2026, 9, 24)
    assert history_interval_for_range(date(2026, 9, 22), today, today=today) == "1m"
    assert history_interval_for_range(date(2026, 9, 4), today, today=today) == "5m"
    assert history_interval_for_range(date(2026, 7, 26), today, today=today) == "60m"


def test_chart_timeframes_have_distinct_ranges_and_granularities():
    adapter = YahooAdapter(offline_mode=True)
    daily = adapter.get_bars("AAPL", timeframe="1D")
    weekly = adapter.get_bars("AAPL", timeframe="1W")
    monthly = adapter.get_bars("AAPL", timeframe="1M")

    assert len(daily) == 26
    assert len(weekly) == 56
    assert len(monthly) == 31
    assert (daily[1].timestamp - daily[0].timestamp).total_seconds() == 15 * 60
    assert (weekly[1].timestamp - weekly[0].timestamp).total_seconds() == 60 * 60
    assert (monthly[1].timestamp - monthly[0].timestamp).total_seconds() == 24 * 60 * 60
    assert all(bar.quality == "synthetic_fixture" and bar.is_stale for bar in monthly)


def test_market_routes_support_search_lookup_and_breadth():
    client = offline_client()

    search = client.get("/api/markets/symbols/search", params={"q": "TSMC", "market": "TW"})
    assert search.status_code == 200
    assert search.json()["results"][0]["symbol"] == "2330.TW"

    lookup = client.get("/api/markets/symbols/NVDA")
    assert lookup.status_code == 200
    assert lookup.json()["coverage"] == "catalog"

    arbitrary = client.get("/api/markets/symbols/BRK.B")
    assert arbitrary.status_code == 200
    assert arbitrary.json()["coverage"] == "on_demand"

    breadth = client.get("/api/markets/breadth", params={"market": "US", "timeframe": "1W"})
    assert breadth.status_code == 200
    body = breadth.json()
    assert body["universe"] == "defined_liquid_universe"
    assert body["coverage_count"] >= 10
    assert body["advances"] + body["declines"] + body["unchanged"] == body["coverage_count"]
    assert body["freshness"]["live_quotes"] is False
    assert "not the complete exchange" in body["disclaimer"]

    singular = client.get("/api/market/breadth", params={"market": "TW"})
    assert singular.status_code == 200
    assert singular.json()["market"] == "TW"


def test_legacy_chart_endpoint_accepts_timeframe_and_discloses_fallback():
    client = offline_client()
    response = client.get("/api/market/bars/2330.TW", params={"timeframe": "1M", "limit": 5})
    assert response.status_code == 200
    body = response.json()
    assert body["timeframe"] == "1M"
    assert body["interval"] == "1d"
    assert body["count"] == 5
    assert body["bars"][0]["quality"] == "synthetic_fixture"


def test_invalid_timeframe_is_rejected():
    client = offline_client()
    response = client.get("/api/markets/bars/AAPL", params={"timeframe": "5m"})
    assert response.status_code == 422


def test_yahoo_live_path_drops_incomplete_ohlc_rows(monkeypatch):
    import pandas as pd

    frame = pd.DataFrame(
        [
            {"Open": 10.0, "High": 11.0, "Low": 9.0, "Close": 10.5, "Volume": 1000.0},
            {"Open": 10.5, "High": 12.0, "Low": 10.0, "Close": None, "Volume": 500.0},
        ],
        index=pd.to_datetime(["2026-09-23T13:30:00Z", "2026-09-23T13:45:00Z"]),
    )

    class FakeTicker:
        def history(self, **_kwargs):
            return frame

    import yfinance as yf
    monkeypatch.setattr(yf, "Ticker", lambda _symbol: FakeTicker())
    adapter = YahooAdapter(offline_mode=False)
    bars = adapter.get_bars("TEST", timeframe="1D")
    assert len(bars) == 1
    assert bars[0].close == 10.5
