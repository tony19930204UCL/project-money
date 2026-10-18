"""Read-only round2 evidence recheck. NOT independent Main final acceptance.

Does not call a model or order service. A fresh --output is required; the original
accept_first_iteration.py is deliberately not replaced or weakened.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cio_market_lab.domain.models import CIODecisionPacket, CIOExecutionReceipt
from cio_market_lab.engine.c0048_shadow import digest, load, validate_frozen, validate_main_rows
from cio_market_lab.engine.cio_packet import validate_cio_packet
from cio_market_lab.integrations.runtime_evidence import RuntimeEvidenceAdapter
from c0048_round2_capture import OUT, now, save

ACCEPT=Path('/home/chihcheng/.hermes/workspace/reports/paper_cio_value_audit_20261016/accept_first_iteration.py')


def verify(junit: Path):
    sid=digest((OUT/'snapshot_manifest_v4.json').read_bytes())
    frozen=validate_frozen(OUT,sid)
    start=load(OUT/'starting_state.json')
    main=load(OUT/'main_validated.json')
    attempt=Path(main['attempt'])
    request=load(attempt/'request.json')
    packet=CIODecisionPacket.model_validate(load(attempt/'packet.json'))
    receipt=CIOExecutionReceipt.model_validate(load(attempt/'receipt.json'))
    raw=load(attempt/'raw_response.json')
    evidence=RuntimeEvidenceAdapter(pinned_provider='openai-codex',pinned_model='gpt-6.1-sol').verify_runtime_evidence(
        metadata=raw['runtime_metadata'],response_text=raw['response'],exit_code=raw['returncode'],
        pinned_provider='openai-codex',pinned_model='gpt-6.1-sol',allow_fixture=False)
    valid,detail=validate_cio_packet(packet,now=receipt.timestamp,processed_case_ids=set())
    rows=validate_main_rows(packet,frozen)
    comparison=load(OUT/'comparison.json')
    replay=load(OUT/'horizon_real_state_replay.json')
    experiment=load(OUT/'candidate_counterfactual.json')
    context=load(OUT/'decision_context.json')
    suites=ET.parse(junit).getroot()
    counts={key:sum(int(s.get(key,0)) for s in suites.iter('testsuite')) for key in ['tests','errors','failures','skipped']}
    command=load(attempt/'command.json')['argv']
    def option(key):return command[command.index(key)+1]
    runtime_deltas=[p for p,h in start['runtime_hashes'].items() if not (ROOT/p).is_file() or digest((ROOT/p).read_bytes())!=h]
    round1_deltas=[p for p,h in start['round1_hashes'].items() if not (ROOT/p).is_file() or digest((ROOT/p).read_bytes())!=h]
    round1_files={str(p.relative_to(ROOT)) for p in (ROOT/'data/c0048/round1').rglob('*') if p.is_file()}
    checks={
        'strict_gate_real_30_symbols_over200_bars':len(frozen['bars'])==30 and min(len(df) for df in frozen['bars'].values())>200,
        'official_research_real_financials_not_urls':len(context['verified_research'])==len(rows) and all(x.get('verified_facts') or x.get('financial_facts') for x in context['verified_research']),
        'authenticated_pinned_runtime_no_fallback':evidence.auth_verified and evidence.is_success_response and not evidence.is_fixture and not evidence.fallback_active and evidence.resolved_provider=='openai-codex' and evidence.resolved_model=='gpt-6.1-sol',
        'tools_disabled_one_agent_turn':option('--toolsets')=='none' and option('--max-turns')=='1',
        'distinct_session_receipt_link':receipt.session_id==load(attempt/'attempt.json')['session_id'] and receipt.session_id.startswith('c0048-r2-main-') and packet.provenance.receipt_id==receipt.receipt_id and packet.case_id==receipt.case_id,
        'receipt_explicit_authority_and_pins':receipt.readback_verified and receipt.authority=='MAIN_CIO' and receipt.provider_id=='openai-codex' and receipt.model_id=='gpt-6.1-sol',
        'raw_receipt_hashes':receipt.raw_prompt_hash==digest(load(attempt/'raw_request.json')['prompt'].encode()) and receipt.raw_response_hash==digest(raw['response'].encode()),
        'signed_packet_valid_at_actual_receipt_time':valid,
        'same_snapshot_all_arms':sid==request['predecision_snapshot']['snapshot_id']==packet.predecision_snapshot['snapshot_id']==comparison['snapshot_id']==experiment['experiment']['snapshot_id'],
        'signed_initial_snapshot_fields_complete':all(packet.predecision_snapshot.get(k)==v for k,v in context['predecision_snapshot'].items()),
        'signed_research_hash_matches':packet.predecision_snapshot.get('research_sha256')==digest((OUT/'official_research.json').read_bytes()),
        'same_actual_initial_accounts':request['canonical_portfolio']=={'swing_book':frozen['book'],'separate_accounts':frozen['accounts']} and request['canonical_portfolio']==context['canonical_portfolio'],
        'same_research_and_features':request['verified_research']==context['verified_research'] and request['verified_quotes']==context['verified_quotes'],
        'per_ticker_complete_comparison':{r['ticker'] for r in comparison['rows']}==set(frozen['manifest']['research_universe']) and len(comparison['rows'])==len(rows),
        'cost_and_version_fields_complete':all({'action','quantity','rationale','invalidation','estimated_transaction_cost_native','realized_cost','currency','version'}<=r[arm].keys() for r in comparison['rows'] for arm in ['mechanical','main']),
        'actual_strategy_idempotence_not_missing_data_wait':load(OUT/'mechanical_baseline.json')['response']=='already 2026-10-16' and load(OUT/'mechanical_baseline.json')['status']=='EXECUTED' and not load(OUT/'mechanical_baseline.json')['signal_recalculation_executed'],
        'real_state_expiry_cancels_only_order':len(replay['expiry_execution_replays'])==4 and all(r['position_quantity_before']==r['position_quantity_after'] and r['cash_before']==r['cash_after'] and r['no_submit_after_expiry'] and r['posted_fill_preserved'] and r['restart_idempotent'] for r in replay['expiry_execution_replays']),
        'real_holding_replays_and_unknown_legacy_metadata_honest':len(replay['real_position_reviews'])==len(frozen['book']['pos']) and all(r['legacy_review_state']['review_state']=='UNKNOWN_LEGACY_METADATA' for r in replay['real_position_reviews']),
        'version_experiment_locked_before_main':datetime.fromisoformat(experiment['experiment']['locked_at'])<receipt.timestamp and experiment['experiment']['delta']=={'TW:pullback.max_hold':{'before':10,'after':20}} and experiment['improvement_proven'] is False,
        'no_claimed_outcomes_or_alpha':comparison['performance_improvement_proven'] is False and comparison['twenty_complete_trading_days'] is False and all(r['action_error'] is None and r['inaction_error'] is None and r['after_cost_outcome'] is None for r in comparison['rows']),
        'main_decision_call_cap':1<=len(list(OUT.glob('main_attempt_*/attempt.json')))<=2,
        'tests_pass_no_skips':counts['tests']>0 and not any(counts[k] for k in ['errors','failures','skipped']),
        'runtime_unchanged':not runtime_deltas,
        'round1_byte_for_byte_and_inventory_unchanged':not round1_deltas and round1_files==set(start['round1_hashes']),
        'original_acceptance_unchanged':digest(ACCEPT.read_bytes())==start['original_acceptance_sha256'],
    }
    return {'verification':'PASS' if all(checks.values()) else 'FAIL','at':now(),'checks':checks,
        'snapshot_id':sid,'cutoff':frozen['cutoff'],'latest_completed_session':'2026-10-16',
        'signature_validation':detail,'signed_packet_executable_now':datetime.now(timezone.utc)<packet.expiry,
        'counts':counts,'junit':str(junit),'junit_sha256':digest(junit.read_bytes()),
        'runtime_deltas':runtime_deltas,'round1_deltas':round1_deltas,
        'receipt_evidence_strength':'local-runtime, post-auth process inspection; NOT provider cryptographic attestation',
        'main_model_calls_in_this_verifier':0,'independent_final_acceptance':False,
        'limits':['Already-processed completed session: the real baseline returns its idempotence guard; no fresh signals or new trade cycle claimed.',
                  'Separate intraday account-only holdings have explicit inapplicable swing-baseline fields; account cash is never combined across currencies.',
                  'Legacy held positions lack owning horizon/review metadata; Main recommendations do not retroactively migrate them.',
                  'No future fills/outcomes, no learning-adoption/20-day/alpha claim. C0051/C0052 remain independent.',
                  'Hypothetical pending orders in horizon replay are test interventions seeded by actual positions, not fabricated historical orders.']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--junit',type=Path,default=OUT/'regression_final_v2.xml')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=verify(args.junit)
    save(args.output,report)
    print(report['verification'],report['counts'])
    if report['verification']!='PASS':print([k for k,v in report['checks'].items() if not v])
    return 0 if report['verification']=='PASS' else 1

if __name__=='__main__':raise SystemExit(main())
