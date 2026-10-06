from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

import pytest

from cio_market_lab.engine.cio_session import CIOSessionHistory
from cio_market_lab.research.issue16_acceptance import (
    ChallengeOutput,
    CommercialOutput,
    DeliveryReceiptConsumer,
    DiscoveryOutput,
    HermesLocalInference,
    InferenceContract,
    PublicOnlyResearchWorkflowAdapter,
    ReceiptAwarePositionConsumer,
    UnderwritingOutput,
    original_role_acceptance,
    seal_public_evidence,
)


NOW=datetime(2026,10,6,7,30,tzinfo=timezone.utc)


class FakeCoordinator:
    def __init__(self, *, empty=False, secondary_only=False, private_payload=None):
        self.empty=empty
        self.secondary_only=secondary_only
        self.private_payload=private_payload

    def refresh_symbol(self,symbol,now,reader=None):
        if self.empty:
            return {
                "symbol":symbol,
                "official_facts":{"status":"SOURCE_UNAVAILABLE","record":None},
                "secondary_finviz":{"record":None},
                "secondary_stock_analysis":{"record":None},
                "peer_market_cap":{"record":None},
                "gaps":[{"symbol":symbol,"reason":"SOURCE_UNAVAILABLE"}],
            }
        official=None if self.secondary_only else {
            "status":"SUCCESS",
            "record":{
                "research_id":"official-msft-20261006",
                "symbol":symbol,
                "source_url":"https://www.sec.gov/Archives/edgar/data/789019/test",
                "source_tier":"official_filing",
                "observed_at":now.isoformat(),
                "published_at":"2026-10-06",
                "verification_status":"verified",
                "verified_facts":["Official public filing fact"],
                "research_scope":"historical_company_facts_not_catalyst",
                "limitations":["public source only"],
                **(self.private_payload or {}),
            },
        }
        return {
            "symbol":symbol,
            "official_facts":official or {"record":None},
            "secondary_finviz":{
                "record":{
                    "research_id":"secondary-msft-20261006",
                    "symbol":symbol,
                    "source_url":"https://finviz.com/quote.ashx?t=MSFT",
                    "source_tier":"secondary_cross_check",
                    "observed_at":now.isoformat(),
                    "published_at":"2026-10-06",
                    "verification_status":"verified",
                    "verified_facts":["Public secondary fact"],
                    "research_scope":"event_input_only_not_order",
                    "limitations":["secondary only"],
                }
            },
            "secondary_stock_analysis":{"record":None},
            "peer_market_cap":{"record":None},
            "gaps":[],
        }


class StubInference:
    def __init__(self, identity, output):
        self.identity=identity
        self.output=output
        self.calls=[]

    def infer(self,stage,payload,schema):
        self.calls.append((stage,payload,schema))
        return self.identity,self.output


def inference_map():
    return {
        "discovery":StubInference("local:discovery-a",{
            "candidate_sources":["https://www.sec.gov/Archives/edgar/data/789019/test"],
            "discovery_summary":"Official filing discovered.",
            "missing_evidence":[],
        }),
        "commercial":StubInference("local:commercial-a",{
            "commercial_summary":"Commercial evidence reviewed.",
            "evidence_used":["official-msft-20261006"],
            "missing_evidence":[],
        }),
        "underwriting":StubInference("local:underwriter-a",{
            "underwriting_status":"PUBLIC_EVIDENCE_READY",
            "thesis":"Public evidence is sufficient for research review only.",
            "evidence_used":["official-msft-20261006"],
            "missing_evidence":[],
        }),
        "challenge":StubInference("local:challenge-b",{
            "verdict":"PASS_PUBLIC_RESEARCH_ONLY",
            "challenge_summary":"Independent challenge found no source-only blocker.",
            "blockers":[],
            "next_action":"MAIN_CIO_RUN_REAL_HOST_PUBLIC_INPUT_ACCEPTANCE",
        }),
    }


