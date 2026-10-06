from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from custom_scripts.issue18_obligation_compat import (
    ACCEPTANCE_FAIL,
    ACCEPTANCE_PASS,
    NON_SUCCESS_TERMINAL,
    LIVE_EXECUTOR_STATES,
    acceptance_check,
    adapt_canonical_registry_snapshot,
)


class ClaimGateError(ValueError):
    pass


def _present(mapping: Mapping[str,Any], key: str) -> bool:
    return key in mapping and mapping[key] is not None and not (isinstance(mapping[key],str) and mapping[key]=="")


def _key_present(mapping: Mapping[str,Any], key: str) -> bool:
    return key in mapping


def _nonempty_string(value: Any) -> bool:
    return isinstance(value,str) and bool(value.strip())


def _valid_result_alias(value: Any) -> bool:
    return isinstance(value,str) and value in {ACCEPTANCE_PASS,ACCEPTANCE_FAIL}


def _parse_alias_time(value: Any) -> Optional[datetime]:
    if not _nonempty_string(value):
        return None
    try:
        parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _require_scope_field(mapping: Mapping[str,Any], key: str, prefix: str) -> Any:
    if not _present(mapping,key):
        raise ClaimGateError(f"{prefix}.{key}:REQUIRED")
    return mapping[key]


def _semantic_time(value: Any) -> Any:
    parsed=_parse_alias_time(value)
    return parsed.isoformat() if parsed is not None else value


def _schedule_alias_conflict(mapping: Mapping[str,Any]) -> bool:
    return (
        mapping.get("_canonical_schedule_alias_conflict") is not None
        or (
            _key_present(mapping,"scheduled_at")
            and _key_present(mapping,"run_at")
            and _semantic_time(mapping.get("scheduled_at"))!=_semantic_time(mapping.get("run_at"))
        )
    )


def _semantic_alias_value(value: Any) -> Any:
    if isinstance(value,Mapping):
        out={str(k):_semantic_alias_value(v) for k,v in value.items() if not str(k).startswith("_legacy_alias_conflicts")}
        if _present(value,"scheduled_at") or _present(value,"run_at"):
            scheduled=value.get("scheduled_at") if _present(value,"scheduled_at") else value.get("run_at")
            out.pop("run_at",None)
            out["scheduled_at"]=_semantic_time(scheduled)
        if _present(value,"id") or _present(value,"obligation_id"):
            identity=value.get("id") if _present(value,"id") else value.get("obligation_id")
            if _present(value,"id") and _present(value,"obligation_id") and value.get("id")!=value.get("obligation_id"):
                return out
            out.pop("obligation_id",None)
            out["id"]=identity
        if _present(value,"acceptance_result") or _present(value,"result"):
            result=value.get("acceptance_result") if _present(value,"acceptance_result") else value.get("result")
            if _present(value,"acceptance_result") and _present(value,"result") and value.get("acceptance_result")!=value.get("result"):
                return out
            out.pop("result",None)
            out["acceptance_result"]=result
        return out
    if isinstance(value,list):
        return [_semantic_alias_value(v) for v in value]
    return value


def _alias_values_equal(left: Any, right: Any) -> bool:
    return _semantic_alias_value(left)==_semantic_alias_value(right)


