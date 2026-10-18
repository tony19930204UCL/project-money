"""Real-state-seeded, isolated execution replay; injected orders are NOT real orders."""
from __future__ import annotations
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cio_market_lab.domain.models import CIODecisionPacket, DecisionScope, Market, Order, OrderSide, OrderType, OrderStatus, Position
from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner
from cio_market_lab.engine.automation_policy import AutomationRiskPolicy
from cio_market_lab.engine.c0048_shadow import digest, load, validate_frozen, validate_main_rows, position_review
from cio_market_lab.engine.decision_learning import CIODecisionLearningStore
from cio_market_lab.engine.portfolio import Ledger
from c0048_round2_capture import OUT, save, now


def expiry_replay(book, sandbox: Path, at: datetime, horizon=DecisionScope.SWING, partial=True):
    """Execute real production residual-expiry method in a sandbox seeded from real book.

    Clock and pending residual are test interventions, not historical trading facts.
    Existing quantity, cost basis, cash and fills must never be reversed by expiry.
    """
    ledger=Ledger(horizon,initial_cash=book['cash'],currency='TWD')
    for symbol,pos in book['pos'].items():
        ledger.positions[symbol]=Position(symbol=symbol,bucket=horizon,currency='TWD',quantity=pos['sh'],average_entry_price=pos['e'])
    symbol=next(iter(book['pos']))
    quantity=book['pos'][symbol]['sh']
    order=Order(order_id='TEST_ONLY_expiring_residual',symbol=symbol,market=Market.TW,bucket=horizon,side=OrderSide.BUY,
        order_type=OrderType.LIMIT,quantity=quantity+1,currency='TWD',created_at=at-timedelta(hours=2),
        audit_metadata={'test_intervention':True},provenance={},status=OrderStatus.PARTIALLY_FILLED if partial else OrderStatus.PENDING)
    ledger.add_order(order)
    packet=CIODecisionPacket(case_id='TEST_ONLY_expiry',as_of=at-timedelta(hours=2),expiry=at-timedelta(hours=1),
        selected_instrument=symbol,action='BUY',quantity=order.quantity,holding_horizon=horizon,
        thesis='TEST_ONLY expired outstanding order; real holdings must survive',is_fixture=True)
    store=CIODecisionLearningStore(sandbox/'learning')
    rec=store.record_decision(packet,pre_decision_portfolio={'cash':ledger.cash,'positions':copy.deepcopy(book['pos'])},pre_decision_quotes={})
    rec.order_id=order.order_id
    rec.status='PARTIALLY_FILLED' if partial else 'ACTIVE'
    rec.fill={'test_intervention':True,'quantity':quantity} if partial else None
    rec.residual_quantity=quantity if partial else None
    store._persist_all()
    before={'cash':ledger.cash,'positions':{s:p.model_dump(mode='json') for s,p in ledger.positions.items()},'fill':copy.deepcopy(rec.fill)}
    runner: Any=object.__new__(AutonomousPaperRunner)
    runner.is_read_only=False
    runner._lock=threading.RLock()
    runner.learning_store=store
    runner.paper_orders=SimpleNamespace(find_order=lambda oid:order if oid==order.order_id else None)
    runner._now=lambda:at
    runner.submit_cio_packet=Mock(side_effect=AssertionError('EXPIRED_ORDER_MUST_NOT_SUBMIT'))
    runner._persist_portfolios=lambda:save(sandbox/'portfolio_after.json',{'cash':ledger.cash,'positions':{s:p.model_dump(mode='json') for s,p in ledger.positions.items()},'order':order.model_dump(mode='json')})
    assert runner.process_pending_orders()==[]
    runner.submit_cio_packet.assert_not_called()
    assert order.status==OrderStatus.EXPIRED
    assert order.rejection_reason=='PACKET_EXPIRED_RESIDUAL_CANCELLED'
    assert rec.fill==before['fill'] and ledger.cash==before['cash']
    assert {s:p.model_dump(mode='json') for s,p in ledger.positions.items()}==before['positions']
    runner.learning_store=CIODecisionLearningStore(sandbox/'learning')
    assert runner.process_pending_orders()==[]
    restored=runner.learning_store.get_record(packet.case_id)
    assert restored is not None
    assert restored.status==('EXPIRED_PARTIAL' if partial else 'EXPIRED') and restored.fill==before['fill']
    return {'horizon':horizon.value,'test_intervention':'Injected hypothetical expired order seeded with actual frozen positions; no historical order is asserted.',
        'partial':partial,'production_method':'AutonomousPaperRunner.process_pending_orders',
        'order_status':order.status.value,'record_status':restored.status,
        'position_quantity_before':quantity,'position_quantity_after':ledger.positions[symbol].quantity,
        'cash_before':before['cash'],'cash_after':ledger.cash,'restart_idempotent':True,
        'posted_fill_preserved':restored.fill==before['fill'],'no_submit_after_expiry':True,'sandbox':str(sandbox)}


