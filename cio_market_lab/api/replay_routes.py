"""Captured-source historical replay caller; never autonomous/broker order authority."""
from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path
import re
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from cio_market_lab.api.security import assert_owner_port
from cio_market_lab.domain.models import Bar
from cio_market_lab.engine.replay_lab import AUTHORITY, walk_forward_replay

router = APIRouter(prefix='/api/replay', tags=['Historical Replay Lab'])
SYMBOLS = {'MSFT': 'USD', '2330.TW': 'TWD'}
RUN_ID = re.compile(r'^walk-forward-[0-9a-f-]{36}$')


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    symbol: str = Field(pattern=r'^(MSFT|2330\.TW)$')
    initial_cash: float = Field(default=100000, gt=0, le=100000000, allow_inf_nan=False)
    folds: int = Field(default=3, ge=2, le=5, strict=True)
    config: dict = Field(default_factory=dict)


def paths(request: Request):
    root = Path(request.app.state.replay_workspace_root)
    capture = root / 'artifacts/checklist_continuation_20261001/real_source_replay'
    runs = root / 'artifacts/replay_lab_runs'
    return capture, runs


def source(capture: Path, symbol: str):
    if symbol not in SYMBOLS:
        raise HTTPException(400, 'UNSUPPORTED_CAPTURE_SYMBOL')
    try:
        receipt = json.loads((capture / 'capture_and_replay_receipt.json').read_text())
        row = next(r for r in receipt['results'] if r['symbol'] == symbol)
        # Ignore arbitrary absolute capture_path supplied in old receipt metadata.
        raw = json.loads((capture / f'{symbol}-source-bars.json').read_text())
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        if digest != row['capture_sha256']:
            raise ValueError('CAPTURE_INTEGRITY_MISMATCH')
        if row['source'] != 'yahoo_public_chart' or row['currency'] != SYMBOLS[symbol] or row.get('client_asset_input') is not False:
            raise ValueError('CAPTURE_PROVENANCE_UNVERIFIED')
        bars = [Bar.model_validate(x) for x in raw]
        if not bars or len(bars) != row['source_bar_count'] or len(bars) > 6000:
            raise ValueError('CAPTURE_BAR_COUNT_MISMATCH')
        if any(b.symbol != symbol or b.is_fixture or b.is_synthetic or b.source != row['source'] for b in bars):
            raise ValueError('CAPTURE_PROVENANCE_UNVERIFIED')
        return bars, {'source': row['source'], 'observed_at': row['observed_at'],
                      'source_period': row['source_period'], 'source_interval': row['source_interval'],
                      'capture_sha256': digest, 'source_bar_count': len(bars),
                      'currency': SYMBOLS[symbol], 'is_fixture': False,
                      'historical_capture_only': True, 'capture_does_not_prove_current_quote': True}
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else 'CAPTURE_NOT_AVAILABLE_OR_UNVERIFIED'
        raise HTTPException(409, reason) from exc


@router.get('/sources')
def catalog(request: Request):
    capture, _ = paths(request)
    rows = []
    for symbol in SYMBOLS:
        try:
            bars, provenance = source(capture, symbol)
            rows.append({'symbol': symbol, **provenance, 'available': True,
                         'first_bar': bars[0].timestamp.isoformat(), 'last_bar': bars[-1].timestamp.isoformat()})
        except HTTPException as exc:
            rows.append({'symbol': symbol, 'available': False, 'reason': exc.detail})
    return {'authority': AUTHORITY, 'broker_connected': False, 'sources': rows}


@router.post('/run')
def run(payload: ReplayRequest, request: Request):
    assert_owner_port(request, 'Historical replay artifacts')
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(403, 'CROSS_ORIGIN_REPLAY_MUTATION_FORBIDDEN')
    capture, runs = paths(request)
    bars, provenance = source(capture, payload.symbol)
    runs.mkdir(parents=True, exist_ok=True)
    with (runs / 'writer.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HTTPException(409, 'REPLAY_ALREADY_RUNNING') from exc
        # A unique directory prevents overlap and accidental overwrite of a prior run.
        out = runs / ('request-' + str(uuid.uuid4()))
        try:
            result = walk_forward_replay(bars, payload.symbol, SYMBOLS[payload.symbol],
                                         payload.initial_cash, payload.config, out, folds=payload.folds)
        except ValueError as exc:
            if out.exists():
                (out / 'failure.json').write_text(json.dumps({'status':'FAILED', 'reason':str(exc),
                                                             'authority':AUTHORITY})+'\n')
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            out.mkdir(exist_ok=True)
            (out / 'failure.json').write_text(json.dumps({'status':'FAILED','error_type':type(exc).__name__,
                                                         'authority':AUTHORITY})+'\n')
            raise HTTPException(500, 'REPLAY_EXECUTION_FAILED_NO_RESULT_PROMOTION') from exc
        result['source_provenance'] = provenance
        (out / 'walk_forward_result.json').write_text(json.dumps(result, indent=2)+'\n')
        return result


@router.get('/runs')
def list_runs(request: Request):
    _, runs = paths(request)
    rows = []
    for path in sorted(runs.glob('request-*/walk_forward_result.json'), reverse=True)[:100]:
        try:
            result = json.loads(path.read_text())
            rows.append({k:result[k] for k in ['run_id','symbol','currency','frozen_at','config','config_hash',
                                              'source_hash','fold_count','final_holdout','authority','live_acceptance']})
        except (ValueError, OSError, KeyError):
            rows.append({'status':'CORRUPT_RESULT_NOT_PROMOTED','authority':AUTHORITY})
    failures=[]
    for path in sorted(runs.glob('request-*/failure.json'), reverse=True)[:100]:
        try:
            failures.append(json.loads(path.read_text()))
        except (ValueError,OSError):
            failures.append({'status':'CORRUPT_FAILURE_RECEIPT'})
    return {'authority':AUTHORITY, 'runs':rows, 'failures':failures}


@router.get('/runs/{run_id}')
def get_run(run_id: str, request: Request):
    if not RUN_ID.fullmatch(run_id):
        raise HTTPException(404, 'REPLAY_RUN_NOT_FOUND')
    _, runs = paths(request)
    for path in runs.glob('request-*/walk_forward_result.json'):
        try:
            data=json.loads(path.read_text())
            if data.get('run_id') == run_id:
                return data
        except (ValueError,OSError):
            continue
    raise HTTPException(404, 'REPLAY_RUN_NOT_FOUND')
