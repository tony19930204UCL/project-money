"""Read-only public acquisition and exclusive-write C0048 round2 freeze.
No runtime, broker, strategy runner, profile, or commitment state is written.
"""
from __future__ import annotations
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data/c0048/round2'

def now():
    return datetime.now(timezone.utc).isoformat()

def sha(data):
    return hashlib.sha256(data).hexdigest()

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')

def capture():
    from curl_cffi import requests
    if (OUT/'snapshot_manifest.json').exists():
        raise RuntimeError('FROZEN_ARTIFACT_EXISTS_NO_OVERWRITE')
    entries = []
    runtime = ROOT/'data/paper_runtime'
    for source in sorted(runtime.rglob('*')):
        if not source.is_file():
            continue
        relative = source.relative_to(runtime)
        target = OUT/'frozen_runtime'/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        before = source.read_bytes()
        if source.suffix == '.db':
            if target.exists():
                raise RuntimeError('FROZEN_DATABASE_EXISTS')
            with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as src:
                with sqlite3.connect(target) as dst:
                    src.backup(dst)
            data = target.read_bytes()
            method = 'sqlite_readonly_transaction_backup'
        else:
            if source.read_bytes() != before:
                raise RuntimeError(f'SOURCE_CHANGED_DURING_CAPTURE:{source}')
            with target.open('xb') as f:
                f.write(before)
            data = before
            method = 'byte_copy_double_read'
        entries.append({'source': str(source), 'snapshot': str(target.relative_to(OUT)),
                        'source_sha256': sha(before), 'sha256': sha(data),
                        'bytes': len(data), 'observed_at': now(), 'method': method})
    spec = importlib.util.spec_from_file_location('optimizer_readonly', ROOT/'custom_scripts/paper_optimizer.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    universe = [s for values in module.UNI.values() for s in values]
    sources = [(f'bars/{s}.json', f'https://query2.finance.yahoo.com/v8/finance/chart/{s}?range=2y&interval=1d&events=splits%2Cdiv', 'daily_ohlcv') for s in universe]
    tw = {'income': 'opendata/t187ap14_L', 'revenue': 'opendata/t187ap05_L',
          'company': 'opendata/t187ap03_L', 'disclosures': 'opendata/t187ap04_L',
          'valuation': 'exchangeReport/BWIBBU_ALL', 'official_close': 'exchangeReport/STOCK_DAY_ALL'}
    sources += [(f'research/tw_{name}.json', f'https://openapi.twse.com.tw/v1/{endpoint}', 'official_primary') for name, endpoint in tw.items()]
    for ticker, cik in {'NVDA':1045810, 'PLTR':1321655, 'AMD':2488}.items():
        sources += [(f'research/{ticker}_facts.json', f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json', 'official_primary'),
                    (f'research/{ticker}_submissions.json', f'https://data.sec.gov/submissions/CIK{cik:010d}.json', 'official_primary')]
    def fetch(item):
        relative, url, kind = item
        attempts = []
        for attempt in range(2):
            requested_url = url if attempt == 0 else url.replace('query2.finance', 'query1.finance')
            try:
                started = now()
                response = requests.get(requested_url, impersonate='chrome',
                    headers={'User-Agent': 'C0048-Paper-Audit research contact research@example.com'}, timeout=45)
                body = response.content
                row = {'url': requested_url, 'resolved_url': response.url, 'status_code': response.status_code,
                       'started_at': started, 'retrieved_at': now(), 'body_sha256': sha(body),
                       'date_header': response.headers.get('date'), 'last_modified': response.headers.get('last-modified')}
                attempts.append(row)
                if response.status_code != 200:
                    raise RuntimeError(f'HTTP_{response.status_code}:{response.text[:120]}')
                parsed = response.json()
                if not parsed:
                    raise ValueError('EMPTY_PUBLIC_RESPONSE')
                target = OUT/relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as f:
                    f.write(body)
                return {'source': requested_url, 'snapshot': relative, 'sha256': sha(body), 'bytes': len(body),
                        'observed_at': row['retrieved_at'], 'kind': kind, 'attempts': attempts}
            except Exception as exc:
                if attempts and 'error' not in attempts[-1]:
                    attempts[-1]['error'] = f'{type(exc).__name__}:{exc}'
                else:
                    attempts.append({'url': requested_url, 'retrieved_at': now(), 'error': f'{type(exc).__name__}:{exc}'})
                if attempt == 0:
                    time.sleep(1)
        return {'source': url, 'snapshot': relative, 'missing': True, 'kind': kind, 'attempts': attempts}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        public = list(pool.map(fetch, sources))
    entries += public
    cutoff = now()
    code_paths = ['custom_scripts/paper_swing_book.py', 'custom_scripts/paper_optimizer.py',
                  'cio_market_lab/engine/automation_policy.py', 'cio_market_lab/engine/autonomous_runner.py',
                  'cio_market_lab/integrations/hermes_chat.py', 'cio_market_lab/integrations/cio_hermes_bootstrap.py',
                  'cio_market_lab/domain/models.py', 'pyproject.toml']
    versions = {'head': subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(),
                'sources': {s: sha((ROOT/s).read_bytes()) for s in code_paths},
                'params_version': json.loads((OUT/'frozen_runtime/paper_params.json').read_text())['version'],
                'book_params_version': json.loads((OUT/'frozen_runtime/swing_book.json').read_text())['params_ver']}
    save(OUT/'versions.json', versions)
    manifest = {'schema': 'c0048-frozen-v2', 'paper_only': True, 'input_kind': 'REAL_CURRENT_READONLY_CAPTURE',
                'cutoff': cutoff, 'experiment_timing': 'PROSPECTIVE_DECISION_AFTER_ACTUAL_RETRIEVAL_NOT_HISTORICAL_ASOF',
                'bars_label': 'latest_completed_exchange_session_not_execution_permission',
                'universe': universe, 'entries': entries,
                'versions_sha256': sha((OUT/'versions.json').read_bytes()),
                'research_universe': ['2317.TW','2454.TW','2382.TW','3017.TW','2303.TW','2308.TW','NVDA','PLTR','2330.TW','AMD'],
                'bounded_candidates': ['2330.TW', 'AMD'],
                'excluded_source': 'personal holdings_master is not consulted'}
    save(OUT/'snapshot_manifest.json', manifest)
    missing = [e['snapshot'] for e in entries if e.get('missing')]
    save(OUT/'progress_capture.json', {'at': now(), 'frozen': True, 'cutoff': cutoff,
         'snapshot_id': sha((OUT/'snapshot_manifest.json').read_bytes()), 'missing': missing,
         'state': 'DONE' if not missing else 'EXTERNAL_BLOCKER', 'model_calls': 0})
    print(json.dumps({'frozen': True, 'entries': len(entries), 'missing': missing, 'cutoff': cutoff}))

if __name__ == '__main__':
    capture()
