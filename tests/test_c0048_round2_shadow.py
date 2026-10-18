"""PAPER ONLY: real round2 replay plus mutations pinning round1's actual failure.

Private frozen inputs are intentionally not committed. Integration cases skip when
that evidence is absent, and the implementer verifier requires zero skipped here.
"""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from cio_market_lab.domain.models import CIODecisionPacket, DecisionScope
from cio_market_lab.engine.c0048_shadow import FrozenInputError, digest, load, validate_frozen, validate_main_rows, position_review

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'data/c0048/round2'
SNAPSHOT='f123f7dfc036ad72c53f66333be879ca0f26bb30449b90cc5ee738cc77b68504'
ROUND1_BASELINE_SHA='ff94a969bbaf71e0325e973cfdee465f6e1cc84ea7dfe40ed768bfcdbb73a8ea'


def script(name):
    path=ROOT/'scripts'/f'{name}.py'
    sys.path.insert(0,str(path.parent))
    spec=importlib.util.spec_from_file_location(name,path)
    assert spec and spec.loader
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def real():
    if not (OUT/'snapshot_manifest_v4.json').exists():
        pytest.skip('Private real round2 evidence not available; not acceptance')
    return validate_frozen(OUT,SNAPSHOT)


@pytest.fixture
def frozen_copy(tmp_path,real):
    manifest=real['manifest']
    for name in ['snapshot_manifest_v4.json','versions.json','starting_state.json']+[e['snapshot'] for e in manifest['entries']]:
        target=tmp_path/name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(OUT/name,target)
    return tmp_path


def rewrite(root,name,obj):
    (root/name).write_text(json.dumps(obj))
    manifest=load(root/'snapshot_manifest_v4.json')
    for entry in manifest['entries']:
        if entry['snapshot']==name:
            entry['sha256']=digest((root/name).read_bytes())
    (root/'snapshot_manifest_v4.json').write_text(json.dumps(manifest))
    return digest((root/'snapshot_manifest_v4.json').read_bytes())


def manifest_change(root,change):
    manifest=load(root/'snapshot_manifest_v4.json')
    change(manifest)
    (root/'snapshot_manifest_v4.json').write_text(json.dumps(manifest))
    return digest((root/'snapshot_manifest_v4.json').read_bytes())


def test_missing_manifest_is_not_wait(tmp_path):
    with pytest.raises(FrozenInputError,match='MISSING_MANIFEST'):
        validate_frozen(tmp_path,'unavailable')


def test_real_snapshot_has_all_actual_positions_and_history(real):
    assert len(real['bars'])==30
    assert min(len(df) for df in real['bars'].values())>200
    assert len(real['book']['pos'])==5
    assert real['book']['last']=='2026-10-16'
    assert real['accounts']['US']['positions']['NVDA']>0
    assert real['accounts']['TW']['positions']['2308.TW']>0
    assert all(d['latest_day']=='2026-10-16' for d in real['diagnostics'].values())


def test_actual_round1_failure_cannot_become_wait_or_pass(real):
    evidence=OUT.parent/'round1/mechanical_baseline.json'
    assert digest(evidence.read_bytes())==ROUND1_BASELINE_SHA
    failure=load(evidence)
    assert failure['status']=='BLOCKED' and failure['orders'] is None
    assert 'MISSING_FROZEN_BASELINE_INPUT' in failure['error']
    assert 'MA200' in failure['error'] and 'swing_book' in failure['error']
    with pytest.raises(FrozenInputError,match='MISSING_MANIFEST'):
        validate_frozen(OUT.parent/'round1',failure['snapshot_id'])


@pytest.mark.parametrize('name,reason',[
    ('frozen_runtime/swing_book.json','MISSING_INPUT'),
    ('bars/AMD.json','MISSING_INPUT'),
    ('versions.json','MALFORMED_OR_MISSING_FROZEN_INPUT'),
    ('frozen_runtime/paper_params.json','MISSING_INPUT'),
])
def test_missing_inputs_all_hard_fail(frozen_copy,name,reason):
    (frozen_copy/name).unlink()
    with pytest.raises(FrozenInputError,match=reason):
        validate_frozen(frozen_copy,SNAPSHOT)


def test_wrong_snapshot_hash(frozen_copy):
    with pytest.raises(FrozenInputError,match='WRONG_SNAPSHOT_HASH'):
        validate_frozen(frozen_copy,'0'*64)


def test_changed_body_hash(frozen_copy):
    p=frozen_copy/'bars/AMD.json'
    p.write_bytes(p.read_bytes()+b' ')
    with pytest.raises(FrozenInputError,match='INPUT_HASH_MISMATCH'):
        validate_frozen(frozen_copy,SNAPSHOT)


