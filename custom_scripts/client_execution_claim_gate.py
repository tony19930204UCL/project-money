from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Optional

from custom_scripts.obligation_deadline_registry import (
    ACCEPTANCE_FAIL,
    ACCEPTANCE_PASS,
    NON_SUCCESS_TERMINAL,
    acceptance_check,
)


class ClaimGateError(ValueError):
    pass


def _binding_equal(expected: Mapping[str,Any], actual: Mapping[str,Any]) -> bool:
    keys=("job_id","run_id","model","provider","route_kind")
    return all(expected.get(k)==actual.get(k) for k in keys)


def _currently_assigned_ids(registry: Mapping[str,Any], owner: str) -> list[str]:
    assigned=[]
    for record in registry.get("obligations") or []:
        if not isinstance(record,Mapping):
            continue
        if record.get("owner")!=owner:
            continue
        if record.get("status") in NON_SUCCESS_TERMINAL|{"COMPLETED"}:
            continue
        assigned.append(str(record.get("id")))
    return sorted(x for x in assigned if x)


def _canonical_scope(registry: Mapping[str,Any], scope_id: str) -> Mapping[str,Any]:
    scopes=registry.get("execution_scopes") or {}
    scope=scopes.get(scope_id) if isinstance(scopes,Mapping) else None
    if not isinstance(scope,Mapping):
        raise ClaimGateError("EXECUTION_SCOPE_NOT_CANONICAL")
    return scope


def _validate_scope(registry: Mapping[str,Any], manifest: Mapping[str,Any]) -> list[str]:
    scope=manifest.get("execution_scope")
    if not isinstance(scope,Mapping):
        raise ClaimGateError("EXECUTION_SCOPE_REQUIRED")
    mode=scope.get("mode")
    owner=str(scope.get("owner") or "")
    if owner!="Main CIO":
        raise ClaimGateError("EXECUTION_SCOPE_OWNER_MISMATCH")

    if mode=="FULL_MAIN_CIO":
        expected=_currently_assigned_ids(registry,owner)
        provided=sorted(str(x) for x in (scope.get("assigned_ids") or []))
        if provided!=expected:
            raise ClaimGateError("FULL_OWNER_SCOPE_OMITS_OR_ADDS_ASSIGNED_CASES")
        return expected

    if mode!="SCOPED_RECOVERY":
        raise ClaimGateError("EXECUTION_SCOPE_MODE_UNSUPPORTED")

    scope_id=str(scope.get("scope_id") or "")
    canonical=_canonical_scope(registry,scope_id)
    if canonical.get("owner")!=owner:
        raise ClaimGateError("SCOPED_RECOVERY_OWNER_MISMATCH")
    if canonical.get("authorization_source")!=scope.get("authorization_source"):
        raise ClaimGateError("SCOPED_RECOVERY_AUTHORIZATION_MISMATCH")

    expected_binding=canonical.get("executor_binding")
    provided_binding=scope.get("executor_binding")
    if not isinstance(expected_binding,Mapping) or not isinstance(provided_binding,Mapping):
        raise ClaimGateError("SCOPED_RECOVERY_EXECUTOR_BINDING_REQUIRED")
    if not _binding_equal(expected_binding,provided_binding):
        raise ClaimGateError("SCOPED_RECOVERY_EXECUTOR_BINDING_MISMATCH")

    live_jobs=registry.get("live_jobs") or {}
    actual=live_jobs.get(str(expected_binding.get("job_id")))
    if not isinstance(actual,Mapping) or not _binding_equal(expected_binding,actual):
        raise ClaimGateError("SCOPED_RECOVERY_LIVE_EXECUTOR_MISMATCH")

    transfer=canonical.get("merged_criteria_transfer")
    provided_transfer=scope.get("merged_criteria_transfer")
    if transfer!=provided_transfer:
        raise ClaimGateError("MERGED_CRITERIA_TRANSFER_MISMATCH")

    expected=sorted(str(x) for x in (canonical.get("assigned_ids") or []))
    provided=sorted(str(x) for x in (scope.get("assigned_ids") or []))
    if not expected or provided!=expected:
        raise ClaimGateError("SCOPED_RECOVERY_OMITS_OR_ADDS_ASSIGNED_CASES")
    return expected