def test_workflow_requires_injected_actual_inference_and_heterogeneous_challenge():
    missing=PublicOnlyResearchWorkflowAdapter(coordinator=FakeCoordinator()).run("MSFT",now=NOW)
    assert missing["status"]=="BLOCKED"
    assert missing["attempts"][-1]["reason"]=="INFERENCE_PREREQUISITE_MISSING"

    same=inference_map()
    same["challenge"]=StubInference("local:underwriter-a",same["challenge"].output)
    blocked=PublicOnlyResearchWorkflowAdapter(coordinator=FakeCoordinator(),stage_inference=same).run("MSFT",now=NOW)
    assert blocked["status"]=="BLOCKED"
    assert blocked["exact_next_action"]=="CONFIGURE_INDEPENDENT_HETEROGENEOUS_CHALLENGE_MODEL"

    stages=inference_map()
    result=PublicOnlyResearchWorkflowAdapter(coordinator=FakeCoordinator(),stage_inference=stages).run("MSFT",now=NOW)
    assert result["status"]=="COMPLETED_PUBLIC_RESEARCH_CANDIDATE"
    assert result["challenge_model_distinct"] is True
    assert [x["stage"] for x in result["attempts"]]==["fetch","discovery","commercial","underwriting","challenge"]
    assert result["attempts"][0]["official_source_present"] is True
    assert result["live_acceptance_claimed"] is False
    assert all(stages[name].calls for name in stages)


def test_real_hermes_transport_adapter_verifies_runtime_identity_and_schema():
    calls=[]
    def transport(message,**kwargs):
        calls.append((message,kwargs))
        return {
            "response":json.dumps({
                "candidate_sources":["https://www.sec.gov/test"],
                "discovery_summary":"Bounded public discovery.",
                "missing_evidence":[],
            }),
            "returncode":0,
            "runtime_metadata":{
                "resolved_provider":"test-local-provider",
                "resolved_model":"test-local-model-a",
                "auth_verified":True,
                "is_success_response":True,
                "is_fixture":False,
                "fallback_active":False,
            },
        }
    engine=HermesLocalInference(
        InferenceContract(
            provider="test-local-provider",model="test-local-model-a",
            session_id="issue16-test",workspace_root="/workspace",
            is_free_or_local_authorized=True,purpose="TEST_ONLY interface regression",
        ),
        transport=transport,
    )
    identity,out=engine.infer("discovery",{"public_evidence":[{"source_url":"https://www.sec.gov/test"}]},DiscoveryOutput)
    assert identity=="test-local-provider:test-local-model-a"
    assert out["candidate_sources"]==["https://www.sec.gov/test"]
    assert calls[0][1]["provider"]=="test-local-provider"
    assert calls[0][1]["model"]=="test-local-model-a"

    unauthorized=HermesLocalInference(
        InferenceContract(
            provider="x",model="y",session_id="s",workspace_root="/w",
            is_free_or_local_authorized=False,purpose="negative",
        ),transport=transport,
    )
    with pytest.raises(RuntimeError,match="NOT_FREE_OR_LOCAL_AUTHORIZED"):
        unauthorized.infer("discovery",{},DiscoveryOutput)


@pytest.mark.parametrize("stage,bad",[
    ("discovery",{"unexpected_field":"invalid_schema"}),
    ("discovery",{"candidate_sources":[],"discovery_summary":"x","missing_evidence":[]}),
    ("commercial",{"commercial_summary":7,"evidence_used":["x"],"missing_evidence":[]}),
    ("underwriting",{"underwriting_status":"READY","thesis":"x","evidence_used":["x"],"missing_evidence":[]}),
    ("challenge",{"verdict":"PASS_PUBLIC_RESEARCH_ONLY","challenge_summary":"","blockers":[],"next_action":"x"}),
])
def test_stage_specific_schema_rejects_extra_empty_and_wrong_types(stage,bad):
    stages=inference_map()
    stages[stage]=StubInference(f"local:{stage}",bad)
    result=PublicOnlyResearchWorkflowAdapter(coordinator=FakeCoordinator(),stage_inference=stages).run("MSFT",now=NOW)
    assert result["status"]=="BLOCKED"
    row=next(x for x in result["attempts"] if x["stage"]==stage)
    assert row["schema_valid"] is False
    assert "ValidationError" in row["reason"]


