"""Offline-testable, PAPER-only intraday market data normalization."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
import json
import math
from urllib.parse import quote
from urllib.request import urlopen


class FeedUnavailable(ValueError):
    """No valid, fresh data source is available."""


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: datetime
    o: float
    h: float
    l: float
    c: float
    v: float
    source: str
    fetched_at: datetime
    staleness_seconds: float


@dataclass(frozen=True)
class Quote:
    symbol: str
    ts: datetime
    o: float
    h: float
    l: float
    c: float
    v: float
    source: str
    fetched_at: datetime
    staleness_seconds: float


@dataclass(frozen=True)
class OptionContract:
    symbol: str
    contract: str
    ts: datetime
    bid: float
    ask: float
    iv: float | None
    delta: float | None
    source: str
    fetched_at: datetime
    staleness_seconds: float


def _utc(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("invalid timestamp")
    if value.tzinfo is None:
        raise ValueError("timezone required")
    return value.astimezone(timezone.utc)


def _number(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not a price")
    n = float(value)
    if not math.isfinite(n):
        raise ValueError("nonfinite number")
    return n


def market_open(market: str, now: datetime) -> bool:
    zone, start, end = (("Asia/Taipei", time(9), time(13, 30)) if market.upper() == "TW"
                        else ("America/New_York", time(9, 30), time(16)))
    local = _utc(now).astimezone(ZoneInfo(zone))
    return local.weekday() < 5 and start <= local.time() < end


class IntradayFeed:
    def __init__(self, fetch=None, *, max_staleness_seconds=300, clock=None):
        self.fetch = fetch or self._http_fetch
        self.max_staleness_seconds = _number(max_staleness_seconds)
        if self.max_staleness_seconds < 0:
            raise ValueError("negative staleness")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _http_fetch(url):
        import time
        from urllib.error import HTTPError
        from urllib.request import Request
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})
        for attempt in range(3):
            try:
                with urlopen(req, timeout=10) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code not in (429, 502, 503) or attempt == 2:
                    raise
                time.sleep(2 ** attempt)

    def _load(self, url):
        data = self.fetch(url)
        if isinstance(data, (str, bytes)):
            data = json.loads(data)
        if not isinstance(data, dict):
            raise ValueError("expected object")
        return data

    def _age(self, ts):
        fetched = _utc(self.clock())
        age = (fetched - _utc(ts)).total_seconds()
        if age < -5 or age > self.max_staleness_seconds:
            raise FeedUnavailable("stale or future data")
        return fetched, max(0.0, age)

    def yahoo(self, symbol):
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + quote(symbol, safe=".") + "?interval=1m&range=1d"
        data = self._load(url)["chart"]["result"][0]
        times = data["timestamp"]
        values = data["indicators"]["quote"][0]
        for i in range(len(times) - 1, -1, -1):
            try:
                ts = _utc(times[i])
                fetched, age = self._age(ts)
                o, h, l, c, v = (_number(values[k][i]) for k in ("open", "high", "low", "close", "volume"))
                if min(o, h, l, c) <= 0 or v < 0 or l > min(o, c) or h < max(o, c):
                    continue
                return Bar(symbol, ts, o, h, l, c, v, "YAHOO_1M", fetched, age)
            except (ValueError, TypeError, IndexError, KeyError, FeedUnavailable):
                continue
        raise FeedUnavailable("no fresh valid Yahoo bars")

    def twse(self, symbol):
        root = symbol.upper().removesuffix(".TW")
        if not root.isdigit():
            raise ValueError("TWSE requires numeric Taiwan symbol")
        url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_" + root + ".tw"
        row = self._load(url)["msgArray"][0]
        ts = _utc(int(row["tlong"]) / 1000)
        fetched, age = self._age(ts)
        close = _number(row["z"])
        opening = _number(row.get("o", close))
        high = _number(row.get("h", close))
        low = _number(row.get("l", close))
        volume = _number(row.get("v", 0))
        if min(opening, high, low, close) <= 0 or volume < 0 or low > min(opening, close) or high < max(opening, close):
            raise ValueError("invalid TWSE quote")
        return Quote(symbol, ts, opening, high, low, close, volume, "TWSE_MIS", fetched, age)

    def cboe(self, symbol):
        url = "https://cdn.cboe.com/api/global/delayed_quotes/options/" + quote(symbol.upper(), safe="") + ".json"
        data = self._load(url)["data"]
        ts = _utc(data["timestamp"])
        fetched, age = self._age(ts)
        result = []
        for item in data["options"]:
            try:
                bid, ask = _number(item["bid"]), _number(item["ask"])
                if bid < 0 or ask < bid:
                    continue
                iv = item.get("iv")
                delta = item.get("delta")
                iv = None if iv is None else _number(iv)
                delta = None if delta is None else _number(delta)
                result.append(OptionContract(symbol, str(item["option"]), ts, bid, ask, iv, delta, "CBOE_DELAYED", fetched, age))
            except (ValueError, TypeError, KeyError):
                continue
        if not result:
            raise FeedUnavailable("no valid Cboe options")
        return result

    latency_log = None  # optional Path: JSONL of per-fetch source/age/failure

    def _log_latency(self, symbol, source, age, error=None):
        if not self.latency_log:
            return
        try:
            with open(self.latency_log, "a") as fh:
                fh.write(json.dumps({"at": _utc(self.clock()).isoformat(), "symbol": symbol,
                                     "source": source, "age_s": age, "error": error}) + "\n")
        except OSError:
            pass

    def latest(self, symbol, *, market=None, require_open=False):
        r = self._latest(symbol, market=market, require_open=require_open)
        self._log_latency(symbol, r.source, round(r.staleness_seconds, 1))
        return r

    def _latest(self, symbol, *, market=None, require_open=False):
        market = market or ("TW" if symbol.upper().endswith(".TW") else "US")
        if require_open and not market_open(market, self.clock()):
            raise FeedUnavailable("market closed")
        sources = (self.twse, self.yahoo) if market.upper() == "TW" else (self.yahoo,)
        failures = []
        for adapter in sources:
            try:
                return adapter(symbol)
            except (ValueError, KeyError, IndexError, TypeError, FeedUnavailable) as exc:
                failures.append(type(exc).__name__ + ":" + str(exc))
        self._log_latency(symbol, None, None, "; ".join(failures)[:300])
        raise FeedUnavailable("; ".join(failures))