def _raw_alias_tree_conflicts(value: Any, *, path: str) -> list[dict[str,Any]]:
    conflicts=[]
    if isinstance(value,Mapping):
        for field in ("scheduled_at","run_at"):
            if _key_present(value,field) and _parse_alias_time(value.get(field)) is None:
                conflicts.append({
                    "kind":"LEGACY_SCHEDULE_ALIAS_DOMAIN_INVALID",
                    "path":path,
                    "field":field,
                    "value":copy.deepcopy(value.get(field)),
                })
        if _schedule_alias_conflict(value):
            conflicts.append({
                "kind":"LEGACY_RAW_SCHEDULE_ALIAS_CONFLICT",
                "path":path,
                "scheduled_at":copy.deepcopy(value.get("scheduled_at")),
                "run_at":copy.deepcopy(value.get("run_at")),
            })

        for field in ("id","obligation_id"):
            if _key_present(value,field) and not _nonempty_string(value.get(field)):
                conflicts.append({
                    "kind":"LEGACY_RESULT_ID_DOMAIN_INVALID",
                    "path":path,
                    "field":field,
                    "value":copy.deepcopy(value.get(field)),
                })
        if _key_present(value,"id") and _key_present(value,"obligation_id"):
            if _nonempty_string(value.get("id")) and _nonempty_string(value.get("obligation_id")) and value.get("id")!=value.get("obligation_id"):
                conflicts.append({
                    "kind":"LEGACY_RESULT_ID_ALIAS_CONFLICT",
                    "path":path,
                    "id":copy.deepcopy(value.get("id")),
                    "obligation_id":copy.deepcopy(value.get("obligation_id")),
                })

        has_acceptance=_key_present(value,"acceptance_result")
        has_result=_key_present(value,"result")
        if has_acceptance and not _valid_result_alias(value.get("acceptance_result")):
            conflicts.append({
                "kind":"LEGACY_ACCEPTANCE_RESULT_DOMAIN_INVALID",
                "path":path,
                "field":"acceptance_result",
                "value":copy.deepcopy(value.get("acceptance_result")),
            })
        if has_result and not _valid_result_alias(value.get("result")):
            conflicts.append({
                "kind":"LEGACY_ACCEPTANCE_RESULT_DOMAIN_INVALID",
                "path":path,
                "field":"result",
                "value":copy.deepcopy(value.get("result")),
            })
        if has_acceptance and has_result:
            left=value.get("acceptance_result"); right=value.get("result")
            if (
                _valid_result_alias(left)
                and _valid_result_alias(right)
                and left!=right
            ):
                conflicts.append({
                    "kind":"LEGACY_ACCEPTANCE_RESULT_ALIAS_CONFLICT",
                    "path":path,
                    "acceptance_result":copy.deepcopy(left),
                    "result":copy.deepcopy(right),
                })
        for key,child in value.items():
            if str(key).startswith("_legacy_alias_conflicts"):
                continue
            conflicts.extend(_raw_alias_tree_conflicts(child,path=f"{path}.{key}"))
    elif isinstance(value,list):
        for index,child in enumerate(value):
            conflicts.extend(_raw_alias_tree_conflicts(child,path=f"{path}[{index}]"))
    return conflicts


def _normalize_binding(binding: Mapping[str,Any]) -> dict[str,Any]:
    if _schedule_alias_conflict(binding):
        raise ClaimGateError("SCHEDULE_ALIAS_CONFLICT")
    out=copy.deepcopy(dict(binding))
    if not _present(out,"scheduled_at") and _present(out,"run_at"):
        out["scheduled_at"]=out["run_at"]
    return out


def _binding_equal(expected: Mapping[str,Any], actual: Mapping[str,Any], *, prefix: str) -> bool:
    if _schedule_alias_conflict(expected) or _schedule_alias_conflict(actual):
        raise ClaimGateError(f"{prefix}:SCHEDULE_ALIAS_CONFLICT")
    left=_normalize_binding(expected); right=_normalize_binding(actual)
    for key in ("job_id","run_id","model","provider","route_kind","state"):
        _require_scope_field(left,key,prefix)
        _require_scope_field(right,key,f"{prefix}.actual")
        if left[key]!=right[key]:
            return False
    if _present(left,"scheduled_at"):
        _require_scope_field(right,"scheduled_at",f"{prefix}.actual")
        if _semantic_time(left["scheduled_at"])!=_semantic_time(right["scheduled_at"]):
            return False
    return True


def _all_owner_ids(registry: Mapping[str,Any], owner: str) -> list[str]:
    return sorted(
        str(r.get("id")) for r in (registry.get("obligations") or [])
        if isinstance(r,Mapping) and r.get("owner")==owner and r.get("id")
    )


