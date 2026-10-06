from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from custom_scripts.client_execution_claim_gate import evaluate_manifest
from custom_scripts.obligation_deadline_registry import (
    acceptance_check,
    migrate_registry_snapshot,
    validate_registry,
)

NOW=datetime(2026,10,6,12,0,tzinfo=timezone.utc)


def live_job(job_id="job-1", *, route_kind="local"):
    return {
        "job_id":job_id,
        "run_id":"run-1",
        "model":"local-model",
        "provider":"local-provider",
        "route_kind":route_kind,
    }


def binding(job_id="job-1", *, scheduled_at="2026-10-09T09:00:00+00:00", route_kind="local"):
    return {
        **live_job(job_id,route_kind=route_kind),
        "scheduled_at":scheduled_at,
    }


def original_history(deadline="2026-10-10T09:00:00+00:00"):
    return [{"kind":"ORIGINAL_SCHEDULE","scheduled_at":deadline,"source_ref":"artifact://original-schedule"}]


def criterion(pass_value=True):
    return {
        "criterion-a":{
            "satisfied":pass_value,
            "source_ref":"artifact://criterion-a",
        }
    }


def active_record(rid="active-1"):
    return {
        "id":rid,
        "owner":"Main CIO",
        "status":"ACTIVE",
        "original_deadline":"2026-10-10T09:00:00+00:00",
        "execution_history":original_history(),
        "executor_binding":binding(),
        "acceptance_result":"FAIL",
        "original_criteria":["criterion-a"],
        "criteria_evidence":criterion(False),
        "required_execution_fields":["attempt_count"],
        "attempt_count":0,
    }


def failed_record(rid="failed-1"):
    return {
        "id":rid,
        "owner":"Main CIO",
        "status":"FAILED",
        "original_deadline":"2026-10-01T09:00:00+00:00",
        "execution_history":original_history("2026-10-01T09:00:00+00:00"),
        "acceptance_result":"FAIL",
        "failure_disposition":"HISTORICAL_ACCEPTANCE_FAILED",
        "original_criteria":["criterion-a"],
        "criteria_evidence":criterion(False),
    }


def closed_record(rid="closed-1"):
    return {
        "id":rid,
        "owner":"Main CIO",
        "status":"CLOSED",
        "original_deadline":"2026-09-30T09:00:00+00:00",
        "execution_history":original_history("2026-09-30T09:00:00+00:00"),
        "acceptance_result":"FAIL",
        "historical_acceptance_result":"FAIL",
        "closure_disposition":"RETIRED_TRANSFERRED_CONTAINER",
        "original_criteria":["criterion-a"],
        "criteria_evidence":criterion(False),
    }


def completed_record(rid="completed-1"):
    return {
        "id":rid,
        "owner":"Main CIO",
        "status":"COMPLETED",
        "original_deadline":"2026-10-10T09:00:00+00:00",
        "execution_history":original_history(),
        "acceptance_result":"PASS",
        "completed_at":"2026-10-09T08:00:00+00:00",
        "completed_at_source_ref":"artifact://original-completion-record",
        "completed_at_source_kind":"ORIGINAL_ARTIFACT",
        "original_criteria":["criterion-a"],
        "criteria_evidence":criterion(True),
    }


def registry(*records, route_kind="local"):
    return {
        "schema_version":1,
        "live_jobs":{"job-1":live_job(route_kind=route_kind)},
        "obligations":list(records),
        "execution_scopes":{},
    }


def documented_late_recovery(rid="late-1"):
    return {
        "id":rid,
        "owner":"Main CIO",
        "status":"COMPLETED",
        "original_deadline":"2026-10-01T09:00:00+00:00",
        "execution_history":original_history("2026-10-01T09:00:00+00:00"),
        "acceptance_result":"FAIL",
        "completed_at":"2026-10-06T10:00:00+00:00",
        "completed_at_source_ref":"artifact://historical-recovery-observation",
        "completed_at_source_kind":"ORIGINAL_ARTIFACT",
        "original_criteria":["criterion-a"],
        "criteria_evidence":criterion(False),
        "recovery":{
            "authorized":True,
            "authorization_source":"decision://main-cio/recovery-1",
            "observed_at":"2026-10-06T10:00:00+00:00",
            "classification":"LATE_RECOVERY",
            "executor_readback":live_job(),
            "source_refs":["artifact://recovery-run","artifact://live-job-readback"],
        },
    }


