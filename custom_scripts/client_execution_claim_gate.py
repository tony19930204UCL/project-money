from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Optional

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

from custom_scripts.obligation_deadline_registry import (
    ACCEPTANCE_FAIL,
    ACCEPTANCE_PASS,
    NON_SUCCESS_TERMINAL,
    acceptance_check,
    adapt_canonical_registry_snapshot,
)


class ClaimGateError(ValueError):
    pass


def _binding_equal(expected: Mapping[str,Any], actual: Mapping[str,Any]) -> bool:
    keys=("job_id","run_id","model","provider","route_kind","state")
    if not all(expected.get(k)==actual.get(k) for k in keys):
        return False
    if "scheduled_at" in expected and expected.get("scheduled_at")!=actual.get("scheduled_at"):
        return False
    return True


def _all_owner_ids(registry: Mapping[str,Any], owner: str) -> list[str]:
    ids=[]
    for record in registry.get("obligations") or []:
        if not isinstance(record,Mapping) or record.get("owner")!=owner:
            continue
        rid=record.get("id")
        if rid:
            ids.append(str(rid))
    return sorted(ids)


def _canonical_scope(registry: Mapping[str,Any], scope_id: str) -> Mapping[str,Any]:
    scopes=registry.get("execution_scopes") or {}
    scope=scopes.get(scope_id) if isinstance(scopes,Mapping) else None
    if not isinstance(scope,Mapping):
        raise ClaimGateError("EXECUTION_SCOPE_NOT_CANONICAL")
    return scope


def _validate_scope_binding(registry: Mapping[str,Any], canonical: Mapping[str,Any], provided: Mapping[str,Any], prefix: str) -> None:
    expected_binding=canonical.get("executor_binding")
    provided_binding=provided.get("executor_binding")
    if not isinstance(expected_binding,Mapping) or not isinstance(provided_binding,Mapping):
        raise ClaimGateError(f"{prefix}_EXECUTOR_BINDING_REQUIRED")
    if not _binding_equal(expected_binding,provided_binding):
        raise ClaimGateError(f"{prefix}_EXECUTOR_BINDING_MISMATCH")
    live_jobs=registry.get("live_jobs") or {}
    actual=live_jobs.get(str(expected_binding.get("job_id")))
    if not isinstance(actual,Mapping) or not _binding_equal(expected_binding,actual):
        raise ClaimGateError(f"{prefix}_LIVE_EXECUTOR_MISMATCH")


def _validate_scope(registry: Mapping[str,Any], manifest: Mapping[str,Any]) -> list[str]:
    scope=manifest.get("execution_scope")
    if not isinstance(scope,Mapping):
        raise ClaimGateError("EXECUTION_SCOPE_REQUIRED")
    mode=scope.get("mode")
    owner=str(scope.get("owner") or "")
    if owner!="Main CIO":
        raise ClaimGateError("EXECUTION_SCOPE_OWNER_MISMATCH")

    scope_id=str(scope.get("scope_id") or "")
    canonical=_canonical_scope(registry,scope_id)
    if canonical.get("mode")!=mode:
        raise ClaimGateError("EXECUTION_SCOPE_MODE_MISMATCH")
    if canonical.get("owner")!=owner:
        raise ClaimGateError("EXECUTION_SCOPE_OWNER_MISMATCH")
    if not scope.get("authorization_source") or canonical.get("authorization_source")!=scope.get("authorization_source"):
        raise ClaimGateError("EXECUTION_SCOPE_AUTHORIZATION_MISMATCH")
    _validate_scope_binding(registry,canonical,scope,mode)

    expected=sorted(str(x) for x in (canonical.get("assigned_ids") or []))
    provided=sorted(str(x) for x in (scope.get("assigned_ids") or []))
    if not expected:
        raise ClaimGateError("EXECUTION_SCOPE_HAS_NO_ASSIGNED_OBLIGATIONS")
    if provided!=expected:
        raise ClaimGateError(f"{mode}_OMITS_OR_ADDS_ASSIGNED_CASES")

    if mode=="FULL_MAIN_CIO":
        owner_ids=_all_owner_ids(registry,owner)
        if expected!=owner_ids:
            raise ClaimGateError("FULL_OWNER_CANONICAL_SCOPE_INCOMPLETE")
        return expected

    if mode!="SCOPED_RECOVERY":
        raise ClaimGateError("EXECUTION_SCOPE_MODE_UNSUPPORTED")

    transfer=canonical.get("merged_criteria_transfer")
    if transfer!=scope.get("merged_criteria_transfer"):
        raise ClaimGateError("MERGED_CRITERIA_TRANSFER_MISMATCH")
    return expected