def _canonical_scope(registry: Mapping[str,Any], scope_id: str) -> Mapping[str,Any]:
    scopes=registry.get("execution_scopes") or {}
    scope=scopes.get(scope_id) if isinstance(scopes,Mapping) else None
    if not isinstance(scope,Mapping):
        raise ClaimGateError("EXECUTION_SCOPE_NOT_CANONICAL")
    return scope


def _validate_scope_binding(registry: Mapping[str,Any], canonical: Mapping[str,Any], provided: Mapping[str,Any], prefix: str) -> None:
    expected=canonical.get("executor_binding"); presented=provided.get("executor_binding")
    if not isinstance(expected,Mapping) or not isinstance(presented,Mapping):
        raise ClaimGateError(f"{prefix}_EXECUTOR_BINDING_REQUIRED")
    if not _binding_equal(expected,presented,prefix=f"{prefix}.binding"):
        raise ClaimGateError(f"{prefix}_EXECUTOR_BINDING_MISMATCH")
    expected_norm=_normalize_binding(expected)
    if str(expected_norm.get("state")) not in LIVE_EXECUTOR_STATES:
        raise ClaimGateError(f"{prefix}_EXECUTOR_NOT_EXECUTABLE")
    live_jobs=registry.get("live_jobs") or {}
    actual=live_jobs.get(str(expected_norm.get("job_id")))
    if not isinstance(actual,Mapping):
        raise ClaimGateError(f"{prefix}_LIVE_EXECUTOR_MISSING")
    if not _binding_equal(expected_norm,actual,prefix=f"{prefix}.live"):
        raise ClaimGateError(f"{prefix}_LIVE_EXECUTOR_MISMATCH")
    if str(_normalize_binding(actual).get("state")) not in LIVE_EXECUTOR_STATES:
        raise ClaimGateError(f"{prefix}_LIVE_EXECUTOR_NOT_EXECUTABLE")


def _validate_scope(registry: Mapping[str,Any], manifest: Mapping[str,Any]) -> list[str]:
    scope=manifest.get("execution_scope")
    if not isinstance(scope,Mapping):
        raise ClaimGateError("EXECUTION_SCOPE_REQUIRED")
    mode=_require_scope_field(scope,"mode","execution_scope")
    owner=_require_scope_field(scope,"owner","execution_scope")
    if owner!="Main CIO":
        raise ClaimGateError("EXECUTION_SCOPE_OWNER_MISMATCH")
    scope_id=str(_require_scope_field(scope,"scope_id","execution_scope"))
    canonical=_canonical_scope(registry,scope_id)
    if canonical.get("mode")!=mode:
        raise ClaimGateError("EXECUTION_SCOPE_MODE_MISMATCH")
    if canonical.get("owner")!=owner:
        raise ClaimGateError("EXECUTION_SCOPE_OWNER_MISMATCH")
    auth=_require_scope_field(scope,"authorization_source","execution_scope")
    if canonical.get("authorization_source")!=auth:
        raise ClaimGateError("EXECUTION_SCOPE_AUTHORIZATION_MISMATCH")
    _validate_scope_binding(registry,canonical,scope,str(mode))

    expected=sorted(str(x) for x in (canonical.get("assigned_ids") or []))
    provided=sorted(str(x) for x in (scope.get("assigned_ids") or []))
    if not expected:
        raise ClaimGateError("EXECUTION_SCOPE_HAS_NO_ASSIGNED_OBLIGATIONS")
    if provided!=expected:
        raise ClaimGateError(f"{mode}_OMITS_OR_ADDS_ASSIGNED_CASES")
    if mode=="FULL_MAIN_CIO":
        if expected!=_all_owner_ids(registry,str(owner)):
            raise ClaimGateError("FULL_OWNER_CANONICAL_SCOPE_INCOMPLETE")
        return expected
    if mode!="SCOPED_RECOVERY":
        raise ClaimGateError("EXECUTION_SCOPE_MODE_UNSUPPORTED")
    if canonical.get("merged_criteria_transfer")!=scope.get("merged_criteria_transfer"):
        raise ClaimGateError("MERGED_CRITERIA_TRANSFER_MISMATCH")
    return expected


