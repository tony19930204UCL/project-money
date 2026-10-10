"""Archive official TWSE/TPEx end-of-day quotes (PAPER ONLY data layer).

Official OpenAPIs only expose the latest trading day, so history must be accumulated by saving
one snapshot per day. Also provides a cross-check of an intraday price against the official close.
"""
import json
from pathlib import Path
from urllib.request import Request, urlopen

SOURCES = {
    "twse": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    "tpex": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
}


def _get(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})
    with urlopen(req, timeout=60) as r:
        return json.load(r)


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def normalize(venue, rows):
    out = {}
    for r in rows:
        if venue == "twse":
            code, close, date = r.get("Code"), _num(r.get("ClosingPrice")), r.get("Date")
            op, hi, lo, vol = r.get("OpeningPrice"), r.get("HighestPrice"), r.get("LowestPrice"), r.get("TradeVolume")
        else:
            code, close, date = r.get("SecuritiesCompanyCode"), _num(r.get("Close")), r.get("Date")
            op, hi, lo, vol = r.get("Open"), r.get("High"), r.get("Low"), r.get("TradingShares")
        if code and close and close > 0:
            out[code] = {"date": date, "open": _num(op), "high": _num(hi), "low": _num(lo),
                         "close": close, "volume": _num(vol), "venue": venue}
    return out


def archive_today(root="data/official_eod", fetch=_get):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    saved = {}
    for venue, url in SOURCES.items():
        data = normalize(venue, fetch(url))
        if not data:
            raise ValueError("empty " + venue)
        date = next(iter(data.values()))["date"]
        path = root / f"{venue}_{date}.json"
        if not path.exists():
            path.write_text(json.dumps(data, ensure_ascii=False))
        saved[venue] = {"date": date, "count": len(data), "file": str(path)}
    return saved


def crosscheck(price, official_close, tol=0.10):
    """True if intraday price is within tol of the official close (flags bad ticks)."""
    return official_close > 0 and abs(price / official_close - 1) <= tol


if __name__ == "__main__":
    print(json.dumps(archive_today()))
