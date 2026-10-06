from __future__ import annotations

import argparse
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

TERMINAL_STATES={"COMPLETED","CANCELLED","FAILED","CLOSED"}
NON_SUCCESS_TERMINAL={"CANCELLED","FAILED","CLOSED"}
ACTIVE_STATES={"PENDING","ACTIVE","RECOVERY"}
ACCEPTANCE_PASS="PASS"
ACCEPTANCE_FAIL="FAIL"
MISSING=object()


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
    # Numeric zero and False are values. Missing means absent/None/empty string only.
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
    if not isinstance(criteria,list) or not criteria:
        return False
    if not isinstance(evidence,Mapping):
        return False
    for criterion in criteria:
        item=evidence.get(str(criterion))
        if not isinstance(item,Mapping):
            return False
        if item.get("satisfied") is not True:
            return False
        if not item.get("source_ref"):
            return False
    return True


def _validate_live_binding(
    binding: Mapping[str,Any],
    live_jobs: Mapping[str,Any],
    *,
    prefix: str,
) -> None:
    job_id=str(_require(binding,"job_id",prefix))
    actual=live_jobs.get(job_id)
    if not isinstance(actual,Mapping):
        raise RegistryValidationError(f"{prefix}:LIVE_JOB_NOT_FOUND")
    for field in ("run_id","model","provider","route_kind"):
        expected=_require(binding,field,prefix)
        if actual.get(field)!=expected:
            raise RegistryValidationError(f"{prefix}.{field}:LIVE_JOB_MISMATCH")
    origin_delivery=binding.get("origin_delivery")
    if origin_delivery is True and actual.get("route_kind")!="origin":
        raise RegistryValidationError(f"{prefix}:LOCAL_ROUTE_CANNOT_BECOME_ORIGIN")


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

        history=record.get("execution_history")
        if not isinstance(history,list) or not history:
            raise RegistryValidationError(f"{prefix}.execution_history:ORIGINAL_SCHEDULE_REQUIRED")
        original=history[0]
        if not isinstance(original,Mapping) or original.get("kind")!="ORIGINAL_SCHEDULE":
            raise RegistryValidationError(f"{prefix}.execution_history:ORIGINAL_SCHEDULE_MUST_BE_FIRST")
        scheduled=_parse_time(_require(original,"scheduled_at",f"{prefix}.execution_history[0]"),
                              f"{prefix}.execution_history[0].scheduled_at")
        if scheduled!=deadline:
            raise RegistryValidationError(f"{prefix}:ORIGINAL_DEADLINE_HISTORY_MISMATCH")

        # Explicit zero is valid; absent numeric execution evidence is not.
        for field in record.get("required_execution_fields") or []:
            if not _present(record,str(field)):
                raise RegistryValidationError(f"{prefix}.{field}:REQUIRED_EXECUTION_FIELD_MISSING")

        acceptance=record.get("acceptance_result")
        if acceptance not in {ACCEPTANCE_PASS,ACCEPTANCE_FAIL}:
            raise RegistryValidationError(f"{prefix}.acceptance_result:PASS_OR_FAIL_REQUIRED")

        if status=="COMPLETED":
            completed=_parse_time(_require(record,"completed_at",prefix),f"{prefix}.completed_at")
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
            # Fresh ordinary registrations require a future original executor.
            binding=record.get("executor_binding")
            if not isinstance(binding,Mapping):
                raise RegistryValidationError(f"{prefix}.executor_binding:REQUIRED")
            _validate_live_binding(binding,live_jobs,prefix=f"{prefix}.executor_binding")
            if deadline <= now:
                # Late active/recovery state must carry evidence-backed recovery,
                # never a bare boolean or deadline extension.
                _validate_recovery(record,live_jobs,deadline=deadline,prefix=prefix)
            else:
                executor_time=_parse_time(_require(binding,"scheduled_at",f"{prefix}.executor_binding"),
                                          f"{prefix}.executor_binding.scheduled_at")
                if executor_time>deadline:
                    raise RegistryValidationError(f"{prefix}:PREDEADLINE_EXECUTOR_REQUIRED")
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
    ids=set()
    for record in records:
        if not isinstance(record,Mapping):
            errors.append("obligation:OBJECT_REQUIRED")
            continue
        rid=record.get("id")
        if rid in ids:
            errors.append(f"obligation[{rid}]:DUPLICATE_ID")
        ids.add(rid)
        record_errors=validate_record(record,live_jobs,now=observed)
        results.append({"id":rid,"valid":not record_errors,"errors":record_errors})
        errors.extend(record_errors)
    return {
        "status":"DEADLINE_REGISTRY_VALID" if not errors else "DEADLINE_REGISTRY_INVALID",
        "errors":errors,
        "record_results":results,
        "validated_at":observed.isoformat(),
    }