def _evidence_backed_failure(claimed: Mapping[str,Any]) -> bool:
    for key in ("external_blocker","historical_evidence"):
        item=claimed.get(key)
        if isinstance(item,Mapping) and item.get("source_ref") and item.get("classification"):
            return True
    criteria=claimed.get("criterion_evidence")
    return isinstance(criteria,Mapping) and bool(criteria) and all(
        isinstance(item,Mapping) and item.get("source_ref") for item in criteria.values()
    )


def adapt_legacy_manifest(raw: Mapping[str,Any]) -> tuple[dict[str,Any],dict[str,Any]]:
    """Nonmutating adapter for the installed client's manifest field aliases."""
    before=copy.deepcopy(raw); out=copy.deepcopy(raw); changed=[]; conflicts=[]

    for field in ("execution_scope","scope"):
        if field in out:
            if not isinstance(out[field],Mapping):
                conflicts.append({"kind":"LEGACY_SCOPE_DOMAIN_INVALID","path":field,"value":copy.deepcopy(out[field])})
            else:
                conflicts.extend(_raw_alias_tree_conflicts(out[field],path=field))
    for field in ("obligation_results","results"):
        if field in out:
            if not isinstance(out[field],list):
                conflicts.append({"kind":"LEGACY_RESULTS_DOMAIN_INVALID","path":field,"value":copy.deepcopy(out[field])})
            else:
                for index,row in enumerate(out[field]):
                    if not isinstance(row,Mapping):
                        conflicts.append({
                            "kind":"LEGACY_RESULT_ROW_DOMAIN_INVALID",
                            "path":f"{field}[{index}]",
                            "value":copy.deepcopy(row),
                        })
                    else:
                        conflicts.extend(_raw_alias_tree_conflicts(row,path=f"{field}[{index}]"))

    if isinstance(out.get("execution_scope"),Mapping) and isinstance(out.get("scope"),Mapping):
        if not _alias_values_equal(out["execution_scope"],out["scope"]):
            conflicts.append({"kind":"LEGACY_SCOPE_ALIAS_CONFLICT","execution_scope":copy.deepcopy(out["execution_scope"]),"scope":copy.deepcopy(out["scope"])})
    elif "execution_scope" not in out and isinstance(out.get("scope"),Mapping):
        out["execution_scope"]=copy.deepcopy(out["scope"]); changed.append("scope->execution_scope")
    if isinstance(out.get("obligation_results"),list) and isinstance(out.get("results"),list):
        if not _alias_values_equal(out["obligation_results"],out["results"]):
            conflicts.append({"kind":"LEGACY_RESULTS_ALIAS_CONFLICT","obligation_results":copy.deepcopy(out["obligation_results"]),"results":copy.deepcopy(out["results"])})
    elif "obligation_results" not in out and isinstance(out.get("results"),list):
        out["obligation_results"]=copy.deepcopy(out["results"]); changed.append("results->obligation_results")
    rows=out.get("obligation_results")
    if isinstance(rows,list):
        adapted=[]
        for raw_row in rows:
            row=copy.deepcopy(raw_row)
            if isinstance(row,dict):
                row_conflicts=[]
                if _key_present(row,"id") and _key_present(row,"obligation_id") and _nonempty_string(row.get("id")) and _nonempty_string(row.get("obligation_id")) and row["id"]!=row["obligation_id"]:
                    row_conflicts.append({"kind":"LEGACY_RESULT_ID_ALIAS_CONFLICT","id":copy.deepcopy(row["id"]),"obligation_id":copy.deepcopy(row["obligation_id"])})
                elif "id" not in row and _nonempty_string(row.get("obligation_id")):
                    row["id"]=row["obligation_id"]; changed.append("obligation_id->id")
                if _key_present(row,"acceptance_result") and _key_present(row,"result") and _valid_result_alias(row.get("acceptance_result")) and _valid_result_alias(row.get("result")) and row["acceptance_result"]!=row["result"]:
                    row_conflicts.append({"kind":"LEGACY_ACCEPTANCE_RESULT_ALIAS_CONFLICT","acceptance_result":copy.deepcopy(row["acceptance_result"]),"result":copy.deepcopy(row["result"])})
                elif "acceptance_result" not in row and _valid_result_alias(row.get("result")):
                    row["acceptance_result"]=row["result"]; changed.append("result->acceptance_result")
                if row_conflicts:
                    row["_legacy_alias_conflicts"]=row_conflicts
                    conflicts.extend(row_conflicts)
            adapted.append(row)
        out["obligation_results"]=adapted
    if conflicts:
        out["_legacy_alias_conflicts"]=copy.deepcopy(conflicts)
    return out,{"mode":"LEGACY_MANIFEST_ADAPTER","input_unchanged":raw==before,"adaptations":sorted(set(changed)),"conflicts":copy.deepcopy(conflicts)}


