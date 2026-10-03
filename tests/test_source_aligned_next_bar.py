"""Frozen requirements tests. Constructed TEST_ONLY data, not live acceptance."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from cio_market_lab.domain.models import Bar, CIODecisionPacket, Order, OrderSide, OrderStatus
from cio_market_lab.engine.execution import ExecutionCostConfig

NOW = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


def make_bar(**changes):
    data = dict(symbol='MSFT', timestamp=NOW+timedelta(minutes=1),
                observed_at=NOW+timedelta(minutes=2), open=101, high=103,
                low=99, close=102, volume=1000, source='TEST_ONLY',
                is_fixture=True, quality='TEST_ONLY')
    data.update(changes)
    return Bar(**data)


def make_order(**changes):
    data = dict(order_id='TEST_ONLY_order', symbol='MSFT', market='US',
                currency='USD', bucket='swing', side='BUY', order_type='MARKET',
                quantity=2, origin='MAIN_CIO', reason='TEST_ONLY', created_at=NOW)
    data.update(changes)
    return Order(**data)


def resolver():
    from cio_market_lab.engine.source_aligned_execution import resolve_next_bar_open
    return resolve_next_bar_open


def test_positive_bar_is_explicit_simulation_not_invented_book():
    result=resolver()(make_bar(),make_order(),ExecutionCostConfig(),
                      now=NOW+timedelta(minutes=2),decision_at=NOW,allow_fixture=True)
    assert result and result.timestamp==NOW+timedelta(minutes=1)
    assert result.base_price==101 and result.effective_price==pytest.approx(101.0505)
    assert result.quote_evidence is None and result.verification=='BAR_NEXT_OPEN_TEST_ONLY'
    assert result.assumptions['source_bar']['open']==101
    assert result.assumptions['timing_assumption']=='next_bar_open_simulation_not_bbo'


@pytest.mark.parametrize('change',[
    {'timestamp':NOW}, {'timestamp':NOW-timedelta(seconds=1)},
    {'timestamp':NOW+timedelta(minutes=3)}, {'is_stale':True},
    {'is_synthetic':True}, {'volume':0}, {'volume':1}, {'open':0},
    {'symbol':'AAPL'}, {'high':100}, {'observed_at':NOW},
    {'delay_seconds':301},
])
def test_unsupported_bar_never_becomes_executable(change):
    assert resolver()(make_bar(**change),make_order(),ExecutionCostConfig(),
                      now=NOW+timedelta(minutes=2),decision_at=NOW,allow_fixture=True) is None


def test_fixture_disabled_and_future_decision_are_not_live():
    assert resolver()(make_bar(),make_order(),ExecutionCostConfig(),
                      now=NOW+timedelta(minutes=2),decision_at=NOW,allow_fixture=False) is None
    assert resolver()(make_bar(),make_order(),ExecutionCostConfig(),
                      now=NOW+timedelta(minutes=2),decision_at=NOW+timedelta(minutes=2),allow_fixture=True) is None


def test_sell_slippage_adverse_and_source_volume_not_reusable_capacity():
    r=resolver()(make_bar(),make_order(side='SELL'),ExecutionCostConfig(),
                 now=NOW+timedelta(minutes=2),decision_at=NOW,allow_fixture=True)
    assert r.effective_price==pytest.approx(100.9495)
    assert r.assumptions['liquidity_assumption']=='full_quantity_within_reported_bar_volume_not_orderbook_depth'


def runner_harness(tmp_path, clock, data):
    from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner
    from cio_market_lab.engine.paper_orders import PaperExperimentSettings, PaperOrderService
    from cio_market_lab.engine.portfolio import PortfolioManager
    from cio_market_lab.events.store import EventStore
    from cio_market_lab.data.base import MarketDataAdapter
    class Adapter(MarketDataAdapter):
        @property
        def source_name(self): return 'TEST_ONLY'
        def get_latest_bar(self,symbol): return data['bar']
        def get_bars(self,*args,**kwargs): return [data['bar']]
        def stream_bars(self,symbols): yield data['bar']
        def get_latest_quote(self,symbol):
            raise AssertionError('NEXT_BAR_OPEN must not require or invent BBO')
    pm=PortfolioManager(initial_cash_swing=0,initial_cash_intraday=0)
    svc=PaperOrderService(pm,EventStore(':memory:'),now_fn=lambda:clock['now'])
    r=AutonomousPaperRunner(tmp_path,pm,svc,Adapter(),now_fn=lambda:clock['now'],
                            require_cio_provider=False,runtime_dir=tmp_path/'runtime')
    r.allow_fixture_quotes=True
    cfg=PaperExperimentSettings(strategy_id='TEST_ONLY_native',market='US',base_currency='USD',
         reporting_currency='USD',initial_cash=1000,universe=['MSFT'],enabled=True,
         max_position_notional=500,paper_execution_model='NEXT_BAR_OPEN')
    r.configure(cfg)
    return r,pm,svc


def packet(case, now, action='BUY', **conditions):
    from cio_market_lab.engine.cio_packet import sign_cio_packet
    return sign_cio_packet(CIODecisionPacket(case_id=case,as_of=now,expiry=now+timedelta(hours=3),
        thesis='TEST_ONLY explicit next-bar simulation, not an investment recommendation',
        selected_instrument='MSFT',action=action,quantity=2,is_fixture=True,
        conditions={'paper_execution_model':'NEXT_BAR_OPEN',**conditions}),signer_id='fixture-test-signer')


def test_actual_caller_pending_restart_buy_sell_and_no_double_debit(tmp_path):
    clock={'now':NOW}; data={'bar':make_bar(timestamp=NOW-timedelta(minutes=1),observed_at=NOW)}
    r,pm,svc=runner_harness(tmp_path,clock,data)
    buy=packet('TEST_ONLY_buy',NOW)
    first=r.submit_cio_packet(buy,strategy_id='TEST_ONLY_native')
    assert first.action=='BUY_PENDING'
    assert svc.all_orders()[0].created_at==NOW
    # A real reconstruction uses durable pending order and case, no generated BUY.
    restored,rpm,rsvc=runner_harness(tmp_path,clock,data)
    assert len(rsvc.all_orders())==1
    clock['now']=NOW+timedelta(minutes=2); data['bar']=make_bar()
    d=restored.process_pending_orders()
    assert [x.action for x in d]==['BUY_FILLED']
    ledger=rpm.get_strategy_ledger('TEST_ONLY_native','swing')
    fill=ledger.fills[0]
    assert fill.currency=='USD' and fill.consumed_quote is None
    assert fill.quote_verification=='BAR_NEXT_OPEN_TEST_ONLY'
    assert ledger.cash==pytest.approx(1000-2*101.0505-1)
    assert restored.process_pending_orders()==[]
    sell=packet('TEST_ONLY_sell',clock['now'],'SELL',target_case_id='TEST_ONLY_buy')
    assert restored.submit_cio_packet(sell,strategy_id='TEST_ONLY_native').action=='SELL_PENDING'
    clock['now']=NOW+timedelta(minutes=4)
    data['bar']=make_bar(timestamp=NOW+timedelta(minutes=3),observed_at=clock['now'],open=105,high=106,close=105)
    assert restored.process_pending_orders()[0].action=='SELL_FILLED'
    assert ledger.positions['MSFT'].quantity==0
    assert ledger.cash==pytest.approx(1000-2*101.0505-1+2*104.9475-1)
    again,apm,asvc=runner_harness(tmp_path,clock,data)
    aledger=apm.get_strategy_ledger('TEST_ONLY_native','swing')
    assert aledger.cash==pytest.approx(ledger.cash) and len(aledger.fills)==2
    assert again.process_pending_orders()==[]
    assert again.learning_store.get_record('TEST_ONLY_buy').status=='CLOSED'


def test_execution_model_requires_explicit_packet_opt_in(tmp_path):
    clock={'now':NOW}; data={'bar':make_bar(timestamp=NOW-timedelta(minutes=1),observed_at=NOW)}
    r,pm,svc=runner_harness(tmp_path,clock,data)
    p=packet('TEST_ONLY_no_opt',NOW);p.conditions.pop('paper_execution_model')
    from cio_market_lab.engine.cio_packet import sign_cio_packet
    p=sign_cio_packet(p,signer_id='fixture-test-signer')
    result=r.submit_cio_packet(p,strategy_id='TEST_ONLY_native')
    assert result.terminal_status=='TERMINAL_RISK_BLOCK'
    assert 'REQUIRES_EXPLICIT_CIO_OPT_IN' in result.reason
    assert not svc.all_orders()


def test_gap_exceeding_frozen_ceiling_blocks_fill_not_cash(tmp_path):
    clock={'now':NOW};data={'bar':make_bar(timestamp=NOW-timedelta(minutes=1),observed_at=NOW)}
    r,pm,svc=runner_harness(tmp_path,clock,data)
    p=packet('TEST_ONLY_gap',NOW)
    r.submit_cio_packet(p,strategy_id='TEST_ONLY_native')
    clock['now']=NOW+timedelta(minutes=2)
    data['bar']=make_bar(open=500,high=501,low=499,close=500)
    d=r.process_pending_orders()[0]
    assert d.terminal_status=='TERMINAL_RISK_BLOCK'
    assert pm.get_strategy_ledger('TEST_ONLY_native','swing').cash==1000
    assert not pm.get_strategy_ledger('TEST_ONLY_native','swing').fills




def test_constructed_last_sale_parser_never_invents_bid_ask():
    from cio_market_lab.data.cnbc import parse_nasdaq_last_sale
    observed=datetime(2026,9,28,20,0,15,tzinfo=timezone.utc)
    payload={"FormattedQuoteResult":{"FormattedQuote":[{"symbol":"NVDA","last":"228.86","last_time":"2026-09-28T16:00:00.000-0400","source":"Last NASDAQ LS, VOL From CTA","realTime":"true"}]}}
    quote=parse_nasdaq_last_sale("NVDA",payload,observed)
    assert quote.last_price == 228.86 and quote.bid is None and quote.ask is None
    assert quote.bid_size == quote.ask_size == 0
    assert quote.source == "cnbc_nasdaq_last_sale"
