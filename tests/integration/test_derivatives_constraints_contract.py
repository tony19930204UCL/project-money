from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest

from cio_market_lab.domain.models import DecisionScope, OrderSide
from cio_market_lab.engine.derivative_lifecycle import PaperDerivativeLifecycle
from cio_market_lab.engine.futures_roll import PaperFuturesRollService
from cio_market_lab.engine.paper_derivatives import ContractSpec, DerivativeQuote, PaperDerivativesEngine
from cio_market_lab.engine.paper_orders import PaperExperimentSettings, PaperOrderService
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.events.store import EventStore

T0=datetime(2026,10,5,1,0,tzinfo=timezone.utc)
SID="TEST_ONLY_DERIVATIVE_CONSTRAINTS"
BUCKET=DecisionScope.SWING

def option(symbol="TXO-202610-C20000", expiry=None):
    return ContractSpec(symbol=symbol,underlying_symbol="TX",instrument_type="OPTION",
        option_right="CALL",strike=20000,expiry=expiry or T0+timedelta(days=10),multiplier=50,
        tick_size=1,currency="TWD",pre_expiry_close_lead_seconds=3600)

def future(symbol="TXF-202610", expiry=None, multiplier=200):
    return ContractSpec(symbol=symbol,underlying_symbol="TX",instrument_type="FUTURE",
        expiry=expiry or T0+timedelta(days=10),multiplier=multiplier,tick_size=1,currency="TWD",
        initial_margin_per_contract=100000,maintenance_margin_per_contract=80000)

def q(spec, when=T0, bid=100, ask=101, **prov):
    base={"authority":"EXPLICIT_OFFLINE_FIXTURE","contract_authorized":True,"session_open":True,
          "bid_size":5,"ask_size":5,"strike":spec.strike,
          "contract_right":spec.option_right.value if spec.option_right else None,
          "expiry":spec.expiry.isoformat() if spec.expiry else None}
    base.update(prov)
    return DerivativeQuote(symbol=spec.symbol,timestamp=when,observed_at=when,bid=bid,ask=ask,
        last_price=(bid+ask)/2,is_fixture=True,source="TEST_ONLY_CONTRACT_BOOK",provenance=base)

def setup(tmp_path, specs):
    store=EventStore(tmp_path/"events.sqlite")
    pm=PortfolioManager()
    pm.register_strategy(SID,1_000_000,currency="TWD",unified_cash=True)
    po=PaperOrderService(pm,store)
    po.experiments[SID]=PaperExperimentSettings(strategy_id=SID,enabled=True,initial_cash=1_000_000,
        base_currency="TWD",universe=[s.symbol for s in specs],max_position_notional=10_000_000,
        allowed_buckets=[DecisionScope.SWING,DecisionScope.INTRADAY])
    return PaperDerivativeLifecycle(pm,po,fixture_mode=True),pm,store

def test_expiry_aware_option_chain_greeks_and_fail_closed():
    engine=PaperDerivativesEngine()
    spec=option()
    snap=engine.option_chain_snapshot(spec,q(spec),underlying_price=20100,implied_volatility=0.22,
                                      risk_free_rate=0.01,as_of=T0)
    assert snap["status"]=="AVAILABLE_TEST_ONLY"
    assert snap["seconds_to_expiry"]>0
    assert snap["multiplier"]==50 and snap["tick_size"]==1 and snap["currency"]=="TWD"
    assert set(snap["greeks"])=={"delta","gamma","vega","theta_per_day"}
    assert 0<snap["greeks"]["delta"]<1 and snap["greeks"]["gamma"]>0 and snap["greeks"]["vega"]>0
    assert snap["execution_enabled"] is False and snap["live_approved"] is False
    assert engine.option_chain_snapshot(spec,None,underlying_price=20100,implied_volatility=.22,as_of=T0)["reason"]=="OPTION_CHAIN_MISSING"
    stale=q(spec,T0-timedelta(minutes=10)).model_copy(update={"is_stale":True})
    assert engine.option_chain_snapshot(spec,stale,underlying_price=20100,implied_volatility=.22,as_of=T0)["reason"]=="QUOTE_STALE"
    assert engine.option_chain_snapshot(spec,q(spec),underlying_price=20100,implied_volatility=None,as_of=T0)["reason"]=="IMPLIED_VOLATILITY_MISSING"
    wrong=q(spec).model_copy(update={"symbol":"TXO-WRONG"})
    assert engine.option_chain_snapshot(spec,wrong,underlying_price=20100,implied_volatility=.22,as_of=T0)["reason"]=="CONTRACT_TARGET_MISMATCH"
    expired=option(expiry=T0-timedelta(seconds=1))
    assert engine.option_chain_snapshot(expired,q(expired),underlying_price=20100,implied_volatility=.22,as_of=T0)["reason"]=="CONTRACT_EXPIRED"

