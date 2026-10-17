"""C-0050 exercises the producer, not only a detached admission helper.

Transport sentinels are negative controls, not fabricated model responses.
All writes are isolated; synthetic facts are explicitly test data.
"""
from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from cio_market_lab.engine.daily_research_plan import DailyResearchPlanProducer
from cio_market_lab.engine.paper_research_trigger import (
    PROMPT_BYTE_BUDGET, TriggerBudgetError, bounded_semantic, compact_learning,
)

NOW = datetime(2026, 10, 17, 8, tzinfo=timezone.utc)


@pytest.fixture
def producer_case(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('NO model, network or subprocess transport permitted')
    monkeypatch.setattr('cio_market_lab.integrations.hermes_chat.run_hermes_cli_chat', forbidden)
    monkeypatch.setattr('requests.sessions.Session.request', forbidden)
    monkeypatch.setattr('subprocess.Popen', forbidden)
    monkeypatch.setenv('CIO_PROVIDER_ID', 'openai-codex')
    monkeypatch.setenv('CIO_MODEL_ID', 'gpt-6.1-sol')
    evidence = {
        'symbol': '2330.TW', 'market': 'TW', 'source_url': 'https://example.invalid/test-only',
        'published_at': '2026-10-10T08:00:00Z', 'observed_at': NOW.isoformat(),
        'title': 'SYNTHETIC test baseline',
        'verified_facts': ['SYNTHETIC test revenue 123', 'SYNTHETIC test EPS 4'],
        'raw_metadata': {'raw_row': {'revenue': 123}, 'observed_at': NOW.isoformat()},
    }
    state = {'evidence': evidence, 'lessons': [], 'outcomes': [],
             'quote': {'symbol': '2330.TW', 'price': 100, 'timestamp': NOW.isoformat()}}
    learning = SimpleNamespace(
        retrieve_context_lessons=lambda **kwargs: state['lessons'],
        retrieve_past_outcomes=lambda **kwargs: state['outcomes'],
    )

    def make():
        producer = DailyResearchPlanProducer(
            root=tmp_path / 'producer', packet_root=tmp_path / 'packets',
            session_id='c0050-test', workspace_root=str(tmp_path),
            learning_store=learning, now_fn=lambda: NOW,
        )
        def acquire(symbols, collector, now):
            collector.rows.append(copy.deepcopy(state['evidence']))
            return {'source': 'SYNTHETIC_TEST_ONLY'}
        producer.official.acquire = acquire
        return producer

    def refresh(producer=None, now=NOW):
        return (producer or make()).refresh('2330.TW', now=now, reference_quote=state['quote'])
    return state, make, refresh, tmp_path


def test_material_event_once_across_restart_and_next_day(producer_case):
    state, make, refresh, root = producer_case
    first = refresh()
    assert first['status'] == 'SCRIPT_RESEARCH_REVIEW_REQUIRED'
    assert first['model_called'] is False and first['paid_calls'] == 0
    assert refresh()['status'] == 'DUPLICATE_SUBSTANTIVE_EVENT'
    state['quote']['timestamp'] = (NOW + timedelta(minutes=1)).isoformat()
    state['evidence']['observed_at'] = state['quote']['timestamp']
    state['evidence']['raw_metadata']['observed_at'] = state['quote']['timestamp']
    assert refresh()['status'] == 'DUPLICATE_SUBSTANTIVE_EVENT'
    state['outcomes'].append({'case_id': 'SELF_WAIT', 'action': 'WAIT', 'thesis': 'unchanged'})
    assert refresh()['status'] == 'DUPLICATE_SUBSTANTIVE_EVENT'
    assert refresh(now=NOW + timedelta(days=1))['status'] == 'DUPLICATE_SUBSTANTIVE_EVENT'
    state['evidence']['verified_facts'].append('SYNTHETIC NEW MATERIAL TEST FACT')
    new = refresh()
    assert new['status'] == 'SCRIPT_RESEARCH_REVIEW_REQUIRED'
    assert new['event_id'] != first['event_id']
    assert refresh()['status'] == 'DUPLICATE_SUBSTANTIVE_EVENT'
    assert len(list((root / 'producer/paper_trigger_events').glob('*.json'))) == 2
    assert not (root / 'producer/trusted_main_cio_approvals.json').exists()
    assert list((root / 'packets').glob('*')) == []


def test_duplicate_reordered_facts_wire_hash_and_price_drift_not_events(producer_case):
    state, _, refresh, _ = producer_case
    first = refresh()
    state['evidence']['verified_facts'].reverse()
    state['evidence']['verified_facts'].append(state['evidence']['verified_facts'][0])
    state['evidence']['raw_metadata']['document_sha256'] = 'new-wire-clock-only'
    state['quote']['price'] = 101
    state['lessons'].append({'lesson_id': 'NEW_SELF_LESSON', 'lesson': 'same official facts'})
    assert refresh()['event_id'] == first['event_id']
    assert refresh()['status'] == 'DUPLICATE_SUBSTANTIVE_EVENT'


@pytest.mark.parametrize('field', ['verified_facts', 'raw_metadata'])
def test_changed_source_fact_and_period_are_material(producer_case, field):
    state, _, refresh, _ = producer_case
    first = refresh()
    if field == 'verified_facts':
        state['evidence'][field][0] = 'SYNTHETIC revenue revised to 124'
    else:
        state['evidence'][field]['raw_row']['revenue'] = 124
    second = refresh()
    assert second['status'] == 'SCRIPT_RESEARCH_REVIEW_REQUIRED'
    assert second['event_id'] != first['event_id']


def test_recursive_learning_is_flattened_without_traversal(producer_case):
    state, _, refresh, _ = producer_case
    row = {'lesson_id': 'L-1', 'lesson': 'x' * 200_000}
    row['predecision_snapshot'] = row
    state['lessons'] = [row] * 200
    state['outcomes'] = [{'action': 'WAIT', 'packet': row}] * 200
    result = refresh()
    saved = json.loads(Path(result['request_path']).read_text())
    assert saved['prior_lessons'][0]['lesson_id'] == 'L-1'
    assert len(saved['prior_lessons'][0]['lesson']) == 2000
    assert 'predecision_snapshot' not in saved['prior_lessons'][0]
    assert saved['matured_past_outcomes'] == []
    assert Path(result['request_path']).stat().st_size <= PROMPT_BYTE_BUDGET


def test_matured_learning_context_never_changes_event_identity(producer_case):
    state, _, refresh, _ = producer_case
    first = refresh()
    state['outcomes'] = [{'case_id': 'DONE-1', 'action': 'BUY', 'realized_pnl': 5,
                          'packet': {'predecision_snapshot': {'nested': 'not copied'}}}]
    assert refresh()['status'] == 'DUPLICATE_SUBSTANTIVE_EVENT'
    state['evidence']['verified_facts'].append('SYNTHETIC new fact')
    result = refresh()
    saved = json.loads(Path(result['request_path']).read_text())
    assert saved['matured_past_outcomes'] == [{'case_id': 'DONE-1', 'action': 'BUY', 'realized_pnl': 5}]
    assert result['event_id'] != first['event_id']


@pytest.mark.parametrize('size', [200_000, 30_000])
def test_oversize_blocks_before_admission_or_transport(producer_case, size):
    state, _, refresh, root = producer_case
    state['evidence']['verified_facts'].append('x' * size)
    with pytest.raises(TriggerBudgetError, match='BUDGET'):
        refresh()
    assert list((root / 'producer').glob('paper_trigger_events/*.json')) == []
    state['evidence']['verified_facts'].pop()
    assert refresh()['status'] == 'SCRIPT_RESEARCH_REVIEW_REQUIRED'


def test_utf8_prompt_budget_not_character_count(producer_case):
    state, _, refresh, _ = producer_case
    state['evidence']['verified_facts'].append('台' * 9000)
    with pytest.raises(TriggerBudgetError, match='PROMPT_BYTE_BUDGET'):
        refresh()


def test_concurrent_admission_is_at_most_once(producer_case):
    _, make, refresh, root = producer_case
    # Separate objects, common persistence: exercises the process-safe O_EXCL claim.
    producers = [make() for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(refresh, producers))
    assert sum(r['status'] == 'SCRIPT_RESEARCH_REVIEW_REQUIRED' for r in results) == 1
    assert sum(r['status'] == 'DUPLICATE_SUBSTANTIVE_EVENT' for r in results) == 7
    saved = list((root / 'producer/paper_trigger_events').glob('*.json'))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text())['model_called'] is False


