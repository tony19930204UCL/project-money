"""Public CNBC Nasdaq last-sale feed for catalogued Nasdaq equities.

This is a vendor-reported last sale, not a book or chart candle. Unavailable,
untimestamped, or unrecognised sources fall back to non-executable Yahoo bars.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from time import monotonic
from typing import Optional

import requests

from cio_market_lab.data.market_data import LIQUID_UNIVERSE
from cio_market_lab.data.yahoo import YahooAdapter
from cio_market_lab.domain.models import Quote

CNBC_QUOTE_URL = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
NASDAQ_CATALOG = frozenset(item.symbol for item in LIQUID_UNIVERSE if item.market == "US" and item.exchange == "NASDAQ" and item.asset_type == "equity")


def parse_nasdaq_last_sale(symbol: str, payload: dict, observed: datetime) -> Quote:
    """Require a dated last sale and explicit Nasdaq LS provenance; never infer it."""
    rows = (payload.get("FormattedQuoteResult") or {}).get("FormattedQuote") or []
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("CNBC_LAST_SALE_MISSING")
    row = rows[0]
    if row.get("symbol") != symbol or not str(row.get("source", "")).startswith("Last NASDAQ LS"):
        raise ValueError("CNBC_LAST_SALE_SOURCE_UNVERIFIED")
    if str(row.get("realTime", "")).lower() != "true":
        raise ValueError("CNBC_LAST_SALE_NOT_REALTIME")
    try:
        last = float(str(row["last"]).replace(",", ""))
        timestamp = datetime.fromisoformat(row["last_time"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("CNBC_LAST_SALE_INVALID_FIELDS") from exc
    if not math.isfinite(last) or last <= 0 or timestamp.tzinfo is None:
        raise ValueError("CNBC_LAST_SALE_INVALID_FIELDS")
    timestamp = timestamp.astimezone(timezone.utc)
    age = (observed - timestamp).total_seconds()
    if age < -60:
        raise ValueError("CNBC_LAST_SALE_FUTURE_TIMESTAMP")
    return Quote(
        symbol=symbol, timestamp=timestamp, observed_at=observed,
        last_price=last, last_size=0.0, bid=None, ask=None,
        source="cnbc_nasdaq_last_sale", quality="public_reported_last_sale",
        delay_seconds=max(0.0, age), is_stale=age > 1800,
        is_synthetic=False,
    )


class CnbcNasdaqAdapter(YahooAdapter):
    """Yahoo for analysis bars; Nasdaq last-sale quote for supported US symbols."""

    def __init__(self, offline_mode: bool = True):
        super().__init__(offline_mode=offline_mode)
        self._quote_cache: dict[str, tuple[float, Quote]] = {}

    @property
    def source_name(self) -> str:
        return "yahoo_bars_cnbc_nasdaq_last_sale"

    def get_latest_quote(self, symbol: str) -> Optional[Quote]:
        symbol = symbol.strip().upper()
        if self.offline_mode or symbol not in NASDAQ_CATALOG:
            return super().get_latest_quote(symbol)
        cached = self._quote_cache.get(symbol)
        if cached and monotonic() < cached[0]:
            return cached[1]
        # Opt-in PAPER-only public book. Failure falls back to last sale, which
        # remains non-fillable; never synthesize a spread or displayed depth.
        import os
        if os.getenv('CIO_PUBLIC_NASDAQ_BOOK', '0') == '1':
            try:
                from cio_market_lab.data.nasdaq_public import fetch_public_book
                quote = fetch_public_book(symbol)
                self._quote_cache[symbol] = (monotonic() + 5, quote)
                self.last_fetch_mode = 'live_nasdaq_public_top_of_book'
                self.last_error = None
                return quote
            except (requests.RequestException, ValueError, TypeError, KeyError):
                pass
        try:
            response = requests.get(
                CNBC_QUOTE_URL,
                params={"symbols": symbol, "requestMethod": "quick", "noform": "1", "fund": "1", "exthrs": "1", "output": "json"},
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=4,
            )
            response.raise_for_status()
            quote = parse_nasdaq_last_sale(symbol, response.json(), datetime.now(timezone.utc))
            self._quote_cache[symbol] = (monotonic() + 5, quote)
            self.last_fetch_mode = "live_cnbc_nasdaq_last_sale"
            self.last_error = None
            return quote
        except (requests.RequestException, ValueError, TypeError) as exc:
            self.last_fetch_mode = "live_last_sale_unavailable"
            self.last_error = f"CNBC {type(exc).__name__}: {exc}"
            # No Yahoo candle can replace an unavailable trade. Returning None
            # is more useful than a quote-shaped but non-executable chart close.
            return None
