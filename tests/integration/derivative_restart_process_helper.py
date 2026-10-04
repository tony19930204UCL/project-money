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
from cio_market_lab.engine.account_margin_review import review_account_margin

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
        initial_margin_per_contract=100000,maintenance_margin_per_contract=1100000)

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
    review_receipts=[]
    cursor=0
    while True:
        batch=store.get_events(event_type=EventType.DERIVATIVE_ACCOUNT_REVIEW_RECORDED,since_id=cursor,limit=500)
        if not batch: break
        for cursor,e in batch:
            review_receipts.append({"event_id":e.event_id,"phase":e.payload.get("phase"),
                                    "review_id":e.payload.get("review_id")})
    derivative=ledger.positions.get(spec("future").symbol) if "TXF-TEST" in ledger.positions else None
    margin_locked=(derivative.assumptions.get("derivative_position",{}).get("margin_locked",0.0)
                   if derivative else 0.0)
    return {"cash":ledger.cash,"equity":ledger.equity,"margin_locked":margin_locked,
        "available_cash":ledger.cash-margin_locked,
        "positions":{k:v.model_dump(mode="json") for k,v in ledger.positions.items()},
        "event_ids":[e["event_id"] for e in events],"events":events,
        "review_receipts":review_receipts}

p=argparse.ArgumentParser()
p.add_argument("runtime");p.add_argument("kind",choices=["option","future"])
p.add_argument("action",choices=["snapshot","open","review-missing","review-close","settle","margin-missing","margin-unauthorized","margin-close","expiry-review","expiry-invalid-settlement"])
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
elif args.action=="margin-missing":
    changed=review_account_margin(svc,strategy_id=SID,quotes={},review_id="MARGIN-MISSING",
        now=T0+timedelta(hours=2))
elif args.action=="margin-unauthorized":
    bad=quote(s.symbol,T0+timedelta(hours=2),1010,1012,contract_authorized=False)
    changed=review_account_margin(svc,strategy_id=SID,quotes={(BUCKET,s.symbol):bad},
        review_id="MARGIN-UNAUTHORIZED",now=T0+timedelta(hours=2))
elif args.action=="margin-close":
    good=quote(s.symbol,T0+timedelta(hours=2,minutes=1),1010,1012)
    changed=review_account_margin(svc,strategy_id=SID,quotes={(BUCKET,s.symbol):good},
        review_id="MARGIN-CLOSE",now=T0+timedelta(hours=2,minutes=1))
elif args.action=="expiry-review":
    changed=svc.review(strategy_id=SID,bucket=BUCKET,symbol=s.symbol,now=T0+timedelta(hours=3))
elif args.action=="expiry-invalid-settlement":
    evidence={"contract_symbol":s.symbol,"underlying_symbol":s.underlying_symbol,
        "expiry":s.expiry.isoformat(),"settlement_type":"PHYSICAL","settlement_price":21000,
        "timestamp":(T0+timedelta(hours=2)).isoformat(),"observed_at":(T0+timedelta(hours=3)).isoformat(),
        "is_fixture":True,"source":"TEST_ONLY_INVALID_DELIVERY",
        "provenance":{"authority":"EXPLICIT_OFFLINE_FIXTURE","kind":"FINAL_SETTLEMENT"}}
    try:
        svc.settle_expiry_cash(strategy_id=SID,bucket=BUCKET,symbol=s.symbol,evidence=evidence,
                               now=T0+timedelta(hours=3))
        changed={"accepted":True}
    except (ValueError,TypeError) as exc:
        changed={"accepted":False,"reason":str(exc)}
out=snapshot(pm,store);out["changed"]=changed
print(json.dumps(out,sort_keys=True))
