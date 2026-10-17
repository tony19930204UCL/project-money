"""Script-only admission for PAPER research; never authenticates a CIO decision.

No recursive history serialization and no model transport. An event is claimed
before returning its request, including across restarts/concurrent producers.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

RAW_BYTE_BUDGET = 128_000
PROMPT_BYTE_BUDGET = 24_000
MAX_NODES = 4096
MAX_DEPTH = 12
MAX_TEXT = 2000

# Acquisition clocks and byte-capture identities are provenance, not new facts.
VOLATILE = frozenset({
    'observed_at', 'timestamp', 'acquired_at', 'fetched_at', 'retrieved_at',
    'refresh_at', 'capture_path', 'capture_file', 'sha256_of_wire_bytes',
    'document_sha256', 'wire_sha256', 'excerpt', 'raw_excerpt',
})


class TriggerBudgetError(ValueError):
    pass


def bounded_semantic(value: Any) -> Any:
    """Iterative copy/canonicalization; reject oversized/deep/cyclic input.

    Only JSON-shaped source evidence belongs here, never full learning records.
    Fact/source sets ignore ordering/repetition; ordinary arrays retain order.
    """
    stack = [(value, 0, False, False)]
    results = []
    active = set()
    nodes = size = 0
    while stack:
        item, depth, finish, unordered = stack.pop()
        if finish:
            kind, keys, identity = item
            count = len(keys)
            children = results[-count:] if count else []
            if count:
                del results[-count:]
            if kind == 'dict':
                result = dict(zip(keys, children))
            elif kind == 'set':
                unique = {json.dumps(x, sort_keys=True, ensure_ascii=False): x for x in children}
                result = [unique[k] for k in sorted(unique)]
            else:
                result = children
            active.remove(identity)
            results.append(result)
            continue
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise TriggerBudgetError('PAPER_TRIGGER_STRUCTURE_BUDGET')
        if isinstance(item, (dict, list, tuple)):
            if id(item) in active:
                raise TriggerBudgetError('PAPER_TRIGGER_CYCLIC_INPUT')
            active.add(id(item))
            if len(item) > MAX_NODES:
                raise TriggerBudgetError('PAPER_TRIGGER_STRUCTURE_BUDGET')
            if isinstance(item, dict):
                if any(not isinstance(k, str) for k in item):
                    raise TriggerBudgetError('PAPER_TRIGGER_NON_JSON_KEY')
                keys = sorted(k for k in item if k not in VOLATILE)
                size += sum(len(k.encode('utf-8')) for k in keys)
                children = [item[k] for k in keys]
                kind = 'dict'
            else:
                keys = list(range(len(item)))
                children = item
                kind = 'set' if unordered else 'list'
            stack.append(((kind, keys, id(item)), depth, True, False))
            stack.extend((child, depth + 1, False,
                          kind == 'dict' and key in {'verified_facts', 'supplemental_source_rows'})
                         for key, child in reversed(list(zip(keys, children))))
        elif item is None or isinstance(item, (str, int, float, bool)):
            try:
                size += len(json.dumps(item, ensure_ascii=False, allow_nan=False).encode('utf-8'))
            except (ValueError, OverflowError) as exc:
                raise TriggerBudgetError('PAPER_TRIGGER_NON_FINITE') from exc
            results.append(item)
        else:
            raise TriggerBudgetError('PAPER_TRIGGER_NON_JSON_VALUE')
        if size > RAW_BYTE_BUDGET:
            raise TriggerBudgetError('PAPER_TRIGGER_RAW_BYTE_BUDGET')
    return results[0]


def compact_learning(rows: Any, *, outcome: bool = False) -> list[dict]:
    """Flat scalar projection only. Never copy packets, conditions or snapshots."""
    if not isinstance(rows, (list, tuple)):
        return []
    fields = ('case_id', 'symbol', 'action', 'thesis', 'realized_pnl') if outcome else (
        'lesson_id', 'case_id', 'symbol', 'lesson', 'summary', 'takeaway',
        'strategy_version', 'validation_status', 'next_time_rule')
    result = []
    for row in rows[:5]:
        if not isinstance(row, dict):
            continue
        action = row.get('action')
        action = action.upper() if isinstance(action, str) else ''
        details = row.get('outcome') if outcome else row.get('outcome_details')
        if outcome:
            # Real store outcomes are {case_id, thesis, outcome:{...}, fill:{...}}
            # and have no top-level action. Keep evaluated non-actions too.
            matured = isinstance(details, dict) and isinstance(details.get('realized_pnl'), (int, float))
            if not matured and (action in {'', 'WAIT', 'HOLD', 'NO_TRADE', 'REJECT'}):
                continue
        flat = {k: (v[:MAX_TEXT] if isinstance(v, str) else v)
                for k in fields if isinstance((v := row.get(k)), (str, bool, int, float))}
        # Fixed one-level scalar paths only; never traverse embedded packet /
        # predecision_snapshot / recursive thesis history in a learning record.
        for name, source, keys in (
            ('outcome', details, ('realized_pnl', 'no_trade_pnl', 'no_fill', 'as_of', 'closed_at')),
            ('decision_delta', row.get('decision_delta'), ('before', 'after', 'reason', 'summary')),
        ):
            if isinstance(source, dict):
                for key in keys:
                    value = source.get(key)
                    if isinstance(value, (str, bool, int, float)):
                        flat[name + '.' + key] = value[:MAX_TEXT] if isinstance(value, str) else value
        if flat:
            result.append(flat)
    return result


def admit_script_request(root: Path, *, symbol: str, evidence: dict,
                         quote: dict | None, lessons: Any, outcomes: Any,
                         ceiling: float) -> dict:
    """Durable at-most-once substantive source event; default cost is zero.

    Quote clocks, price drift, lessons and self-generated WAIT are context only,
    not admission signals. A genuinely changed official fact creates a new event.
    This is research_triage, never a substitute for independent Main approval.
    """
    semantic = bounded_semantic({k: evidence.get(k) for k in (
        'symbol', 'market', 'source_url', 'published_at', 'title',
        'verified_facts', 'raw_metadata')})
    event_id = hashlib.sha256(json.dumps(semantic, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    summary = {
        'purpose': 'PAPER_RESEARCH_TRIAGE_NOT_CIO_APPROVAL_NOT_ORDER',
        'event_id': event_id, 'symbol': symbol, 'official_evidence': semantic,
        'reference_quote_for_valuation_only': {
            k: quote[k] for k in ('symbol', 'price', 'close', 'last_price', 'source', 'source_date', 'date')
            if quote and k in quote and isinstance(quote[k], (str, int, float, bool))
        },
        'position_ceiling_fraction': ceiling,
        'prior_lessons': compact_learning(lessons),
        'matured_past_outcomes': compact_learning(outcomes, outcome=True),
        'route': 'script', 'model_called': False, 'paid_calls': 0,
    }
    try:
        encoded = json.dumps(summary, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, OverflowError) as exc:
        raise TriggerBudgetError('PAPER_TRIGGER_NON_FINITE') from exc
    if len(encoded) > PROMPT_BYTE_BUDGET:
        raise TriggerBudgetError('PAPER_TRIGGER_PROMPT_BYTE_BUDGET')
    directory = Path(root) / 'paper_trigger_events'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (event_id + '.json')
    # O_EXCL arbitrates concurrent processes. A crash leaves a claimed event,
    # never an automatic retry that can charge again on identical evidence.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return {'status': 'DUPLICATE_SUBSTANTIVE_EVENT', 'symbol': symbol,
                'event_id': event_id, 'model_called': False, 'paid_calls': 0,
                'request_path': str(path)}
    with os.fdopen(fd, 'wb') as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    return {'status': 'SCRIPT_RESEARCH_REVIEW_REQUIRED', 'symbol': symbol,
            'event_id': event_id, 'model_called': False, 'paid_calls': 0,
            'prompt_bytes': len(encoded), 'request_path': str(path)}
