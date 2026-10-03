#!/usr/bin/env python3
"""Read-only Public Market-Data Probe Command for Main CIO.

Executes a non-mutating, zero-credential public probe against public data sources
(TWSE/TPEX OpenAPI and Yahoo delayed/public).
Preserves source timestamps, measures latency / data age, and provides honest
session freshness classification and next market-session release conditions.
"""
from __future__ import annotations

import argparse
from datetime import datetime, time as dtime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
import pandas as pd

from cio_market_lab.data.tw_official import TwOfficialAdapter, official_quote, resolve_tw_symbol
from cio_market_lab.data.yahoo import YahooAdapter
from cio_market_lab.domain.models import Bar, Market
from cio_market_lab.engine.market_schedule import _calendar, calendar_name, intraday_market_open


class CalendarUncertaintyError(ValueError):
    """Raised when authoritative exchange calendar cannot be determined or resolved."""
    pass


def classify_market_session(symbol: str, now: datetime, is_eod: bool = False) -> Dict[str, Any]:
    """Determine market session state and honest freshness classification using authoritative calendars."""
    norm = symbol.strip().upper()
    if not norm:
        raise CalendarUncertaintyError("Calendar uncertainty: empty symbol")
    if any(norm.endswith(sfx) for sfx in [".L", ".LON", ".HK", ".T", ".AX", ".TO", ".DE"]):
        raise CalendarUncertaintyError(f"Calendar uncertainty: unsupported non-TW/US exchange market for symbol {symbol}")

    is_tw = norm.endswith((".TW", ".TWO")) or (norm.isdigit() and len(norm) >= 4)
    cal_id = "XTAI" if is_tw else "XNYS"

    try:
        cal = _calendar(cal_id)
        if cal is None:
            raise CalendarUncertaintyError(f"Calendar uncertainty: calendar {cal_id} could not be loaded")
    except Exception as exc:
        raise CalendarUncertaintyError(f"Calendar uncertainty for {symbol}: {exc}") from exc

    now_utc = now.astimezone(timezone.utc)
    minute = pd.Timestamp(now_utc).floor("min")

    # Timezone conversion using IANA names (America/New_York handles DST automatically)
    if is_tw:
        local_tz = ZoneInfo("Asia/Taipei")
    else:
        local_tz = ZoneInfo("America/New_York")
    local_dt = now.astimezone(local_tz)

    # Check open status using authoritative exchange calendar
    try:
        is_open = bool(cal.is_open_on_minute(minute))
    except Exception as exc:
        raise CalendarUncertaintyError(f"Calendar uncertainty on is_open_on_minute for {symbol}: {exc}") from exc

    # Determine session state
    if is_open:
        session_state = "REGULAR_OPEN"
    elif local_dt.weekday() in (5, 6):
        session_state = "CLOSED_WEEKEND"
    else:
        # Weekday: check if today is an exchange holiday
        try:
            is_session_today = bool(cal.is_session(minute.date()))
        except Exception:
            is_session_today = False

        if not is_session_today:
            session_state = "CLOSED_HOLIDAY"
        else:
            try:
                sess_open = cal.session_open(minute.date()).to_pydatetime().astimezone(timezone.utc)
                sess_close = cal.session_close(minute.date()).to_pydatetime().astimezone(timezone.utc)
                if now_utc < sess_open:
                    session_state = "CLOSED_PRE_MARKET"
                elif now_utc >= sess_close:
                    session_state = "CLOSED_AFTER_HOURS"
                else:
                    session_state = "CLOSED_SESSION_PAUSE"
            except Exception:
                session_state = "CLOSED_SESSION_PAUSE"

    # Next market open using authoritative calendar (never past, respects holidays & weekends)
    try:
        next_open_ts = cal.next_open(minute)
        next_open_utc = next_open_ts.to_pydatetime().astimezone(timezone.utc)
    except Exception as exc:
        raise CalendarUncertaintyError(f"Calendar uncertainty on next_open for {symbol}: {exc}") from exc

    # Release condition disclosing EOD vs tick capabilities
    if is_tw:
        if is_eod:
            release_condition = (
                "TWSE/TPEX official OpenAPI provides EOD settled quotes published post-session "
                "(approx 14:30 CST); regular market opens at 09:00:00 CST (01:00:00 UTC) "
                "but adapter does not provide live intraday ticks."
            )
        else:
            release_condition = (
                "TWSE/TPEX regular trading opens at 09:00:00 CST (01:00:00 UTC); "
                "live ticks become fresh intra-session upon 09:00 market open."
            )
    else:
        tz_name = local_dt.tzname() or ("EDT" if local_dt.dst() else "EST")
        utc_open_str = next_open_utc.strftime("%H:%M:%S")
        release_condition = (
            f"NYSE/NASDAQ regular trading opens at 09:30:00 {tz_name} ({utc_open_str} UTC); "
            "live quotes transition to fresh intra-session upon 09:30 market open."
        )

    return {
        "market": "TW" if is_tw else "US",
        "market_open_now": is_open,
        "session_state": session_state,
        "local_time": local_dt.isoformat(),
        "next_market_open_iso": next_open_utc.isoformat(),
        "release_condition": release_condition,
    }


