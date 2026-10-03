from types import SimpleNamespace
import sys

import pandas as pd

import cio_market_lab.data.tw_official as tw
from cio_market_lab.api import market_routes
from cio_market_lab.api.app import create_app
from cio_market_lab.api.shioaji_facade import _latest_bar, _snapshot, scanner, ticks
from cio_market_lab.data.yahoo import YahooAdapter
from cio_market_lab.domain.models import Bar, Market
from datetime import datetime, timezone
from fastapi.testclient import TestClient


def test_otc_symbol_resolution_uses_official_market(monkeypatch):
    monkeypatch.setattr(tw, "quote_rows", lambda force=False: {"00948B": {"market": "TPEX"}})
    assert tw.resolve_tw_symbol("00948B") == "00948B.TWO"


def test_live_adapter_never_falls_back_to_synthetic_prices(monkeypatch):
    adapter = YahooAdapter(offline_mode=False)
    fake_yahoo = SimpleNamespace(
        Ticker=lambda symbol: SimpleNamespace(history=lambda **kwargs: pd.DataFrame())
    )
    monkeypatch.setitem(sys.modules, "yfinance", fake_yahoo)
    assert adapter.get_bars("MISSING.TW", timeframe="1D") == []
    assert adapter.last_fetch_mode == "live_unavailable"


def test_latest_bar_retries_the_other_taiwan_exchange(monkeypatch):
    monkeypatch.setattr(
        "cio_market_lab.api.shioaji_facade.resolve_tw_symbol",
        lambda code: f"{code}.TW",
    )
    bar = Bar(
        symbol="00948B.TWO",
        market=Market.TW,
        timestamp=datetime.now(timezone.utc),
        open=9.0,
        high=9.02,
        low=8.99,
        close=9.01,
        volume=100,
        source="test",
        observed_at=datetime.now(timezone.utc),
    )

    class Adapter:
        def __init__(self):
            self.calls = []

        def get_latest_bar(self, symbol):
            self.calls.append(symbol)
            return bar if symbol.endswith(".TWO") else None

    adapter = Adapter()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(app_state=SimpleNamespace(market_adapter=adapter))))
    result = _latest_bar(request, {"code": "00948B", "region": "TW"})
    assert result is bar
    assert adapter.calls == ["00948B.TW", "00948B.TWO"]


def test_sector_snapshot_matches_frontend_parallel_array_contract(monkeypatch):
    monkeypatch.setattr(
        tw,
        "quote_rows",
        lambda force=False: {
            "2330": {
                "code": "2330", "name": "台積電", "market": "TWSE", "date": "1150924",
                "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0,
                "change": 1.0, "volume": 10, "amount": 1000, "bid": 100.5, "ask": 101.0,
            }
        },
    )
    monkeypatch.setattr(
        tw,
        "profile_rows",
        lambda force=False: {"2330": {"name": "台積電", "industry": "24", "shares": 1000}},
    )
    payload = tw.index_components_snapshot(limit=10)
    assert payload["info"] == [{"code": "2330"}]
    assert payload["category"] == ["24"]
    assert len(payload["pct_chg"]) == len(payload["reference_weight_ppm"]) == 1
    assert payload["source"] == "TWSE_TPEX_OPENAPI"


def test_bare_tw_symbol_is_canonicalized_before_chart_fetch(tmp_path, monkeypatch):
    monkeypatch.setenv("CIO_MARKET_LAB_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("CIO_MARKET_LAB_OFFLINE", "1")
    monkeypatch.setattr(market_routes, "resolve_tw_symbol", lambda symbol: "2330.TW")
    with TestClient(create_app()) as client:
        payload = client.get("/api/market/bars/2330", params={"timeframe": "1D", "limit": 2}).json()
    assert payload["symbol"] == "2330.TW"
    assert payload["count"] == 2
    assert payload["data_status"] == "ok"


def test_delayed_bar_snapshot_never_fabricates_depth_or_tick_side():
    now = datetime.now(timezone.utc)
    bars = [
        Bar(symbol="2330.TW", market=Market.TW, timestamp=now, open=100, high=101, low=99, close=100, volume=10, source="yahoo_delayed", quality="delayed", observed_at=now),
        Bar(symbol="2330.TW", market=Market.TW, timestamp=now, open=100, high=102, low=100, close=101, volume=20, source="yahoo_delayed", quality="delayed", observed_at=now),
    ]

    class Adapter:
        def get_latest_bar(self, symbol):
            return bars[-1]

        def get_bars(self, symbol, timeframe="1D", limit=2):
            return bars[-limit:]

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(app_state=SimpleNamespace(market_adapter=Adapter()))))
    payload = _snapshot(request, {"code": "2330", "region": "TW"})
    assert payload["buy_price"] is None
    assert payload["sell_price"] is None
    assert payload["tick_type"] is None
    assert payload["quality"] == "delayed"


def test_scanner_honors_sort_direction_and_count(monkeypatch):
    volumes = {"2330": 10, "2317": 30, "2454": 20}
    monkeypatch.setattr(
        "cio_market_lab.api.shioaji_facade._stock_contracts",
        lambda: [{"code": code, "security_type": "STK", "exchange": "TSE", "region": "TW"} for code in volumes],
    )
    monkeypatch.setattr(
        "cio_market_lab.api.shioaji_facade._snapshot",
        lambda request, contract: {
            "code": contract["code"], "close": 100, "change_price": 0,
            "change_rate": 0, "volume": volumes[contract["code"]],
            "amount": volumes[contract["code"]] * 100, "source": "test", "is_stale": False,
        },
    )
    request = SimpleNamespace(query_params={})
    payload = scanner(request, {"scanner_type": "VolumeRank", "ascending": False, "count": 2})
    assert [row["code"] for row in payload] == ["2317", "2454"]
    assert [row["rank"] for row in payload] == [1, 2]


def test_tick_endpoint_discloses_unsupported_instead_of_relabelling_bars():
    request = SimpleNamespace()
    payload = ticks(request, {"contract": {"code": "2330"}, "date": "2026-09-24"})
    assert payload["ticks"] == []
    assert payload["data_status"] == "unsupported"
    assert payload["reason"] == "tick_history_provider_not_configured"