def main():
    sid=digest((OUT/'snapshot_manifest_v4.json').read_bytes())
    frozen=validate_frozen(OUT,sid)
    validated=load(OUT/'main_validated.json')
    packet=CIODecisionPacket.model_validate(load(Path(validated['packet'])))
    rows=validate_main_rows(packet,frozen)
    base=load(OUT/'baseline_per_ticker.json')
    comparison=[]
    for mainrow in rows:
        baseline=next(r for r in base['rows'] if r['ticker']==mainrow['ticker'])
        symbol=mainrow['ticker']
        rate=.003425 if symbol.endswith('.TW') else .0005
        price=frozen['diagnostics'][symbol]['last_raw_close']
        main={**mainrow,'currency':baseline['currency'],'version':f'Main:{validated["provider"]}/{validated["model"]}:{packet.case_id}',
            'estimated_transaction_cost_native':mainrow['quantity']*price*rate,'realized_cost':None,
            'cost_model':'same legacy actual swing-book per-side rate','execution_status':'NOT_EXECUTED_SHADOW_ONLY'}
        comparison.append({'ticker':symbol,'mechanical':baseline,'main':main,
            'action_difference':None if baseline['action'] is None else baseline['action']!=mainrow['action'],
            'quantity_difference':None if baseline['quantity'] is None else mainrow['quantity']-baseline['quantity'],
            'action_error':None,'inaction_error':None,'after_cost_outcome':None})
    save(OUT/'comparison.json',{'snapshot_id':sid,'initial_account_sha256':load(OUT/'decision_context.json')['predecision_snapshot']['initial_account_sha256'],
        'same_frozen_book':True,'same_availability_dates':True,'same_transaction_cost_model':True,
        'scope':'Actual latest-day idempotent strategy vs authenticated research review; no new mechanical signal execution is falsely claimed.',
        'rows':comparison,'performance_improvement_proven':False,'twenty_complete_trading_days':False,
        'actual_model_cost':validated['actual_cost'],'tokens':validated['tokens']})
    at=datetime.now(timezone.utc)
    expiry=[]
    for horizon in (DecisionScope.SWING,DecisionScope.LONG_TERM):
        for partial in (False,True):
            expiry.append(expiry_replay(frozen['book'],OUT/f'horizon_replay/{horizon.value}_{partial}',at,horizon,partial))
    review=[]
    for symbol,pos in frozen['book']['pos'].items():
        current=frozen['diagnostics'][symbol]['last_raw_close']
        out=position_review(quantity=pos['sh'],current_price=current,stop_price=pos['stop'],thesis_broken=False,review_at=None,now=at)
        risk={}
        for horizon in (DecisionScope.SWING,DecisionScope.LONG_TERM):
            policy=AutomationRiskPolicy()
            args={'symbol':symbol,'bucket':horizon,'quantity':pos['sh'],'average_entry_price':pos['e'],'current_price':current,'now':at}
            risk[horizon.value]={'intact':policy.evaluate(**args).model_dump(mode='json'),
                'thesis_broken':policy.evaluate(**args,thesis_broken=True).model_dump(mode='json'),
                'stop_breach':policy.evaluate(**{**args,'current_price':pos['e']*.8}).model_dump(mode='json')}
        held=len(frozen['bars'][symbol].loc[pos['entry_day']:])-1
        review.append({'ticker':symbol,'real_quantity':pos['sh'],'real_strategy_family':pos['fam'],'entry_day':pos['entry_day'],
            'mechanical_stop':pos['stop'],'latest_completed_close':current,'held_exchange_sessions':held,
            'legacy_review_state':out,'main_review_at':next(r['review_at'] for r in rows if r['ticker']==symbol),
            'baseline_time_stop_sessions':10,'candidate_time_stop_sessions':20,
            'baseline_time_stop_triggered':held>=10,'candidate_time_stop_triggered':held>=20,
            'clock_separation':'Order clock is not an input to position risk or elapsed trading-session time stop.',
            'risk_policy_execution':risk})
    db=sqlite3.connect(f'file:{OUT}/frozen_runtime/events.db?mode=ro',uri=True)
    types=dict(db.execute('select event_type,count(*) from events group by event_type'))
    db.close()
    save(OUT/'horizon_real_state_replay.json',{'snapshot_id':sid,'at':now(),'expiry_execution_replays':expiry,'real_position_reviews':review,
        'authenticated_main_horizon_policy':packet.conditions['horizon_policy'],
        'order_event_inventory':types,
        'event_limitation':'events.db contains ORDER_CREATED but no fill/cancel terminal event history; injected expiry orders are explicitly test interventions, not reconstructed historical outstanding orders.',
        'legacy_horizon_ownership':'UNKNOWN; no retroactive position-horizon classification or new orders',
        'paper_only':True,'runtime_state_written':False})
    save(OUT/'progress_step3.json',{'step':3,'state':'DONE','at':now(),'snapshot_id':sid,'main_decision_calls':1,
        'same_snapshot_comparison':str(OUT/'comparison.json'),'honest_baseline_scope':'LATEST_SESSION_ALREADY_PROCESSED_IDEMPOTENT_GUARD',
        'provider':validated['provider'],'model':validated['model'],'actual_cost':None,'tokens':validated['tokens']})
    save(OUT/'progress_step4.json',{'step':4,'state':'DONE','at':now(),'snapshot_id':sid,'real_state_seeded_expiry_cases':len(expiry),
        'full_real_position_replays':len(review),'version_experiment':str(OUT/'candidate_counterfactual.json'),
        'candidate_effect':'No decision delta on already-processed same snapshot; actual held exchange sessions also do not trigger either time stop.',
        'no_improvement_assertion':True})
    print('REAL_STATE_REPLAY_OK',len(comparison),'tickers',len(expiry),'expiry cases',len(review),'real held positions')

if __name__=='__main__':main()