def _evidence_backed_failure(claimed: Mapping[str,Any]) -> bool:
    blocker=claimed.get("external_blocker")
    if isinstance(blocker,Mapping) and blocker.get("source_ref") and blocker.get("classification"):
        return True
    historical=claimed.get("historical_evidence")
    if isinstance(historical,Mapping) and historical.get("source_ref") and historical.get("classification"):
        return True
    criteria=claimed.get("criterion_evidence")
    if isinstance(criteria,Mapping) and criteria:
        return all(
            isinstance(item,Mapping) and item.get("source_ref")
            for item in criteria.values()
        )
    return False


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
    if not results:
        return {
            "status":"CLIENT_EXECUTION_CLAIM_BLOCKED",
            "reason":"EMPTY_OBLIGATION_RESULTS_CANNOT_COMPLETE",
            "completion_claim_allowed":False,
        }

    rows=[r for r in results if isinstance(r,Mapping) and r.get("id")]
    ids=[str(r.get("id")) for r in rows]
    if len(ids)!=len(set(ids)):
        return {
            "status":"CLIENT_EXECUTION_CLAIM_BLOCKED",
            "reason":"DUPLICATE_MANIFEST_OBLIGATION_ID",
            "completion_claim_allowed":False,
        }
    by_id={str(r.get("id")):r for r in rows}
    missing=[rid for rid in required_ids if rid not in by_id]
    extra=sorted(set(by_id)-set(required_ids))
    if missing:
        return {
            "status":"CLIENT_EXECUTION_CLAIM_BLOCKED",
            "reason":"ASSIGNED_CASES_OMITTED:"+",".join(missing),
            "completion_claim_allowed":False,
        }
    if extra:
        return {
            "status":"CLIENT_EXECUTION_CLAIM_BLOCKED",
            "reason":"UNASSIGNED_CASES_INCLUDED:"+",".join(extra),
            "completion_claim_allowed":False,
        }

    case_results=[]
    any_blocked=False
    for rid in required_ids:
        claimed=by_id[rid]
        matches=[r for r in registry.get("obligations",[]) if isinstance(r,Mapping) and r.get("id")==rid]
        if len(matches)!=1:
            any_blocked=True
            case_results.append({"id":rid,"status":"BLOCKED","reason":"CANONICAL_IDENTITY_AMBIGUOUS"})
            continue
        canonical=matches[0]
        canonical_acceptance=acceptance_check(registry,rid)
        claimed_outcome=claimed.get("acceptance_result")
        claimed_status=claimed.get("status")

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

        if claimed_outcome!=ACCEPTANCE_FAIL:
            any_blocked=True
            case_results.append({"id":rid,"status":"BLOCKED","reason":"EXPLICIT_ACCEPTANCE_RESULT_REQUIRED"})
            continue
        if not _evidence_backed_failure(claimed):
            any_blocked=True
            case_results.append({"id":rid,"status":"BLOCKED","reason":"FAIL_REQUIRES_VERIFIED_CASE_EVIDENCE"})
            continue
        case_results.append({
            "id":rid,
            "status":"FAIL",
            "reason":"EVIDENCE_BACKED_NONCOMPLETION",
        })

    completion_allowed=bool(required_ids) and not any_blocked and all(r["status"]=="PASS" for r in case_results)
    if any_blocked:
        status="CLIENT_EXECUTION_CLAIM_BLOCKED"
    elif completion_allowed:
        status="CLIENT_EXECUTION_CLAIM_PASS"
    else:
        status="CLIENT_EXECUTION_CLAIM_RECORDED_NONCOMPLETION"
    return {
        "status":status,
        "required_ids":required_ids,
        "case_results":case_results,
        "completion_claim_allowed":completion_allowed,
    }


def main(argv: Optional[list[str]]=None) -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--registry",type=Path,required=True)
    p.add_argument("--manifest",type=Path,required=True)
    args=p.parse_args(argv)
    raw_registry=json.loads(args.registry.read_text(encoding="utf-8"))
    registry,_=adapt_canonical_registry_snapshot(raw_registry)
    manifest=json.loads(args.manifest.read_text(encoding="utf-8"))
    result=evaluate_manifest(registry,manifest)
    print(json.dumps(result,indent=2,sort_keys=True))
    return 0 if result["status"] in {"CLIENT_EXECUTION_CLAIM_PASS","CLIENT_EXECUTION_CLAIM_RECORDED_NONCOMPLETION"} else 4


if __name__=="__main__":
    raise SystemExit(main())
