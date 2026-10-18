"""Immutable PAPER shadow inputs. No network, model, runtime writes or trade authority.

The real strategy is executed separately, against frozen inputs with writes trapped.
Validation precedes its already-processed-session guard: missing inputs are not WAIT.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import exchange_calendars as xcals
import pandas as pd


class FrozenInputError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load(path: Path) -> Any:
    return json.loads(path.read_text())


def roc_date(value: str) -> str:
    text = value.replace('/', '').replace('-', '')
    return f'{int(text[:3])+1911:04d}-{text[3:5]}-{text[5:7]}'


def validate_frozen(root: Path, expected_snapshot_id: str) -> dict:
    """All incomplete/corrupt input paths fail in the same pre-model error class."""
    try:
        return _validate_frozen(root, expected_snapshot_id)
    except FrozenInputError:
        raise
    except (OSError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise FrozenInputError(f'MALFORMED_OR_MISSING_FROZEN_INPUT:{type(exc).__name__}:{exc}') from exc


def _validate_frozen(root: Path, expected_snapshot_id: str) -> dict:
    path = root/'snapshot_manifest_v4.json'
    if not path.is_file():
        raise FrozenInputError('MISSING_MANIFEST_OR_ROUND1_INCOMPLETE_INPUTS')
    if digest(path.read_bytes()) != expected_snapshot_id:
        raise FrozenInputError('WRONG_SNAPSHOT_HASH')
    manifest = load(path)
    if (not manifest.get('paper_only') or not isinstance(manifest.get('universe'), list)
            or len(manifest['universe']) != len(set(manifest['universe']))
            or len(manifest['universe']) < 1
            or not manifest.get('research_universe')
            or len(manifest['research_universe']) != len(set(manifest['research_universe']))
            or not set(manifest.get('research_universe', [])) <= set(manifest['universe'])):
        raise FrozenInputError('BAD_OR_EMPTY_UNIVERSE')
    try:
        cutoff = pd.Timestamp(manifest['cutoff'])
        if cutoff.tzinfo is None:
            raise ValueError('timezone')
    except (KeyError, ValueError, TypeError) as exc:
        raise FrozenInputError('MISSING_OR_BAD_ASOF') from exc
    if cutoff > pd.Timestamp.now(tz='UTC'):
        raise FrozenInputError('FUTURE_ASOF')
    entries = manifest.get('entries', [])
    by_name = {}
    for entry in entries:
        name = entry.get('snapshot', '')
        target = (root/name).resolve()
        if not target.is_relative_to(root.resolve()) or name in by_name:
            raise FrozenInputError('DUPLICATE_OR_ESCAPED_INPUT')
        by_name[name] = entry
        if entry.get('missing') or not target.is_file():
            raise FrozenInputError(f'MISSING_INPUT:{name}')
        if digest(target.read_bytes()) != entry.get('sha256'):
            raise FrozenInputError(f'INPUT_HASH_MISMATCH:{name}')
        try:
            observed = pd.Timestamp(entry['observed_at'])
            if observed.tzinfo is None or observed > cutoff:
                raise ValueError('after cutoff')
        except (KeyError, ValueError, TypeError) as exc:
            raise FrozenInputError(f'FUTURE_OR_MISSING_INPUT_ASOF:{name}') from exc
    required = ['swing_book.json', 'TW_account.json', 'US_account.json',
                'paper_params.json', 'paper_params_history.jsonl', 'events.db',
                'TW_journal.jsonl', 'US_journal.jsonl', 'US_lessons.jsonl', 'swing_decisions.jsonl']
    for name in required:
        if f'frozen_runtime/{name}' not in by_name:
            raise FrozenInputError(f'MISSING_REQUIRED_INPUT:{name}')
    primary_required = ['research/tw_official_close.json', 'research/tw_income.json',
                        'research/tw_revenue.json', 'research/tw_company.json', 'research/tw_valuation.json']
    primary_required += [f'research/{ticker}_{kind}.json'
                         for ticker in manifest['research_universe'] if not ticker.endswith('.TW')
                         for kind in ['facts', 'submissions']]
    for name in primary_required:
        if name not in by_name:
            raise FrozenInputError(f'MISSING_REQUIRED_PRIMARY_RESEARCH:{name}')
    if digest((root/'versions.json').read_bytes()) != manifest.get('versions_sha256'):
        raise FrozenInputError('WRONG_VERSION_HASH')
    versions = load(root/'versions.json')
    for source, expected_hash in versions['sources'].items():
        if digest((Path(__file__).resolve().parents[2]/source).read_bytes()) != expected_hash:
            raise FrozenInputError(f'FROZEN_RULE_SOURCE_CHANGED:{source}')
    book = load(root/'frozen_runtime/swing_book.json')
    for key in ('cash', 'pos', 'closed', 'bench0', 'last', 'params_ver', 'equity'):
        if key not in book:
            raise FrozenInputError(f'MISSING_BOOK_FIELD:{key}')
    day_events = [json.loads(line) for line in (root/'frozen_runtime/swing_decisions.jsonl').read_text().splitlines() if line.strip()]
    last_day = next((row for row in reversed(day_events) if row.get('act') == 'DAY'), None)
    if last_day is None or last_day.get('day') != book['last'] or set(last_day['open']) != set(book['pos']):
        raise FrozenInputError('FALSELY_EMPTY_OR_DETACHED_SWING_ACCOUNT')
    if not math.isfinite(book['cash']) or book['cash'] < 0:
        raise FrozenInputError('BAD_BOOK_CASH')
    for symbol, pos in book['pos'].items():
        if symbol not in manifest['universe']:
            raise FrozenInputError(f'HELD_SYMBOL_MISSING_FROM_UNIVERSE:{symbol}')
        for key in ('fam', 'e', 'sh', 'peak', 'stop', 'atr', 'entry_day'):
            if key not in pos:
                raise FrozenInputError(f'MISSING_POSITION_FIELD:{symbol}:{key}')
        if any(not math.isfinite(pos[k]) or pos[k] <= 0 for k in ('e', 'sh', 'peak', 'stop', 'atr')):
            raise FrozenInputError(f'BAD_POSITION:{symbol}')
        if pos['entry_day'] > book['last']:
            raise FrozenInputError('FUTURE_POSITION')
    # Calendar packages can lack emergency closures. Only an official complete
    # monthly exchange trade statement may resolve a missing session, not peers.
    official_months = {}
    for name in by_name:
        if name.startswith('research/exchange_month_'):
            statement = load(root/name)
            if statement.get('stat') != 'OK' or not statement.get('data'):
                raise FrozenInputError('BAD_OFFICIAL_SESSION_STATEMENT')
            dates = {roc_date(row[0]) for row in statement['data']}
            official_months[next(iter(dates))[:7]] = dates
    bars = {}
    diagnostics = {}
    for symbol in manifest['universe']:
        name = manifest.get('bar_sources', {}).get(symbol, f'bars/{symbol}.json')
        if name not in by_name:
            raise FrozenInputError(f'MISSING_BARS:{symbol}')
        try:
            chart = load(root/name)['chart']['result'][0]
            meta = chart['meta']
            currency = 'TWD' if symbol.endswith('.TW') else 'USD'
            zone = 'Asia/Taipei' if currency == 'TWD' else 'America/New_York'
            if meta['symbol'] != symbol or meta['currency'] != currency or meta['exchangeTimezoneName'] != zone:
                raise FrozenInputError(f'CURRENCY_EXCHANGE_MISMATCH:{symbol}')
            stamps = pd.to_datetime(chart['timestamp'], unit='s', utc=True)
            if stamps.has_duplicates or not stamps.is_monotonic_increasing:
                raise FrozenInputError(f'DUPLICATE_OR_UNSORTED_BARS:{symbol}')
            if any(stamps > cutoff):
                raise FrozenInputError(f'FUTURE_BARS:{symbol}')
            days = stamps.tz_convert(zone).tz_localize(None).normalize()
            quote = chart['indicators']['quote'][0]
            df = pd.DataFrame({k.title(): quote[k] for k in ('open','high','low','close','volume')}, index=days)
            if len(df) <= 200 or df.isna().any().any():
                raise FrozenInputError(f'INSUFFICIENT_OR_MISSING_BARS:{symbol}')
            if not all(math.isfinite(float(x)) for x in df.to_numpy().flat):
                raise FrozenInputError(f'NONFINITE_BARS:{symbol}')
            if ((df[['Open','High','Low','Close']] <= 0).any().any() or (df.Volume < 0).any()
                    or (df.Low > df[['Open','Close']].min(axis=1)).any()
                    or (df.High < df[['Open','Close']].max(axis=1)).any()):
                raise FrozenInputError(f'BAD_OHLCV:{symbol}')
            cal = xcals.get_calendar('XTAI' if currency == 'TWD' else 'XNYS')
            expected = cal.sessions_in_range(days[0], days[-1])
            missing = expected.difference(days)
            reconciled = []
            for missing_day in missing:
                text = missing_day.strftime('%Y-%m-%d')
                if currency != 'TWD' or text[:7] not in official_months or text in official_months[text[:7]]:
                    raise FrozenInputError(f'MISSING_EXCHANGE_DAY_BAR:{symbol}:{text}')
                reconciled.append(text)
            if len(days.difference(expected)):
                raise FrozenInputError(f'NONSESSION_BAR:{symbol}')
            recent = cal.sessions_in_range((cutoff-pd.Timedelta(days=12)).tz_localize(None).normalize(), cutoff.tz_localize(None).normalize())
            complete = [d for d in recent if cal.session_close(d) <= cutoff]
            if not complete or days[-1] != complete[-1]:
                raise FrozenInputError(f'NOT_LATEST_COMPLETED_SESSION:{symbol}')
            if cal.session_close(days[-1]) > cutoff:
                raise FrozenInputError(f'INCOMPLETE_SESSION:{symbol}')
            adj = chart['indicators'].get('adjclose', [{}])[0].get('adjclose')
            if adj is None or len(adj) != len(df) or any(x is None or not math.isfinite(x) or x <= 0 for x in adj):
                raise FrozenInputError(f'MISSING_ADJUSTMENT_BASIS:{symbol}')
            factor = pd.Series(adj, index=days)/df.Close
            if (factor <= 0).any():
                raise FrozenInputError('BAD_ADJUSTMENT_FACTOR')
            # Matches real yfinance auto_adjust=True. Yahoo OHLC is split-adjusted;
            # adjclose adds dividends. Never multiply holdings by a split twice.
            adjusted = df.copy()
            adjusted[['Open','High','Low','Close']] = df[['Open','High','Low','Close']].mul(factor, axis=0)
            splits = chart.get('events', {}).get('splits', {})
            for split in splits.values():
                split_at = pd.Timestamp(split['date'], unit='s', tz='UTC')
                split_day = pd.Timestamp(str(split_at.tz_convert(zone).date()))
                if (split_at > cutoff or split_day not in days
                        or not math.isfinite(float(split['numerator']))
                        or not math.isfinite(float(split['denominator']))
                        or float(split['numerator']) <= 0 or float(split['denominator']) <= 0):
                    raise FrozenInputError(f'INVALID_OR_FUTURE_SPLIT:{symbol}')
            if symbol in book['pos']:
                pos = book['pos'][symbol]
                if pd.Timestamp(pos['entry_day']) not in days:
                    raise FrozenInputError(f'ENTRY_BAR_MISSING:{symbol}')
                for split in splits.values():
                    event_day = pd.Timestamp(split['date'], unit='s', tz='UTC').tz_convert(zone).date().isoformat()
                    if event_day > pos['entry_day']:
                        raise FrozenInputError(f'UNRECONCILED_POSITION_SPLIT:{symbol}')
            bars[symbol] = adjusted
            diagnostics[symbol] = {'count': len(df), 'first_day': str(days[0].date()), 'latest_day': str(days[-1].date()),
                'available_at': cal.session_close(days[-1]).isoformat(), 'currency': currency,
                'calendar_reconciled_closures': reconciled, 'split_events': splits,
                'adjustment': 'Yahoo split-adjusted OHLC multiplied by adjclose/close, same as yfinance auto_adjust=True',
                'last_raw_close': float(df.Close.iloc[-1]), 'last_adjusted_close': float(adjusted.Close.iloc[-1])}
        except FrozenInputError:
            raise
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise FrozenInputError(f'MALFORMED_BARS:{symbol}:{exc}') from exc
    # Independent official cross-check of last TW daily prices (native TWD).
    official = {r['Code']+'.TW': r for r in load(root/'research/tw_official_close.json')}
    for symbol in (s for s in bars if s.endswith('.TW')):
        row = official[symbol]
        if roc_date(row['Date']) != diagnostics[symbol]['latest_day'] or abs(float(row['ClosingPrice'].replace(',',''))-diagnostics[symbol]['last_raw_close']) > .011:
            raise FrozenInputError(f'OFFICIAL_PRICE_DATE_MISMATCH:{symbol}')
    if book['last'] != max(d['latest_day'] for d in diagnostics.values()):
        raise FrozenInputError('BOOK_BAR_DAY_MISMATCH')
    accounts = {market: load(root/f'frozen_runtime/{market}_account.json') for market in ('TW','US')}
    held_symbols = set(book['pos']) | {symbol for account in accounts.values()
                                       for symbol, quantity in account['positions'].items() if quantity > 0}
    if not held_symbols <= set(manifest['research_universe']):
        raise FrozenInputError('HELD_SYMBOL_MISSING_OFFICIAL_RESEARCH')
    for market, account in accounts.items():
        if not {'cash','positions','marks'} <= account.keys() or not math.isfinite(account['cash']):
            raise FrozenInputError(f'MISSING_ACCOUNT_STATE:{market}')
        # Anchor even a self-consistently rehashed false empty-account mutation
        # to the independently captured starting runtime digest.
        start = load(root/'starting_state.json')['runtime_hashes']
        name = f'data/paper_runtime/{market}_account.json'
        if digest((root/f'frozen_runtime/{market}_account.json').read_bytes()) != start[name]:
            raise FrozenInputError(f'FALSELY_EMPTY_OR_CHANGED_INITIAL_ACCOUNT:{market}')
        for symbol, quantity in account['positions'].items():
            if symbol not in bars or not math.isfinite(quantity) or quantity < 0:
                raise FrozenInputError(f'BAD_OR_UNCOVERED_ACCOUNT_POSITION:{market}:{symbol}')
            if diagnostics[symbol]['currency'] != ('TWD' if market == 'TW' else 'USD'):
                raise FrozenInputError(f'ACCOUNT_CURRENCY_MISMATCH:{market}:{symbol}')
    return {'snapshot_id': expected_snapshot_id, 'manifest': manifest, 'bars': bars,
            'book': book, 'accounts': accounts, 'diagnostics': diagnostics, 'cutoff': cutoff.isoformat()}


def validate_main_rows(packet, frozen: dict) -> list[dict]:
    rows = packet.conditions.get('per_ticker_decisions')
    universe = frozen['manifest']['research_universe']
    if not isinstance(rows, list) or len(rows) != len(universe) or {r.get('ticker') for r in rows} != set(universe):
        raise ValueError('MAIN_MISSING_OR_DUPLICATE_TICKERS')
    for row in rows:
        required = {'ticker','action','quantity','rationale','invalidation','evidence_status','review_at','holding_horizon'}
        if not required <= row.keys() or not row['rationale'] or not row['invalidation']:
            raise ValueError('MAIN_MISSING_REQUIRED_FIELDS')
        if row['action'] not in {'BUY','SELL','HOLD','REJECT','NO_TRADE','EVIDENCE_INSUFFICIENT'}:
            raise ValueError('MAIN_INVALID_ACTION')
        if not isinstance(row['quantity'], (int,float)) or not math.isfinite(row['quantity']) or row['quantity'] < 0:
            raise ValueError('MAIN_INVALID_QUANTITY')
        if row['action'] in {'HOLD','REJECT','NO_TRADE','EVIDENCE_INSUFFICIENT'} and row['quantity'] != 0:
            raise ValueError('MAIN_NONTRADE_QUANTITY')
        if row['action'] == 'NO_TRADE' and row['evidence_status'] != 'SUFFICIENT':
            raise ValueError('MAIN_INSUFFICIENCY_IS_NOT_NO_TRADE')
        if row['evidence_status'] not in {'SUFFICIENT','INSUFFICIENT'} or row['holding_horizon'] not in {'intraday','swing','long_term'}:
            raise ValueError('MAIN_BAD_EVIDENCE_OR_HORIZON')
        if row['action'] in {'BUY','SELL'} and (row['quantity'] <= 0 or row['evidence_status'] != 'SUFFICIENT'):
            raise ValueError('MAIN_UNSUPPORTED_OR_EMPTY_TRADE')
        review = pd.Timestamp(row['review_at'])
        if review.tzinfo is None or review <= pd.Timestamp(frozen['cutoff']):
            raise ValueError('MAIN_BAD_REVIEW_DATE')
    if packet.predecision_snapshot.get('snapshot_id') != frozen['snapshot_id']:
        raise ValueError('MAIN_WRONG_SNAPSHOT')
    return rows


def position_review(*, quantity: float, current_price: float, stop_price: float,
                    thesis_broken: bool, review_at: datetime | None, now: datetime) -> dict:
    """No order clock input by construction. Review due is not a sell signal.

    Stop and thesis inputs come from the owning strategy/Main, not this helper.
    Missing historical review metadata stays unknown; it is never backfilled.
    """
    if quantity <= 0:
        return {'action': 'INAPPLICABLE', 'reason': 'NO_POSITION', 'review_state': 'INAPPLICABLE'}
    review_state = 'UNKNOWN_LEGACY_METADATA' if review_at is None else 'DUE' if now >= review_at else 'SCHEDULED'
    if thesis_broken:
        return {'action': 'EXIT', 'reason': 'THESIS_BROKEN', 'review_state': review_state}
    if current_price <= stop_price:
        return {'action': 'EXIT', 'reason': 'CONFIGURED_POSITION_STOP', 'review_state': review_state}
    return {'action': 'HOLD', 'reason': 'POSITION_THESIS_AND_STOP_INTACT', 'review_state': review_state}