@pytest.mark.parametrize('kind', ['deep', 'wide', 'cycle', 'nan', 'non_json'])
def test_malformed_evidence_fails_closed(kind):
    value = {}
    if kind == 'deep':
        for _ in range(20):
            value = {'x': value}
    elif kind == 'wide':
        value = list(range(5000))
    elif kind == 'cycle':
        value['x'] = value
    elif kind == 'nan':
        value['x'] = float('nan')
    else:
        value['x'] = object()
    with pytest.raises(TriggerBudgetError):
        bounded_semantic(value)


def test_shallow_outcome_projection_and_lesson_ids():
    rows = [{'lesson_id': 'L-real', 'summary': 'bounded', 'packet': {'huge': []}}]
    assert compact_learning(rows) == [{'lesson_id': 'L-real', 'summary': 'bounded'}]


def test_duplicate_is_admitted_before_reading_self_generated_learning(producer_case):
    state, make, refresh, _ = producer_case
    first = refresh()
    producer = make()

    def forbidden(**kwargs):
        pytest.fail('duplicate evidence must not load recursive learning history')

    producer.learning_store.retrieve_context_lessons = forbidden
    producer.learning_store.retrieve_past_outcomes = forbidden
    state['outcomes'].append({'action': 'WAIT', 'thesis': 'self-added'})
    result = refresh(producer)
    assert result['status'] == 'DUPLICATE_SUBSTANTIVE_EVENT'
    assert result['event_id'] == first['event_id']


