from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json

from custom_scripts.client_execution_claim_gate import evaluate_manifest
from custom_scripts.obligation_deadline_registry import (
    acceptance_check,
    adapt_canonical_registry_snapshot,
    main as registry_main,
    migrate_registry_snapshot,
    validate_registry,
)

NOW=datetime(2026,10,6,12,0,tzinfo=timezone.utc)


def live_job(job_id="job-1", *, scheduled_at="2026-10-09T09:00:00+00:00", route_kind="local", state="ENABLED"):
    return {
        "job_id":job_id,
        "run_id":"run-1",
        "model":"local-model",
        "provider":"local-provider",
        "route_kind":route_kind,
        "state":state,
        "scheduled_at":scheduled_at,
    }


def binding(job_id="job-1", *, scheduled_at="2026-10-09T09:00:00+00:00", route_kind="local", state="ENABLED"):
    return live_job(job_id,scheduled_at=scheduled_at,route_kind=route_kind,state=state)


def original_history(scheduled_at="2026-10-09T08:00:00+00:00"):
    return [{
        "kind":"ORIGINAL_SCHEDULE",
        "scheduled_at":scheduled_at,
        "source_ref":"artifact://original-schedule",
    }]


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
        "original_deadline_source_ref":"artifact://deadline",
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
        "original_deadline_source_ref":"artifact://deadline-failed",
        "execution_history":original_history("2026-10-01T08:00:00+00:00"),
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
        "original_deadline_source_ref":"artifact://deadline-closed",
        "execution_history":original_history("2026-09-30T08:00:00+00:00"),
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
        "original_deadline_source_ref":"artifact://deadline-completed",
        "execution_history":original_history(),
        "acceptance_result":"PASS",
        "completed_at":"2026-10-09T08:30:00+00:00",
        "completed_at_source_ref":"artifact://original-completion-record",
        "completed_at_source_kind":"ORIGINAL_ARTIFACT",
        "original_criteria":["criterion-a"],
        "criteria_evidence":criterion(True),
    }


def registry(*records, job=None):
    return {
        "schema_version":1,
        "live_jobs":{"job-1":job or live_job()},
        "obligations":list(records),
        "execution_scopes":{},
    }


