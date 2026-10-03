"""Offline TEST_ONLY requirements replay; never live acceptance evidence."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from cio_market_lab.domain.models import CIODecisionPacket
from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner
from cio_market_lab.engine.cio_packet import sign_cio_packet
from cio_market_lab.engine.decision_learning import CIODecisionLearningStore
from cio_market_lab.engine.cio_session import FrozenDecisionContext


def harness(tmp_path, action='NO_TRADE'):
    now=datetime(2026,10,1,12,tzinfo=timezone.utc)
    r=object.__new__(AutonomousPaperRunner)
    r.runtime_dir=tmp_path; r.cio_session_id='TEST_ONLY_session'; r._now=lambda:now
    r.material_gate_enabled=True; r.allow_fixture_quotes=True
    r.learning_store=CIODecisionLearningStore(tmp_path/'learning')
    packet=sign_cio_packet(CIODecisionPacket(case_id='TEST_ONLY_case',as_of=now,expiry=now+timedelta(hours=1),selected_instrument='MSFT',action=action,quantity=1 if action=='BUY' else 0,thesis='TEST_ONLY justified missing valuation WAIT',is_fixture=True),signer_id='fixture-test-signer')
    r.cio_executor=SimpleNamespace(is_available=lambda:True,request_decision=Mock(return_value=packet),last_receipt=SimpleNamespace(readback_verified=True,case_id=packet.case_id,provider_id='TEST_ONLY',model_id='TEST_ONLY',model_dump=lambda **_: {'readback_verified':True,'case_id':packet.case_id,'test_only':True}))
    r.last_receipt=None
    obs={'symbol':'MSFT','market':'US','official_material_ids':['TEST_ONLY_fact_v1'],'research_only':True,'stance':'WAIT','missing_evidence':['TEST_ONLY valuation'],'verified_facts':['TEST_ONLY revenue fact'],'thesis':'TEST_ONLY missing valuation WAIT','buy_zone':None,'invalidation':'TEST_ONLY new official disclosure required'}
    context=FrozenDecisionContext(context_id='TEST_ONLY_MSFT_2026-10-01',session_date='2026-10-01',official_source_lineage=[{'source_url':'https://data.sec.gov/TEST_ONLY','verified_facts':['TEST_ONLY revenue fact']}],thesis=obs['thesis'],valuation_scenarios={},catalysts=[],entry_zone={},invalidation={'rule':obs['invalidation']},exposure_ceiling=0)
    r.material_observation_provider=Mock(return_value=obs)
    r.material_observation_provider.context_for=lambda _:context
    portfolio=SimpleNamespace(cash=100000,currency='USD',initial_cash=100000,equity=100000,positions={})
    r.portfolio_manager=SimpleNamespace(get_strategy_portfolio=lambda *a:portfolio)
    r._market_call=Mock(return_value=SimpleNamespace(symbol='MSFT',timestamp=now-timedelta(minutes=1),close=400.0,source='fixture',is_fixture=True,is_synthetic=False,quality='TEST_ONLY'))
    r._decision=Mock(side_effect=lambda *args,**kw: {'action':args[3],'reason':args[4],'inputs':args[5],**kw})
    settings=SimpleNamespace(strategy_id='TEST_ONLY_us',universe=['MSFT'],mode='swing',max_position_notional=2000,base_currency='USD',allowed_buckets=['swing'])
    return r,settings,now


def test_real_caller_has_daily_wait_bridge():
    import inspect
    assert '_review_daily_unarmed_plan' in inspect.getsource(AutonomousPaperRunner._run_one_cycle_locked)


def test_wait_without_executable_book_creates_formal_expiring_case(tmp_path):
    r,s,now=harness(tmp_path)
    d=r._review_daily_unarmed_plan('TEST_ONLY_run',s,'MSFT')
    assert d['action']=='NO_TRADE' and d['terminal_status']=='TERMINAL_NO_TRADE'
    rec=r.learning_store.get_record('TEST_ONLY_case')
    assert rec and rec.status=='ACTIVE' and rec.fill is None
    assert rec.pre_decision_quotes['MSFT']==400
    assert rec.packet.conditions['observation_deadline']==(now+timedelta(days=7)).isoformat()
    assert rec.pre_decision_portfolio['currency']=='USD'
    assert r._review_daily_unarmed_plan('TEST_ONLY_run2',s,'MSFT')['reason'].startswith('CIO material gate:')
    assert r.cio_executor.request_decision.call_count==1
    restored=CIODecisionLearningStore(tmp_path/'learning')
    assert restored.get_record('TEST_ONLY_case').packet.conditions['observation_deadline']


def test_unarmed_plan_never_forces_or_accepts_buy(tmp_path):
    r,s,_=harness(tmp_path,'BUY')
    d=r._review_daily_unarmed_plan('TEST_ONLY_run',s,'MSFT')
    assert d['terminal_status']=='TERMINAL_RISK_BLOCK'
    assert not r.learning_store.processed_case_ids


def test_missing_analysis_reference_is_explicit_not_invented(tmp_path):
    r,s,_=harness(tmp_path);r._market_call.side_effect=RuntimeError('TEST_ONLY missing feed')
    d=r._review_daily_unarmed_plan('TEST_ONLY_run',s,'MSFT')
    assert d['terminal_status']=='TERMINAL_NO_TRADE'
    rec=r.learning_store.get_record('TEST_ONLY_case')
    assert rec.pre_decision_quotes=={}
    assert rec.packet.conditions['observation_status']=='WAITING_FOR_BASELINE_NO_COUNTERFACTUAL_CLAIM'


def test_synthetic_reference_is_never_counterfactual_baseline(tmp_path):
    r,s,_=harness(tmp_path);r.allow_fixture_quotes=False
    r.cio_executor.request_decision.return_value.is_fixture=False
    d=r._review_daily_unarmed_plan('TEST_ONLY_run',s,'MSFT')
    rec=r.learning_store.get_record('TEST_ONLY_case')
    assert rec.pre_decision_quotes=={}


def test_horizon_uses_observed_later_price_and_never_reports_fill(tmp_path):
    r,s,now=harness(tmp_path);r._review_daily_unarmed_plan('TEST_ONLY_run',s,'MSFT')
    later=now+timedelta(days=8);r._now=lambda:later
    r._market_call=Mock(return_value=SimpleNamespace(symbol='MSFT',last_price=420.0,is_stale=False,is_synthetic=False,is_fixture=True,source='fixture',timestamp=later,observed_at=later))
    assert r.evaluate_elapsed_non_actions('MSFT')==['TEST_ONLY_case']
    rec=r.learning_store.get_record('TEST_ONLY_case')
    assert rec.status=='CLOSED' and rec.outcome['no_fill'] and rec.outcome['realized_pnl']==0
    assert rec.outcome['counterfactual_price_change_pct']==5
    assert r.evaluate_elapsed_non_actions('MSFT')==[]


@pytest.mark.parametrize('context_id',[
    'project-money-main-cio-2330.TW-2026-10-02-authenticated',
    'project-money-main-cio-6488.TWO-2026-10-02-authenticated',
    'project-money-main-cio-MSFT-2026-10-01-authenticated',
])
def test_daily_context_punctuation_preserves_identity(tmp_path, context_id):
    r,s,_=harness(tmp_path)
    old=r.material_observation_provider.context_for('MSFT')
    context=FrozenDecisionContext(**{**old.model_dump(), 'context_id':context_id})
    r.material_observation_provider.context_for=lambda _:context
    decision=r._review_daily_unarmed_plan('TEST_ONLY_punctuation',s,'MSFT')
    assert decision['terminal_status']=='TERMINAL_NO_TRADE'
    record=r.learning_store.get_record('TEST_ONLY_case')
    assert record.packet.conditions['daily_context_id']==context_id
    assert record.pre_decision_portfolio['currency']=='USD'
    assert record.packet.predecision_snapshot['frozen_daily_context']['context_id']==context_id