def classify_data_freshness(
    bar: Optional[Bar],
    session_info: Dict[str, Any],
    now: datetime,
    is_fixture: bool = False,
    is_eod: bool = False,
) -> str:
    if is_fixture:
        return "SYNTHETIC_FIXTURE"
    if bar is None:
        return "UNAVAILABLE"

    # Future timestamp must fail freshness
    age_seconds = (now - bar.timestamp).total_seconds()
    if age_seconds < -1e-3:
        return "FUTURE_TIMESTAMP_INVALID"

    is_eod_data = (
        is_eod
        or getattr(bar, "quality", "") == "official_eod"
        or getattr(bar, "source", "") == "twse_tpex_openapi"
    )

    if session_info["market_open_now"]:
        if is_eod_data:
            # EOD adapter must not promise fresh ticks during active intra-session
            return "STALE_INTRA_SESSION"
        if age_seconds <= 1800.0:
            return "FRESH_INTRA_SESSION"
        elif age_seconds <= 7200.0:
            return "DELAYED_INTRA_SESSION"
        else:
            return "STALE_INTRA_SESSION"
    else:
        # Market closed
        if session_info["session_state"] in ("CLOSED_WEEKEND", "CLOSED_HOLIDAY"):
            # Weekend / Holiday expectation: last bar <= 5 days old
            if age_seconds <= 432000.0:  # 5 days
                return "STALE_OFF_SESSION_EXPECTED"
            return "STALE_OUTDATED"
        elif session_info["session_state"] in ("CLOSED_AFTER_HOURS", "CLOSED_PRE_MARKET"):
            # Off-session expectation: last bar from today or prior day
            if age_seconds <= 86400.0:
                return "FRESH_POST_SESSION_SETTLED"
            elif age_seconds <= 259200.0:  # 3 days
                return "STALE_OFF_SESSION_EXPECTED"
            return "STALE_OUTDATED"
        return "STALE_OFF_SESSION_EXPECTED"


def probe_single_symbol(
    symbol: str,
    now: datetime,
    test_fixture: bool = False,
    offline_mode: bool = False,
) -> Dict[str, Any]:
    norm_symbol = symbol.strip().upper()
    session_info = classify_market_session(norm_symbol, now, is_eod=False)
    market = session_info["market"]

    if test_fixture:
        # Hermetic test fixture explicitly labeled is_fixture=True
        px = 1000.0 if market == "TW" else 225.0
        bar_ts = now - timedelta(days=2 if session_info["session_state"] in ("CLOSED_WEEKEND", "CLOSED_HOLIDAY") else 1)
        bar = Bar(
            symbol=norm_symbol,
            timestamp=bar_ts,
            observed_at=now,
            open=px, high=px * 1.01, low=px * 0.99, close=px,
            volume=500000.0, source="fixture_probe", delay_seconds=max(0.0, (now - bar_ts).total_seconds()),
            quality="synthetic_fixture", is_stale=True,
        )
        freshness = "SYNTHETIC_FIXTURE"
        return {
            "symbol": norm_symbol,
            "market": market,
            "source": "fixture_probe",
            "provider_mode": "fixture_transport",
            "status": "OK",
            "is_fixture": True,
            "is_eod": False,
            "capabilities": {
                "eod_quotes": True,
                "intraday_ticks": True,
            },
            "last_price": px,
            "bar_timestamp": bar_ts.isoformat(),
            "observed_at": now.isoformat(),
            "data_age_seconds": round(bar.delay_seconds, 2),
            "market_open_now": session_info["market_open_now"],
            "session_state": session_info["session_state"],
            "freshness_classification": freshness,
            "quality": bar.quality,
            "is_stale": bar.is_stale,
            "next_session": {
                "next_market_open": session_info["next_market_open_iso"],
                "release_condition": session_info["release_condition"],
            },
        }

    # Live public probe
    bar: Optional[Bar] = None
    source_name = "unknown"
    fetch_mode = "unknown"
    error: Optional[str] = None
    is_eod = False

    if market == "TW":
        # First try TWSE/TPEX official OpenAPI
        tw_adapter = TwOfficialAdapter(offline_mode=offline_mode)
        bars = tw_adapter.get_bars(norm_symbol, limit=1)
        source_name = tw_adapter.source_name
        fetch_mode = tw_adapter.last_fetch_mode
        error = tw_adapter.last_error

        if bars:
            bar = bars[-1]
            is_eod = getattr(tw_adapter, "is_eod", True)
        else:
            # Fallback to YahooAdapter public delayed
            yahoo_adapter = YahooAdapter(offline_mode=offline_mode)
            try:
                y_bars = yahoo_adapter.get_bars(norm_symbol, limit=1)
                if y_bars:
                    bar = y_bars[-1]
                    source_name = yahoo_adapter.source_name
                    fetch_mode = yahoo_adapter.last_fetch_mode
                    error = None
                    is_eod = False
            except Exception as exc:
                if not error:
                    error = f"Yahoo: {exc}"
    else:
        # US symbol: Yahoo public delayed
        yahoo_adapter = YahooAdapter(offline_mode=offline_mode)
        try:
            bars = yahoo_adapter.get_bars(norm_symbol, limit=1)
            source_name = yahoo_adapter.source_name
            fetch_mode = yahoo_adapter.last_fetch_mode
            error = yahoo_adapter.last_error
            if bars:
                bar = bars[-1]
            is_eod = False
        except Exception as exc:
            fetch_mode = "live_unavailable"
            error = f"Yahoo: {exc}"

    freshness = classify_data_freshness(bar, session_info, now, is_fixture=False, is_eod=is_eod)
    data_age = round((now - bar.timestamp).total_seconds(), 2) if bar else None

    # EOD adapter must not promise fresh ticks in release condition
    if is_eod or source_name == "twse_tpex_openapi":
        release_cond = (
            "TWSE/TPEX official OpenAPI provides EOD settled quotes published post-session "
            "(approx 14:30 CST); regular market opens at 09:00:00 CST (01:00:00 UTC) "
            "but adapter does not provide live intraday ticks."
        )
    else:
        release_cond = session_info["release_condition"]

    return {
        "symbol": norm_symbol,
        "market": market,
        "source": source_name,
        "provider_mode": fetch_mode,
        "status": "OK" if bar is not None else "UNAVAILABLE",
        "error": error,
        "is_fixture": False,
        "is_eod": is_eod,
        "capabilities": {
            "eod_quotes": is_eod or source_name == "twse_tpex_openapi",
            "intraday_ticks": not is_eod and source_name != "twse_tpex_openapi",
        },
        "last_price": bar.close if bar else None,
        "bar_timestamp": bar.timestamp.isoformat() if bar else None,
        "observed_at": now.isoformat(),
        "data_age_seconds": data_age,
        "market_open_now": session_info["market_open_now"],
        "session_state": session_info["session_state"],
        "freshness_classification": freshness,
        "quality": bar.quality if bar else "missing",
        "is_stale": bar.is_stale if bar else True,
        "next_session": {
            "next_market_open": session_info["next_market_open_iso"],
            "release_condition": release_cond,
        },
    }


