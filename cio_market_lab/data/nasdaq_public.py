"""Read-only Nasdaq public top-of-book snapshots for PAPER simulation.

No broker, paid-feed entitlement or exchange-book timestamp is claimed.
The vendor last-trade minute is a conservative freshness proxy; exact book
exchange time is unavailable. Sizes are consumed as raw shares, never lots.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import hashlib
import json
import math
import requests
from cio_market_lab.domain.models import Quote


def parse_public_book(symbol: str, payload: dict, observed: datetime) -> Quote:
    data = payload.get('data') or {}
    row = data.get('primaryData') or {}
    if data.get('symbol') != symbol or data.get('marketStatus') != 'Open' or row.get('isRealTime') is not True:
        raise ValueError('NASDAQ_PUBLIC_BOOK_NOT_OPEN_REALTIME')
    def number(key):
        n = float(str(row[key]).replace('$', '').replace(',', ''))
        if not math.isfinite(n) or n <= 0:
            raise ValueError('NASDAQ_PUBLIC_BOOK_INVALID_NUMBER')
        return n
    bid, ask = number('bidPrice'), number('askPrice')
    if bid > ask:
        raise ValueError('NASDAQ_PUBLIC_BOOK_CROSSED')
    raw_time = row['lastTradeTimestamp']
    if not raw_time.endswith(' ET'):
        raise ValueError('NASDAQ_PUBLIC_BOOK_TIMEZONE_UNVERIFIED')
    stamp = datetime.strptime(raw_time[:-3], '%b %d, %Y %I:%M %p').replace(tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc)
    age = (observed - stamp).total_seconds()
    if age < 0 or age > 120:
        raise ValueError('NASDAQ_PUBLIC_BOOK_STALE_OR_FUTURE')
    quote_hash = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
    return Quote(symbol=symbol, timestamp=stamp, observed_at=observed,
        bid=bid, ask=ask, bid_size=number('bidSize'), ask_size=number('askSize'),
        last_price=number('lastSalePrice'), source='nasdaq_public_top_of_book',
        quality='public_snapshot_trade_minute_freshness_proxy', delay_seconds=age,
        is_stale=False, is_synthetic=False, quote_id=quote_hash,
        source_capabilities={
            'quote_kind': 'public_top_of_book', 'entitlement_status': 'VERIFIED',
            'entitlement_scope': 'unauthenticated_public_page_only_not_paid_feed',
            'broker_connected': False, 'is_fixture': False,
            'book_exchange_timestamp_available': False,
            'timestamp_semantics': 'vendor_last_trade_minute_conservative_proxy',
            'size_unit': 'raw_vendor_size_conservatively_consumed_as_shares',
            'source_url': f'https://api.nasdaq.com/api/quote/{symbol}/info?assetclass=stocks',
            'raw_primary_data_sha256': quote_hash,
        })


def fetch_public_book(symbol: str) -> Quote:
    url = f'https://api.nasdaq.com/api/quote/{symbol}/info'
    response = requests.get(url, params={'assetclass': 'stocks'},
        headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json', 'Origin': 'https://www.nasdaq.com'}, timeout=5)
    response.raise_for_status()
    return parse_public_book(symbol, response.json(), datetime.now(timezone.utc))
