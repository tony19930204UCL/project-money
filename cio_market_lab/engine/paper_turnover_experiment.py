"""Frozen-panel cadence ablation, not a new holdout or deployable alpha claim."""
from __future__ import annotations
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import numpy as np
from . import paper_challenger as c, paper_dual_speed as ds


def matched(a, b):
    x, y = np.asarray(a['returns']), np.asarray(b['returns'])
    va, vb = np.std(x, ddof=1), np.std(y, ddof=1)
    common = min(va, vb)
    ax = common / va if va else 0.; by = common / vb if vb else 0.
    xx, yy = x * ax, y * by
    return xx - yy, {'strategy_cash_scale': float(ax), 'benchmark_cash_scale': float(by),
                     'common_annualized_volatility': float(common * np.sqrt(252)),
                     'matched_net_return_delta': float(np.prod(1 + xx) - np.prod(1 + yy)),
                     'diagnostic_only': True,
                     'limitation': 'Ex-post cash scaling of already net return paths; not a tradeable rerun or fee-recomputed portfolio.'}


def bootstrap(delta, draws=250000, block=21, seed=20261020, progress=None):
    """Paired moving blocks, fixed seeds; report subperiods and numerical stability.
    Fixed finite precision budget is not a parameter or strategy search.
    """
    delta = np.asarray(delta); n = len(delta); block = min(block, n)
    rng = np.random.default_rng(seed); means = np.empty(draws)
    # Explicit blocks retain dependence and avoid IID p-value exaggeration.
    for j in range(draws):
        ix = []
        while len(ix) < n:
            at = int(rng.integers(0, n - block + 1)); ix.extend(range(at, at + block))
        means[j] = np.mean(delta[np.asarray(ix[:n])])
        if progress and (j + 1) % 25000 == 0: progress(j + 1, draws)
    return {'draws': draws, 'seed': seed, 'block_sessions': block,
            'daily_delta_mean': float(np.mean(delta)),
            'daily_95pct_CI': np.quantile(means, [.025, .975]).tolist(),
            'half_sample_CI': np.quantile(means[:max(2, draws // 2)], [.025, .975]).tolist(),
            'prob_bootstrap_mean_nonpositive': float(np.mean(means <= 0)),
            'not_confirmatory_p_value': True}


def run(request):
    req = json.loads(Path(request).read_text()); out = Path(req['output'])
    out.mkdir(parents=True, exist_ok=True)
    panel = json.loads(Path(req['panel']).read_text())
    if ds.digest(panel) != req['panel_sha256']: raise ValueError('FROZEN_PANEL_HASH_MISMATCH')
    if not (req['authorized'] is True and req['paper_only'] is True and req['kind'] == 'TW_TURNOVER_ABLATION_V1'):
        raise ValueError('AUTHORIZATION_REQUIRED')
    start = next(i for i, row in enumerate(panel) if row['date'] >= '2025-01-01')
    if start < 200: raise ValueError('WARMUP_MISSING')
    spec = {**req, 'registered_at': datetime.now(timezone.utc).isoformat(),
            'signal_unchanged': '126-session momentum, 200-session trend, ex-ante vol 12%',
            'cadence_sessions': [21, 63], 'bootstrap_draws_per_comparison': 250000,
            'bootstrap_seeds': [20261020, 20261021], 'parameter_search': False,
            'previously_inspected_period': True, 'independent_OOS': False,
            'cost_scope': '0.1425% each side, minimum TWD20, 5bp slippage each side; liquidation charged',
            'levy_cases': {'ETF_0.1pct': .001, 'USER_0.3pct_STRESS_NOT_ETF_LAW': .003},
            'implementation_sha256': {str(p.name): hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__), Path(c.__file__), Path(ds.__file__)]}}
    ds.dump(out / 'preregister.json', spec); results = {}; comparisons = {}
    for case, levy in spec['levy_cases'].items():
        cost = ds.Costs(commission_rate=.001425, minimum_fee=20., slippage_bps=5., sell_levy_rate=levy, dividend_withholding=0.)
        results[case] = {'costs': asdict(cost), 'arms': {}}
        for cadence in [21, 63]:
            swing = c.simulate(panel, start, 'TW_SWING', ['0050.TW'], cost, rebalance_sessions=cadence)
            core = c.simulate(panel, start, '0050.TW', ['0050.TW'], cost, initial=7000.)
            small = c.simulate(panel, start, 'TW_SWING', ['0050.TW'], cost, initial=3000., rebalance_sessions=cadence)
            results[case]['arms'][f'SWING_{cadence}'] = swing
            results[case]['arms'][f'DUAL_{cadence}'] = c.dual(core, small)
        results[case]['arms']['BUY_HOLD'] = c.simulate(panel, start, '0050.TW', ['0050.TW'], cost)
    ds.dump(out / 'results.json', results)
    for case, data in results.items():
        arms = data['arms']; comparisons[case] = {}
        for a, b in [('SWING_63', 'SWING_21'), ('DUAL_63', 'DUAL_21'), ('SWING_63', 'BUY_HOLD'), ('DUAL_63', 'BUY_HOLD')]:
            key = a + '_vs_' + b; delta, norm = matched(arms[a], arms[b])
            comparison = {'normalization': norm, 'raw_return_delta': arms[a]['net_return'] - arms[b]['net_return'],
                          'MDD_delta': arms[a]['max_drawdown'] - arms[b]['max_drawdown'],
                          'trade_count_delta': arms[a]['trades'] - arms[b]['trades'],
                          'first_half_daily_mean_delta': float(np.mean(delta[:len(delta)//2])),
                          'second_half_daily_mean_delta': float(np.mean(delta[len(delta)//2:])), 'bootstrap': []}
            for seed in spec['bootstrap_seeds']:
                def progress(done, total):
                    ds.dump(out / 'progress.json', {'case': case, 'comparison': key, 'seed': seed, 'draws_completed': done,
                                                   'draws_total': total, 'at': datetime.now(timezone.utc).isoformat()})
                comparison['bootstrap'].append(bootstrap(delta, seed=seed, progress=progress))
            comparisons[case][key] = comparison; ds.dump(out / 'comparisons_partial.json', comparisons)
    summary = {case: {name: {k: v for k, v in arm.items() if k not in ('curve', 'returns')}
                     for name, arm in data['arms'].items()} for case, data in results.items()}
    report = {'overall': 'EXPERIMENT_EXECUTED', 'paper_only': True, 'panel_sha256': req['panel_sha256'],
              'first': panel[start]['date'], 'last': panel[-1]['date'], 'sessions': len(panel)-start,
              'summary': summary, 'comparisons': comparisons, 'promotion': 'SHADOW_ONLY',
              'independent_alpha_proven': False,
              'limitations': ['Inspected history, no independent holdout; no strategy selected for deployment.',
                             'Single ETF timing, not TW single-stock momentum; 0.3% is sensitivity not ETF levy.',
                             'Bootstrap precision is not additional market observations.',
                             'Adjusted fractional prices; no actual board/odd-lot fills or broker tariff verification.',
                             'Withholding/cash yield not modeled; source coverage inherited from C0099.']}
    ds.dump(out / 'report.json', report); return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--request', required=True); args = parser.parse_args()
    run(args.request)
