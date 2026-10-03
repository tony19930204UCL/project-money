"""Account-wide paper margin review over ONE strategy cash pool.

A retained or fixture derivative lifecycle is still unavailable for production.
This caller marks both holding-horizon buckets before deciding whether shared
maintenance is deficient. Unknown marks never turn into zero or cash-only NAV.
"""
from __future__ import annotations

import math
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from cio_market_lab.domain.events import EventEnvelope, EventType
from cio_market_lab.domain.models import DecisionScope, OrderSide
from cio_market_lab.engine.paper_derivatives import (
    AccountingDelta, ContractSpec, DerivativeInstrumentType, NormalizedAccountingEvent,
)

BUCKETS = (DecisionScope.SWING, DecisionScope.INTRADAY)


def review_account_margin(service: Any, *, strategy_id: str, quotes: dict,
                          review_id: str, now: Any) -> dict:
    """Fixture-only deterministic shared margin liquidation with durable receipts.

    This is reduction, never a new position or a broker order. Close highest
    maintenance first and stop once the shared account is healthy. Requests are
    immutable at the account/scope/clock boundary and recover committed closes.
    """
    if not service.fixture_mode:
        raise ValueError('DERIVATIVES_UNAVAILABLE_PENDING_ADAPTER_ACCEPTANCE')
    if not review_id or not isinstance(review_id, str) or now.tzinfo is None:
        raise ValueError('MARGIN_REVIEW_ID_AND_AWARE_CLOCK_REQUIRED')
    settings = service.orders.experiments[strategy_id]
    ledgers = {b:service.portfolios.get_strategy_ledger(strategy_id,b,settings.initial_cash)
               for b in BUCKETS}
    cash_account = ledgers[BUCKETS[0]]._cash_account
    if cash_account is None or any(l._cash_account is not cash_account for l in ledgers.values()):
        raise ValueError('SHARED_CANONICAL_CASH_ACCOUNT_REQUIRED')
    if any(l.currency != settings.base_currency for l in ledgers.values()):
        raise ValueError('MARGIN_ACCOUNT_CURRENCY_MISMATCH')

    def event_id(phase):
        return str(uuid5(NAMESPACE_URL,f'paper-margin-review:{strategy_id}:{review_id}:{phase}'))

    def record(phase,payload):
        service.store.append_once(EventEnvelope(event_id=event_id(phase),
            event_type=EventType.DERIVATIVE_ACCOUNT_REVIEW_RECORDED,aggregate_id=strategy_id,
            timestamp=now,payload=dict(phase=phase,review_id=review_id,
                strategy_id=strategy_id,review_at=now.isoformat(),is_fixture=True,
                paper_only=True,broker_connected=False,production_adapter_accepted=False,
                **payload)))

    def validate_receipt(envelope):
        if (envelope.payload.get('strategy_id')!=strategy_id or
            envelope.payload.get('review_at')!=now.isoformat()):
            raise ValueError('MARGIN_REVIEW_ID_REUSED_WITH_CHANGED_REQUEST')

    completed=service.store.get_by_event_id(event_id('MARGIN_REVIEW_COMPLETED'))
    if completed:
        validate_receipt(completed)
        service.replay()
        return completed.payload['result']
    started=service.store.get_by_event_id(event_id('MARGIN_REVIEW_STARTED'))
    if started:
        validate_receipt(started)
        service.replay()

    def positions():
        result=[]
        for b,l in ledgers.items():
            for symbol,canonical in sorted(l.positions.items()):
                if not canonical.quantity or 'derivative_position' not in canonical.assumptions:
                    continue
                pos=service._position(strategy_id,b,symbol)
                spec=ContractSpec.model_validate(canonical.assumptions['contract_spec'])
                if spec.currency != settings.base_currency:
                    raise ValueError('MARGIN_CONTRACT_CURRENCY_MISMATCH')
                if spec.instrument_type not in {DerivativeInstrumentType.FUTURE,DerivativeInstrumentType.OPTION}:
                    raise ValueError('UNSUPPORTED_MARGIN_ACCOUNT_INSTRUMENT')
                result.append((b,symbol,pos,spec))
        return result

    held=positions()
    if started:
        original={ (p['bucket'],p['symbol']):p for p in started.payload['positions'] }
        for b,symbol,pos,spec in held:
            prev=original.get((b.value,symbol))
            if (not prev or prev['position_id']!=pos.position_id or
                prev['quantity']!=pos.quantity or prev['contract_spec']!=spec.model_dump(mode='json')):
                raise ValueError('MARGIN_ACCOUNT_CHANGED_DURING_REVIEW')
    else:
        record('MARGIN_REVIEW_STARTED',{'positions':[{'bucket':b.value,'symbol':symbol,
            'position_id':pos.position_id,'quantity':pos.quantity,
            'contract_spec':spec.model_dump(mode='json')} for b,symbol,pos,spec in held]})

    def quote_valid(quote,spec):
        return (quote is not None and quote.symbol==spec.symbol and quote.is_fixture and
            bool(quote.provenance.get('authority')) and quote.bid is not None and quote.ask is not None and
            math.isfinite(quote.bid) and math.isfinite(quote.ask) and
            quote.bid>0 and quote.ask>=quote.bid and spec.expiry is not None and
            spec.expiry.tzinfo is not None and now<spec.expiry and
            service.engine.validate_tick_size(quote.bid,spec.tick_size) and
            service.engine.validate_tick_size(quote.ask,spec.tick_size) and
            service.engine.validate_quote(quote,as_of=now)[0])

    # Complete all marks before checking a shared account: never liquidate using
    # the first bucket's local equity or counting its cash a second time.
    for b,symbol,pos,spec in held:
        quote=quotes.get((b,symbol))
        valid=quote_valid(quote,spec)
        if valid:
            marked=service.engine.mark_to_market(pos,spec,quote,as_of=now)
        else:
            marked=pos.model_copy(update={'current_price':None,'market_value':None,
                                          'unrealized_pnl':None,'last_updated_at':now})
        key=f'account-margin-mark:{review_id}:{pos.position_id}:{b.value}'
        event=NormalizedAccountingEvent(event_type='DERIVATIVE_MARK' if valid else 'DERIVATIVE_MARK_UNAVAILABLE',
            aggregate_id=symbol,timestamp=now,idempotency_key=key,
            delta=AccountingDelta(idempotency_key=key,aggregate_id=symbol,
                event_type='DERIVATIVE_MARK' if valid else 'DERIVATIVE_MARK_UNAVAILABLE',timestamp=now),
            payload={'position':marked.model_dump(mode='json'),'review_id':review_id,
                'quote_source':quote.source if quote else None,
                'quote_provenance':quote.provenance if quote else None,
                'execution_status':'MARKED' if valid else 'UNRESOLVED_EXECUTABLE_QUOTE',
                'is_fixture':True,'paper_only':True,'broker_connected':False})
        service._record(event,strategy_id,b,spec)

    def metrics():
        market_value=0.0
        unknown=[]
        maintenance=0.0
        requirements=[]
        for b,l in ledgers.items():
            for symbol,p in l.positions.items():
                if not p.quantity:
                    continue
                if (p.current_price is None or p.market_value is None or
                    not math.isfinite(p.current_price) or not math.isfinite(p.market_value)):
                    unknown.append({'bucket':b.value,'symbol':symbol})
                else:
                    market_value+=p.market_value
                if 'derivative_position' not in p.assumptions:
                    continue
                spec=ContractSpec.model_validate(p.assumptions['contract_spec'])
                pos=service._position(strategy_id,b,symbol)
                if spec.instrument_type!=DerivativeInstrumentType.FUTURE:
                    continue # premium-paid LONG options carry no futures maintenance.
                required=(abs(pos.quantity)*spec.maintenance_margin_per_contract
                    if spec.maintenance_margin_per_contract>0 else
                    abs(pos.quantity)*(pos.current_price or pos.average_entry_price)*spec.multiplier*spec.maintenance_margin_rate)
                if not math.isfinite(required) or required<0:
                    raise ValueError('INVALID_MAINTENANCE_REQUIREMENT')
                maintenance+=required
                requirements.append((required,b,symbol,pos,spec))
        cash=ledgers[BUCKETS[0]].cash
        if not math.isfinite(cash):
            raise ValueError('INVALID_ACCOUNT_CASH')
        return (None if unknown else cash+market_value),maintenance,unknown,requirements

    initial_equity,initial_maintenance,unknown,requirements=metrics()
    baseline=service.store.get_by_event_id(event_id('MARGIN_REVIEW_BASELINE'))
    if baseline:
        initial_equity=baseline.payload['account_equity']
        initial_maintenance=baseline.payload['maintenance_required']
    else:
        record('MARGIN_REVIEW_BASELINE',{'account_equity':initial_equity,
            'maintenance_required':initial_maintenance,'unknown_marks':unknown})

    closed=[]
    # Recover exact durable close evidence; no snapshot or retry invents a fill.
    cursor=0
    prefix=f'margin-close:{review_id}:'
    while True:
        batch=service.store.get_events(event_type=EventType.POSITION_UPDATED,since_id=cursor,limit=500)
        if not batch:break
        for cursor,envelope in batch:
            p=envelope.payload
            if (p.get('strategy_id')==strategy_id and p.get('risk_exit') and
                str(p.get('order_id','')).startswith(prefix)):
                closed.append({'bucket':p['bucket'],'symbol':envelope.aggregate_id,
                    'order_id':p['order_id'],'event_id':envelope.event_id})

    equity,maintenance,unknown,requirements=metrics()
    unresolved=[]
    if equity is not None and equity<maintenance:
        # Deterministic largest-maintenance-first reductions; no new exposure.
        for _,b,symbol,pos,spec in sorted(requirements,key=lambda p:(-p[0],p[1].value,p[2])):
            equity,maintenance,unknown,_=metrics()
            if equity is None or equity>=maintenance:
                break
            quote=quotes.get((b,symbol))
            key=f'margin-required:{review_id}:{pos.position_id}:{b.value}'
            service._record(NormalizedAccountingEvent(event_type='LIQUIDATION_REQUIRED',
                aggregate_id=symbol,timestamp=now,idempotency_key=key,
                delta=AccountingDelta(idempotency_key=key,aggregate_id=symbol,
                    event_type='LIQUIDATION_REQUIRED',timestamp=now,
                    metadata={'total_account_equity':equity,'maintenance_margin_required':maintenance}),
                payload={'review_id':review_id,'position_id':pos.position_id,
                    'equity':equity,'maintenance_margin_required':maintenance,
                    'execution_claim':'NONE_SIMULATION_FLAG_ONLY','paper_only':True,
                    'broker_connected':False}),strategy_id,b,spec)
            if not quote_valid(quote,spec):
                unresolved.append({'bucket':b.value,'symbol':symbol,'reason':'UNRESOLVED_EXECUTABLE_QUOTE'})
                continue
            side=OrderSide.SELL if pos.quantity>0 else OrderSide.BUY
            oid=f'{prefix}{pos.position_id}:{b.value}'
            try:
                result=service.execute(strategy_id=strategy_id,bucket=b,spec=spec,quote=quote,
                    side=side,quantity=abs(pos.quantity),order_id=oid,now=now,risk_exit=True)
            except ValueError as exc:
                unresolved.append({'bucket':b.value,'symbol':symbol,'reason':str(exc)})
                continue
            if not result.success:
                unresolved.append({'bucket':b.value,'symbol':symbol,'reason':result.rejection_reason})
                continue
            scoped=f'{strategy_id}:{b.value}:{result.event.idempotency_key}'
            closed.append({'bucket':b.value,'symbol':symbol,'order_id':oid,
                'event_id':str(uuid5(NAMESPACE_URL,f'paper-derivative:{scoped}'))})

    final_equity,final_maintenance,unknown,_=metrics()
    status=('UNRESOLVED_ACCOUNT_NAV' if final_equity is None else
        'PAPER_ACCOUNT_DEFICIT' if final_equity<0 and final_maintenance==0 else
        'UNRESOLVED_MARGIN_EXECUTION' if final_equity<final_maintenance else
        'PAPER_MARGIN_CURED' if closed else 'PAPER_MARGIN_HEALTHY')
    result={'status':status,'strategy_id':strategy_id,'review_id':review_id,
        'initial_account_equity':initial_equity,'initial_maintenance_required':initial_maintenance,
        'final_account_equity':final_equity,'final_maintenance_required':final_maintenance,
        'closed_positions':closed,'unresolved':unresolved,'unknown_marks':unknown,
        'is_fixture':True,'paper_only':True,'broker_connected':False,
        'production_adapter_accepted':False,'cash_pool_count':1}
    record('MARGIN_REVIEW_COMPLETED',{'result':result})
    return result
