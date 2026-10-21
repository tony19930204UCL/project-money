"""Deterministic fixtures only. Never counted as forward market observations."""
from datetime import datetime, timezone
import copy
import json
import math
import pytest
import exchange_calendars as xc
from cio_market_lab.engine import paper_dual_speed as d
from cio_market_lab.engine import paper_autonomy as a

@pytest.fixture
def panel():
    dates=xc.get_calendar('XNYS').sessions_in_range('2023-01-01','2026-10-19')
    return [{'date':v.date().isoformat(),'bars':{s:{'open':100+i*.05,'close':100+i*.05+.01,
       'high':101+i*.05,'low':99+i*.05,'volume':10000.,'dividend':0.} for s in d.UNIVERSE}} for i,v in enumerate(dates)]

def test_no_lookahead_same_day_and_future(panel):
    w=d.weights(panel,700,'SWING');other=copy.deepcopy(panel)
    for row in other[700:]:
        for bar in row['bars'].values():bar['close']=1000000
    assert d.weights(other,700,'SWING')==w

def test_missing_universe_rejected(panel):
    with pytest.raises(ValueError,match='INCOMPLETE_UNIVERSE'):d.panel_from_downloads([])

def test_cash_is_valid_no_trade(panel):
    start=next(i for i,r in enumerate(panel) if r['date']>='2025-01-01')
    r=d.simulate(panel,start,'CASH',d.Costs())
    assert r['trades']==0 and r['net_return']==0 and r['terminal_liquidation_nav']==10000

def test_closing_costs_are_in_all_reported_cost_fields(panel):
    start=next(i for i,r in enumerate(panel) if r['date']>='2025-01-01')
    r=d.simulate(panel,start,'SPY',d.Costs())
    assert r['closing_costs']['fees']>0 and r['closing_costs']['sell_levies']>0
    assert r['fees']>r['closing_costs']['fees']
    assert r['slippage']>r['closing_costs']['slippage']

def test_double_cost_is_not_free_alpha(panel):
    start=next(i for i,r in enumerate(panel) if r['date']>='2025-01-01')
    base=d.simulate(panel,start,'SWING',d.Costs())
    stress=d.simulate(panel,start,'SWING',d.Costs(commission_rate=.001,minimum_fee=2,slippage_bps=10,sell_levy_rate=.0002))
    assert stress['terminal_liquidation_nav']<=base['terminal_liquidation_nav']

def test_dividend_net_withholding_once():
    b=d.new_book();b['positions']={'SPY':10};b['cash']-=1000
    bar={'SPY':{'open':100,'close':100,'dividend':1.}}
    r=d.apply_day(b,bar,None,d.Costs());assert math.isclose(b['net_dividends'],7) and math.isclose(r['nav'],10007)

def test_no_stale_preopen_plan(tmp_path,panel):
    with pytest.raises(ValueError,match='STALE_PREOPEN'):d.slow(tmp_path,datetime(2026,10,20,12,tzinfo=timezone.utc),panel[:-1])
    assert not (tmp_path/'forward/plans/2026-10-20.json').exists()

def test_fake_verified_boolean_and_missing_receipt_is_not_authority(tmp_path,panel):
    d.dump(tmp_path/'main_policy.json',{'readback_verified':True,'source_receipt':'/not/real','swing_cap':.2})
    d.slow(tmp_path,datetime(2026,10,20,12,tzinfo=timezone.utc),panel)
    p=json.loads((tmp_path/'forward/plans/2026-10-20.json').read_text());assert p['main_authority_verified'] is False

def test_no_backfill_postclose_and_no_count_from_history(tmp_path,panel):
    r=d.slow(tmp_path,datetime(2026,10,20,21,tzinfo=timezone.utc),panel)
    assert r['full_forward_sessions']==0 and r['eligible_C0052_pairs']==0

def test_forward_idempotence_and_books_crash_recovery(tmp_path,panel):
    pre=datetime(2026,10,20,12,tzinfo=timezone.utc)
    d.slow(tmp_path,pre,panel)
    new=copy.deepcopy(panel[-1]);new['date']='2026-10-20';both=panel+[new]
    post=datetime(2026,10,20,21,tzinfo=timezone.utc)
    one=d.slow(tmp_path,post,both);daily=json.loads((tmp_path/'forward/daily/2026-10-20.json').read_text())
    assert one['full_forward_sessions']==1 and one['eligible_C0052_pairs']==0
    d.dump(tmp_path/'forward/books.json',{'THIS_IS_A_CRASHED_AUXILIARY_FILE':True})
    two=d.slow(tmp_path,post,both)
    assert two['full_forward_sessions']==1 and not two['finalized_now']
    assert json.loads((tmp_path/'forward/books.json').read_text())==daily['post_books']
    assert daily['post_books']['CORE'] is not daily['post_books']['SWING']

def test_next_day_is_not_accidentally_daily_rebalanced(tmp_path,panel):
    d.slow(tmp_path,datetime(2026,10,20,12,tzinfo=timezone.utc),panel)
    new=copy.deepcopy(panel[-1]);new['date']='2026-10-20';both=panel+[new]
    d.slow(tmp_path,datetime(2026,10,20,21,tzinfo=timezone.utc),both)
    d.slow(tmp_path,datetime(2026,10,21,12,tzinfo=timezone.utc),both)
    p=json.loads((tmp_path/'forward/plans/2026-10-21.json').read_text())
    assert p['rebalance'] is False and all(v is None for v in p['arms'].values())

def test_holiday_does_not_create_plan(tmp_path,panel):
    d.slow(tmp_path,datetime(2026,12,25,12,tzinfo=timezone.utc),panel)
    assert not (tmp_path/'forward/plans/2026-12-25.json').exists()

def test_real_us_early_close_uses_calendar():
    before=a.market_state(datetime(2026,11,27,17,tzinfo=timezone.utc))
    after=a.market_state(datetime(2026,11,27,18,6,tzinfo=timezone.utc))
    assert before['us_open'] and not after['us_open'] and after['last_completed']=='2026-11-27'

def test_unauthenticated_policy_rejects_continuation(tmp_path):
    d.dump(tmp_path/'main_policy.json',{'readback_verified':True})
    with pytest.raises(ValueError,match='AUTHENTICATED_MAIN'):a.next_experiment(tmp_path)

def test_risk_matched_signal_is_causal(panel):
    before=d.weights(panel,700,'VOL_SPY');other=copy.deepcopy(panel)
    for row in other[700:]:row['bars']['SPY']['close']*=100
    assert d.weights(other,700,'VOL_SPY')==before
    assert sum(before.values())<=1