@pytest.mark.parametrize('asof,reason',[(None,'MISSING_OR_BAD_ASOF'),('2026-10-18T14:00:00','MISSING_OR_BAD_ASOF'),('2099-01-01T00:00:00Z','FUTURE_ASOF')])
def test_missing_or_bad_cutoff(frozen_copy,asof,reason):
    sid=manifest_change(frozen_copy,lambda m:m.update(cutoff=asof))
    with pytest.raises(FrozenInputError,match=reason):
        validate_frozen(frozen_copy,sid)


def test_post_cutoff_research_is_not_historical_asof(frozen_copy):
    def change(m):
        next(e for e in m['entries'] if e['snapshot']=='research/NVDA_facts.json')['observed_at']='2099-01-01T00:00:00Z'
    sid=manifest_change(frozen_copy,change)
    with pytest.raises(FrozenInputError,match='FUTURE_OR_MISSING_INPUT_ASOF'):
        validate_frozen(frozen_copy,sid)


def test_primary_research_cannot_be_removed_from_manifest(frozen_copy):
    sid=manifest_change(frozen_copy,lambda m:m.update(entries=[e for e in m['entries'] if e['snapshot']!='research/NVDA_facts.json']))
    with pytest.raises(FrozenInputError,match='MISSING_REQUIRED_PRIMARY_RESEARCH'):
        validate_frozen(frozen_copy,sid)


def test_held_symbol_cannot_be_omitted_from_research(frozen_copy):
    sid=manifest_change(frozen_copy,lambda m:m['research_universe'].remove('2317.TW'))
    with pytest.raises(FrozenInputError,match='HELD_SYMBOL_MISSING_OFFICIAL_RESEARCH'):
        validate_frozen(frozen_copy,sid)


def test_future_split_event_rejected_even_when_rehashed(frozen_copy):
    name='bars/NFLX.json'
    obj=load(frozen_copy/name)
    split=next(iter(obj['chart']['result'][0]['events']['splits'].values()))
    split['date']=4070908800
    sid=rewrite(frozen_copy,name,obj)
    with pytest.raises(FrozenInputError,match='INVALID_OR_FUTURE_SPLIT'):
        validate_frozen(frozen_copy,sid)


@pytest.mark.parametrize('defect,reason',[
    ('duplicate','DUPLICATE_OR_UNSORTED_BARS'),('bad','BAD_OHLCV'),('missing','MISSING_EXCHANGE_DAY_BAR'),
    ('null','INSUFFICIENT_OR_MISSING_BARS'),('future','FUTURE_BARS'),('currency','CURRENCY_EXCHANGE_MISMATCH'),
    ('too_short','INSUFFICIENT_OR_MISSING_BARS'),('adjustment','MISSING_ADJUSTMENT_BASIS'),
])
def test_real_bar_mutations_are_rejected(frozen_copy,defect,reason):
    name='bars/AMD.json'
    obj=load(frozen_copy/name)
    chart=obj['chart']['result'][0]
    quote=chart['indicators']['quote'][0]
    if defect=='duplicate':chart['timestamp'][2]=chart['timestamp'][1]
    if defect=='bad':quote['low'][2]=-1
    if defect=='null':quote['close'][2]=None
    if defect=='future':chart['timestamp'][-1]=4070908800
    if defect=='currency':chart['meta']['currency']='TWD'
    if defect=='adjustment':del chart['indicators']['adjclose']
    if defect in ['missing','too_short']:
        indices=[2] if defect=='missing' else list(range(200,len(chart['timestamp'])))
        for idx in reversed(indices):
            chart['timestamp'].pop(idx)
            for values in quote.values():values.pop(idx)
            chart['indicators']['adjclose'][0]['adjclose'].pop(idx)
    sid=rewrite(frozen_copy,name,obj)
    with pytest.raises(FrozenInputError,match=reason):validate_frozen(frozen_copy,sid)


def test_falsely_empty_swing_book_is_rejected_even_when_rehashed(frozen_copy):
    name='frozen_runtime/swing_book.json'
    book=load(frozen_copy/name);book['pos']={}
    sid=rewrite(frozen_copy,name,book)
    with pytest.raises(FrozenInputError,match='FALSELY_EMPTY_OR_DETACHED_SWING_ACCOUNT'):
        validate_frozen(frozen_copy,sid)


def test_falsely_empty_account_is_rejected_even_when_rehashed(frozen_copy):
    name='frozen_runtime/US_account.json'
    account=load(frozen_copy/name);account['positions']={};account['cash']=100000
    sid=rewrite(frozen_copy,name,account)
    with pytest.raises(FrozenInputError,match='FALSELY_EMPTY_OR_CHANGED_INITIAL_ACCOUNT'):
        validate_frozen(frozen_copy,sid)