def execute_public_market_data_probe(
    symbols: Optional[List[str]] = None,
    test_fixture: bool = False,
    offline_mode: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current_time = now or datetime.now(timezone.utc)
    target_symbols = symbols or ["2330.TW", "00948B.TWO", "AAPL", "NVDA"]

    results = [
        probe_single_symbol(
            sym,
            now=current_time,
            test_fixture=test_fixture,
            offline_mode=offline_mode,
        )
        for sym in target_symbols
    ]

    has_live = any(not r["is_fixture"] and r["status"] == "OK" for r in results)
    has_fixture = any(r["is_fixture"] for r in results)

    # Future market session gaps with EOD awareness
    tw_gap = classify_market_session("2330.TW", current_time, is_eod=True)
    us_gap = classify_market_session("AAPL", current_time, is_eod=False)

    return {
        "probe_id": f"probe-{int(current_time.timestamp())}",
        "probed_at": current_time.isoformat(),
        "authority": "MAIN_CIO",
        "read_only": True,
        "broker_connected": False,
        "credentials_loaded": False,
        "is_fixture": has_fixture and not has_live,
        "symbols_probed": target_symbols,
        "results": results,
        "future_market_session_gaps": {
            "TW": {
                "market": "TW",
                "market_open_now": tw_gap["market_open_now"],
                "session_state": tw_gap["session_state"],
                "next_market_open": tw_gap["next_market_open_iso"],
                "release_condition": tw_gap["release_condition"],
            },
            "US": {
                "market": "US",
                "market_open_now": us_gap["market_open_now"],
                "session_state": us_gap["session_state"],
                "next_market_open": us_gap["next_market_open_iso"],
                "release_condition": us_gap["release_condition"],
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-Only Public Market-Data Probe")
    parser.add_argument(
        "--symbols",
        default="2330.TW,00948B.TWO,AAPL,NVDA",
        help="Comma-separated symbols to probe",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write output JSON",
    )
    parser.add_argument(
        "--test-fixture",
        action="store_true",
        default=False,
        help="Run hermetic offline fixture probe explicitly marked is_fixture=True",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        default=False,
        help="Run in explicit offline mode",
    )

    args = parser.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    probe_result = execute_public_market_data_probe(
        symbols=symbols,
        test_fixture=args.test_fixture,
        offline_mode=args.offline or (os.getenv("CIO_MARKET_LAB_OFFLINE") == "1"),
    )

    output_json = json.dumps(probe_result, indent=2)
    print(output_json)

    if args.output:
        out_path = Path(args.output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_json, encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
