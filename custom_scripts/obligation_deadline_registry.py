from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

TERMINAL_STATES={"COMPLETED","CANCELLED","FAILED","CLOSED"}
NON_SUCCESS_TERMINAL={"CANCELLED","FAILED","CLOSED"}
ACTIVE_STATES={"PENDING","ACTIVE","RECOVERY"}
ACCEPTANCE_PASS="PASS"
ACCEPTANCE_FAIL="FAIL"
LIVE_EXECUTOR_STATES={"ENABLED","SCHEDULED","ACTIVE","READY"}


class RegistryValidationError(ValueError):
    pass


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value,str) or not value.strip():
        raise RegistryValidationError(f"{field}:TIMESTAMP_REQUIRED")
    try:
        parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    except Exception as exc:
        raise RegistryValidationError(f"{field}:INVALID_TIMESTAMP") from exc
    if parsed.tzinfo is None:
        raise RegistryValidationError(f"{field}:TIMEZONE_REQUIRED")
    return parsed.astimezone(timezone.utc)


def _hash(value: Any) -> str:
    raw=json.dumps(value,sort_keys=True,separators=(",",":"),default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _present(mapping: Mapping[str,Any], key: str) -> bool:
    if key not in mapping:
        return False
    value=mapping[key]
    return value is not None and not (isinstance(value,str) and value=="")


def _require(mapping: Mapping[str,Any], key: str, prefix: str) -> Any:
    if not _present(mapping,key):
        raise RegistryValidationError(f"{prefix}.{key}:REQUIRED")
    return mapping[key]


def _criterion_complete(record: Mapping[str,Any]) -> bool:
    criteria=record.get("original_criteria")
    evidence=record.get("criteria_evidence")
    if not isinstance(criteria,list) or not criteria or not isinstance(evidence,Mapping):
        return False
    for criterion in criteria:
        item=evidence.get(str(criterion))
        if not isinstance(item,Mapping) or item.get("satisfied") is not True or not item.get("source_ref"):
            return False
    return True


def _criterion_names(value: Any) -> list[str]:
    if not isinstance(value,list):
        return []
    names=[]
    for item in value:
        if isinstance(item,str) and item:
            names.append(item)
        elif isinstance(item,Mapping):
            name=item.get("criterion_id") or item.get("id") or item.get("name")
            if isinstance(name,str) and name:
                names.append(name)
    return names


def _adapt_execution_one_shot(record: Mapping[str,Any]) -> tuple[Optional[list[dict[str,Any]]],Optional[dict[str,Any]]]:
    one=record.get("execution_one_shot")
    if not isinstance(one,Mapping):
        return None,None
    scheduled=one.get("scheduled_at")
    source_ref=one.get("source_ref")
    history=None
    if scheduled is not None or source_ref is not None:
        history=[{"kind":"ORIGINAL_SCHEDULE","scheduled_at":scheduled,"source_ref":source_ref}]
    binding_fields=("job_id","run_id","model","provider","route_kind","state","scheduled_at","origin_delivery")
    binding={key:one[key] for key in binding_fields if key in one}
    return history,(binding or None)


def adapt_canonical_registry_snapshot(
    raw_registry: Mapping[str,Any],
    *,
    live_jobs: Optional[Mapping[str,Any]]=None,
) -> tuple[dict[str,Any],dict[str,Any]]:
    """Nonmutating adapter for the installed canonical registry shape.

    The installed interface may use obligation_id/acceptance_criteria/
    execution_one_shot.  This adapter projects those fields into the validator's
    internal schema without rewriting the input or manufacturing source facts.
    """
    before=copy.deepcopy(raw_registry)
    adapted=copy.deepcopy(raw_registry)
    raw_records=raw_registry.get("obligations")
    if not isinstance(raw_records,list):
        return adapted,{
            "mode":"NO_ADAPTATION",
            "before_sha256":_hash(before),
            "after_sha256":_hash(adapted),
            "input_unchanged":raw_registry==before,
            "adapted_ids":[],
        }

    out=[]
    adapted_ids=[]
    for raw in raw_records:
        if not isinstance(raw,Mapping):
            out.append(copy.deepcopy(raw))
            continue
        record=copy.deepcopy(raw)
        changed=False
        if not _present(record,"id") and _present(record,"obligation_id"):
            record["id"]=record["obligation_id"]
            changed=True
        if not _present(record,"original_criteria") and _present(record,"acceptance_criteria"):
            record["original_criteria"]=_criterion_names(record["acceptance_criteria"])
            changed=True
        if not _present(record,"criteria_evidence") and isinstance(record.get("acceptance_evidence"),Mapping):
            record["criteria_evidence"]=copy.deepcopy(record["acceptance_evidence"])
            changed=True
        if not _present(record,"original_deadline") and _present(record,"deadline"):
            record["original_deadline"]=record["deadline"]
            changed=True
        if not _present(record,"original_deadline_source_ref") and _present(record,"deadline_source_ref"):
            record["original_deadline_source_ref"]=record["deadline_source_ref"]
            changed=True

        history,binding=_adapt_execution_one_shot(record)
        if not _present(record,"execution_history") and history is not None:
            record["execution_history"]=history
            changed=True
        if record.get("status") in ACTIVE_STATES and not _present(record,"executor_binding") and binding is not None:
            record["executor_binding"]=binding
            changed=True
        if changed:
            adapted_ids.append(str(record.get("id") or record.get("obligation_id") or "<unknown>"))
        out.append(record)
    adapted["obligations"]=out

    supplied_live=live_jobs
    if supplied_live is None:
        if isinstance(raw_registry.get("live_jobs"),Mapping):
            supplied_live=raw_registry.get("live_jobs")
        elif isinstance(raw_registry.get("executor_readback"),Mapping):
            supplied_live=raw_registry.get("executor_readback")
    adapted["live_jobs"]=copy.deepcopy(supplied_live or {})

    return adapted,{
        "mode":"CANONICAL_SHAPE_ADAPTER",
        "before_sha256":_hash(before),
        "after_sha256":_hash(adapted),
        "input_unchanged":raw_registry==before,
        "adapted_ids":adapted_ids,
    }


def _validate_live_binding(
    binding: Mapping[str,Any],
    live_jobs: Mapping[str,Any],
    *,
    prefix: str,
    require_schedule: bool=False,
) -> Mapping[str,Any]:
    job_id=str(_require(binding,"job_id",prefix))
    actual=live_jobs.get(job_id)
    if not isinstance(actual,Mapping):
        raise RegistryValidationError(f"{prefix}:LIVE_JOB_NOT_FOUND")
    for field in ("run_id","model","provider","route_kind","state"):
        expected=_require(binding,field,prefix)
        if actual.get(field)!=expected:
            raise RegistryValidationError(f"{prefix}.{field}:LIVE_JOB_MISMATCH")
    if str(actual.get("state")) not in LIVE_EXECUTOR_STATES:
        raise RegistryValidationError(f"{prefix}.state:LIVE_JOB_NOT_EXECUTABLE")
    if require_schedule:
        expected_schedule=_parse_time(_require(binding,"scheduled_at",prefix),f"{prefix}.scheduled_at")
        actual_schedule=_parse_time(_require(actual,"scheduled_at",f"{prefix}.live"),f"{prefix}.live.scheduled_at")
        if actual_schedule!=expected_schedule:
            raise RegistryValidationError(f"{prefix}.scheduled_at:LIVE_JOB_MISMATCH")
    if binding.get("origin_delivery") is True and actual.get("route_kind")!="origin":
        raise RegistryValidationError(f"{prefix}:LOCAL_ROUTE_CANNOT_BECOME_ORIGIN")
    return actual


def _validate_recovery(
    record: Mapping[str,Any],
    live_jobs: Mapping[str,Any],
    *,
    deadline: datetime,
    prefix: str,
) -> None:
    recovery=record.get("recovery")
    if not isinstance(recovery,Mapping):
        raise RegistryValidationError(f"{prefix}.recovery:AUTHORIZED_RECOVERY_REQUIRED")
    if recovery.get("authorized") is not True:
        raise RegistryValidationError(f"{prefix}.recovery:AUTHORIZATION_REQUIRED")
    _require(recovery,"authorization_source",f"{prefix}.recovery")
    observed=_parse_time(_require(recovery,"observed_at",f"{prefix}.recovery"),f"{prefix}.recovery.observed_at")
    if observed <= deadline:
        raise RegistryValidationError(f"{prefix}.recovery:LATE_RECOVERY_MUST_BE_AFTER_ORIGINAL_DEADLINE")
    if recovery.get("classification")!="LATE_RECOVERY":
        raise RegistryValidationError(f"{prefix}.recovery:CLASSIFICATION_REQUIRED")
    binding=_require(recovery,"executor_readback",f"{prefix}.recovery")
    if not isinstance(binding,Mapping):
        raise RegistryValidationError(f"{prefix}.recovery.executor_readback:OBJECT_REQUIRED")
    _validate_live_binding(binding,live_jobs,prefix=f"{prefix}.recovery.executor_readback")
    refs=recovery.get("source_refs")
    if not isinstance(refs,list) or not refs or not all(isinstance(x,str) and x for x in refs):
        raise RegistryValidationError(f"{prefix}.recovery.source_refs:REQUIRED")


def validate_record(record: Mapping[str,Any], live_jobs: Mapping[str,Any], *, now: datetime) -> list[str]:
    errors=[]
    rid=str(record.get("id") or "<missing>")
    prefix=f"obligation[{rid}]"
    try:
        _require(record,"id",prefix)
        _require(record,"owner",prefix)
        status=str(_require(record,"status",prefix))
        if status not in TERMINAL_STATES|ACTIVE_STATES:
            raise RegistryValidationError(f"{prefix}.status:UNSUPPORTED")
        deadline=_parse_time(_require(record,"original_deadline",prefix),f"{prefix}.original_deadline")
        _require(record,"original_deadline_source_ref",prefix)

        history=record.get("execution_history")
        if not isinstance(history,list) or not history:
            raise RegistryValidationError(f"{prefix}.execution_history:ORIGINAL_SCHEDULE_REQUIRED")
        original=history[0]
        if not isinstance(original,Mapping) or original.get("kind")!="ORIGINAL_SCHEDULE":
            raise RegistryValidationError(f"{prefix}.execution_history:ORIGINAL_SCHEDULE_MUST_BE_FIRST")
        scheduled=_parse_time(_require(original,"scheduled_at",f"{prefix}.execution_history[0]"),
                              f"{prefix}.execution_history[0].scheduled_at")
        _require(original,"source_ref",f"{prefix}.execution_history[0]")
        if scheduled>deadline:
            raise RegistryValidationError(f"{prefix}:ORIGINAL_SCHEDULE_AFTER_DEADLINE")

        for field in record.get("required_execution_fields") or []:
            if not _present(record,str(field)):
                raise RegistryValidationError(f"{prefix}.{field}:REQUIRED_EXECUTION_FIELD_MISSING")

        acceptance=record.get("acceptance_result")
        if acceptance not in {ACCEPTANCE_PASS,ACCEPTANCE_FAIL}:
            raise RegistryValidationError(f"{prefix}.acceptance_result:PASS_OR_FAIL_REQUIRED")

        if status=="COMPLETED":
            completed=_parse_time(_require(record,"completed_at",prefix),f"{prefix}.completed_at")
            _require(record,"completed_at_source_ref",prefix)
            if record.get("completed_at_source_kind")!="ORIGINAL_ARTIFACT":
                raise RegistryValidationError(f"{prefix}.completed_at_source_kind:ORIGINAL_ARTIFACT_REQUIRED")
            if completed>deadline:
                _validate_recovery(record,live_jobs,deadline=deadline,prefix=prefix)
            if acceptance==ACCEPTANCE_PASS and not _criterion_complete(record):
                raise RegistryValidationError(f"{prefix}:PASS_REQUIRES_COMPLETE_ORIGINAL_CRITERIA")
        elif status=="FAILED":
            if acceptance!=ACCEPTANCE_FAIL:
                raise RegistryValidationError(f"{prefix}:FAILED_CANNOT_PASS")
            _require(record,"failure_disposition",prefix)
        elif status=="CLOSED":
            _require(record,"closure_disposition",prefix)
            historical=_require(record,"historical_acceptance_result",prefix)
            if historical not in {ACCEPTANCE_PASS,ACCEPTANCE_FAIL}:
                raise RegistryValidationError(f"{prefix}.historical_acceptance_result:PASS_OR_FAIL_REQUIRED")
            if acceptance!=ACCEPTANCE_FAIL:
                raise RegistryValidationError(f"{prefix}:CLOSED_CANNOT_PASS")
        elif status=="CANCELLED":
            if acceptance!=ACCEPTANCE_FAIL:
                raise RegistryValidationError(f"{prefix}:CANCELLED_CANNOT_PASS")
        else:
            binding=record.get("executor_binding")
            if not isinstance(binding,Mapping):
                raise RegistryValidationError(f"{prefix}.executor_binding:REQUIRED")
            _validate_live_binding(binding,live_jobs,prefix=f"{prefix}.executor_binding",require_schedule=True)
            executor_time=_parse_time(_require(binding,"scheduled_at",f"{prefix}.executor_binding"),
                                      f"{prefix}.executor_binding.scheduled_at")
            if deadline <= now:
                _validate_recovery(record,live_jobs,deadline=deadline,prefix=prefix)
            else:
                if not (now < executor_time <= deadline):
                    raise RegistryValidationError(f"{prefix}:FUTURE_PREDEADLINE_EXECUTOR_REQUIRED")
    except RegistryValidationError as exc:
        errors.append(str(exc))
    return errors


def validate_registry(registry: Mapping[str,Any], *, now: Optional[datetime]=None) -> dict[str,Any]:
    observed=(now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    live_jobs=registry.get("live_jobs") or {}
    records=registry.get("obligations")
    if not isinstance(records,list):
        return {"status":"DEADLINE_REGISTRY_INVALID","errors":["obligations:LIST_REQUIRED"],"record_results":[]}
    results=[]
    errors=[]
    counts={}
    for record in records:
        if not isinstance(record,Mapping):
            errors.append("obligation:OBJECT_REQUIRED")
            continue
        rid=record.get("id")
        counts[rid]=counts.get(rid,0)+1
        record_errors=validate_record(record,live_jobs,now=observed)
        results.append({"id":rid,"valid":not record_errors,"errors":record_errors})
        errors.extend(record_errors)
    for rid,count in counts.items():
        if count>1:
            errors.append(f"obligation[{rid}]:DUPLICATE_ID")
    return {
        "status":"DEADLINE_REGISTRY_VALID" if not errors else "DEADLINE_REGISTRY_INVALID",
        "errors":errors,
        "record_results":results,
        "validated_at":observed.isoformat(),
    }


def acceptance_check(registry: Mapping[str,Any], obligation_id: str, *, now: Optional[datetime]=None) -> dict[str,Any]:
    schema=validate_registry(registry,now=now)
    records=[r for r in registry.get("obligations",[]) if isinstance(r,Mapping) and r.get("id")==obligation_id]
    if not records:
        return {
            "schema_status":schema["status"],"obligation_id":obligation_id,
            "status":"OBLIGATION_ACCEPTANCE_BLOCKED","criterion_outcome":"MISSING_OBLIGATION",
        }
    if len(records)!=1:
        return {
            "schema_status":schema["status"],"obligation_id":obligation_id,
            "status":"OBLIGATION_ACCEPTANCE_BLOCKED","criterion_outcome":"IDENTITY_AMBIGUOUS",
            "matching_record_count":len(records),
        }
    record=records[0]
    named_results=[x for x in schema["record_results"] if x["id"]==obligation_id]
    named_valid=len(named_results)==1 and named_results[0]["valid"] is True
    status=record.get("status")
    acceptance=record.get("acceptance_result")
    criterion_ok=_criterion_complete(record)
    success=status=="COMPLETED" and acceptance==ACCEPTANCE_PASS and criterion_ok and named_valid
    return {
        "schema_status":schema["status"],"obligation_id":obligation_id,
        "status":"OBLIGATION_ACCEPTANCE_PASS" if success else "OBLIGATION_ACCEPTANCE_BLOCKED",
        "criterion_outcome":"PASS" if success else "FAIL",
        "record_status":status,"record_acceptance_result":acceptance,
        "original_criteria_complete":criterion_ok,
    }


def _source_ref(refs: Mapping[str,Any], field: str) -> str:
    value=refs.get(field)
    if not isinstance(value,str) or not value:
        raise RegistryValidationError(f"migration.source_refs.{field}:REQUIRED")
    return value


def migrate_registry_snapshot(before: Mapping[str,Any], source_refs: Mapping[str,Any]) -> tuple[dict[str,Any],dict[str,Any]]:
    after=copy.deepcopy(before)
    before_hash=_hash(before)
    changes=[]
    for record in after.get("obligations",[]):
        if not isinstance(record,dict):
            continue
        original_deadline=record.get("original_deadline")
        original_failure=record.get("failure_disposition")
        original_status=record.get("status")
        legacy_terminal=record.get("legacy_terminal_state")
        if legacy_terminal in {"FAILED","CLOSED"} and record.get("status") not in {"FAILED","CLOSED"}:
            record["status"]=legacy_terminal
            changes.append({"id":record.get("id"),"field":"status","before":original_status,"after":legacy_terminal,
                            "source_ref":_source_ref(source_refs,"status")})
        if legacy_terminal=="CLOSED" and not record.get("historical_acceptance_result"):
            historical=record.get("legacy_acceptance_result")
            if historical in {ACCEPTANCE_PASS,ACCEPTANCE_FAIL}:
                record["historical_acceptance_result"]=historical
                changes.append({"id":record.get("id"),"field":"historical_acceptance_result",
                                "before":None,"after":historical,
                                "source_ref":_source_ref(source_refs,"historical_acceptance_result")})
        if record.get("status")=="COMPLETED" and not record.get("completed_at"):
            if record.get("migration_blocked_reason")!="ORIGINAL_COMPLETION_TIMESTAMP_MISSING":
                record["migration_blocked_reason"]="ORIGINAL_COMPLETION_TIMESTAMP_MISSING"
                changes.append({"id":record.get("id"),"field":"migration_blocked_reason",
                                "before":None,"after":"ORIGINAL_COMPLETION_TIMESTAMP_MISSING",
                                "source_ref":_source_ref(source_refs,"missing_completion_timestamp")})
        if record.get("original_deadline")!=original_deadline:
            raise RegistryValidationError("migration:ORIGINAL_DEADLINE_MUTATED")
        if record.get("failure_disposition")!=original_failure:
            raise RegistryValidationError("migration:ORIGINAL_FAILURE_MUTATED")
    after_hash=_hash(after)
    return after,{
        "before_sha256":before_hash,"after_sha256":after_hash,"changes":changes,
        "reversible":True,"rollback":{"replace_with_before_sha256":before_hash},
        "original_deadline_invariant":True,"original_failure_invariant":True,
    }


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_path(explicit: Optional[Path], env_name: str) -> Path:
    if explicit is not None:
        return explicit
    raw=os.environ.get(env_name)
    if raw:
        return Path(raw)
    raise RegistryValidationError(f"{env_name}:PATH_REQUIRED")


def _dump(value: Any) -> str:
    return json.dumps(value,indent=2,sort_keys=True,default=str)


def _add_registry_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--registry",type=Path)
    parser.add_argument("--live-jobs",type=Path)


def main(argv: Optional[list[str]]=None) -> int:
    p=argparse.ArgumentParser()
    _add_registry_options(p)
    sub=p.add_subparsers(dest="command",required=True)
    v=sub.add_parser("validate")
    _add_registry_options(v)
    a=sub.add_parser("acceptance-check")
    _add_registry_options(a)
    a.add_argument("--id",required=True)
    m=sub.add_parser("migrate")
    _add_registry_options(m)
    m.add_argument("--source-refs",type=Path,required=True)
    m.add_argument("--output",type=Path)
    args=p.parse_args(argv)

    registry_path=_resolve_path(args.registry,"PROJECT_MONEY_OBLIGATION_REGISTRY")
    raw=_load(registry_path)
    live_jobs_path=args.live_jobs
    if live_jobs_path is None and os.environ.get("PROJECT_MONEY_LIVE_JOBS"):
        live_jobs_path=Path(os.environ["PROJECT_MONEY_LIVE_JOBS"])
    supplied_jobs=_load(live_jobs_path) if live_jobs_path else None
    registry,compat=adapt_canonical_registry_snapshot(raw,live_jobs=supplied_jobs)

    if args.command=="validate":
        result=validate_registry(registry)
        result["compatibility"]=compat
        print(_dump(result))
        return 0 if result["status"]=="DEADLINE_REGISTRY_VALID" else 2
    if args.command=="acceptance-check":
        result=acceptance_check(registry,args.id)
        result["compatibility"]=compat
        print(_dump(result))
        return 0 if result["status"]=="OBLIGATION_ACCEPTANCE_PASS" else 3
    after,report=migrate_registry_snapshot(registry,_load(args.source_refs))
    print(_dump({"compatibility":compat,"migration":report,"candidate":after}))
    if args.output:
        args.output.write_text(_dump(after)+"\n",encoding="utf-8")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
