"""Fixture-only intraday feed tests; never perform HTTP requests."""
from datetime import datetime, timezone
import pytest
from cio_market_lab.data.intraday_feed import IntradayFeed, FeedUnavailable, market_open

NOW = datetime(2026, 10, 9, 14, 0, tzinfo=timezone.utc)
TS = int(NOW.timestamp()) - 30


def yahoo():
    return {"chart": {"result": [{"timestamp": [TS], "indicators": {"quote": [{
        "open": [100], "high": [102], "low": [99], "close": [101], "volume": [50]}]}}]}}


def twse():
    return {"msgArray": [{"tlong": str(TS * 1000), "z": "101", "o": "100",
                           "h": "102", "l": "99", "v": "50"}]}


def cboe():
    return {"data": {"timestamp": NOW.isoformat(), "options": [
        {"option": "AAPL261016C00100000", "bid": 2, "ask": 2.2, "iv": 0.3, "delta": 0.5}]}}


def feed(payload, **kwargs):
    return IntradayFeed(fetch=lambda url: payload, clock=lambda: NOW, **kwargs)


def test_yahoo_normalized():
    b = feed(yahoo()).yahoo("AAPL")
    assert (b.c, b.v, b.source, b.staleness_seconds) == (101, 50, "YAHOO_1M", 30)


def test_yahoo_url():
    seen = []
    f = IntradayFeed(fetch=lambda url: (seen.append(url), yahoo())[1], clock=lambda: NOW)
    f.yahoo("2330.TW")
    assert "2330.TW?interval=1m&range=1d" in seen[0]


def test_twse_normalized():
    q = feed(twse()).twse("2330.TW")
    assert (q.c, q.source) == (101, "TWSE_MIS")


def test_twse_url():
    seen = []
    f = IntradayFeed(fetch=lambda url: (seen.append(url), twse())[1], clock=lambda: NOW)
    f.twse("2330.TW")
    assert "ex_ch=tse_2330.tw" in seen[0]


def test_cboe_contract():
    o = feed(cboe()).cboe("AAPL")[0]
    assert (o.bid, o.ask, o.iv, o.delta, o.source) == (2, 2.2, 0.3, 0.5, "CBOE_DELAYED")


def test_cboe_url():
    seen = []
    f = IntradayFeed(fetch=lambda url: (seen.append(url), cboe())[1], clock=lambda: NOW)
    f.cboe("AAPL")
    assert seen[0].endswith("/AAPL.json")


def test_malformed_yahoo():
    with pytest.raises((FeedUnavailable, KeyError)):
        feed({"chart": {}}).yahoo("AAPL")


def test_stale_yahoo():
    with pytest.raises(FeedUnavailable):
        feed(yahoo(), max_staleness_seconds=10).yahoo("AAPL")


def test_stale_twse():
    with pytest.raises(FeedUnavailable):
        feed(twse(), max_staleness_seconds=10).twse("2330.TW")


def test_stale_cboe():
    with pytest.raises(FeedUnavailable):
        feed(cboe(), max_staleness_seconds=0).cboe("AAPL") if False else feed(
            {"data": {"timestamp": datetime.fromtimestamp(TS, timezone.utc).isoformat(),
                      "options": cboe()["data"]["options"]}},
            max_staleness_seconds=10).cboe("AAPL")


def test_tw_fallback():
    f = IntradayFeed(fetch=lambda url: yahoo() if "yahoo" in url else {"msgArray": []},
                     clock=lambda: NOW)
    assert f.latest("2330.TW").source == "YAHOO_1M"


def test_tw_primary():
    f = IntradayFeed(fetch=lambda url: twse() if "twse" in url else yahoo(), clock=lambda: NOW)
    assert f.latest("2330.TW").source == "TWSE_MIS"


def test_all_sources_fail():
    with pytest.raises(FeedUnavailable):
        feed({}).latest("2330.TW")


def test_closed_market_gate():
    # NOW is Friday 10:00 ET (open); use Saturday to test the closed gate.
    closed = datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc)
    def no_fetch(url):
        pytest.fail("closed-market gate must reject before fetching")
    f = IntradayFeed(fetch=no_fetch, clock=lambda: closed)
    with pytest.raises(FeedUnavailable, match="market closed"):
        f.latest("AAPL", require_open=True)


def test_market_hours():
    assert market_open("US", NOW)
    assert not market_open("TW", NOW)


def test_bad_cboe_spread():
    p = cboe()
    p["data"]["options"][0]["ask"] = 1
    with pytest.raises(FeedUnavailable):
        feed(p).cboe("AAPL")
