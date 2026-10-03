"""Bounded public Yahoo chart transport; bars are analysis, never fill books."""
from datetime import datetime, timezone
from urllib.parse import quote
import math
import requests
from cio_market_lab.domain.models import Bar


def parse_chart(symbol, payload, observed):
    results = (payload.get('chart') or {}).get('result') or []
    if len(results) != 1 or results[0].get('meta', {}).get('symbol') != symbol:
        raise ValueError('YAHOO_CHART_SYMBOL_OR_RESULT_INVALID')
    row = results[0]
    timestamps = row.get('timestamp') or []
    values = (row.get('indicators', {}).get('quote') or [{}])[0]
    bars = []
    for i, raw in enumerate(timestamps):
        try:
            fields = {k: float(values[k][i]) for k in ('open', 'high', 'low', 'close', 'volume')}
            stamp = datetime.fromtimestamp(raw, timezone.utc)
        except (ValueError, TypeError, IndexError, KeyError):
            continue
        if any(not math.isfinite(v) for v in fields.values()) or min(fields[k] for k in ('open', 'high', 'low', 'close')) <= 0 or fields['volume'] < 0:
            continue
        if fields['high'] < max(fields['open'], fields['close'], fields['low']) or fields['low'] > min(fields['open'], fields['close']):
            continue
        age = (observed - stamp).total_seconds()
        if age < 0:
            continue
        bars.append(Bar(symbol=symbol, timestamp=stamp, observed_at=observed,
            **fields, source='yahoo_public_chart', quality='public_analysis_candle_not_execution',
            delay_seconds=age, is_stale=age > 172800))
    return bars


def fetch_chart(symbol, period, interval):
    response = requests.get('https://query1.finance.yahoo.com/v8/finance/chart/' + quote(symbol, safe=''),
        params={'range': period, 'interval': interval}, headers={'User-Agent': 'Mozilla/5.0'}, timeout=4)
    response.raise_for_status()
    return parse_chart(symbol, response.json(), datetime.now(timezone.utc))