def test_cache_digest_uses_same_bounded_nonrecursive_identity(producer_case):
    from cio_market_lab.engine.daily_research_plan import semantic_research_digest

    state, _, refresh, _ = producer_case
    before = semantic_research_digest(state['evidence'])
    first = refresh()
    assert before == first['event_id']
    state['evidence']['verified_facts'].reverse()
    state['evidence']['raw_metadata']['wire_sha256'] = 'refresh-only'
    assert semantic_research_digest(state['evidence']) == before
    state['evidence']['raw_metadata']['recursive'] = state['evidence']
    with pytest.raises(TriggerBudgetError, match='CYCLIC'):
        semantic_research_digest(state['evidence'])


def test_learning_failure_does_not_claim_material_event(producer_case):
    _, make, refresh, root = producer_case
    producer = make()

    def unavailable(**kwargs):
        raise RuntimeError('test-only learning unavailable')

    producer.learning_store.retrieve_context_lessons = unavailable
    with pytest.raises(RuntimeError, match='learning unavailable'):
        refresh(producer)
    assert list((root / 'producer').glob('paper_trigger_events/*.json')) == []


def test_prompt_budget_failure_does_not_claim_event(producer_case):
    state, _, refresh, root = producer_case
    state['lessons'] = [{'lesson_id': 'L', 'lesson': '台' * 2000,
                         'summary': '台' * 2000}] * 5
    with pytest.raises(TriggerBudgetError, match='PROMPT_BYTE_BUDGET'):
        refresh()
    assert list((root / 'producer').glob('paper_trigger_events/*.json')) == []
    state['lessons'] = []
    assert refresh()['status'] == 'SCRIPT_RESEARCH_REVIEW_REQUIRED'