def evaluate_manifest(registry: Mapping[str,Any], manifest: Mapping[str,Any]) -> dict[str,Any]:
    try:
        required_ids=_validate_scope(registry,manifest)
    except ClaimGateError as exc:
        return {
            "status":"CLIENT_EXECUTION_CLAIM_BLOCKED",
            "reason":str(exc),
            "completion_claim_allowed":False,
        }

    results=manifest.get("obligation_results")
    if not isinstance(results,list):
        return {
            "status":"CLIENT_EXECUTION_CLAIM_BLOCKED",
            "reason":"OBLIGATION_RESULTS_LIST_REQUIRED",
            "completion_claim_allowed":False,
        }
    by_id={str(r.get("id")):r for r in results if isinstance(r,Mapping) and r.get("id")}
    missing=[rid for rid in required_ids if rid not in by_id]
    if missing:
        return {
            "status":"CLIENT_EXECUTION_CLAIM_BLOCKED",
            "reason":"ASSIGNED_CASES_OMITTED:"+",".join(missing),
            "completion_claim_allowed":False,
        }

    case_results=[]
    any_blocked=False
    for rid in required_ids:
        claimed=by_id[rid]
        canonical=next((r for r in registry.get("obligations",[]) if isinstance(r,Mapping) and r.get("id")==rid),None)
        if not isinstance(canonical,Mapping):
            any_blocked=True
            case_results.append({"id":rid,"status":"BLOCKED","reason":"CANONICAL_OBLIGATION_MISSING"})
            continue

        canonical_acceptance=acceptance_check(registry,rid)
        claimed_outcome=claimed.get("acceptance_result")
        claimed_status=claimed.get("status")
        external_blocker=claimed.get("external_blocker")

        if canonical.get("status") in NON_SUCCESS_TERMINAL and claimed_outcome==ACCEPTANCE_PASS:
            any_blocked=True
            case_results.append({"id":rid,"status":"BLOCKED","reason":"NON_SUCCESS_TERMINAL_CANNOT_PASS"})
            continue
        if claimed_status=="CANCELLED" and claimed_outcome==ACCEPTANCE_PASS:
            any_blocked=True
            case_results.append({"id":rid,"status":"BLOCKED","reason":"CANCELLED_CANNOT_PASS"})
            continue

        if claimed_outcome==ACCEPTANCE_PASS:
            if canonical_acceptance["status"]!="OBLIGATION_ACCEPTANCE_PASS":
                any_blocked=True
                case_results.append({"id":rid,"status":"BLOCKED","reason":"CANONICAL_ACCEPTANCE_NOT_PASS"})
                continue
            evidence=claimed.get("completion_evidence")
            effect=claimed.get("effect_evidence")
            if not isinstance(evidence,Mapping) or not evidence.get("source_ref"):
                any_blocked=True
                case_results.append({"id":rid,"status":"BLOCKED","reason":"COMPLETION_EVIDENCE_REQUIRED"})
                continue
            if not isinstance(effect,Mapping) or effect.get("verified") is not True or not effect.get("source_ref"):
                any_blocked=True
                case_results.append({"id":rid,"status":"BLOCKED","reason":"EFFECT_EVIDENCE_REQUIRED"})
                continue
            case_results.append({"id":rid,"status":"PASS","reason":"CANONICAL_COMPLETION_AND_EFFECT_VERIFIED"})
            continue

        # A scoped recovery can legitimately report FAIL with an evidence-backed
        # blocker. This is not completion and cannot flip canonical history.
        if claimed_outcome!=ACCEPTANCE_FAIL:
            any_blocked=True
            case_results.append({"id":rid,"status":"BLOCKED","reason":"EXPLICIT_ACCEPTANCE_RESULT_REQUIRED"})
            continue
        if isinstance(external_blocker,Mapping) and external_blocker.get("source_ref") and external_blocker.get("classification"):
            case_results.append({
                "id":rid,
                "status":"FAIL",
                "reason":"EVIDENCE_BACKED_EXTERNAL_BLOCKER",
                "external_blocker":dict(external_blocker),
            })
        else:
            # Routine status-only execution is not enough evidence.
            any_blocked=True
            case_results.append({"id":rid,"status":"BLOCKED","reason":"FAIL_REQUIRES_EVIDENCE_BACKED_BLOCKER_OR_CRITERION_RESULT"})

    completion_allowed=(not any_blocked and all(r["status"]=="PASS" for r in case_results))
    return {
        "status":"CLIENT_EXECUTION_CLAIM_PASS" if completion_allowed else "CLIENT_EXECUTION_CLAIM_BLOCKED",
        "required_ids":required_ids,
        "case_results":case_results,
        "completion_claim_allowed":completion_allowed,
    }


def main(argv: Optional[list[str]]=None) -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--registry",type=Path,required=True)
    p.add_argument("--manifest",type=Path,required=True)
    args=p.parse_args(argv)
    registry=json.loads(args.registry.read_text(encoding="utf-8"))
    manifest=json.loads(args.manifest.read_text(encoding="utf-8"))
    result=evaluate_manifest(registry,manifest)
    print(json.dumps(result,indent=2,sort_keys=True))
    return 0 if result["status"]=="CLIENT_EXECUTION_CLAIM_PASS" else 4


if __name__=="__main__":
    raise SystemExit(main())
