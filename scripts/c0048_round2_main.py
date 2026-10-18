"""One authenticated, tools-disabled Main invocation; global round2 cap TWO.

No runtime runner, order placement, profile configuration or final acceptance.
A failed/partial response is preserved and refused rather than fabricated.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import traceback
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cio_market_lab.domain.models import CIODecisionContextRequest
from cio_market_lab.engine.c0048_shadow import digest, load, validate_frozen, validate_main_rows
from cio_market_lab.engine.cio_packet import validate_cio_packet
import cio_market_lab.integrations.hermes_chat as bridge
from c0048_round2_capture import OUT, now, save


def main():
    sid=digest((OUT/'snapshot_manifest_v4.json').read_bytes())
    frozen=validate_frozen(OUT,sid)
    if (OUT/'main_validated.json').exists():
        raise RuntimeError('VALIDATED_MAIN_ALREADY_EXISTS_NO_NEW_CALL')
    attempts=list(OUT.glob('main_attempt_*/attempt.json'))
    if len(attempts)>=2:
        raise RuntimeError('TWO_MAIN_DECISION_CALL_CAP_REACHED')
    number=len(attempts)+1
    output=OUT/f'main_attempt_{number}'
    output.mkdir(exist_ok=False)
    session='c0048-r2-main-'+uuid.uuid4().hex[:16]
    save(output/'attempt.json',{'at':now(),'call_number':number,'max_new_decision_calls':2,
        'session_id':session,'provider':'openai-codex','model':'gpt-6.1-sol','tools':'none',
        'max_turns':1,'profile':'default','independent_of_implementer_session':True,'snapshot_id':sid})
    request=CIODecisionContextRequest.model_validate(load(OUT/'decision_context.json'))
    if number>1:
        request.desk_posture['retry_reason']='First authenticated response could not satisfy the mandatory per-ticker completeness contract. Preserve original frozen portfolio, costs, research, snapshot; do not fetch or invent new inputs.'
    save(output/'request.json',request.model_dump(mode='json'))
    original_run=bridge.run_hermes_cli_chat
    original_builder=bridge.build_production_chat_command
    raw={}
    def bounded_builder(**kwargs):
        kwargs['max_turns']=1
        kwargs['toolsets']='none'
        kwargs['run_budget']=300
        command=original_builder(**kwargs)
        save(output/'command.json',{'argv':command,'workspace_isolated':True,'profile':'default'})
        return command
    def captured_run(*args,**kwargs):
        save(output/'raw_request.json',{'prompt':args[0],'sha256':digest(args[0].encode())})
        kwargs['extra_env']={'HERMES_PROFILE':'default'}
        result=original_run(*args,**kwargs)
        raw.update(result)
        save(output/'raw_response.json',result)
        return result
    bridge.build_production_chat_command=bounded_builder
    bridge.run_hermes_cli_chat=captured_run
    status={'step':3,'state':'EXTERNAL_BLOCKER','at':now(),'snapshot_id':sid,'attempt':str(output)}
    try:
        executor=bridge.HermesCIODecisionExecutor(session_id=session,workspace_root=str(output/'isolated_workspace'),
            timeout_seconds=390,provider_id='openai-codex',model_id='gpt-6.1-sol',allow_fixture=False,is_production=True)
        packet=executor.request_decision(request)
        receipt=executor.last_receipt
        if receipt:
            save(output/'receipt.json',receipt.model_dump(mode='json'))
        if packet:
            save(output/'packet.json',packet.model_dump(mode='json'))
        if not packet or not receipt or not receipt.readback_verified:
            raise RuntimeError('MISSING_AUTHENTICATED_PACKET_OR_RECEIPT')
        valid,detail=validate_cio_packet(packet,now=datetime.now(timezone.utc),processed_case_ids=set())
        if not valid:
            raise ValueError(f'SIGNED_PACKET_VALIDATION_FAILED:{detail}')
        rows=validate_main_rows(packet,frozen)
        if receipt.raw_prompt_hash != load(output/'raw_request.json')['sha256'] or receipt.raw_response_hash != digest(raw['response'].encode()):
            raise ValueError('RAW_RECEIPT_HASH_MISMATCH')
        record={'snapshot_id':sid,'attempt':str(output),'session_id':receipt.session_id,'provider':receipt.provider_id,
            'model':receipt.model_id,'receipt':str(output/'receipt.json'),'packet':str(output/'packet.json'),
            'rows':rows,'tokens':raw.get('runtime_metadata',{}).get('tokens'),
            'actual_cost':None,'actual_cost_status':'UNKNOWN_NOT_REPORTED_DO_NOT_ASSUME_ZERO',
            'signature_validation':detail,'independent_final_acceptance':False,'execution_status':'NOT_EXECUTED'}
        save(OUT/'main_validated.json',record)
        status.update(state='DONE',receipt=record['receipt'],packet=record['packet'])
    except Exception as exc:
        status.update(error_type=type(exc).__name__,error=str(exc),traceback=traceback.format_exc(),
            alternatives='Pinned provider/model retained. Only a second fresh decision session is allowed if cap remains; no escalation.')
    finally:
        bridge.run_hermes_cli_chat=original_run
        bridge.build_production_chat_command=original_builder
    save(output/'status.json',status)
    save(OUT/f'progress_step3_attempt{number}.json',status)
    print(json.dumps(status,ensure_ascii=False))
    return 0 if status['state']=='DONE' else 2

if __name__=='__main__':
    raise SystemExit(main())
