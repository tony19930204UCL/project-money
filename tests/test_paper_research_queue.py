import json
import os
import subprocess
import sys
from types import SimpleNamespace
import numpy as np
import pytest
from cio_market_lab.engine import paper_research_queue as q, paper_dual_speed as ds, paper_turnover_experiment as e


def item(tmp_path, **changes):
    row = {'id': 'fixture', 'kind': q.KIND, 'authorized': True, 'paper_only': True,
           'authorization_basis': 'TEST_FIXTURE_ONLY', 'timeout_seconds': 1200,
           'state': 'PENDING', 'output': str(tmp_path / 'out')}
    row.update(changes); return row


def put(tmp_path, rows): ds.dump(tmp_path / 'queue.json', {'items': rows})


def test_empty_queue_never_invents_work(tmp_path):
    assert q.consume(tmp_path)['status'] == 'NO_AUTHORIZED_PENDING_EXPERIMENT'


def test_unauthorized_never_starts(tmp_path, monkeypatch):
    put(tmp_path, [item(tmp_path, authorized=False)])
    monkeypatch.setattr(q.subprocess, 'Popen', lambda *a, **k: pytest.fail('unauthorized spawn'))
    assert q.consume(tmp_path)['status'] == 'NO_AUTHORIZED_PENDING_EXPERIMENT'


def test_active_identity_no_duplicate(tmp_path, monkeypatch):
    put(tmp_path, [item(tmp_path, state='RUNNING', handle=q.process_identity(os.getpid())), item(tmp_path, id='next')])
    monkeypatch.setattr(q.subprocess, 'Popen', lambda *a, **k: pytest.fail('duplicate spawn'))
    assert q.consume(tmp_path)['status'] == 'RESEARCH_RUNNING'


def test_dead_pid_is_failure_not_retry(tmp_path):
    put(tmp_path, [item(tmp_path, state='RUNNING', handle={'pid': 999999999, 'start_ticks': 1})])
    assert q.consume(tmp_path)['status'] == 'NO_AUTHORIZED_PENDING_EXPERIMENT'
    assert q.load(tmp_path)['items'][0]['state'] == 'FAILED_EXECUTION_UNKNOWN'


def test_reused_pid_not_alive():
    assert not q.alive({'pid': os.getpid(), 'start_ticks': 0})


def test_consumer_claims_one_and_second_tick_no_duplicate(tmp_path, monkeypatch):
    put(tmp_path, [item(tmp_path), item(tmp_path, id='second')]); calls = []
    def popen(*a, **k): calls.append(a); return SimpleNamespace(pid=os.getpid())
    monkeypatch.setattr(q.subprocess, 'Popen', popen)
    assert q.consume(tmp_path)['status'] == 'DISPATCHED'
    assert q.consume(tmp_path)['status'] == 'RESEARCH_RUNNING'
    assert len(calls) == 1 and q.load(tmp_path)['items'][1]['state'] == 'PENDING'


def test_worker_failure_keeps_exit_evidence(tmp_path):
    put(tmp_path, [item(tmp_path)])
    # Real child interpreter, missing request is intentionally the negative fixture.
    assert q.run_item(tmp_path, 'fixture') != 0
    row = q.load(tmp_path)['items'][0]
    assert row['state'] == 'FAILED' and row['exit_code'] != 0
    assert (tmp_path / 'out/exit.txt').exists()


def test_symmetric_matching_does_not_fake_leverage():
    a = {'returns': [.04, -.02, .06, -.03]}; b = {'returns': [.004, -.002, .006, -.003]}
    delta, receipt = e.matched(a, b)
    assert receipt['strategy_cash_scale'] == pytest.approx(.1)
    assert receipt['benchmark_cash_scale'] == 1.
    assert np.max(np.abs(delta)) < 1e-12


def test_bootstrap_null_has_zero_ci():
    result = e.bootstrap(np.zeros(50), draws=20)
    assert result['daily_95pct_CI'] == [0., 0.]
    assert result['not_confirmatory_p_value'] is True


def test_queue_error_propagates_scheduled_failure():
    from cio_market_lab.engine.paper_tick import daily_result
    child = SimpleNamespace(returncode=0, stdout=json.dumps({'daily': {}, 'research_queue': {'status': 'ERROR'}}))
    assert daily_result(child) == (False, 'RESEARCH_QUEUE_ERROR_STATUS')