def test_current_baseline_executes_actual_strategy_without_mutation(real):
    module=script('c0048_round2_prepare')
    before=digest((OUT/'frozen_runtime/swing_book.json').read_bytes())
    result=module.baseline(real,load(OUT/'frozen_runtime/paper_params.json'))
    assert result['status']=='EXECUTED' and result['response']=='already 2026-10-16'
    assert result['events']==[] and result['orders']==[]
    assert digest((OUT/'frozen_runtime/swing_book.json').read_bytes())==before
    assert not result['signal_recalculation_executed']


def test_real_splits_match_installed_yfinance_adjustment(real):
    from yfinance.utils import auto_adjust
    seen=[]
    for symbol in ['ANET','CRWD','NFLX']:
        chart=load(OUT/f'bars/{symbol}.json')['chart']['result'][0]
        assert chart['events']['splits']
        df=pd.DataFrame({k.title():v for k,v in chart['indicators']['quote'][0].items()},index=real['bars'][symbol].index)
        df['Adj Close']=chart['indicators']['adjclose'][0]['adjclose']
        pd.testing.assert_frame_equal(auto_adjust(df),real['bars'][symbol],check_like=True)
        seen.extend(chart['events']['splits'].values())
    assert len(seen)==3


@pytest.mark.parametrize('horizon',[DecisionScope.SWING,DecisionScope.LONG_TERM])
@pytest.mark.parametrize('partial',[False,True])
def test_real_state_seeded_actual_expiry_path_preserves_positions(tmp_path,real,horizon,partial):
    replay=script('c0048_round2_replay')
    result=replay.expiry_replay(real['book'],tmp_path,datetime.now(timezone.utc),horizon,partial)
    assert result['position_quantity_before']==result['position_quantity_after']
    assert result['cash_before']==result['cash_after']
    assert result['restart_idempotent'] and result['posted_fill_preserved']
    assert result['no_submit_after_expiry']


def test_position_thesis_stop_and_review_clocks_are_separate(real):
    at=datetime.now(timezone.utc)
    p=next(iter(real['book']['pos'].values()))
    args: dict[str,Any]=dict(quantity=p['sh'],current_price=p['e'],stop_price=p['stop'],thesis_broken=False,review_at=at-timedelta(days=1),now=at)
    assert position_review(**args)=={'action':'HOLD','reason':'POSITION_THESIS_AND_STOP_INTACT','review_state':'DUE'}
    assert position_review(**{**args,'thesis_broken':True})['reason']=='THESIS_BROKEN'
    assert position_review(**{**args,'current_price':p['stop']*.99})['reason']=='CONFIGURED_POSITION_STOP'
    assert position_review(**{**args,'review_at':None})['review_state']=='UNKNOWN_LEGACY_METADATA'


@pytest.mark.parametrize('defect,reason',[
    ('missing_row','MAIN_MISSING_OR_DUPLICATE_TICKERS'),('duplicate','MAIN_MISSING_OR_DUPLICATE_TICKERS'),
    ('missing_rationale','MAIN_MISSING_REQUIRED_FIELDS'),('wrong_snapshot','MAIN_WRONG_SNAPSHOT'),
    ('insufficient','MAIN_INSUFFICIENCY_IS_NOT_NO_TRADE'),('quantity','MAIN_NONTRADE_QUANTITY'),
    ('zero_buy','MAIN_UNSUPPORTED_OR_EMPTY_TRADE'),('unknown_horizon','MAIN_BAD_EVIDENCE_OR_HORIZON'),
])
def test_main_packet_missing_fields_refused(real,defect,reason):
    validated=load(OUT/'main_validated.json')
    packet=CIODecisionPacket.model_validate(load(Path(validated['packet'])))
    rows=packet.conditions['per_ticker_decisions']
    if defect=='missing_row':rows.pop()
    if defect=='duplicate':rows[-1]=copy.deepcopy(rows[0])
    if defect=='missing_rationale':rows[0].pop('rationale')
    if defect=='wrong_snapshot':packet.predecision_snapshot['snapshot_id']='detached'
    if defect=='insufficient':rows[-1].update(action='NO_TRADE',evidence_status='INSUFFICIENT')
    if defect=='quantity':rows[0]['quantity']=1
    if defect=='zero_buy':rows[0].update(action='BUY',quantity=0)
    if defect=='unknown_horizon':rows[0]['holding_horizon']='forever'
    with pytest.raises(ValueError,match=reason):validate_main_rows(packet,real)


def test_frozen_artifacts_refuse_overwrite(tmp_path):
    capture=script('c0048_round2_capture')
    target=tmp_path/'frozen.json'
    capture.save(target,{'original':True})
    before=target.read_bytes()
    with pytest.raises(FileExistsError):capture.save(target,{'replacement':True})
    assert target.read_bytes()==before
