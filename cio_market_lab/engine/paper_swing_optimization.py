"""PAPER-only, deterministic research optimization; never promotes live policy.
Historical risk matching is ex post diagnostic, not a tradable benchmark or proof
that a lower realized drawdown means unused safe risk capacity.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from itertools import product
import json
import math
from pathlib import Path
import numpy as np
from . import paper_dual_speed as ds

VERSION='swing-research-v2'


def panel_source(root):
    original=json.loads((root/'sandbox_latest.json').read_text())
    candidates=sorted((root/'snapshots').glob('*/panel.json'))
    for path in candidates:
        panel=json.loads(path.read_text())
        if ds.digest(panel)==original['panel_sha256']:
            return path,panel
    raise ValueError('EXACT_INITIAL_PANEL_NOT_FOUND')


def summary(result):
    return {k:v for k,v in result.items() if k not in ('curve','trade_journal')}


def returns(result):
    nav=np.array([10000.]+[r['nav'] for r in result['curve']],dtype=float)
    nav[-1]=result['terminal_liquidation_nav']
    return nav[1:]/nav[:-1]-1


def paired_diagnostic(strategy,benchmark,repetitions=2000,block=21):
    a=returns(strategy);b=returns(benchmark)
    if len(a)!=len(b) or len(a)<block*2:raise ValueError('INSUFFICIENT_PAIRED_SAMPLE')
    rng=np.random.default_rng(20261020)
    # Paired moving-block bootstrap preserves co-movement and short autocorrelation.
    samples=[];n=len(a);blocks=math.ceil(n/block)
    for _ in range(repetitions):
        starts=rng.integers(0,n-block+1,size=blocks)
        ix=np.concatenate([np.arange(i,i+block) for i in starts])[:n]
        samples.append(float(np.prod(1+a[ix])-np.prod(1+b[ix])))
    low,high=np.quantile(samples,[.025,.975])
    va=float(np.std(a,ddof=1)*np.sqrt(252));vb=float(np.std(b,ddof=1)*np.sqrt(252))
    # Downscale each cash-mixed sleeve to the smaller realized risk; never lever.
    vol_scale=min(1.,va/vb) if vb else 0.
    target_dd=strategy['max_mark_to_market_drawdown']
    def scaled_drawdown(scale):
        curve=np.r_[1.,np.cumprod(1+b*scale)]
        return float(np.max(1-curve/np.maximum.accumulate(curve)))
    lo,hi=0.,1.
    for _ in range(60):
        mid=(lo+hi)/2
        if scaled_drawdown(mid)<=target_dd:lo=mid
        else:hi=mid
    dd_scale=lo
    matched_vol=float(np.prod(1+b*vol_scale)-1)
    matched_dd=float(np.prod(1+b*dd_scale)-1)
    risk_samples=[];rng=np.random.default_rng(20261020)
    for _ in range(repetitions):
        starts=rng.integers(0,n-block+1,size=blocks)
        ix=np.concatenate([np.arange(i,i+block) for i in starts])[:n]
        risk_samples.append(float(np.prod(1+a[ix])-np.prod(1+b[ix]*vol_scale)))
    risk_low,risk_high=np.quantile(risk_samples,[.025,.975])
    return {'net_excess':strategy['net_return']-benchmark['net_return'],
        'block_bootstrap_95pct_excess_interval':[float(low),float(high)],
        'positive_excess_significant_95pct':bool(low>0),
        'annualized_realized_vol_strategy':va,'annualized_realized_vol_benchmark':vb,
        'vol_matched_bootstrap_95pct_excess_interval':[float(risk_low),float(risk_high)],
        'vol_matched_positive_excess_significant_95pct':bool(risk_low>0),
        'dd_matched_benchmark_realized_drawdown':scaled_drawdown(dd_scale),
        'ex_post_benchmark_vol_scale':vol_scale,'ex_post_vol_matched_excess':strategy['net_return']-matched_vol,
        'ex_post_benchmark_dd_scale':dd_scale,'ex_post_dd_matched_excess':strategy['net_return']-matched_dd,
        'ex_post_diagnostics_not_investable_or_significance_tests':True,
        'multiplicity_adjusted':False,'cash_return_assumption':0.}


def run(root=ds.ROOT,output=None,stress=False):
    root=Path(root);source,panel=panel_source(root)
    start=next(i for i,p in enumerate(panel) if p['date']>='2025-01-01')
    n=len(panel)-start
    if start<200 or n<120:raise ValueError('INSUFFICIENT_SPLIT_WARMUP')
    train=start+int(n*.6);validation=start+int(n*.8)
    costs=ds.Costs()
    if stress:costs=ds.Costs(commission_rate=.001,minimum_fee=2.,slippage_bps=10.,sell_levy_rate=.0002)
    grid=[{'lookback':l,'vol_target':v,'parking':p} for l,v,p in product((42,63,126),(.12,.16,.20,.24),('CASH','SPY','QQQ'))]
    spec={'version':VERSION,'paper_only':True,'registered_at':datetime.now(timezone.utc).isoformat(),
          'panel_sha256':ds.digest(panel),'panel_source':str(source),'grid':grid,'risk_screen_drawdown':.22,
          'implementation_sha256':ds.digest({'optimizer':Path(__file__).read_text(),'simulation':Path(ds.__file__).read_text()}),
          'costs':asdict(costs),'stress':stress,'selection':'train top five under 22% DD, validation selects one; holdout scored after selection',
          'splits':{'train':[panel[start]['date'],panel[train-1]['date']],
                    'validation':[panel[train]['date'],panel[validation-1]['date']],
                    'holdout':[panel[validation]['date'],panel[-1]['date']]},
          'independent_unseen_holdout':False,'selection_bias':'Same ETF universe and historical era already inspected; this is nested retrospective research, not clean OOS.',
          'universe':list(ds.UNIVERSE),'tw_0050_constituents_tested':False,
          'tw_gap':'No point-in-time constituent membership, corporate action and TW cost baseline in this frozen US panel; no fabricated Taiwan test.',
          'leverage_allowed':False,'promotion':'SHADOW_ONLY'}
    out=Path(output) if output else root/'optimizations'/(('stress-' if stress else 'base-')+ds.digest(spec)[:16])
    ds.dump(out/'preregister.json',spec)
    train_results=[]
    for conf in grid:
        r=ds.simulate(panel[:train],start,'SWING',costs,**conf)
        train_results.append({'config':conf,'metrics':summary(r)})
    eligible=[r for r in train_results if r['metrics']['max_mark_to_market_drawdown']<=.22]
    finalists=sorted(eligible,key=lambda r:r['metrics']['net_return'],reverse=True)[:5]
    if not finalists:raise ValueError('NO_TRAIN_CANDIDATE_WITHIN_DRAWDOWN')
    for item in finalists:
        item['validation']=summary(ds.simulate(panel[:validation],train,'SWING',costs,**item['config']))
    safe=[r for r in finalists if r['validation']['max_mark_to_market_drawdown']<=.22]
    if not safe:raise ValueError('NO_VALIDATION_CANDIDATE_WITHIN_DRAWDOWN')
    chosen=max(safe,key=lambda r:r['validation']['net_return'])['config']
    holdout=ds.simulate(panel,validation,'SWING',costs,**chosen)
    full=ds.simulate(panel,start,'SWING',costs,**chosen)
    baseline=ds.simulate(panel,start,'SWING',costs)
    benchmark={b:ds.simulate(panel,start,b,costs) for b in ('SPY','QQQ','VOL_SPY','VOL_QQQ')}
    holdout_bench={b:summary(ds.simulate(panel,validation,b,costs)) for b in ('SPY','QQQ','SWING')}
    diagnostics={b:paired_diagnostic(baseline,r) for b,r in benchmark.items()}
    passed=full['max_mark_to_market_drawdown']<=.22 and holdout['max_mark_to_market_drawdown']<=.22
    result={'overall':'PASS','paper_only':True,'version':VERSION,'experiment_executed':True,
            'candidate_count':len(grid),'config':chosen,'source_snapshot_hash':ds.digest(panel),
            'baseline':summary(baseline),'selected_full_history':summary(full),
            'selected_holdout':summary(holdout),'holdout_benchmarks':holdout_bench,
            'incremental_full_return':full['net_return']-baseline['net_return'],
            'historical_drawdown_screen_pass':passed,'future_drawdown_cap_guaranteed':False,
            'risk_matched_diagnostics':diagnostics,'promotion':'SHADOW_ONLY',
            'performance_improvement_proven':False,'independent_unseen_validation':False,
            'research_decision':'RETAIN_FOR_FORWARD_REVIEW' if passed else 'REJECT_THIS_CONFIGURATION',
            'artifact':str(out),'completed_at':datetime.now(timezone.utc).isoformat()}
    ds.dump(out/'train_grid.json',train_results);ds.dump(out/'validation.json',finalists)
    ds.dump(out/'selected_curve.json',full);ds.dump(out/'holdout_curve.json',holdout)
    ds.dump(out/'acceptance.json',result)
    ds.dump(root/('optimization_stress_latest.json' if stress else 'optimization_latest.json'),result)
    return result


def rolling_stress(root, output, candidate_source):
    """Freeze one candidate and test overlapping historical windows; no re-selection."""
    root=Path(root);output=Path(output);source,panel=panel_source(root)
    locked=json.loads(Path(candidate_source).read_text())
    if locked.get('source_snapshot_hash')!=ds.digest(panel):raise ValueError('FROZEN_PANEL_MISMATCH')
    config=locked['config'];n=len(panel)
    starts=[n-offset for offset in (126,252,378,504) if n-offset>=200]
    if not starts:raise ValueError('INSUFFICIENT_ROLLING_HISTORY')
    spec={'paper_only':True,'kind':'LOCKED_CANDIDATE_ROLLING_COST_STRESS',
        'registered_at':datetime.now(timezone.utc).isoformat(),'source_snapshot_hash':ds.digest(panel),
        'candidate_source':str(candidate_source),'candidate_sha256':ds.digest(locked),'config':config,
        'implementation_sha256':ds.digest({'optimizer':Path(__file__).read_text(),'simulation':Path(ds.__file__).read_text()}),
        'start_dates':[panel[i]['date'] for i in starts],'cost_multipliers':[2,4],
        'overlapping_retrospective_windows_not_independent_oos':True,'reselection_allowed':False}
    ds.dump(output/'preregister.json',spec);rows=[]
    base=ds.Costs()
    for multiplier in spec['cost_multipliers']:
        costs=ds.Costs(commission_rate=base.commission_rate*multiplier,minimum_fee=base.minimum_fee*multiplier,
            slippage_bps=base.slippage_bps*multiplier,sell_levy_rate=base.sell_levy_rate*multiplier)
        for start in starts:
            values={label:summary(ds.simulate(panel,start,mode,costs,**(config if label=='LOCKED_CANDIDATE' else {})))
                for label,mode in [('LOCKED_CANDIDATE','SWING'),('ORIGINAL_SWING','SWING'),('SPY','SPY'),('QQQ','QQQ')]}
            rows.append({'start':panel[start]['date'],'end':panel[-1]['date'],'cost_multiplier':multiplier,'results':values})
    result={'overall':'PASS','experiment_executed':True,'paper_only':True,'kind':spec['kind'],'cases':rows,
        'candidate_source':str(candidate_source),'candidate_sha256':spec['candidate_sha256'],
        'source_snapshot_hash':ds.digest(panel),'retrospective_window_count':len(rows),
        'historical_22pct_dd_breaches':sum(r['results']['LOCKED_CANDIDATE']['max_mark_to_market_drawdown']>.22 for r in rows),
        'promotion':'SHADOW_ONLY','future_drawdown_cap_guaranteed':False,'independent_unseen_validation':False,
        'performance_improvement_proven':False,'completed_at':datetime.now(timezone.utc).isoformat()}
    ds.dump(output/'acceptance.json',result);return result


def consume_request(root=ds.ROOT,now=None):
    root=Path(root);request=root/'optimization_request.json'
    if not request.exists():return {'status':'NO_REQUEST'}
    req=json.loads(request.read_text());now=now or datetime.now(timezone.utc)
    if req.get('scope')!='PAPER_RESEARCH_ONLY' or not req.get('client_authorized'):
        raise ValueError('RESEARCH_AUTHORIZATION_REQUIRED')
    if now<datetime.fromisoformat(req['not_before']):return {'status':'SCHEDULED','request':str(request),'not_before':req['not_before']}
    output=Path(req['output'])
    if (output/'acceptance.json').exists():return {'status':'ALREADY_EXECUTED','artifact':str(output)}
    import fcntl
    with (root/'.optimization.lock').open('a') as f:
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'OTHER_CONSUMER_OWNS_LOCK'}
        if req.get('kind')=='LOCKED_CANDIDATE_ROLLING_COST_STRESS':
            return rolling_stress(root,output,req['candidate_source'])
        return run(root,output,stress=True)


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=ds.ROOT);p.add_argument('--output',type=Path);p.add_argument('--stress',action='store_true');p.add_argument('--consume',action='store_true')
    a=p.parse_args();result=consume_request(a.root) if a.consume else run(a.root,a.output,a.stress)
    print(json.dumps(result,sort_keys=True));return 0

if __name__=='__main__':raise SystemExit(main())
