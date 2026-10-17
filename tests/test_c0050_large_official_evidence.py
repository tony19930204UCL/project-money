"""Large-source admission uses scripts only; all evidence here is synthetic."""
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cio_market_lab.engine.paper_research_trigger import (
    EVIDENCE_SUMMARY_BYTE_BUDGET, LARGE_MAX_NODES, LARGE_RAW_BYTE_BUDGET,
    PROMPT_BYTE_BUDGET, TriggerBudgetError, admit_script_request, bounded_semantic,
    semantic_evidence_identity,
)


def evidence():
    return {
        'symbol': '2330.TW', 'market': 'TW',
        'source_url': 'https://example.invalid/synthetic-only',
        'published_at': '2026-10-16',
        'verified_facts': ['synthetic fact ' + str(i) + ' 台' * 500 for i in range(40)],
        'raw_metadata': {'raw_row': {'revenue': 123},
                         'document': 'synthetic raw document ' + 'x' * 400_000},
    }


def admit(root, source):
    return admit_script_request(root, symbol='2330.TW', evidence=source,
                                quote=None, lessons=[], outcomes=[], ceiling=0.02)


def test_leaf_summary_retains_operating_fields_and_hashes_all_omissions(tmp_path):
    source = evidence()
    original = copy.deepcopy(source)
    summary, digest = semantic_evidence_identity(source)
    assert source == original
    assert summary['summary_policy'] == 'bounded-official-leaves-v1'
    assert summary['semantic_sha256'] == digest
    assert summary['lossy'] and summary['omitted_leaves'] > 0
    assert {'path': ['raw_metadata', 'raw_row', 'revenue'], 'value': 123} in summary['retained_leaves']
    assert len(json.dumps(summary, ensure_ascii=False).encode()) <= EVIDENCE_SUMMARY_BYTE_BUDGET
    first = admit(tmp_path, source)
    assert first['status'] == 'SCRIPT_RESEARCH_REVIEW_REQUIRED'
    assert Path(first['request_path']).stat().st_size <= PROMPT_BYTE_BUDGET
    # Change content far beyond the retained preview, not a clock or prefix.
    source['raw_metadata']['document'] += 'material tail revision'
    assert semantic_evidence_identity(source)[1] != digest
    source = original
    source['verified_facts'].reverse()
    source['verified_facts'].append(source['verified_facts'][0])
    source['raw_metadata']['observed_at'] = 'new clock'
    assert admit(tmp_path, source)['status'] == 'DUPLICATE_SUBSTANTIVE_EVENT'


def test_large_single_fact_repetition_does_not_change_summary_mode():
    source = evidence()
    source['verified_facts'] = ['x' * 80_000]
    digest = semantic_evidence_identity(source)[1]
    source['verified_facts'] *= 2
    assert semantic_evidence_identity(source)[1] == digest


def test_preexisting_event_identity_survives_new_summary_policy(tmp_path):
    source = evidence()
    source['raw_metadata'].pop('document')
    source['verified_facts'] = ['x' * 30_000]
    legacy = bounded_semantic({k: source.get(k) for k in (
        'symbol', 'market', 'source_url', 'published_at', 'title',
        'verified_facts', 'raw_metadata')})
    legacy_id = hashlib.sha256(json.dumps(legacy, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    directory = tmp_path / 'paper_trigger_events'
    directory.mkdir()
    (directory / (legacy_id + '.json')).write_text('{"existing_claim": true}')
    result = admit(tmp_path, source)
    assert result['event_id'] == legacy_id
    assert result['status'] == 'DUPLICATE_SUBSTANTIVE_EVENT'


@pytest.mark.parametrize('kind', ['bytes', 'nodes', 'depth', 'cycle', 'nan'])
def test_hard_limits_still_fail_without_claiming(tmp_path, kind):
    source = evidence()
    if kind == 'bytes':
        source['raw_metadata']['document'] = 'x' * (LARGE_RAW_BYTE_BUDGET + 1)
    elif kind == 'nodes':
        source['raw_metadata']['too_wide'] = list(range(LARGE_MAX_NODES + 1))
    elif kind == 'depth':
        nested = {}
        for _ in range(20):
            nested = {'next': nested}
        source['raw_metadata']['deep'] = nested
    elif kind == 'cycle':
        source['raw_metadata']['cycle'] = source
    else:
        source['raw_metadata']['bad'] = float('nan')
    with pytest.raises(TriggerBudgetError):
        admit(tmp_path, source)
    assert not (tmp_path / 'paper_trigger_events').exists()


def test_large_concurrent_claim_is_once(tmp_path):
    source = evidence()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: admit(tmp_path, source), range(4)))
    assert sum(r['status'] == 'SCRIPT_RESEARCH_REVIEW_REQUIRED' for r in results) == 1
    assert sum(r['status'] == 'DUPLICATE_SUBSTANTIVE_EVENT' for r in results) == 3
    assert all(r['paid_calls'] == 0 and r['model_called'] is False for r in results)
    assert len(list((tmp_path / 'paper_trigger_events').glob('*.json'))) == 1