@pytest.mark.parametrize("bad_meta",[
    {"strike":"not-a-number"},
    {"strike":{"bad":1}},
    {"strike":float("nan")},
    {"expiry":"not-a-date"},
    {"expiry":{"bad":1}},
    {"contract_right":"INVALID"},
    {"contract_right":["CALL"]},
])
def test_malformed_contract_metadata_is_bounded_and_never_mutates_ledger(tmp_path,bad_meta):
    spec=option()
    engine=PaperDerivativesEngine()
    malformed=q(spec).model_copy(update={"provenance":{**q(spec).provenance,**bad_meta}})
    snap=engine.option_chain_snapshot(spec,malformed,underlying_price=20100,
        implied_volatility=.22,as_of=T0)
    assert snap["status"]=="UNAVAILABLE"
    assert snap["reason"]=="CONTRACT_TARGET_MISMATCH"

    svc,pm,store=setup(tmp_path,[spec])
    before=pm.get_strategy_ledger(SID,BUCKET).cash
    result=svc.execute(strategy_id=SID,bucket=BUCKET,spec=spec,quote=malformed,
        side=OrderSide.BUY,quantity=1,order_id="malformed",now=T0)
    assert result.success is False
    assert result.rejection_reason=="CONTRACT_TARGET_MISMATCH"
    ledger=pm.get_strategy_ledger(SID,BUCKET)
    assert ledger.cash==before and not ledger.positions and store.count()==0


def test_option_valuation_can_remain_available_when_session_closed_but_execution_refuses(tmp_path):
    spec=option()
    closed=q(spec,session_open=False)
    snap=PaperDerivativesEngine().option_chain_snapshot(spec,closed,
        underlying_price=20100,implied_volatility=.22,as_of=T0)
    assert snap["status"]=="AVAILABLE_TEST_ONLY"
    assert snap["execution_enabled"] is False

    svc,pm,store=setup(tmp_path,[spec])
    before=pm.get_strategy_ledger(SID,BUCKET).cash
    result=svc.execute(strategy_id=SID,bucket=BUCKET,spec=spec,quote=closed,
        side=OrderSide.BUY,quantity=1,order_id="closed-session",now=T0)
    assert not result.success and result.rejection_reason=="EXECUTION_SESSION_UNAUTHORIZED"
    assert pm.get_strategy_ledger(SID,BUCKET).cash==before
    assert store.count()==0


def test_option_chain_naive_as_of_is_explicitly_unavailable():
    spec=option()
    result=PaperDerivativesEngine().option_chain_snapshot(spec,q(spec),
        underlying_price=20100,implied_volatility=.22,
        as_of=datetime(2026,10,5,1,0))
    assert result["status"]=="UNAVAILABLE"
    assert result["reason"]=="AS_OF_TIMEZONE_REQUIRED"


@pytest.mark.parametrize("scope",[DecisionScope.SWING,DecisionScope.INTRADAY])
def test_concrete_per_scope_option_fixture_executes_only_exact_contract(tmp_path,scope):
    spec=option()
    svc,pm,_=setup(tmp_path,[spec])
    opened=svc.execute(strategy_id=SID,bucket=scope,spec=spec,quote=q(spec),side=OrderSide.BUY,
                       quantity=1,order_id=f"open-{scope.value}",now=T0)
    assert opened.success and opened.fill_price==101
    ledger=pm.get_strategy_ledger(SID,scope)
    assert ledger.positions[spec.symbol].quantity==1
    assert ledger.positions[spec.symbol].assumptions["contract_spec"]["multiplier"]==50

def test_calendar_bbo_capacity_and_exact_target_authority_refusal(tmp_path):
    spec=option()
    svc,pm,store=setup(tmp_path,[spec])
    before=pm.get_strategy_ledger(SID,BUCKET).cash
    cases=[
        (q(spec,session_open=False),"EXECUTION_SESSION_UNAUTHORIZED"),
        (q(spec,ask_size=0),"DISPLAYED_CAPACITY_INSUFFICIENT"),
        (q(spec,contract_authorized=False),"CONTRACT_TARGET_MISMATCH"),
        (q(spec,expiry=(spec.expiry+timedelta(days=1)).isoformat()),"CONTRACT_TARGET_MISMATCH"),
        (q(spec,strike=19900),"CONTRACT_TARGET_MISMATCH"),
        (q(spec,contract_right="PUT"),"CONTRACT_TARGET_MISMATCH"),
    ]
    for i,(quote,reason) in enumerate(cases):
        r=svc.execute(strategy_id=SID,bucket=BUCKET,spec=spec,quote=quote,side=OrderSide.BUY,
                      quantity=1,order_id=f"reject-{i}",now=T0)
        assert not r.success and r.rejection_reason==reason
    with pytest.raises(ValueError,match="NO_EXECUTABLE_CONTRACT_QUOTE"):
        svc.execute(strategy_id=SID,bucket=BUCKET,spec=spec,
            quote=q(spec).model_copy(update={"bid":None}),side=OrderSide.BUY,
            quantity=1,order_id="missing-bbo",now=T0)
    expired=option(symbol=spec.symbol,expiry=T0-timedelta(seconds=1))
    expired_result=svc.execute(strategy_id=SID,bucket=BUCKET,spec=expired,quote=q(expired),
        side=OrderSide.BUY,quantity=1,order_id="expired",now=T0)
    assert not expired_result.success and expired_result.rejection_reason=="CONTRACT_EXPIRED"
    ledger=pm.get_strategy_ledger(SID,BUCKET)
    assert ledger.cash==before and not ledger.positions
    assert store.count()==0

