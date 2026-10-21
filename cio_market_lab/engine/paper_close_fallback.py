"""Bounded Yahoo intraday repair for a completed daily null close (paper only)."""
from datetime import datetime
import math
import time
import requests
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


REASON = "INCOMPLETE_DAILY_BAR_NULL_CLOSE"


def valid_close(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def daily_close(closes, *, fetcher=None, sleep=time.sleep, retries=2):
    """Retry an incomplete latest daily close at most twice."""
    if retries < 0 or retries > 2:
        raise ValueError("INVALID_RETRY_BOUND")
    for attempt in range(retries + 1):
        value = valid_close(closes[-1]) if isinstance(closes, (list, tuple)) and closes else None
        if value is not None:
            return value
        if attempt < retries and fetcher is not None:
            sleep(0)
            closes = fetcher()
        else:
            break
    raise ValueError(REASON)


def completed_close(symbol, session, now, *, get=requests.get):
    """Return same-session final intraday close; reject stale/future sessions."""
    day = datetime.fromisoformat(session).date()
    if day >= now.astimezone(NY).date():
        raise ValueError("INTRADAY_SESSION_NOT_COMPLETED")
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol
    response = get(url, params={"period1": int(datetime.combine(day, datetime.min.time(), NY).timestamp()),
                                "period2": int(datetime.combine(day, datetime.max.time(), NY).timestamp()) + 1,
                                "interval": "5m"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    response.raise_for_status()
    result = response.json()["chart"]["result"][0]
    if result["meta"]["symbol"] != symbol:
        raise ValueError("INTRADAY_SYMBOL_MISMATCH")
    closes = result["indicators"]["quote"][0]["close"]
    valid = []
    for stamp, close in zip(result.get("timestamp", []), closes):
        local = datetime.fromtimestamp(stamp, NY)
        if local.date() == day and (local.hour, local.minute) >= (15, 55) and (local.hour, local.minute) < (16, 0):
            if close is not None and valid_close(close) is not None:
                valid.append(float(close))
    if not valid:
        raise ValueError("NO_SAME_SESSION_FINAL_INTRADAY_CLOSE")
    return valid[-1]