def test_public_boundary_rejects_extra_key_nested_secret_quantity_and_local_path_before_transport():
    for private_payload in (
        {"api_key":"synthetic-key"},
        {"position_quantity":3},
        {"nested":{"secret":"synthetic-secret"}},
        {"verified_facts":["public fact","capture at /home/user/private/runtime.json"]},
    ):
        stages=inference_map()
        result=PublicOnlyResearchWorkflowAdapter(
            coordinator=FakeCoordinator(private_payload=private_payload),stage_inference=stages
        ).run("MSFT",now=NOW)
        assert result["status"]=="BLOCKED"
        assert result["attempts"][0]["stage"]=="fetch"
        assert not any(engine.calls for engine in stages.values())

    with pytest.raises(ValueError,match="PUBLIC_OUTBOUND_VALUE_REJECTED"):
        seal_public_evidence({
            "research_id":"official-x","symbol":"MSFT","source_url":"https://www.sec.gov/x",
            "source_tier":"official_filing","observed_at":NOW.isoformat(),
            "verification_status":"verified",
            "verified_facts":["see /private/local/secret.json"],"research_scope":"event_input_only_not_order",
        },now=NOW)


def test_secondary_only_real_host_shape_never_counts_as_official_acceptance():
    result=PublicOnlyResearchWorkflowAdapter(
        coordinator=FakeCoordinator(secondary_only=True),stage_inference=inference_map()
    ).run("MSFT",now=NOW)
    assert result["status"]=="BLOCKED"
    assert result["attempts"][0]["official_source_present"] is False
    assert result["exact_next_action"]=="SUPPLY_GENUINELY_FRESH_OFFICIAL_PUBLIC_INPUT"


def _bindings():
    result={}
    for i,function in enumerate((
        "main_cio","tw_research","us_research","underwriting","allocation",
        "source_audio","industry_mapping","red_team","blindside",
    )):
        result[function]={
            "function":function,
            "owner":"MAIN_CIO" if function=="main_cio" else function.upper(),
            "private_context_allowed":function=="main_cio",
            "public_worker_export_only":function!="main_cio",
            "privacy_evidence":True,
            "downstream_evidence":True,
            "live_output_evidence":False,
            "desk_role":{
                "role_id":f"role-{i}","role_name":function,"title":function,
                "scope":"research_only","status":"MONITORING","current_task":"",
            },
        }
    return result


def test_original_roles_validate_existing_desk_role_bindings_separate_evidence_and_scheduler():
    bindings=_bindings()
    roles=original_role_acceptance(bindings,[{"role_id":"role-1","enabled":False}])
    assert set(roles)==set(bindings)
    assert all(r["identity_status"]=="CONTRACT_VALID" for r in roles.values())
    assert roles["tw_research"]["schedule_status"]=="SCHEDULE_DISABLED"
    assert roles["underwriting"]["schedule_status"]=="SCHEDULE_NOT_CONFIGURED"
    assert roles["tw_research"]["privacy_evidence"]=="PASS"
    assert roles["tw_research"]["downstream_evidence"]=="PASS"
    assert roles["tw_research"]["live_output_evidence"]=="MISSING"

    bad=_bindings()
    bad["tw_research"]["function"]="us_research"
    bad["underwriting"]["public_worker_export_only"]=False
    out=original_role_acceptance(bad)
    assert out["tw_research"]["identity_status"]=="BLOCKED_INVALID_BINDING"
    assert out["underwriting"]["identity_status"]=="BLOCKED_INVALID_BINDING"

    missing=_bindings(); missing.pop("blindside")
    assert original_role_acceptance(missing)["blindside"]["identity_status"]=="BLOCKED_BINDING_MISSING"