def test_futures_margin_reserve_multiplier_and_roll_constraints(tmp_path):
    old=future("TXF-202610",T0+timedelta(days=2),multiplier=200)
    new=future("TXF-202611",T0+timedelta(days=32),multiplier=200)
    svc,pm,_=setup(tmp_path,[old,new])
    opened=svc.execute(strategy_id=SID,bucket=BUCKET,spec=old,quote=q(old,bid=999,ask=1001),
        side=OrderSide.BUY,quantity=1,order_id="fut-open",now=T0)
    assert opened.success
    pos=pm.get_strategy_ledger(SID,BUCKET).positions[old.symbol]
    d=pos.assumptions["derivative_position"]
    assert d["multiplier"]==200 and d["margin_locked"]==100000
    cash_before=pm.get_strategy_ledger(SID,BUCKET).cash
    assert svc.settle_futures_daily(strategy_id=SID,bucket=BUCKET,symbol=old.symbol,
        settlement_price=1020,settlement_date="2026-10-05",
        quote=q(old,T0+timedelta(hours=1),1019,1021),now=T0+timedelta(hours=1))
    cash_after=pm.get_strategy_ledger(SID,BUCKET).cash
    assert cash_after==pytest.approx(cash_before+(1020-1001)*200)
    assert not svc.settle_futures_daily(strategy_id=SID,bucket=BUCKET,symbol=old.symbol,
        settlement_price=1020,settlement_date="2026-10-05",
        quote=q(old,T0+timedelta(hours=1),1019,1021),now=T0+timedelta(hours=1))
    assert pm.get_strategy_ledger(SID,BUCKET).cash==pytest.approx(cash_after)
    roller=PaperFuturesRollService(svc)
    receipt=roller.roll(strategy_id=SID,bucket=BUCKET,old_spec=old,new_spec=new,
        close_quote=q(old,T0+timedelta(hours=2),1029,1031),
        open_quote=q(new,T0+timedelta(hours=2),1039,1041),quantity=1,roll_id="ROLL-1",
        now=T0+timedelta(hours=2))
    assert receipt["status"]=="PAPER_ROLL_COMPLETED"
    assert receipt["expiry_calendar"]["old_expiry"]==old.expiry.isoformat()
    assert receipt["expiry_calendar"]["new_expiry"]==new.expiry.isoformat()
    incompatible=future("TXF-BAD",T0+timedelta(days=40),multiplier=100)
    with pytest.raises(ValueError,match="ROLL_COMPATIBLE_EXPLICIT_FUTURE_SERIES_REQUIRED"):
        roller.roll(strategy_id=SID,bucket=BUCKET,old_spec=new,new_spec=incompatible,
            close_quote=q(new,T0+timedelta(hours=3),1039,1041),
            open_quote=q(incompatible,T0+timedelta(hours=3),1049,1051),quantity=1,
            roll_id="ROLL-BAD",now=T0+timedelta(hours=3))

def test_shared_cash_reserve_across_scopes_blocks_second_future(tmp_path):
    s1=future("TXF-SWING",T0+timedelta(days=10))
    s2=future("TXF-INTRA",T0+timedelta(days=10))
    svc,pm,_=setup(tmp_path,[s1,s2])
    # Reconfigure a deliberately small shared account to prove reserve accounting.
    pm2=PortfolioManager(); pm2.register_strategy(SID,150000,currency="TWD",unified_cash=True)
    po=PaperOrderService(pm2,svc.store)
    po.experiments[SID]=PaperExperimentSettings(strategy_id=SID,enabled=True,initial_cash=150000,
        base_currency="TWD",universe=[s1.symbol,s2.symbol],max_position_notional=10_000_000,
        allowed_buckets=[DecisionScope.SWING,DecisionScope.INTRADAY])
    svc2=PaperDerivativeLifecycle(pm2,po,fixture_mode=True)
    assert svc2.execute(strategy_id=SID,bucket=DecisionScope.SWING,spec=s1,
        quote=q(s1,bid=999,ask=1000),side=OrderSide.BUY,quantity=1,order_id="one",now=T0).success
    blocked=svc2.execute(strategy_id=SID,bucket=DecisionScope.INTRADAY,spec=s2,
        quote=q(s2,bid=999,ask=1000),side=OrderSide.BUY,quantity=1,order_id="two",now=T0)
    assert not blocked.success and blocked.rejection_reason=="MARGIN_DEFICIENCY"
