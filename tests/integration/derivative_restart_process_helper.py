from __future__ import annotations
import argparse, json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cio_market_lab.domain.events import EventType
from cio_market_lab.domain.models import DecisionScope, OrderSide
from cio_market_lab.engine.derivative_lifecycle import PaperDerivativeLifecycle
from cio_market_lab.engine.paper_derivatives import ContractSpec, DerivativeQuote
from cio_market_lab.engine.paper_orders import PaperExperimentSettings, PaperOrderService
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.events.store import EventStore

T0=datetime(2026,10,5,1,0,tzinfo=timezone.utc)
SID="TEST_ONLY_DERIV_RESTART"
BUCKET=DecisionScope.SWING

def spec(kind):
    if kind=="option":
        return ContractSpec(symbol="TXO-TEST",underlying_symbol="TX",instrument_type="OPTION",
            option_right="CALL",strike=20000,expiry=T0+timedelta(hours=2),multiplier=50,
            tick_size=1,currency="TWD",pre_expiry_close_lead_seconds=3600)
    return ContractSpec(symbol="TXF-TEST",underlying_symbol="TX",instrument_type="FUTURE",
        expiry=T0+timedelta(days=5),multiplier=200,tick_size=1,currency="TWD",
        initial_margin_per_contract=100000,maintenance_margin_per_contract=80000)

def quote(symbol,when,bid,ask,**extra):
    provenance={"authority":"EXPLICIT_OFFLINE_FIXTURE","session_open":True,
        "contract_authorized":True,"bid_size":10,"ask_size":10,**extra}
    return DerivativeQuote(symbol=symbol,timestamp=when,observed_at=when,bid=bid,ask=ask,
        last_price=(bid+ask)/2,is_fixture=True,source="TEST_ONLY_DERIVATIVE_BOOK",provenance=provenance)

def build(runtime,kind):
    store=EventStore(Path(runtime)/"events.db")
    pm=PortfolioManager()
    pm.register_strategy(SID,1_000_000,currency="TWD",unified_cash=True)
    orders=PaperOrderService(pm,store)
    orders.experiments[SID]=PaperExperimentSettings(strategy_id=SID,enabled=True,initial_cash=1_000_000,
        base_currency="TWD",universe=[spec(kind).symbol],max_position_notional=10_000_000,
        allowed_buckets=[BUCKET])
    svc=PaperDerivativeLifecycle(pm,orders,fixture_mode=True)
    svc.replay()
    return svc,pm,store

def snapshot(pm,store):
    ledger=pm.get_strategy_ledger(SID,BUCKET)
    events=[]
    cursor=0
    while True:
        batch=store.get_events(event_type=EventType.POSITION_UPDATED,since_id=cursor,limit=500)
        if not batch: break
        for cursor,e in batch:
            events.append({"event_id":e.event_id,"type":e.payload.get("normalized_event_type"),
                           "idempotency_key":e.payload.get("idempotency_key")})
    return {"cash":ledger.cash,"equity":ledger.equity,
        "positions":{k:v.model_dump(mode="json") for k,v in ledger.positions.items()},
        "event_ids":[e["event_id"] for e in events],"events":events}

p=argparse.ArgumentParser()
p.add_argument("runtime");p.add_argument("kind",choices=["option","future"])
p.add_argument("action",choices=["snapshot","open","review-missing","review-close","settle"])
args=p.parse_args()
Path(args.runtime).mkdir(parents=True,exist_ok=True)
svc,pm,store=build(args.runtime,args.kind)
s=spec(args.kind)
changed=None
if args.action=="open":
    q=quote(s.symbol,T0,100,101) if args.kind=="option" else quote(s.symbol,T0,999,1001)
    r=svc.execute(strategy_id=SID,bucket=BUCKET,spec=s,quote=q,side=OrderSide.BUY,quantity=1,
                  order_id=f"{args.kind}-open",now=T0)
    changed={"success":r.success,"rejection_reason":r.rejection_reason}
elif args.action=="review-missing":
    changed=svc.review(strategy_id=SID,bucket=BUCKET,symbol=s.symbol,now=T0+timedelta(hours=1,minutes=30))
elif args.action=="review-close":
    changed=svc.review(strategy_id=SID,bucket=BUCKET,symbol=s.symbol,now=T0+timedelta(hours=1,minutes=30),
                       quote=quote(s.symbol,T0+timedelta(hours=1,minutes=30),120,121))
elif args.action=="settle":
    changed=svc.settle_futures_daily(strategy_id=SID,bucket=BUCKET,symbol=s.symbol,
        settlement_price=1020,settlement_date="2026-10-05",
        quote=quote(s.symbol,T0+timedelta(hours=1),1019,1021),now=T0+timedelta(hours=1))
out=snapshot(pm,store);out["changed"]=changed
print(json.dumps(out,sort_keys=True))