def test_terminal_failed_and_closed_are_structurally_valid_but_never_acceptance_pass():
    reg=registry(failed_record(),closed_record())
    assert validate_registry(reg,now=NOW)["status"]=="DEADLINE_REGISTRY_VALID"
    for rid in ("failed-1","closed-1"):
        result=acceptance_check(reg,rid,now=NOW)
        assert result["status"]=="OBLIGATION_ACCEPTANCE_BLOCKED"
        assert result["criterion_outcome"]=="FAIL"


def test_documented_late_recovery_is_structurally_valid_but_retains_fail():
    reg=registry(documented_late_recovery())
    assert validate_registry(reg,now=NOW)["status"]=="DEADLINE_REGISTRY_VALID"
    result=acceptance_check(reg,"late-1",now=NOW)
    assert result["status"]=="OBLIGATION_ACCEPTANCE_BLOCKED"
    assert result["record_acceptance_result"]=="FAIL"


def test_forged_boolean_recovery_and_ordinary_late_registration_are_rejected():
    ordinary=active_record("ordinary-late")
    ordinary["original_deadline"]="2026-10-01T09:00:00+00:00"
    ordinary["execution_history"]=original_history("2026-10-01T09:00:00+00:00")
    ordinary["executor_binding"]=binding(scheduled_at="2026-10-01T08:00:00+00:00")
    ordinary["recovery_authorized"]=True
    result=validate_registry(registry(ordinary),now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_INVALID"
    assert "AUTHORIZED_RECOVERY_REQUIRED" in result["errors"][0]

    forged=deepcopy(ordinary)
    forged["recovery"]={"authorized":True}
    result=validate_registry(registry(forged),now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_INVALID"
    assert any("authorization_source" in error for error in result["errors"])


def test_positive_normal_registration_accepts_zero_numeric_execution_field():
    reg=registry(active_record())
    result=validate_registry(reg,now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_VALID"
    assert result["record_results"][0]["valid"] is True


def test_completed_without_original_completion_timestamp_or_source_is_blocked():
    missing_time=completed_record("missing-time")
    missing_time.pop("completed_at")
    result=validate_registry(registry(missing_time),now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_INVALID"
    assert any("completed_at:REQUIRED" in error for error in result["errors"])

    missing_source=completed_record("missing-source")
    missing_source.pop("completed_at_source_ref")
    result=validate_registry(registry(missing_source),now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_INVALID"
    assert any("completed_at_source_ref:REQUIRED" in error for error in result["errors"])

    fake_source=completed_record("fake-source")
    fake_source["completed_at_source_kind"]="REVIEWED_AT"
    result=validate_registry(registry(fake_source),now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_INVALID"
    assert any("ORIGINAL_ARTIFACT_REQUIRED" in error for error in result["errors"])


def test_local_only_job_cannot_be_claimed_as_origin_delivery():
    late=documented_late_recovery()
    late["recovery"]["executor_readback"]["origin_delivery"]=True
    result=validate_registry(registry(late,route_kind="local"),now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_INVALID"
    assert any("LOCAL_ROUTE_CANNOT_BECOME_ORIGIN" in error for error in result["errors"])


def test_original_deadline_history_mismatch_is_rejected():
    record=active_record()
    record["original_deadline"]="2026-10-11T09:00:00+00:00"
    result=validate_registry(registry(record),now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_INVALID"
    assert any("ORIGINAL_DEADLINE_HISTORY_MISMATCH" in error for error in result["errors"])


def test_cancelled_failed_and_closed_cannot_be_converted_to_acceptance_pass():
    cancelled=failed_record("cancelled")
    cancelled["status"]="CANCELLED"
    cancelled.pop("failure_disposition")
    cancelled["acceptance_result"]="PASS"
    failed=failed_record()
    failed["acceptance_result"]="PASS"
    closed=closed_record()
    closed["acceptance_result"]="PASS"
    for record,reason in ((cancelled,"CANCELLED_CANNOT_PASS"),(failed,"FAILED_CANNOT_PASS"),(closed,"CLOSED_CANNOT_PASS")):
        result=validate_registry(registry(record),now=NOW)
        assert result["status"]=="DEADLINE_REGISTRY_INVALID"
        assert any(reason in error for error in result["errors"])


def test_migration_is_reversible_idempotent_and_preserves_deadline_failure_history():
    before=registry({
        "id":"legacy-failed",
        "owner":"Main CIO",
        "status":"ACTIVE",
        "legacy_terminal_state":"FAILED",
        "legacy_acceptance_result":"FAIL",
        "original_deadline":"2026-09-30T09:00:00+00:00",
        "execution_history":original_history("2026-09-30T09:00:00+00:00"),
        "acceptance_result":"FAIL",
        "failure_disposition":"ORIGINAL_FAILURE",
        "original_criteria":["criterion-a"],
        "criteria_evidence":criterion(False),
    },{
        "id":"legacy-complete-no-time",
        "owner":"Main CIO",
        "status":"COMPLETED",
        "original_deadline":"2026-10-01T09:00:00+00:00",
        "execution_history":original_history("2026-10-01T09:00:00+00:00"),
        "acceptance_result":"FAIL",
        "original_criteria":["criterion-a"],
        "criteria_evidence":criterion(False),
    })
    refs={
        "status":"artifact://terminal-disposition",
        "historical_acceptance_result":"artifact://historical-acceptance",
        "missing_completion_timestamp":"artifact://legacy-record-review",
    }
    after,report=migrate_registry_snapshot(before,refs)
    assert report["reversible"] is True
    assert report["before_sha256"]!=report["after_sha256"]
    assert report["original_deadline_invariant"] is True
    assert report["original_failure_invariant"] is True
    assert after["obligations"][0]["status"]=="FAILED"
    assert after["obligations"][0]["original_deadline"]==before["obligations"][0]["original_deadline"]
    assert after["obligations"][0]["failure_disposition"]=="ORIGINAL_FAILURE"
    assert after["obligations"][1]["migration_blocked_reason"]=="ORIGINAL_COMPLETION_TIMESTAMP_MISSING"
    assert "completed_at" not in after["obligations"][1]
    assert all(change["source_ref"].startswith("artifact://") for change in report["changes"])
    assert all("before" in change and "after" in change for change in report["changes"])

    replay,replay_report=migrate_registry_snapshot(after,refs)
    assert replay==after
    assert replay_report["changes"]==[]
    assert replay_report["before_sha256"]==replay_report["after_sha256"]


def scoped_registry(*records, assigned_ids):
    reg=registry(*records)
    reg["execution_scopes"]={
        "scope-recovery":{
            "owner":"Main CIO",
            "authorization_source":"decision://main-cio/scope-recovery",
            "assigned_ids":list(assigned_ids),
            "executor_binding":live_job(),
            "merged_criteria_transfer":{
                "source_ids":["legacy-container-a","legacy-container-b"],
                "target_ids":list(assigned_ids),
                "source_ref":"artifact://merged-criteria-transfer",
            },
        }
    }
    return reg


def scoped_manifest(assigned_ids):
    return {
        "execution_scope":{
            "mode":"SCOPED_RECOVERY",
            "scope_id":"scope-recovery",
            "owner":"Main CIO",
            "authorization_source":"decision://main-cio/scope-recovery",
            "assigned_ids":list(assigned_ids),
            "executor_binding":live_job(),
            "merged_criteria_transfer":{
                "source_ids":["legacy-container-a","legacy-container-b"],
                "target_ids":list(assigned_ids),
                "source_ref":"artifact://merged-criteria-transfer",
            },
        },
        "obligation_results":[],
    }


def test_scoped_claim_rejects_omitted_assigned_case_and_forged_scope():
    reg=scoped_registry(active_record("a"),active_record("b"),assigned_ids=["a","b"])
    manifest=scoped_manifest(["a","b"])
    manifest["obligation_results"]=[{
        "id":"a","status":"ACTIVE","acceptance_result":"FAIL",
        "external_blocker":{"classification":"EXTERNAL","source_ref":"artifact://blocker-a"},
    }]
    result=evaluate_manifest(reg,manifest)
    assert result["status"]=="CLIENT_EXECUTION_CLAIM_BLOCKED"
    assert result["reason"]=="ASSIGNED_CASES_OMITTED:b"

    forged=scoped_manifest(["a","b"])
    forged["execution_scope"]["authorization_source"]="decision://forged"
    result=evaluate_manifest(reg,forged)
    assert result["status"]=="CLIENT_EXECUTION_CLAIM_BLOCKED"
    assert result["reason"]=="SCOPED_RECOVERY_AUTHORIZATION_MISMATCH"


def test_scoped_claim_rejects_cancelled_or_terminal_failure_as_pass():
    cancelled=failed_record("cancelled")
    cancelled["status"]="CANCELLED"
    cancelled.pop("failure_disposition")
    failed=failed_record("failed")
    for record in (cancelled,failed):
        reg=scoped_registry(record,assigned_ids=[record["id"]])
        manifest=scoped_manifest([record["id"]])
        manifest["obligation_results"]=[{
            "id":record["id"],"status":record["status"],"acceptance_result":"PASS",
            "completion_evidence":{"source_ref":"artifact://fake-completion"},
            "effect_evidence":{"verified":True,"source_ref":"artifact://fake-effect"},
        }]
        result=evaluate_manifest(reg,manifest)
        assert result["status"]=="CLIENT_EXECUTION_CLAIM_BLOCKED"
        assert result["completion_claim_allowed"] is False


def test_routine_status_only_fail_is_blocked_but_evidence_backed_scoped_recovery_is_recorded_noncompletion():
    reg=scoped_registry(active_record("a"),assigned_ids=["a"])

    status_only=scoped_manifest(["a"])
    status_only["obligation_results"]=[{"id":"a","status":"ACTIVE","acceptance_result":"FAIL"}]
    result=evaluate_manifest(reg,status_only)
    assert result["status"]=="CLIENT_EXECUTION_CLAIM_BLOCKED"
    assert result["case_results"][0]["reason"]=="FAIL_REQUIRES_EVIDENCE_BACKED_BLOCKER_OR_CRITERION_RESULT"

    evidence_backed=scoped_manifest(["a"])
    evidence_backed["obligation_results"]=[{
        "id":"a","status":"ACTIVE","acceptance_result":"FAIL",
        "external_blocker":{
            "classification":"EXTERNAL_DATA_PREREQUISITE",
            "source_ref":"artifact://public-input-gap",
        },
    }]
    result=evaluate_manifest(reg,evidence_backed)
    assert result["status"]=="CLIENT_EXECUTION_CLAIM_RECORDED_NONCOMPLETION"
    assert result["completion_claim_allowed"] is False
    assert result["case_results"][0]["status"]=="FAIL"


def test_full_main_cio_scope_requires_every_currently_assigned_nonterminal_case():
    reg=registry(active_record("a"),active_record("b"),failed_record("historical-failed"),closed_record())
    manifest={
        "execution_scope":{
            "mode":"FULL_MAIN_CIO","owner":"Main CIO","assigned_ids":["a"],
        },
        "obligation_results":[],
    }
    result=evaluate_manifest(reg,manifest)
    assert result["status"]=="CLIENT_EXECUTION_CLAIM_BLOCKED"
    assert result["reason"]=="FULL_OWNER_SCOPE_OMITS_OR_ADDS_ASSIGNED_CASES"


def test_completed_pass_claim_requires_canonical_acceptance_and_completion_effect_evidence():
    completed=completed_record("done")
    reg=scoped_registry(completed,assigned_ids=["done"])
    manifest=scoped_manifest(["done"])
    manifest["obligation_results"]=[{
        "id":"done","status":"COMPLETED","acceptance_result":"PASS",
        "completion_evidence":{"source_ref":"artifact://completion"},
        "effect_evidence":{"verified":True,"source_ref":"artifact://effect"},
    }]
    result=evaluate_manifest(reg,manifest)
    assert result["status"]=="CLIENT_EXECUTION_CLAIM_PASS"
    assert result["completion_claim_allowed"] is True
