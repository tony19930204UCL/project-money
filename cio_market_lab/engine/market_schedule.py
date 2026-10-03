from __future__ import annotations

"""Exchange-calendar gates for scheduled paper experiments.

Intraday decisions are eligible only while the symbol's primary exchange is
open. Swing decisions are eligible once per exchange session, after the final
daily bar has closed, and only during a bounded post-close review window.
"""

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

import exchange_calendars as xcals
import pandas as pd


@lru_cache(maxsize=2)
def _calendar(name: str):
    return xcals.get_calendar(name)


def calendar_name(symbol: str) -> str:
    return "XTAI" if symbol.upper().endswith((".TW", ".TWO")) else "XNYS"


def _utc_timestamp(value: datetime) -> pd.Timestamp:
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return pd.Timestamp(aware.astimezone(timezone.utc))


def intraday_market_open(symbol: str, now: datetime) -> bool:
    calendar = _calendar(calendar_name(symbol))
    minute = _utc_timestamp(now).floor("min")
    return bool(calendar.is_open_on_minute(minute))


def swing_session_slot(
    symbol: str,
    now: datetime,
    *,
    close_delay: timedelta = timedelta(minutes=10),
    review_window: timedelta = timedelta(hours=4),
) -> Optional[str]:
    """Return a once-per-session slot when a completed daily bar is actionable."""

    calendar_id = calendar_name(symbol)
    calendar = _calendar(calendar_id)
    minute = _utc_timestamp(now).floor("min")
    try:
        session = calendar.minute_to_session(minute, direction="previous")
    except ValueError:
        return None
    close = calendar.session_close(session)
    eligible_at = close + close_delay
    if minute < eligible_at or minute > close + review_window:
        return None
    return f"{calendar_id}:{session.date().isoformat()}"