def _observation(symbol="VTI"):
    return {
        "symbol":symbol,"session_id":"existing-monitor-session",
        "official_material_ids":["existing-contract-material"],
        "buy_zone":{"low":100.0,"high":110.0},
        "invalidation_condition":{"field":"last_price","operator":"lt","threshold":90.0},
        "invalidation":"last_price below 90",
        "research_only":False,
    }


def _quote(symbol="VTI", at=NOW):
    return {
        "symbol":symbol,"source":"cnbc_nasdaq_last_sale","quality":"public_reported_last_sale",
        "last_price":105.0,"observed_at":at.isoformat(),"bar_time":at.isoformat(),
        "is_stale":False,"is_synthetic":False,"verified":True,
    }


def test_monitor_reuses_canonical_quote_edge_contract_and_does_not_invent_vti_contract():
    consumer=ReceiptAwarePositionConsumer()
    obs={"VTI":_observation()}
    first=consumer.evaluate(obs,{"VTI":_quote()},now=NOW)
    second=consumer.evaluate(obs,{"VTI":_quote()},now=NOW+timedelta(seconds=10))
    assert first["vti_contract_covered"] is True
    assert first["private_positions_exported"] is False
    row=first["results"][0]
    assert row["classification"]=="FRESH"
    assert row["triggered"] is True
    assert row["entry_edge"]["triggered"] is True
    assert row["stable_identity"]==second["results"][0]["stable_identity"]

    no_vti=consumer.evaluate({"MSFT":_observation("MSFT")},{"MSFT":_quote("MSFT")},now=NOW)
    assert no_vti["vti_contract_covered"] is False

    missing=consumer.evaluate(obs,{},now=NOW)
    assert missing["results"][0]["classification"]=="UNKNOWN"


def _expected():
    return {
        "execution_hash":"exec-1","body_hash":"body-1","job_id":"job-1",
        "platform":"telegram","target":"main-cio-thread","thread_id":"thread-7",
    }


@pytest.mark.parametrize("status,delivered",[
    ("FAILED",False),("FAILED",True),("DELIVERED",True),("UNKNOWN",True),
    ("AMBIGUOUS",True),("SUCCESS",True),("",True),("ACKNOWLEDGED",False),
])
def test_receipt_consumer_rejects_failed_unsupported_and_contradictory_states(tmp_path,status,delivered):
    consumer=DeliveryReceiptConsumer(CIOSessionHistory(tmp_path/"receipts","issue16-receipts"))
    receipt={**_expected(),"transport_status":status,"delivered":delivered,"platform_message_id":"msg-1"}
    result=consumer.consume(_expected(),receipt)
    assert result["status"]=="UNKNOWN"
    assert result["acknowledged"] is False
    assert result["replay_permitted"] is False


def test_receipt_consumer_persists_ack_and_restart_dedup(tmp_path):
    root=tmp_path/"receipts"
    receipt={**_expected(),"transport_status":"ACKNOWLEDGED","delivered":True,"platform_message_id":"msg-1"}

    first=DeliveryReceiptConsumer(CIOSessionHistory(root,"issue16-receipts")).consume(_expected(),receipt)
    assert first["status"]=="ACKNOWLEDGED"

    restarted=DeliveryReceiptConsumer(CIOSessionHistory(root,"issue16-receipts"))
    duplicate=restarted.consume(_expected(),receipt)
    assert duplicate["status"]=="ACKNOWLEDGED_DUPLICATE"
    assert duplicate["receipt_identity"]==first["receipt_identity"]
    assert len(CIOSessionHistory(root,"issue16-receipts").history())==1

    mismatch={**receipt,"body_hash":"wrong"}
    bad=restarted.consume(_expected(),mismatch)
    assert bad["status"]=="UNKNOWN"
    assert "body_hash" in bad["reason"]

    no_message={**receipt,"platform_message_id":""}
    assert restarted.consume(_expected(),no_message)["reason"]=="PLATFORM_MESSAGE_ID_MISSING"