def acceptance_check(registry: Mapping[str,Any], obligation_id: str, *, now: Optional[datetime]=None) -> dict[str,Any]:
    schema=validate_registry(registry,now=now)
    record=next((r for r in registry.get("obligations",[]) if isinstance(r,Mapping) and r.get("id")==obligation_id),None)
    if record is None:
        return {
            "schema_status":schema["status"],
            "obligation_id":obligation_id,
            "status":"OBLIGATION_ACCEPTANCE_BLOCKED",
            "criterion_outcome":"MISSING_OBLIGATION",
        }
    status=record.get("status")
    acceptance=record.get("acceptance_result")
    criterion_ok=_criterion_complete(record)
    success=(
        status=="COMPLETED"
        and acceptance==ACCEPTANCE_PASS
        and criterion_ok
        and not next((x for x in schema["record_results"] if x["id"]==obligation_id),{"valid":False})["valid"] is False
    )
    return {
        "schema_status":schema["status"],
        "obligation_id":obligation_id,
        "status":"OBLIGATION_ACCEPTANCE_PASS" if success else "OBLIGATION_ACCEPTANCE_BLOCKED",
        "criterion_outcome":"PASS" if success else "FAIL",
        "record_status":status,
        "record_acceptance_result":acceptance,
        "original_criteria_complete":criterion_ok,
    }


def _source_ref(refs: Mapping[str,Any], field: str) -> str:
    value=refs.get(field)
    if not isinstance(value,str) or not value:
        raise RegistryValidationError(f"migration.source_refs.{field}:REQUIRED")
    return value


def migrate_registry_snapshot(before: Mapping[str,Any], source_refs: Mapping[str,Any]) -> tuple[dict[str,Any],dict[str,Any]]:
    """Pure, reversible migration helper. Never writes installed/runtime state."""
    after=copy.deepcopy(before)
    before_hash=_hash(before)
    changes=[]
    for record in after.get("obligations",[]):
        if not isinstance(record,dict):
            continue
        original_deadline=record.get("original_deadline")
        original_failure=record.get("failure_disposition")
        legacy_terminal=record.pop("legacy_terminal_state",None)
        if legacy_terminal in {"FAILED","CLOSED"} and record.get("status") not in {"FAILED","CLOSED"}:
            record["status"]=legacy_terminal
            changes.append({"id":record.get("id"),"field":"status","source_ref":_source_ref(source_refs,"status")})
        if legacy_terminal=="CLOSED" and not record.get("historical_acceptance_result"):
            historical=record.get("legacy_acceptance_result")
            if historical in {ACCEPTANCE_PASS,ACCEPTANCE_FAIL}:
                record["historical_acceptance_result"]=historical
                changes.append({"id":record.get("id"),"field":"historical_acceptance_result",
                                "source_ref":_source_ref(source_refs,"historical_acceptance_result")})
        # Never synthesize completed_at from mtime/review/current/scheduled values.
        if record.get("status")=="COMPLETED" and not record.get("completed_at"):
            record["migration_blocked_reason"]="ORIGINAL_COMPLETION_TIMESTAMP_MISSING"
            changes.append({"id":record.get("id"),"field":"migration_blocked_reason",
                            "source_ref":_source_ref(source_refs,"missing_completion_timestamp")})
        if record.get("original_deadline")!=original_deadline:
            raise RegistryValidationError("migration:ORIGINAL_DEADLINE_MUTATED")
        if record.get("failure_disposition")!=original_failure:
            raise RegistryValidationError("migration:ORIGINAL_FAILURE_MUTATED")

    after_hash=_hash(after)
    report={
        "before_sha256":before_hash,
        "after_sha256":after_hash,
        "changes":changes,
        "reversible":True,
        "rollback":{"replace_with_before_sha256":before_hash},
        "original_deadline_invariant":True,
        "original_failure_invariant":True,
    }
    return after,report


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(value: Any) -> str:
    return json.dumps(value,indent=2,sort_keys=True,default=str)


def main(argv: Optional[list[str]]=None) -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--registry",type=Path,required=True)
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("validate")
    a=sub.add_parser("acceptance-check")
    a.add_argument("--id",required=True)
    m=sub.add_parser("migrate")
    m.add_argument("--source-refs",type=Path,required=True)
    m.add_argument("--output",type=Path)
    args=p.parse_args(argv)
    registry=_load(args.registry)
    if args.command=="validate":
        result=validate_registry(registry)
        print(_dump(result))
        return 0 if result["status"]=="DEADLINE_REGISTRY_VALID" else 2
    if args.command=="acceptance-check":
        result=acceptance_check(registry,args.id)
        print(_dump(result))
        return 0 if result["status"]=="OBLIGATION_ACCEPTANCE_PASS" else 3
    after,report=migrate_registry_snapshot(registry,_load(args.source_refs))
    print(_dump({"migration":report,"candidate":after}))
    if args.output:
        args.output.write_text(_dump(after)+"\n",encoding="utf-8")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
