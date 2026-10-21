"""Bounded client-authorized PAPER experiments; existing ticks are the consumer.
No models, brokerage, arbitrary commands, auto-retry or self-generated research.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from . import paper_dual_speed as ds

ROOT = ds.ROOT / 'research_queue'
KIND = 'TW_TURNOVER_ABLATION_V1'


def stamp():
    return datetime.now(timezone.utc).isoformat()


def process_identity(pid):
    try:
        text = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': int(pid), 'start_ticks': int(text[19]), 'state': text[0]}
    except (OSError, ValueError, IndexError):
        return None


def alive(handle):
    now = process_identity(handle.get('pid', 0))
    return bool(now and now['state'] not in ('Z', 'X') and
                now['start_ticks'] == handle.get('start_ticks'))


@contextmanager
def locked(root):
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    with (root / 'queue.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield root


def load(root):
    p = root / 'queue.json'
    return json.loads(p.read_text()) if p.exists() else {'version': 1, 'items': []}


def allowed(item):
    return (item.get('paper_only') is True and item.get('authorized') is True and
            item.get('kind') == KIND and item.get('timeout_seconds') == 1200 and
            bool(item.get('authorization_basis')))



def _candidate(candidate):
    """Fail closed: only explicitly pre-authorized, bounded paper experiments."""
    if not isinstance(candidate, dict):
        raise ValueError('PRE_AUTHORIZED_PAPER_SANDBOX_REQUIRED')
    if not (candidate.get('authorization') == 'PRE_AUTHORIZED_PAPER_SANDBOX'
            and candidate.get('sandboxed') is True
            and candidate.get('paper_only') is True
            and candidate.get('live_orders') is False
            and candidate.get('cost_model_escalation') is False
            and allowed(candidate)):
        raise ValueError('PRE_AUTHORIZED_PAPER_SANDBOX_REQUIRED')
    ident = candidate.get('id')
    if not isinstance(ident, str) or not ident or len(ident) > 80 or any(
            c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in ident):
        raise ValueError('INVALID_CANDIDATE_ID')
    if candidate.get('state', 'PENDING') != 'PENDING':
        raise ValueError('INVALID_INITIAL_STATE')
    if not isinstance(candidate.get('output'), str) or not candidate['output']:
        raise ValueError('INVALID_OUTPUT')
    return {**candidate, 'state': 'PENDING'}


def enqueue(candidate, root=ROOT):
    """Atomically persist a single authorized candidate; never dispatch it."""
    item = _candidate(candidate)
    with locked(root) as root:
        queue = load(root)
        if (not isinstance(queue, dict) or queue.get('version') != 1
                or not isinstance(queue.get('items'), list)
                or any(not isinstance(x, dict) or not isinstance(x.get('id'), str)
                       for x in queue['items'])):
            raise ValueError('CORRUPT_RESEARCH_QUEUE')
        if any(x['id'] == item['id'] for x in queue['items']):
            return {'status': 'DUPLICATE', 'id': item['id']}
        queue['items'].append(item)
        ds.dump(root / 'queue.json', queue)
        return {'status': 'ENQUEUED', 'id': item['id']}


def register(candidate, root=ROOT):
    """Registration is an enqueue-only interface, not execution authorization."""
    return enqueue(candidate, root=root)

def reconcile(queue):
    for item in queue['items']:
        if item.get('state') in ('RUNNING', 'STARTING') and not alive(item.get('handle', {})):
            # Interrupted work is evidence, never silently retried or called complete.
            item.update(state='FAILED_EXECUTION_UNKNOWN', ended_at=stamp(),
                        error='PROCESS_IDENTITY_GONE_WITHOUT_TERMINAL_ARTIFACT')


def consume(root=ROOT):
    with locked(root) as root:
        queue = load(root); reconcile(queue)
        active = [x for x in queue['items'] if x.get('state') in ('STARTING', 'RUNNING')]
        if active:
            result = {'status': 'RESEARCH_RUNNING', 'id': active[0]['id'], 'handle': active[0]['handle']}
        else:
            pending = next((x for x in queue['items'] if x.get('state') == 'PENDING' and allowed(x)), None)
            if pending is None:
                result = {'status': 'NO_AUTHORIZED_PENDING_EXPERIMENT'}
            else:
                with (root / (pending['id'] + '.log')).open('a') as log:
                    env = os.environ.copy()
                    for key in ('PYTHONPATH', 'PYTHONHOME'): env.pop(key, None)
                    child = subprocess.Popen([sys.executable, '-m', 'cio_market_lab.engine.paper_research_queue', 'run', '--root', str(root), '--id', pending['id']],
                                             cwd=Path(__file__).resolve().parents[2], env=env,
                                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                handle = process_identity(child.pid)
                if handle is None: raise RuntimeError('DISPATCH_IDENTITY_MISSING')
                pending.update(state='STARTING', handle=handle, dispatched_at=stamp())
                result = {'status': 'DISPATCHED', 'id': pending['id'], 'handle': handle}
        ds.dump(root / 'queue.json', queue)
        ds.dump(root / 'consumer_latest.json', {**result, 'observed_at': stamp(), 'paper_only': True})
        return result


def run_item(root, item_id):
    root = Path(root)
    # One long-lived execution lease for the entire queue, independent of tick locks.
    with (root / 'execution.lock').open('a') as execution:
        try: fcntl.flock(execution, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return 75
        with locked(root):
            queue = load(root); reconcile(queue)
            item = next(x for x in queue['items'] if x['id'] == item_id)
            if not allowed(item): raise ValueError('EXPERIMENT_NOT_AUTHORIZED')
            if item.get('state') == 'STARTING' and item['handle']['pid'] != os.getpid(): return 75
            if item.get('state') not in ('PENDING', 'STARTING'): return 75
            if any(x['id'] != item_id and x.get('state') in ('RUNNING', 'STARTING') for x in queue['items']): return 75
            item.update(state='RUNNING', handle=process_identity(os.getpid()), started_at=stamp())
            ds.dump(root / 'queue.json', queue)
        out = Path(item['output']); out.mkdir(parents=True, exist_ok=True)
        receipt = {'id': item_id, 'paper_only': True, 'state': 'RUNNING', 'handle': item['handle'], 'started_at': stamp()}
        ds.dump(out / 'executor.json', receipt)
        exit_code = 2
        try:
            with (out / 'experiment.log').open('a') as log:
                child = subprocess.run([sys.executable, '-m', 'cio_market_lab.engine.paper_turnover_experiment',
                                        '--request', str(root / (item_id + '.request.json'))],
                                       cwd=Path(__file__).resolve().parents[2], stdout=log,
                                       stderr=subprocess.STDOUT, timeout=item['timeout_seconds'])
            exit_code = child.returncode
            report = json.loads((out / 'report.json').read_text()) if exit_code == 0 else {}
            if exit_code == 0 and report.get('overall') != 'EXPERIMENT_EXECUTED': exit_code = 2
            receipt.update(state='PASS' if exit_code == 0 else 'FAILED', exit_code=exit_code)
        except Exception as exc:
            receipt.update(state='FAILED', exit_code=2, error=repr(exc))
        receipt['ended_at'] = stamp(); ds.dump(out / 'executor.json', receipt)
        (out / 'exit.txt').write_text(str(exit_code) + '\n')
        with locked(root):
            queue = load(root); current = next(x for x in queue['items'] if x['id'] == item_id)
            current.update(state=receipt['state'], ended_at=receipt['ended_at'], exit_code=exit_code,
                           artifact=str(out / 'report.json'), executor_artifact=str(out / 'executor.json'))
            ds.dump(root / 'queue.json', queue)
        return exit_code


def main():
    p = argparse.ArgumentParser(); p.add_argument('action', choices=['consume', 'run'])
    p.add_argument('--root', type=Path, default=ROOT); p.add_argument('--id'); a = p.parse_args()
    if a.action == 'consume': print(json.dumps(consume(a.root))); return 0
    return run_item(a.root, a.id)


if __name__ == '__main__': raise SystemExit(main())
