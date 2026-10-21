"""PAPER-only daily sandbox and forward, bar-model shadow accounting.
No brokerage/LLM imports. A historical bar is never an authenticated execution fill.
The pre-open plan + post-close mark are the forward evidence; no backfilling plans.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, time, timedelta
import fcntl
import hashlib
import json
import math
from pathlib import Path
import statistics
from zoneinfo import ZoneInfo
import requests

VERSION = 'daily-dual-speed-v1'
UNIVERSE = ('SPY', 'QQQ', 'IEF', 'GLD')
NY = ZoneInfo('America/New_York')
ROOT = Path(__file__).resolve().parents[2] / 'data/paper_dual_speed'


def dump(path, obj):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, allow_nan=False) + '\n')
    tmp.replace(path)


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class Costs:
    commission_rate: float = .0005
    minimum_fee: float = 1.
    slippage_bps: float = 5.
    sell_levy_rate: float = .0001
    dividend_withholding: float = .30
    # Conservative modeled envelope, NOT a claim of a verified broker tariff.


def download(symbol, now):
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + symbol
    response = requests.get(url, params={'range': '5y', 'interval': '1d', 'events': 'div,splits'},
                            headers={'User-Agent': 'Mozilla/5.0'}, timeout=25)
    response.raise_for_status(); payload = response.json()
    result = payload['chart']['result'][0]
    if result['meta']['symbol'] != symbol:
        raise ValueError('SYMBOL_MISMATCH')
    # Yahoo daily OHLC may already be split-adjusted: refuse ambiguity, not double-adjust.
    if result.get('events', {}).get('splits'):
        raise ValueError('SPLIT_BASIS_REQUIRES_EXPLICIT_RECONCILIATION:' + symbol)
    q = result['indicators']['quote'][0]
    divs = {datetime.fromtimestamp(v['date'], NY).date().isoformat(): float(v['amount'])
            for v in result.get('events', {}).get('dividends', {}).values()}
    rows = {}
    from cio_market_lab.engine.paper_autonomy import market_state
    latest_complete = market_state(now)['last_completed']
    for i, stamp in enumerate(result['timestamp']):
        d = datetime.fromtimestamp(stamp, NY).date()
        if latest_complete is None or d.isoformat()>latest_complete:
            continue
        try:
            row = {k: float(q[k][i]) for k in ('open', 'high', 'low', 'close', 'volume')}
        except (ValueError, TypeError, KeyError, IndexError):
            continue
        if any(not math.isfinite(v) for v in row.values()) or min(row[k] for k in ('open', 'low', 'close')) <= 0:
            raise ValueError('INVALID_BAR:' + symbol)
        if row['low'] > min(row['open'], row['close']) or row['high'] < max(row['open'], row['close']):
            raise ValueError('INVALID_OHLC:' + symbol)
        row['dividend'] = divs.get(d.isoformat(), 0.)
        rows[d.isoformat()] = row
    return symbol, rows, {'url': url, 'observed_at': now.isoformat(), 'sha256': digest(payload),
                           'source': 'yahoo_public_daily_analysis_not_execution', 'raw': payload}


def panel_from_downloads(results):
    allrows = {s: rows for s, rows, meta in results}
    if set(allrows) != set(UNIVERSE):
        raise ValueError('INCOMPLETE_UNIVERSE')
    dates = sorted(set.intersection(*(set(r) for r in allrows.values())))
    if len(dates) < 500:
        raise ValueError('INSUFFICIENT_HISTORY')
    return [{'date': d, 'bars': {s: allrows[s][d] for s in UNIVERSE}} for d in dates]


def get_panel(root, now):
    results = list(ThreadPoolExecutor(max_workers=4).map(lambda s: download(s, now), UNIVERSE))
    panel = panel_from_downloads(results)
    key = now.strftime('%Y%m%dT%H%M%S') + '-' + digest(panel)[:12]
    folder = root / 'snapshots' / key
    for s, rows, meta in results:
        dump(folder / (s + '.raw.json'), meta)
    dump(folder / 'panel.json', panel)
    from cio_market_lab.engine.paper_autonomy import market_state
    expected=market_state(now)['last_completed']
    if panel[-1]['date']!=expected:
        dump(folder/'rejected.json',{'reason':'STALE_OR_FUTURE_COMMON_CLOSE','expected':expected,'received':panel[-1]['date']})
        raise ValueError('STALE_OR_FUTURE_COMMON_CLOSE')
    dump(folder / 'manifest.json', {'version': VERSION, 'observed_at': now.isoformat(),
         'panel_sha256': digest(panel), 'rows': len(panel), 'last_session': panel[-1]['date'],
         'selection_caveat': 'Four named ETFs selected prospectively today; not a point-in-time equity universe and no unbiased-universe alpha claim.',
         'price_basis': 'raw daily OHLC plus explicit net dividends; splits rejected'})
    return panel, folder


def weights(panel, i, strategy, lookback=63, vol_target=.12):
    """All signals use observations strictly BEFORE execution-day index i."""
    if strategy == 'CASH': return {}
    if strategy in ('SPY', 'QQQ'): return {strategy: 1.}
    if strategy in ('VOL_SPY', 'VOL_QQQ'):
        s = strategy[4:]
        if i < 23: return {}
        prices = [p['bars'][s]['close'] for p in panel[i-22:i]]
        rs = [b/a-1 for a,b in zip(prices,prices[1:])]
        vol = statistics.stdev(rs)*math.sqrt(252)
        return {s:min(1.,vol_target/vol)} if vol>0 else {s:1.}
    if i < max(200, lookback + 1): return {}
    scores = {}
    for s in UNIVERSE:
        closes = [p['bars'][s]['close'] for p in panel[i-200:i]]
        momentum = closes[-1] / panel[i-1-lookback]['bars'][s]['close'] - 1.
        if closes[-1] > statistics.mean(closes) and momentum > 0:
            scores[s] = momentum
    if not scores: return {}
    s = max(scores, key=lambda x: (scores[x], x))
    prices = [p['bars'][s]['close'] for p in panel[i-22:i]]
    rs = [b / a - 1 for a, b in zip(prices, prices[1:])]
    vol = statistics.stdev(rs) * math.sqrt(252) if len(rs) > 2 else float('inf')
    return {s: min(1., vol_target / vol)} if vol > 0 and math.isfinite(vol) else {}


def new_book(capital=10000.):
    return {'cash': capital, 'positions': {}, 'fees': 0., 'slippage': 0., 'levies': 0.,
            'net_dividends': 0., 'trades': 0, 'initial': capital}


def nav(book, bars):
    return book['cash'] + sum(q * bars[s]['close'] for s, q in book['positions'].items())


def apply_day(book, bars, target, costs=Costs(), rebalance=True):
    """Fractional-share, next-open costed BAR MODEL; never claims a broker fill."""
    if target is None:target={};rebalance=False
    if any(not math.isfinite(w) or w < 0 for w in target.values()) or sum(target.values()) > 1.00000001:
        raise ValueError('INVALID_WEIGHTS')
    old = dict(book['positions']); trades = []
    for s, qty in old.items():
        credit = qty * bars[s].get('dividend', 0) * (1-costs.dividend_withholding)
        book['cash'] += credit; book['net_dividends'] += credit
    if rebalance:
        value = book['cash'] + sum(q * bars[s]['open'] for s, q in old.items())
        desired = {s: value * w / bars[s]['open'] for s, w in target.items()}
        for s in sorted(set(old) | set(desired), key=lambda s: desired.get(s,0)-old.get(s,0)):
            delta = desired.get(s, 0.) - book['positions'].get(s, 0.)
            if abs(delta * bars[s]['open']) < 10: continue
            side = 'BUY' if delta > 0 else 'SELL'; raw = bars[s]['open']
            price = raw * (1 + costs.slippage_bps/10000 * (1 if delta>0 else -1))
            if delta > 0:
                # Reserve commission; no leverage, no negative cash due to fee minimum.
                qty = min(delta, max(0., (book['cash']-costs.minimum_fee)/(price*(1+costs.commission_rate))))
            else: qty = min(-delta, book['positions'].get(s, 0.))
            if qty <= 0: continue
            notion = qty*price; fee=max(costs.minimum_fee, notion*costs.commission_rate)
            levy=notion*costs.sell_levy_rate if side=='SELL' else 0.
            book['cash'] += (-notion-fee if side=='BUY' else notion-fee-levy)
            book['positions'][s] = book['positions'].get(s,0.) + (qty if side=='BUY' else -qty)
            if abs(book['positions'][s]) < 1e-10: book['positions'].pop(s)
            book['fees'] += fee;book['levies'] += levy;book['slippage'] += qty*abs(price-raw);book['trades'] += 1
            trades.append({'symbol':s,'side':side,'quantity':qty,'bar_model_price':price,'fee':fee,'levy':levy,'not_execution_fill':True})
    if book['cash'] < -1e-7 or any(q < -1e-7 for q in book['positions'].values()):
        raise AssertionError('NEGATIVE_CASH_OR_SHORT')
    return {'nav': nav(book,bars), 'cash':book['cash'], 'trades':trades}


def simulate(panel, start, strategy, costs=Costs(), lookback=63, vol_target=.12, parking='CASH'):
    if parking not in ('CASH', 'SPY', 'QQQ'):
        raise ValueError('INVALID_PARKING')
    book=new_book(); curve=[]; turnover=[]; peak=book['initial']; dd=0.
    # Monthly cadence is frozen before reading the holdout results.
    for i in range(start, len(panel)):
        rebal = (i-start)%21==0
        target=weights(panel,i,strategy,lookback=lookback,vol_target=vol_target)
        if strategy == 'SWING' and parking != 'CASH':
            # Residual cash is an equity-risk allocation, NOT a risk-free yield.
            target=dict(target)
            target[parking]=target.get(parking,0.)+max(0.,1.-sum(target.values()))
        row=apply_day(book,panel[i]['bars'],target,costs,rebal)
        peak=max(peak,row['nav']);dd=max(dd,1-row['nav']/peak)
        curve.append({'date':panel[i]['date'],'nav':row['nav'],'cash':row['cash']})
        if row['trades']: turnover.append({'date':panel[i]['date'],'trades':row['trades']})
    # Liquidate at final close in the model, including closing fee/slippage/levy.
    terminal = book['cash']
    closing_costs={'fees':0.,'slippage':0.,'sell_levies':0.}
    for s,qty in book['positions'].items():
        raw=panel[-1]['bars'][s]['close'];price=raw*(1-costs.slippage_bps/10000)
        n=qty*price;fee=max(costs.minimum_fee,n*costs.commission_rate);levy=n*costs.sell_levy_rate
        terminal+=n-fee-levy
        closing_costs['fees']+=fee;closing_costs['sell_levies']+=levy;closing_costs['slippage']+=qty*(raw-price)
    dd=max(dd,1-terminal/peak)
    return {'strategy':strategy,'version':VERSION,'start':panel[start]['date'],'end':panel[-1]['date'],
            'sessions':len(curve),'terminal_liquidation_nav':terminal,'net_return':terminal/book['initial']-1,
            'max_mark_to_market_drawdown':dd,'fees':book['fees']+closing_costs['fees'],'sell_levies':book['levies']+closing_costs['sell_levies'],
            'slippage':book['slippage']+closing_costs['slippage'],'closing_costs':closing_costs,'net_dividends':book['net_dividends'],'trades':book['trades'],
            'curve':curve,'trade_journal':turnover,'fractional_bar_model':True}


def sandbox(root=ROOT, now=None, panel=None):
    root=Path(root);root.mkdir(parents=True,exist_ok=True);now=now or datetime.now(timezone.utc)
    if panel is None: panel,folder=get_panel(root,now)
    else: folder=None
    start=next(i for i,p in enumerate(panel) if p['date']>='2025-01-01')
    if start<200: raise ValueError('NO_PREHOLDOUT_WARMUP')
    runid=now.strftime('%Y%m%dT%H%M%S')+'-'+digest(panel)[:12];run=root/'experiments'/runid
    outputs={s:simulate(panel,start,s) for s in ['SPY','QQQ','CASH','SWING']}
    stress=simulate(panel,start,'SWING',Costs(commission_rate=.001, minimum_fee=2, slippage_bps=10, sell_levy_rate=.0002))
    for name,result in outputs.items(): dump(run/(name+'.json'),result)
    dump(run/'SWING_stress.json',stress)
    swing=outputs['SWING'];alpha=swing['net_return']-outputs['SPY']['net_return']
    # This first selected-universe historical test is discovery, not proof of deployable alpha.
    report={'paper_only':True,'version':VERSION,'observed_at':now.isoformat(),'snapshot':str(folder),
            'panel_sha256':digest(panel),'first_holdout':'2025-01-01','parameter_selection':'63D,200DMA,21-session rebalance,12% vol target fixed before first test; no OOS optimization',
            'cost_assumptions':asdict(Costs()),'actual_broker_tariff_verified':False,
            'summary':{k:{a:v for a,v in o.items() if a not in ('curve','trade_journal')} for k,o in outputs.items()},
            'swing_excess_vs_SPY':alpha,'swing_excess_vs_QQQ':swing['net_return']-outputs['QQQ']['net_return'],
            'stress_net_return':stress['net_return'],'promotion':'SHADOW_ONLY',
            'promotion_reason':'First selected-universe historical discovery is not independent validation; no authenticated intraday fills or exact broker tariffs. Shadow observation allowed, not validated-alpha claim.',
            'runtime_artifact':str(run),'performance_improvement_proven':False,'sleep_free':True}
    dump(run/'result.json',report);dump(root/'sandbox_latest.json',report)
    lesson={'lesson_id':'daily-cost-comparison-'+digest(report)[:16], 'source':str(run/'result.json'),
            'takeaway':'Measured same-capital, same-period net comparison; do not promote on gross returns or profitable cash PnL alone.',
            'facts':{'net_excess_vs_SPY':alpha,'net_excess_vs_QQQ':report['swing_excess_vs_QQQ'],'stress_net_return':stress['net_return']},
            'scope':'promotion/allocation of daily SWING in paper only','not_proof_of_future_alpha':True}
    dump(run/'lesson.json',lesson);dump(root/'latest_lesson.json',lesson)
    return report


def slow(root=ROOT, now=None, panel=None):
    root=Path(root);root.mkdir(parents=True,exist_ok=True);now=now or datetime.now(timezone.utc)
    with (root/'.slow.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if panel is None: panel,folder=get_panel(root,now)
        else: folder=None
        local=now.astimezone(NY);day=local.date().isoformat(); planpath=root/'forward/plans'/f'{day}.json'
        policyfile=root/'main_policy.json'; policy=json.loads(policyfile.read_text()) if policyfile.exists() else None
        from cio_market_lab.engine.paper_autonomy import market_state, verified_policy
        state=market_state(now)
        daily=sorted((root/'forward/daily').glob('*.json'))
        # Canonical per-day post-state wins over auxiliary books.json after a crash.
        books=json.loads(daily[-1].read_text())['post_books'] if daily else {k:new_book() for k in ['CORE','SWING','MECHANICAL','MAIN_SHADOW']}
        last_day=json.loads(daily[-1].read_text())['date'] if daily else None
        import exchange_calendars as xc
        is_session=xc.get_calendar('XNYS').is_session(day)
        preopen=is_session and local.time().replace(tzinfo=None)<time(9,30)
        if preopen and not planpath.exists():
            if panel[-1]['date']!=state['last_completed']:raise ValueError('STALE_PREOPEN_PRICE_SESSION')
            rebalance=len(daily)%21==0
            w=weights(panel,len(panel),'SWING') if rebalance else None
            # Main arm is frozen only from a signed/verified lesson-policy acceptance.
            verified=bool(policy and verified_policy(policy))
            cap=float((policy or {}).get('swing_cap',1)) if verified else 1.
            plan={'date':day,'observed_at':now.isoformat(),'before_open':True,'source_last_session':panel[-1]['date'],
                  'snapshot_hash':digest(panel),'version':VERSION,'costs':asdict(Costs()),'main_authority_verified':verified,
                  'main_policy_hash':digest(policy) if verified else None,'main_policy_evidence':policy if verified else None,'rebalance':rebalance,
                  'arms':{'CORE':{'SPY':1.} if rebalance else None,'SWING':w,'MECHANICAL':w,
                          'MAIN_SHADOW':{s:x*cap for s,x in w.items()} if w is not None else None},
                  'bar_model_only':True,'not_broker_fill':True}
            dump(planpath,plan)
        finals=[]
        bookpath=root/'forward/books.json'
        # Completed sessions are accepted only if a genuine before-open plan already exists.
        for row in panel:
            d=row['date'];p=root/'forward/plans'/f'{d}.json';f=root/'forward/daily'/f'{d}.json'
            if f.exists() or not p.exists(): continue
            if last_day is not None and d<=last_day:raise ValueError('OUT_OF_SEQUENCE_FORWARD_PLAN')
            plan=json.loads(p.read_text());at=datetime.fromisoformat(plan['observed_at']).astimezone(NY)
            if at.date().isoformat()!=d or at.time().replace(tzinfo=None)>=time(9,30):
                raise ValueError('RETROSPECTIVE_PLAN_REJECTED')
            out={arm:apply_day(books[arm],row['bars'],target or {},Costs(**plan['costs']),rebalance=target is not None) for arm,target in plan['arms'].items()}
            record={'date':d,'plan_sha256':digest(plan),'source_row_sha256':digest(row),'recorded_at':now.isoformat(),
                    'arms':out,'version':VERSION,'full_forward_bar_model_session':True,
                    'eligible_C0052_pair':plan['main_authority_verified'],'main_policy_hash':plan['main_policy_hash'],
                    'same_snapshot_same_costs':True,'execution_fill_verified':False,'post_books':books}
            # Single canonical daily record contains post-state: recover after any interrupted auxiliary write.
            dump(f,record);dump(bookpath,books);finals.append(d)
        daily=sorted((root/'forward/daily').glob('*.json'))
        if daily:
            last=json.loads(daily[-1].read_text());dump(bookpath,last['post_books'])
        records=[json.loads(p.read_text()) for p in daily]
        eligible=[r for r in records if r['eligible_C0052_pair']]
        result={'observed_at':now.isoformat(),'paper_only':True,'last_completed_price_session':panel[-1]['date'],
                'preopen_plan_exists':planpath.exists(),'finalized_now':finals,'full_forward_sessions':len(records),
                'eligible_C0052_pairs':len(eligible),'required_pairs':20,'twenty_session_acceptance':len(eligible)>=20,
                'books':str(bookpath),'source_snapshot':str(folder),'version':VERSION,
                'status':'RUNNING_SHADOW','no_backfilled_forward_evidence':True,
                'blockers':[] if policy else ['SIGNED_MAIN_LESSON_POLICY_NOT_YET_PRESENT']}
        dump(root/'slow_latest.json',result);return result


def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['sandbox','slow','both']);p.add_argument('--root',type=Path,default=ROOT)
    a=p.parse_args();now=datetime.now(timezone.utc)
    try:
        panel,folder=get_panel(a.root,now);result={}
        if a.mode in ('sandbox','both'):result['sandbox']=sandbox(a.root,now,panel)
        if a.mode in ('slow','both'):result['slow']=slow(a.root,now,panel)
        result['source_snapshot']=str(folder);print(json.dumps(result,sort_keys=True));return 0
    except Exception as e:
        dump(a.root/'last_error.json',{'at':now.isoformat(),'error':repr(e),'paper_only':True})
        raise

if __name__=='__main__':raise SystemExit(main())