def evaluate_manifest(registry: Mapping[str,Any], manifest: Mapping[str,Any], *, now: Optional[datetime]=None) -> dict[str,Any]:
    observed=(now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if manifest.get("_legacy_alias_conflicts"):
        kinds=",".join(str(x.get("kind")) for x in manifest["_legacy_alias_conflicts"] if isinstance(x,Mapping))
        return {"status":"CLIENT_EXECUTION_CLAIM_BLOCKED","reason":"LEGACY_MANIFEST_ALIAS_CONFLICT:"+kinds,"completion_claim_allowed":False}
    try:
        required_ids=_validate_scope(registry,manifest)
    except ClaimGateError as exc:
        return {"status":"CLIENT_EXECUTION_CLAIM_BLOCKED","reason":str(exc),"completion_claim_allowed":False}

    results=manifest.get("obligation_results")
    if not isinstance(results,list):
        return {"status":"CLIENT_EXECUTION_CLAIM_BLOCKED","reason":"OBLIGATION_RESULTS_LIST_REQUIRED","completion_claim_allowed":False}
    if not results:
        return {"status":"CLIENT_EXECUTION_CLAIM_BLOCKED","reason":"EMPTY_OBLIGATION_RESULTS_CANNOT_COMPLETE","completion_claim_allowed":False}
    rows=[r for r in results if isinstance(r,Mapping) and r.get("id")]
    ids=[str(r.get("id")) for r in rows]
    if len(ids)!=len(set(ids)):
        return {"status":"CLIENT_EXECUTION_CLAIM_BLOCKED","reason":"DUPLICATE_MANIFEST_OBLIGATION_ID","completion_claim_allowed":False}
    by_id={str(r.get("id")):r for r in rows}
    missing=[rid for rid in required_ids if rid not in by_id]
    extra=sorted(set(by_id)-set(required_ids))
    if missing:
        return {"status":"CLIENT_EXECUTION_CLAIM_BLOCKED","reason":"ASSIGNED_CASES_OMITTED:"+",".join(missing),"completion_claim_allowed":False}
    if extra:
        return {"status":"CLIENT_EXECUTION_CLAIM_BLOCKED","reason":"UNASSIGNED_CASES_INCLUDED:"+",".join(extra),"completion_claim_allowed":False}

    case_results=[]; any_blocked=False
    for rid in required_ids:
        claimed=by_id[rid]
        matches=[r for r in registry.get("obligations",[]) if isinstance(r,Mapping) and r.get("id")==rid]
        if len(matches)!=1:
            any_blocked=True; case_results.append({"id":rid,"status":"BLOCKED","reason":"CANONICAL_IDENTITY_AMBIGUOUS"}); continue
        canonical=matches[0]
        canonical_acceptance=acceptance_check(registry,rid,now=observed)
        outcome=claimed.get("acceptance_result")
        if canonical.get("status") in NON_SUCCESS_TERMINAL and outcome==ACCEPTANCE_PASS:
            any_blocked=True; case_results.append({"id":rid,"status":"BLOCKED","reason":"NON_SUCCESS_TERMINAL_CANNOT_PASS"}); continue
        if outcome==ACCEPTANCE_PASS:
            if canonical_acceptance["status"]!="OBLIGATION_ACCEPTANCE_PASS":
                any_blocked=True; case_results.append({"id":rid,"status":"BLOCKED","reason":"CANONICAL_ACCEPTANCE_NOT_PASS"}); continue
            evidence=claimed.get("completion_evidence"); effect=claimed.get("effect_evidence")
            if not isinstance(evidence,Mapping) or not evidence.get("source_ref"):
                any_blocked=True; case_results.append({"id":rid,"status":"BLOCKED","reason":"COMPLETION_EVIDENCE_REQUIRED"}); continue
            if not isinstance(effect,Mapping) or effect.get("verified") is not True or not effect.get("source_ref"):
                any_blocked=True; case_results.append({"id":rid,"status":"BLOCKED","reason":"EFFECT_EVIDENCE_REQUIRED"}); continue
            case_results.append({"id":rid,"status":"PASS","reason":"CANONICAL_COMPLETION_AND_EFFECT_VERIFIED"}); continue
        if outcome!=ACCEPTANCE_FAIL:
            any_blocked=True; case_results.append({"id":rid,"status":"BLOCKED","reason":"EXPLICIT_ACCEPTANCE_RESULT_REQUIRED"}); continue
        if not _evidence_backed_failure(claimed):
            any_blocked=True; case_results.append({"id":rid,"status":"BLOCKED","reason":"FAIL_REQUIRES_VERIFIED_CASE_EVIDENCE"}); continue
        case_results.append({"id":rid,"status":"FAIL","reason":"EVIDENCE_BACKED_NONCOMPLETION"})

    completion_allowed=bool(required_ids) and not any_blocked and all(r["status"]=="PASS" for r in case_results)
    status="CLIENT_EXECUTION_CLAIM_BLOCKED" if any_blocked else (
        "CLIENT_EXECUTION_CLAIM_PASS" if completion_allowed else "CLIENT_EXECUTION_CLAIM_RECORDED_NONCOMPLETION"
    )
    return {"status":status,"required_ids":required_ids,"case_results":case_results,
            "completion_claim_allowed":completion_allowed,"observed_at":observed.isoformat()}


def main(argv: Optional[list[str]]=None) -> int:
    p=argparse.ArgumentParser(description="Issue #18 read-only client-claim compatibility adapter")
    p.add_argument("manifest",nargs="?",type=Path)
    p.add_argument("--manifest",dest="manifest_option",type=Path)
    p.add_argument("--registry",type=Path)
    p.add_argument("--root",type=Path)
    args=p.parse_args(argv)
    manifest_path=args.manifest_option or args.manifest
    if manifest_path is None:
        p.error("manifest path is required")
    registry_path=args.registry
    if registry_path is None and args.root is not None:
        registry_path=args.root/"obligation_registry.json"
    if registry_path is None:
        p.error("--registry or --root is required for this namespaced adapter")
    raw_registry=json.loads(registry_path.read_text(encoding="utf-8"))
    registry,_=adapt_canonical_registry_snapshot(raw_registry)
    raw_manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest,compat=adapt_legacy_manifest(raw_manifest)
    result=evaluate_manifest(registry,manifest); result["manifest_compatibility"]=compat
    print(json.dumps(result,indent=2,sort_keys=True))
    return 0 if result["status"] in {"CLIENT_EXECUTION_CLAIM_PASS","CLIENT_EXECUTION_CLAIM_RECORDED_NONCOMPLETION"} else 4


if __name__=="__main__":
    raise SystemExit(main())
