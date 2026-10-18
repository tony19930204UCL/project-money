"""Prepare immutable C0048 shadow context; execute the actual frozen-data baseline."""
from __future__ import annotations
from datetime import datetime, timezone
import copy
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cio_market_lab.engine.c0048_shadow import validate_frozen, digest, load, roc_date
from cio_market_lab.domain.models import CIODecisionContextRequest
from c0048_round2_capture import OUT, save as exclusive_save, now


def save(path, value):
    """Resume preparation without altering an existing artifact's bytes."""
    if path.exists():
        def stable(obj):
            if isinstance(obj, dict):
                return {k:stable(v) for k,v in obj.items() if k not in {'at','locked_at','derivation_at'}}
            if isinstance(obj, list):
                return [stable(v) for v in obj]
            return obj
        if stable(load(path)) != stable(value):
            raise RuntimeError(f'FROZEN_PREPARATION_DIFFERS:{path}')
        return
    exclusive_save(path,value)


def source_module():
    spec = importlib.util.spec_from_file_location('c0048_real_baseline',ROOT/'custom_scripts/paper_swing_book.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def baseline(frozen, params):
    module = source_module()
    frozen_root = OUT/'frozen_runtime'
    module.ROOT = frozen_root
    module.BOOK = frozen_root/'swing_book.json'
    captured = []
    def forbidden_write(*args,**kwargs):
        raise RuntimeError('SHADOW_BASELINE_ATTEMPTED_STATE_WRITE')
    def frozen_download(*args,**kwargs):
        import pandas as pd
        # Actual step() calls feats() on the output of download(auto_adjust=True).
        return pd.concat(frozen['bars'],axis=1,sort=True)
    original_read = Path.read_text
    def controlled_read(path,*args,**kwargs):
        if path == frozen_root/'paper_params.json':
            return json.dumps(params)
        return original_read(path,*args,**kwargs)
    with patch.object(module.yf,'download',frozen_download), patch.object(Path,'write_text',forbidden_write), patch.object(Path,'read_text',controlled_read), patch.object(module,'log',lambda **kw:captured.append(kw)):
        response = module.step()
    return {'status':'EXECUTED','response':response,'events':captured,
        'snapshot_id':frozen['snapshot_id'],'initial_account_sha256':digest((frozen_root/'swing_book.json').read_bytes()),
        'params_version':params['version'],'rule_source_sha256':digest((ROOT/'custom_scripts/paper_swing_book.py').read_bytes()),
        'execution_status':'NOT_EXECUTED_SHADOW_ONLY','orders':[],
        'explanation':'The genuine latest-session book is already processed; the actual strategy returns its idempotence guard. No missing-data-to-WAIT conversion and no fabricated flat account.',
        'signal_recalculation_executed':False, 'realized_cost':None,'forward_outcome':None,
        'action_error':None,'inaction_error':None}


def research(frozen):
    r = OUT/'research'
    def table(name):
        return {str(x.get('公司代號',x.get('Code'))):x for x in load(r/f'tw_{name}.json')}
    income,revenue,valuation,company = [table(n) for n in ('income','revenue','valuation','company')]
    result = []
    for ticker in frozen['manifest']['research_universe']:
        if ticker.endswith('.TW'):
            code=ticker[:4]
            i,v,m,c = income[code],valuation[code],revenue[code],company[code]
            facts = [f"{i['公司名稱']}; official industry={i['產業別']}",
                f"ROC year {i['年度']} quarter {i['季別']}: reported revenue {i['營業收入']}, operating profit {i['營業利益']}, net income {i['稅後淨利']} (TWSE financial statement units, TWD thousands); EPS TWD {i['基本每股盈餘(元)']}",
                f"Revenue month {m['資料年月']}: revenue TWD-thousands {m['營業收入-當月營收']}, YoY {m['營業收入-去年同月增減(%)']}%, MoM {m['營業收入-上月比較增減(%)']}%; YTD YoY {m['累計營業收入-前期比較增減(%)']}%",
                f"Exchange valuation dated {roc_date(v['Date'])}: PE {v['PEratio'] or 'not reported'}, PB {v['PBratio'] or 'not reported'}, dividend yield {v['DividendYield']}%",
                f"Revenue filing comment: {m['備註']}"]
            business = {k:v for k,v in c.items() if k in ['公司名稱','公司簡稱','產業別','主要經營業務','網址','上市日期']}
            margin=float(i['營業利益'])/float(i['營業收入']) if float(i['營業收入']) else None
            item={'ticker':ticker,'evidence_status':'SUFFICIENT_FOR_BOUNDED_REVIEW_NOT_EXECUTION','verified_facts':facts,
                'business':business,'catalyst_fact':facts[2],
                'risk_inference':f'Operating margin {margin:.4%}; compare reported monthly growth with exchange earnings multiple. Growth reversal and multiple compression are testable risks, not sourced forecasts.',
                'valuation_fact':facts[3],
                'source_refs':[f'research/tw_{n}.json' for n in ('income','revenue','valuation','company')],
                'asof':frozen['cutoff'],'published_dates':{n:x.get('出表日期',x.get('Date')) for n,x in [('income',i),('revenue',m),('valuation',v)]},
                'limitations':['No DCF or verified forward forecast; PE is the exchange trailing measure, not a fair-value target.','Official revenue update is a realized catalyst, not a promised future event.']}
        else:
            companyfacts=load(r/f'{ticker}_facts.json')
            submissions=load(r/f'{ticker}_submissions.json')
            cutoff=frozen['cutoff'][:10]
            chosen={}
            tags=['RevenueFromContractWithCustomerExcludingAssessedTax','Revenues','NetIncomeLoss','OperatingIncomeLoss',
                  'EarningsPerShareDiluted','StockBasedCompensation','NetCashProvidedByUsedInOperatingActivities','CashAndCashEquivalentsAtCarryingValue']
            for tag in tags:
                fact=companyfacts['facts'].get('us-gaap',{}).get(tag)
                if not fact:continue
                units={}
                for unit, rows in fact['units'].items():
                    filtered=[x for x in rows if x.get('filed','9999') <= cutoff and x.get('end','9999') <= cutoff and x.get('form') in ['10-K','10-Q']]
                    unique={}
                    for x in sorted(filtered,key=lambda x:(x['filed'],x['end']),reverse=True):
                        key=(x.get('start'),x['end'])
                        if key not in unique:unique[key]=x
                    units[unit]=list(unique.values())[:3]
                if units:chosen[tag]=units
            dei=companyfacts['facts'].get('dei',{}).get('EntityCommonStockSharesOutstanding',{}).get('units',{}).get('shares',[])
            if not dei:
                dei=companyfacts['facts'].get('us-gaap',{}).get('CommonStockSharesOutstanding',{}).get('units',{}).get('shares',[])
            shares=sorted([x for x in dei if x.get('filed','9999')<=cutoff and x.get('end','9999')<=cutoff],key=lambda x:(x['filed'],x['end']),reverse=True)
            share=shares[0] if shares else None
            close=frozen['diagnostics'][ticker]['last_raw_close']
            marketcap=close*share['val'] if share else None
            eps=chosen.get('EarningsPerShareDiluted',{}).get('USD/shares',[])
            annual=next((e for e in eps if e.get('start') and (datetime.fromisoformat(e['end'])-datetime.fromisoformat(e['start'])).days>330),None)
            val={'last_completed_close_USD':close,'shares_record':share,'indicative_market_cap_USD':marketcap,
                 'annual_EPS_record':annual,'indicative_close_to_annual_EPS':close/annual['val'] if annual and annual['val']>0 else None,
                 'limitations':'Not a forward PE; shares-date differs from close; no modeled future stock dilution.'}
            if not chosen or not share:
                raise ValueError(f'INSUFFICIENT_ACTUAL_FINANCIALS:{ticker}')
            item={'ticker':ticker,'evidence_status':'SUFFICIENT_FOR_BOUNDED_REVIEW_NOT_EXECUTION',
                'company_name':companyfacts['entityName'],'business':{'sic':submissions.get('sic'),'description':submissions.get('sicDescription')},
                'financial_facts':chosen,'valuation_fact':val,
                'catalyst_fact':'Latest actually filed SEC revenue/income/cash-flow records below; filing dates are availability dates, not projected results.',
                'risk_inference':'Revenue/earnings volatility, dilution from stock compensation, and price relative to disclosed earning power must be evaluated from these actual figures. No numerical future forecast is claimed.',
                'source_refs':[f'research/{ticker}_facts.json',f'research/{ticker}_submissions.json'],
                'asof':frozen['cutoff'],'limitations':['No newly reported future catalyst or price target inferred from absent IR guidance.','SEC facts are financial inputs, not complete competitive or qualitative risk-factor diligence.']}
        result.append(item)
    return result


def main():
    sid=digest((OUT/'snapshot_manifest_v4.json').read_bytes())
    frozen=validate_frozen(OUT,sid)
    module=source_module()
    features={}
    for symbol,df in frozen['bars'].items():
        f=module.feats(df)
        row=f.iloc[-1]
        features[symbol]={k:float(row[k]) for k in ['Open','High','Low','Close','Volume','ma20','ma50','ma200','hi20','atr']}
    save(OUT/'derived_indicators.json',{'snapshot_id':sid,'derivation_at':now(),'rule_source_sha256':digest((ROOT/'custom_scripts/paper_swing_book.py').read_bytes()),'features':features})
    save(OUT/'pre_model_gate.json',{'state':'DONE','at':now(),'snapshot_id':sid,'strict_input_gate':'PASS',
        'bar_diagnostics':frozen['diagnostics'],'initial_book':frozen['book'],'initial_accounts':frozen['accounts'],
        'no_historical_asof_certification':True,'no_runtime_mutation':True})
    params=load(OUT/'frozen_runtime/paper_params.json')
    # Candidate locked before either arm or Main outcome is observed.
    candidate=copy.deepcopy(params)
    candidate['version']=params['version']+'-c0048-shadow-maxhold20-v1'
    candidate['per_market']['TW:pullback']['params']['max_hold']=20
    experiment={'snapshot_id':sid,'locked_at':now(),'baseline_version':params['version'],
        'candidate_version':candidate['version'],'delta':{'TW:pullback.max_hold':{'before':10,'after':20}},
        'rationale':'Isolate sensitivity of existing swing time-stop from order TTL; no investment policy or runtime params are changed.',
        'baseline_params_sha256':digest((OUT/'frozen_runtime/paper_params.json').read_bytes()),
        'candidate_params':candidate,'prospective_or_preregistered':False,
        'no_performance_improvement_assertion':True}
    save(OUT/'version_experiment_locked.json',experiment)
    base=baseline(frozen,params)
    alternate=baseline(frozen,candidate)
    save(OUT/'mechanical_baseline.json',base)
    save(OUT/'candidate_counterfactual.json',{'experiment':experiment,'baseline':base,'candidate':alternate,
        'decision_difference':base['events']!=alternate['events'],
        'outcome_comparison':'INAPPLICABLE: already-processed session; no new fills or forward outcomes',
        'cost_after_outcome_difference':None,'improvement_proven':False})
    evidence=research(frozen)
    save(OUT/'official_research.json',{'snapshot_id':sid,'cutoff':frozen['cutoff'],'holdings_and_candidates':evidence,
        'all_bodies_preserved':True,'primary_only':True,'insufficient_evidence_is_not_a_trade_verdict':True})
    per_ticker=[]
    for symbol in frozen['manifest']['research_universe']:
        owned=symbol in frozen['book']['pos']
        is_account_only=not owned and any(a['positions'].get(symbol,0)>0 for a in frozen['accounts'].values())
        per_ticker.append({'ticker':symbol,'action':'HOLD' if owned else None if is_account_only else 'NO_TRADE',
            'quantity':0 if not is_account_only else None,
            'rationale':'Latest completed session already consumed: actual strategy idempotence guard.' if not is_account_only else 'Separate intraday account: swing-book baseline has no ownership of this position; no invented migration.',
            'invalidation':'Next unprocessed completed exchange session; stop/thesis independent of order expiry.' if not is_account_only else None,
            'status':'APPLICABLE_IDEMPOTENT_REPLAY' if not is_account_only else 'INAPPLICABLE_SEPARATE_ACCOUNT',
            'estimated_transaction_cost_native':0 if not is_account_only else None,'realized_cost':None,
            'currency':'TWD' if symbol.endswith('.TW') else 'USD','version':params['version']})
    save(OUT/'baseline_per_ticker.json',{'snapshot_id':sid,'rows':per_ticker,
        'scope':'Actual swing book baseline; intraday account-only holdings remain explicitly inapplicable, not WAIT.'})
    mandate={
        'scope':'C0048 PAPER ONLY shadow recommendation; not independent final acceptance. All inputs frozen before this call; Sunday is not permission to execute.',
        'time':now(),'cutoff':frozen['cutoff'],'baseline':base,
        'actual_swing_book_is_separate_from_US_TW_accounts':True,
        'baseline_limit':'The actual strategy is already processed for latest completed 2026-10-16. Report same-day idempotence, not fresh mechanical signals or executed trading. Do not silently rerun with an invented earlier last day.',
        'output_requirements':{
            'conditions.per_ticker_decisions':'REQUIRED: one object for EACH of the 10 supplied research_universe tickers. This explicit completeness requirement overrides the default 2500-character target. Each object has ticker, action BUY/SELL/HOLD/REJECT/NO_TRADE/EVIDENCE_INSUFFICIENT, quantity (transaction shares; zero for no action), rationale, invalidation, evidence_status SUFFICIENT or INSUFFICIENT, review_at future ISO timestamp, holding_horizon swing/long_term/intraday. No missing rows.',
            'evidence':'Evaluate actual company financials, recent disclosed growth catalyst, risks, valuation and book fit. Business/economic judgment is required, not generic URLs. No BUY is forced. If facts insufficient use EVIDENCE_INSUFFICIENT, not a falsely justified NO_TRADE.',
            'horizon':'Order expiry cancels only outstanding unfilled quantity, never a held position. Explicit independent swing/long_term review dates; historical positions lack owning Main horizon metadata: do not invent it retrospectively.',
            'conditions.lesson_application':'Explicit no_lesson_changed_decision=true unless exact supplied lesson IDs causally alter decision. No lesson adoption claim required here.',
            'top_level':'One schema-valid packet; selected_instrument is a real supplied ticker; conditions carries all per-ticker rows. Parent alone does final acceptance.'},
        'cash_and_currency':'Swing book current holdings are TW names, account values are never added across TWD/USD. No cross-currency sizing without verified FX. All existing accounts are separate, not duplicated capital.',
        'transaction_costs':{'source':'actual paper_swing_book.COST; same cost model for both arms, per-side rate includes its legacy slippage assumption', 'US':0.0005,'TW':0.003425},
        'execution':'All recommendations unexecuted. No actual transaction costs, no outcome, no performance edge, no 20-day/alpha claim.',
        'candidate_set':['2330.TW','AMD']}
    context=CIODecisionContextRequest(request_id='c0048-r2-'+sid[:16],universe=frozen['manifest']['research_universe'],
        canonical_portfolio={'swing_book':frozen['book'],'separate_accounts':frozen['accounts']},
        verified_quotes={s:{**features[s],**frozen['diagnostics'][s]} for s in frozen['manifest']['research_universe']},
        verified_research=evidence,research_gaps=[{'scope':'Execution','missing':'No future executable quotes. No qualitative IR guidance or prospective outcomes supplied.'}],
        desk_posture=mandate, tactical_risk_limits={'paper_only':True,'shadow_only':True,'no_order_mutation':True,'params':params},
        predecision_snapshot={'snapshot_id':sid,'cutoff':frozen['cutoff'],'manifest_path':str(OUT/'snapshot_manifest_v4.json'),
            'initial_book_sha256':digest((OUT/'frozen_runtime/swing_book.json').read_bytes()),
            'initial_account_sha256':{m:digest((OUT/f'frozen_runtime/{m}_account.json').read_bytes()) for m in ['US','TW']},
            'cost_model':mandate['transaction_costs'],'research_sha256':digest((OUT/'official_research.json').read_bytes())})
    save(OUT/'decision_context.json',context.model_dump(mode='json'))
    save(OUT/'progress_step1.json',{'step':1,'state':'DONE','at':now(),'snapshot_id':sid,'min_bars':min(len(f) for f in frozen['bars'].values()),'bar_count_by_ticker':{s:len(f) for s,f in frozen['bars'].items()},'official_vendor_gap_repair':'Actual missing 2025-08-01 bars replaced with primary TWSE daily OHLCV; no synthetic prices.'})
    save(OUT/'progress_step2.json',{'step':2,'state':'DONE','at':now(),'official_research_tickers':len(evidence),'bodies_and_hashes_preserved':True,'cutoff':frozen['cutoff']})
    print('PREPARED',sid,'official research',len(evidence),'baseline',base['response'])

if __name__=='__main__':
    main()