def documented_late_recovery(rid="late-1"):
    return {
        "id":rid,
        "owner":"Main CIO",
        "status":"COMPLETED",
        "original_deadline":"2026-10-01T09:00:00+00:00",
        "original_deadline_source_ref":"artifact://deadline-late",
        "execution_history":original_history("2026-10-01T08:00:00+00:00"),
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


def scoped_registry(*records, assigned_ids, mode="SCOPED_RECOVERY"):
    reg=registry(*records)
    reg["execution_scopes"]={
        "scope-1":{
            "mode":mode,
            "owner":"Main CIO",
            "authorization_source":"decision://main-cio/scope-1",
            "assigned_ids":list(assigned_ids),
            "executor_binding":live_job(),
            "merged_criteria_transfer":{
                "source_ids":["legacy-container-a","legacy-container-b"],
                "target_ids":list(assigned_ids),
                "source_ref":"artifact://merged-criteria-transfer",
            } if mode=="SCOPED_RECOVERY" else None,
        }
    }
    return reg


def scoped_manifest(assigned_ids, mode="SCOPED_RECOVERY"):
    return {
        "execution_scope":{
            "mode":mode,
            "scope_id":"scope-1",
            "owner":"Main CIO",
            "authorization_source":"decision://main-cio/scope-1",
            "assigned_ids":list(assigned_ids),
            "executor_binding":live_job(),
            "merged_criteria_transfer":{
                "source_ids":["legacy-container-a","legacy-container-b"],
                "target_ids":list(assigned_ids),
                "source_ref":"artifact://merged-criteria-transfer",
            } if mode=="SCOPED_RECOVERY" else None,
        },
        "obligation_results":[],
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


def test_ordinary_executor_requires_future_predeadline_schedule_and_live_schedule_state():
    good=active_record()
    assert validate_registry(registry(good),now=NOW)["status"]=="DEADLINE_REGISTRY_VALID"

    past=active_record("past")
    past["executor_binding"]=binding(scheduled_at="2026-10-05T08:00:00+00:00")
    result=validate_registry(registry(past,job=live_job(scheduled_at="2026-10-05T08:00:00+00:00")),now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_INVALID"
    assert any("FUTURE_PREDEADLINE_EXECUTOR_REQUIRED" in e for e in result["errors"])

    mismatch=active_record("mismatch")
    result=validate_registry(registry(mismatch,job=live_job(scheduled_at="2026-10-09T10:00:00+00:00")),now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_INVALID"
    assert any("scheduled_at:LIVE_JOB_MISMATCH" in e for e in result["errors"])

    disabled=active_record("disabled")
    disabled["executor_binding"]["state"]="DISABLED"
    result=validate_registry(registry(disabled,job=live_job(state="DISABLED")),now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_INVALID"
    assert any("LIVE_JOB_NOT_EXECUTABLE" in e for e in result["errors"])


def test_original_schedule_and_deadline_are_independent_source_backed_values():
    record=active_record()
    assert record["execution_history"][0]["scheduled_at"] < record["original_deadline"]
    assert validate_registry(registry(record),now=NOW)["status"]=="DEADLINE_REGISTRY_VALID"

    after=active_record("after")
    after["execution_history"]=original_history("2026-10-11T08:00:00+00:00")
    result=validate_registry(registry(after),now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_INVALID"
    assert any("ORIGINAL_SCHEDULE_AFTER_DEADLINE" in e for e in result["errors"])

    missing_ref=active_record("missing-ref")
    missing_ref["execution_history"][0].pop("source_ref")
    result=validate_registry(registry(missing_ref),now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_INVALID"


def test_duplicate_canonical_id_blocks_named_acceptance_even_if_first_record_passes():
    done=completed_record("dup")
    failed=failed_record("dup")
    reg=registry(done,failed)
    schema=validate_registry(reg,now=NOW)
    assert schema["status"]=="DEADLINE_REGISTRY_INVALID"
    assert any("DUPLICATE_ID" in e for e in schema["errors"])
    result=acceptance_check(reg,"dup",now=NOW)
    assert result["status"]=="OBLIGATION_ACCEPTANCE_BLOCKED"
    assert result["criterion_outcome"]=="IDENTITY_AMBIGUOUS"
    assert result["matching_record_count"]==2


def test_unrelated_global_failure_does_not_convert_unique_named_criterion_outcome():
    done=completed_record("done")
    bad=active_record("bad")
    bad["original_deadline_source_ref"]=""
    reg=registry(done,bad)
    result=acceptance_check(reg,"done",now=NOW)
    assert result["schema_status"]=="DEADLINE_REGISTRY_INVALID"
    assert result["status"]=="OBLIGATION_ACCEPTANCE_PASS"


def test_forged_boolean_recovery_and_ordinary_late_registration_are_rejected():
    ordinary=active_record("ordinary-late")
    ordinary["original_deadline"]="2026-10-01T09:00:00+00:00"
    ordinary["original_deadline_source_ref"]="artifact://deadline-old"
    ordinary["execution_history"]=original_history("2026-10-01T08:00:00+00:00")
    ordinary["executor_binding"]=binding(scheduled_at="2026-10-01T08:30:00+00:00")
    ordinary["recovery_authorized"]=True
    result=validate_registry(registry(ordinary,job=live_job(scheduled_at="2026-10-01T08:30:00+00:00")),now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_INVALID"
    assert any("AUTHORIZED_RECOVERY_REQUIRED" in e for e in result["errors"])

    forged=deepcopy(ordinary)
    forged["recovery"]={"authorized":True}
    result=validate_registry(registry(forged,job=live_job(scheduled_at="2026-10-01T08:30:00+00:00")),now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_INVALID"
    assert any("authorization_source" in e for e in result["errors"])


def test_zero_execution_field_is_present_and_completion_timestamp_must_be_original_artifact():
    assert validate_registry(registry(active_record()),now=NOW)["status"]=="DEADLINE_REGISTRY_VALID"

    missing=completed_record("missing")
    missing.pop("completed_at")
    assert validate_registry(registry(missing),now=NOW)["status"]=="DEADLINE_REGISTRY_INVALID"

    fake=completed_record("fake")
    fake["completed_at_source_kind"]="REVIEWED_AT"
    result=validate_registry(registry(fake),now=NOW)
    assert any("ORIGINAL_ARTIFACT_REQUIRED" in e for e in result["errors"])


def test_local_only_job_cannot_be_claimed_as_origin_delivery():
    late=documented_late_recovery()
    late["recovery"]["executor_readback"]["origin_delivery"]=True
    result=validate_registry(registry(late),now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_INVALID"
    assert any("LOCAL_ROUTE_CANNOT_BECOME_ORIGIN" in e for e in result["errors"])


def test_cancelled_failed_and_closed_cannot_be_converted_to_pass():
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
        assert any(reason in e for e in result["errors"])


def test_migration_is_reversible_idempotent_and_preserves_deadline_failure_history():
    before=registry({
        "id":"legacy-failed","owner":"Main CIO","status":"ACTIVE",
        "legacy_terminal_state":"FAILED","legacy_acceptance_result":"FAIL",
        "original_deadline":"2026-09-30T09:00:00+00:00",
        "original_deadline_source_ref":"artifact://deadline",
        "execution_history":original_history("2026-09-30T08:00:00+00:00"),
        "acceptance_result":"FAIL","failure_disposition":"ORIGINAL_FAILURE",
        "original_criteria":["criterion-a"],"criteria_evidence":criterion(False),
    },{
        "id":"legacy-complete-no-time","owner":"Main CIO","status":"COMPLETED",
        "original_deadline":"2026-10-01T09:00:00+00:00",
        "original_deadline_source_ref":"artifact://deadline",
        "execution_history":original_history("2026-10-01T08:00:00+00:00"),
        "acceptance_result":"FAIL","original_criteria":["criterion-a"],
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
    assert after["obligations"][0]["original_deadline"]==before["obligations"][0]["original_deadline"]
    assert after["obligations"][0]["failure_disposition"]=="ORIGINAL_FAILURE"
    assert "completed_at" not in after["obligations"][1]
    assert all("before" in c and "after" in c and c["source_ref"].startswith("artifact://") for c in report["changes"])
    replay,replay_report=migrate_registry_snapshot(after,refs)
    assert replay==after
    assert replay_report["changes"]==[]


def test_scoped_claim_requires_exact_authenticated_scope_and_all_assigned_cases():
    reg=scoped_registry(active_record("a"),active_record("b"),assigned_ids=["a","b"])
    manifest=scoped_manifest(["a","b"])
    manifest["obligation_results"]=[{
        "id":"a","status":"ACTIVE","acceptance_result":"FAIL",
        "external_blocker":{"classification":"EXTERNAL","source_ref":"artifact://blocker-a"},
    }]
    result=evaluate_manifest(reg,manifest)
    assert result["reason"]=="ASSIGNED_CASES_OMITTED:b"

    forged=scoped_manifest(["a","b"])
    forged["execution_scope"]["authorization_source"]="decision://forged"
    result=evaluate_manifest(reg,forged)
    assert result["reason"]=="EXECUTION_SCOPE_AUTHORIZATION_MISMATCH"


def test_full_main_cio_requires_authenticated_canonical_scope_including_terminal_outcomes():
    done=completed_record("done")
    failed=failed_record("failed")
    reg=scoped_registry(done,failed,assigned_ids=["done","failed"],mode="FULL_MAIN_CIO")
    manifest=scoped_manifest(["done","failed"],mode="FULL_MAIN_CIO")
    manifest["obligation_results"]=[
        {
            "id":"done","status":"COMPLETED","acceptance_result":"PASS",
            "completion_evidence":{"source_ref":"artifact://completion"},
            "effect_evidence":{"verified":True,"source_ref":"artifact://effect"},
        },
        {
            "id":"failed","status":"FAILED","acceptance_result":"FAIL",
            "historical_evidence":{"classification":"HISTORICAL_FAILURE","source_ref":"artifact://failed-evidence"},
        },
    ]
    result=evaluate_manifest(reg,manifest)
    assert result["status"]=="CLIENT_EXECUTION_CLAIM_RECORDED_NONCOMPLETION"
    assert result["completion_claim_allowed"] is False
    assert {x["id"] for x in result["case_results"]}=={"done","failed"}


def test_full_main_cio_empty_results_and_incomplete_terminal_scope_never_pass_vacuously():
    done=completed_record("done")
    reg=scoped_registry(done,assigned_ids=["done"],mode="FULL_MAIN_CIO")
    manifest=scoped_manifest(["done"],mode="FULL_MAIN_CIO")
    result=evaluate_manifest(reg,manifest)
    assert result["status"]=="CLIENT_EXECUTION_CLAIM_BLOCKED"
    assert result["reason"]=="EMPTY_OBLIGATION_RESULTS_CANNOT_COMPLETE"
    assert result["completion_claim_allowed"] is False

    reg_bad=scoped_registry(done,failed_record("failed"),assigned_ids=["done"],mode="FULL_MAIN_CIO")
    manifest_bad=scoped_manifest(["done"],mode="FULL_MAIN_CIO")
    result=evaluate_manifest(reg_bad,manifest_bad)
    assert result["status"]=="CLIENT_EXECUTION_CLAIM_BLOCKED"
    assert result["reason"]=="FULL_OWNER_CANONICAL_SCOPE_INCOMPLETE"


def test_full_scope_with_no_owner_obligations_is_noncompletion_not_pass():
    reg=scoped_registry(assigned_ids=[],mode="FULL_MAIN_CIO")
    manifest=scoped_manifest([],mode="FULL_MAIN_CIO")
    result=evaluate_manifest(reg,manifest)
    assert result["status"]=="CLIENT_EXECUTION_CLAIM_BLOCKED"
    assert result["reason"]=="EXECUTION_SCOPE_HAS_NO_ASSIGNED_OBLIGATIONS"
    assert result["completion_claim_allowed"] is False


def test_terminal_or_cancelled_pass_claims_and_status_only_fail_are_rejected():
    failed=failed_record("failed")
    reg=scoped_registry(failed,assigned_ids=["failed"])
    manifest=scoped_manifest(["failed"])
    manifest["obligation_results"]=[{
        "id":"failed","status":"FAILED","acceptance_result":"PASS",
        "completion_evidence":{"source_ref":"artifact://fake"},
        "effect_evidence":{"verified":True,"source_ref":"artifact://fake-effect"},
    }]
    assert evaluate_manifest(reg,manifest)["status"]=="CLIENT_EXECUTION_CLAIM_BLOCKED"

    active=active_record("a")
    reg=scoped_registry(active,assigned_ids=["a"])
    manifest=scoped_manifest(["a"])
    manifest["obligation_results"]=[{"id":"a","status":"ACTIVE","acceptance_result":"FAIL"}]
    result=evaluate_manifest(reg,manifest)
    assert result["case_results"][0]["reason"]=="FAIL_REQUIRES_VERIFIED_CASE_EVIDENCE"


def test_evidence_backed_scoped_recovery_fail_is_recorded_without_completion_claim():
    reg=scoped_registry(active_record("a"),assigned_ids=["a"])
    manifest=scoped_manifest(["a"])
    manifest["obligation_results"]=[{
        "id":"a","status":"ACTIVE","acceptance_result":"FAIL",
        "external_blocker":{"classification":"EXTERNAL_DATA_PREREQUISITE","source_ref":"artifact://gap"},
    }]
    result=evaluate_manifest(reg,manifest)
    assert result["status"]=="CLIENT_EXECUTION_CLAIM_RECORDED_NONCOMPLETION"
    assert result["completion_claim_allowed"] is False


def test_completed_pass_still_requires_completion_and_effect_evidence():
    done=completed_record("done")
    reg=scoped_registry(done,assigned_ids=["done"])
    manifest=scoped_manifest(["done"])
    manifest["obligation_results"]=[{"id":"done","status":"COMPLETED","acceptance_result":"PASS"}]
    result=evaluate_manifest(reg,manifest)
    assert result["status"]=="CLIENT_EXECUTION_CLAIM_BLOCKED"
    assert result["case_results"][0]["reason"]=="COMPLETION_EVIDENCE_REQUIRED"


def canonical_shape_fixture():
    return {
        "schema_version":"installed-v1",
        "executor_readback":{"job-1":live_job()},
        "obligations":[{
            "obligation_id":"canonical-active",
            "owner":"Main CIO",
            "status":"ACTIVE",
            "deadline":"2026-10-10T09:00:00+00:00",
            "deadline_source_ref":"artifact://canonical-deadline",
            "acceptance_criteria":[{"criterion_id":"criterion-a"}],
            "acceptance_evidence":criterion(False),
            "execution_one_shot":{
                **live_job(),
                "source_ref":"artifact://canonical-one-shot",
            },
            "acceptance_result":"FAIL",
            "required_execution_fields":["attempt_count"],
            "attempt_count":0,
        }],
        "execution_scopes":{},
    }


def test_canonical_installed_shape_adapter_is_nonmutating_and_validates_without_rewriting_source():
    raw=canonical_shape_fixture()
    before=deepcopy(raw)
    adapted,report=adapt_canonical_registry_snapshot(raw)
    assert raw==before
    assert report["input_unchanged"] is True
    assert report["adapted_ids"]==["canonical-active"]
    row=adapted["obligations"][0]
    assert row["id"]=="canonical-active"
    assert row["original_criteria"]==["criterion-a"]
    assert row["execution_history"][0]["scheduled_at"]=="2026-10-09T09:00:00+00:00"
    assert row["original_deadline"]=="2026-10-10T09:00:00+00:00"
    assert validate_registry(adapted,now=NOW)["status"]=="DEADLINE_REGISTRY_VALID"


def test_cli_preserves_validate_command_shape_via_source_backed_env_and_both_registry_option_orders(tmp_path,monkeypatch,capsys):
    raw=canonical_shape_fixture()
    path=tmp_path/"canonical.json"
    path.write_text(json.dumps(raw),encoding="utf-8")

    monkeypatch.setenv("PROJECT_MONEY_OBLIGATION_REGISTRY",str(path))
    assert registry_main(["validate"])==0
    payload=json.loads(capsys.readouterr().out)
    assert payload["status"]=="DEADLINE_REGISTRY_VALID"
    assert payload["compatibility"]["mode"]=="CANONICAL_SHAPE_ADAPTER"

    assert registry_main(["--registry",str(path),"validate"])==0
    capsys.readouterr()
    assert registry_main(["validate","--registry",str(path)])==0
    capsys.readouterr()


def test_canonical_shape_adapter_never_invents_missing_source_backed_schedule_or_deadline():
    raw=canonical_shape_fixture()
    raw["obligations"][0]["execution_one_shot"].pop("source_ref")
    adapted,_=adapt_canonical_registry_snapshot(raw)
    result=validate_registry(adapted,now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_INVALID"
    assert any("source_ref:REQUIRED" in e for e in result["errors"])

    raw=canonical_shape_fixture()
    raw["obligations"][0].pop("deadline_source_ref")
    adapted,_=adapt_canonical_registry_snapshot(raw)
    result=validate_registry(adapted,now=NOW)
    assert result["status"]=="DEADLINE_REGISTRY_INVALID"
    assert any("original_deadline_source_ref:REQUIRED" in e for e in result["errors"])
