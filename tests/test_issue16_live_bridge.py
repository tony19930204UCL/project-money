from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from cio_market_lab.engine.cio_session import CIOSessionHistory
from cio_market_lab.research.issue16_acceptance import HermesLocalInference, InferenceContract
from cio_market_lab.research.issue16_live_bridge import (
    LocalPositionReceiptBridge,
    OriginalResearchCallbackBridge,
    PositionMonitorContract,
    StageRoute,
)


NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


class FakeCoordinator:
    def refresh_symbol(self, symbol, now, reader=None):
        return {
            "symbol": symbol,
            "official_facts": {
                "status": "SUCCESS",
                "record": {
                    "research_id": "official-msft-live-bridge",
                    "symbol": symbol,
                    "source_url": "https://data.sec.gov/submissions/CIK0000789019.json",
                    "source_tier": "official_filing",
                    "observed_at": now.isoformat(),
                    "published_at": "2026-10-07",
                    "verification_status": "verified",
                    "verified_facts": ["Official public filing fact"],
                    "research_scope": "historical_company_facts_not_catalyst",
                    "limitations": ["public source only"],
                    "raw_metadata": {"source": "official SEC EDGAR"},
                },
            },
            "secondary_finviz": {"record": None},
            "secondary_stock_analysis": {"record": None},
            "peer_market_cap": {"record": None},
            "gaps": [],
        }


def _transport_for(stage, output, *, provider, model):
    def transport(message, **kwargs):
        assert f"stage={stage}" in message
        assert kwargs["provider"] == provider
        assert kwargs["model"] == model
        return {
            "response": json.dumps(output),
            "returncode": 0,
            "runtime_metadata": {
                "resolved_provider": provider,
                "resolved_model": model,
                "auth_verified": True,
                "is_success_response": True,
                "is_fixture": False,
                "fallback_active": False,
            },
        }
    return transport


def _engine(stage, output, provider="local-provider", model="model-a", authorized=True):
    return HermesLocalInference(
        InferenceContract(
            provider=provider,
            model=model,
            session_id=f"issue16-{stage}-{model}",
            workspace_root="/workspace",
            is_free_or_local_authorized=authorized,
            purpose=f"issue16 {stage}",
        ),
        transport=_transport_for(stage, output, provider=provider, model=model),
    )


def _routes():
    return {
        "discovery": StageRoute(primary=_engine("discovery", {
            "candidate_sources": ["https://www.sec.gov/test"],
            "discovery_summary": "public discovery",
            "missing_evidence": [],
        }, model="discover-a")),
        "commercial": StageRoute(primary=_engine("commercial", {
            "commercial_summary": "commercial evidence",
            "evidence_used": ["official-msft-live-bridge"],
            "missing_evidence": [],
        }, model="commercial-a")),
        "underwriting": StageRoute(primary=_engine("underwriting", {
            "underwriting_status": "PUBLIC_EVIDENCE_READY",
            "thesis": "bounded public-only underwriting",
            "evidence_used": ["official-msft-live-bridge"],
            "missing_evidence": [],
        }, model="underwriter-a")),
        "challenge": StageRoute(primary=_engine("challenge", {
            "verdict": "PASS_PUBLIC_RESEARCH_ONLY",
            "challenge_summary": "independent verified-model challenge",
            "blockers": [],
            "next_action": "MAIN_CIO_REVIEW",
        }, model="challenger-b")),
    }


def test_original_entrypoint_receives_real_callbacks_and_runtime_receipts():
    bridge = OriginalResearchCallbackBridge(routes=_routes(), coordinator=FakeCoordinator())

    def original_entrypoint(*, symbol, now, fetch_callback, generate_callback, challenge_callback):
        fetched = fetch_callback(symbol, now=now)
        assert fetched["status"] == "COMPLETED"
        payload = {"symbol": symbol, "public_evidence": fetched["public_evidence"], "gaps": fetched["gaps"]}
        discovery = generate_callback("discovery", payload, now=now)
        commercial = generate_callback("commercial", {**payload, "discovery": discovery["output"]}, now=now)
        underwriting = generate_callback(
            "underwriting",
            {**payload, "discovery": discovery["output"], "commercial": commercial["output"]},
            now=now,
        )
        challenge = challenge_callback(
            {**payload, "underwriting": underwriting["output"]},
            underwriting["model_identity"],
            now=now,
        )
        assert discovery["runtime_receipt"]["auth_verified"] is True
        assert discovery["runtime_receipt"]["is_fixture"] is False
        assert discovery["runtime_receipt"]["returncode"] == 0
        assert challenge["challenge_model_distinct"] is True
        return {
            "status": "COMPLETED_PUBLIC_RESEARCH_CANDIDATE",
            "underwriting_status": underwriting["output"]["underwriting_status"],
            "challenge_verdict": challenge["output"]["verdict"],
        }

    result = bridge.run_original_entrypoint(original_entrypoint, symbol="MSFT", now=NOW)
    assert result["status"] == "COMPLETED_PUBLIC_RESEARCH_CANDIDATE"
    assert result["live_acceptance_claimed"] is False


def test_original_entrypoint_missing_route_and_contract_mismatch_fail_closed():
    bridge = OriginalResearchCallbackBridge(routes={}, coordinator=FakeCoordinator())
    missing = bridge.run_original_entrypoint(None, symbol="MSFT", now=NOW)
    assert missing["status"] == "BLOCKED"
    assert missing["reason"] == "ORIGINAL_RESEARCH_ENGINE_ENTRYPOINT_MISSING"

    bad = bridge.generate("discovery", {"symbol": "MSFT"}, now=NOW)
    assert bad["status"] == "BLOCKED"
    assert bad["reason"] == "INFERENCE_ROUTE_MISSING"

    mismatch = bridge.generate("challenge", {"symbol": "MSFT"}, now=NOW)
    assert mismatch["status"] == "BLOCKED"
    assert mismatch["reason"] == "GENERATE_STAGE_MISMATCH"


def test_primary_blocked_free_route_uses_only_explicit_authorized_fallback():
    good = _engine("discovery", {
        "candidate_sources": ["https://www.sec.gov/test"],
        "discovery_summary": "fallback public discovery",
        "missing_evidence": [],
    }, model="fallback-a")
    blocked = _engine("discovery", {
        "candidate_sources": ["https://www.sec.gov/never"],
        "discovery_summary": "must not run",
        "missing_evidence": [],
    }, model="paid-x", authorized=False)
    bridge = OriginalResearchCallbackBridge(
        routes={"discovery": StageRoute(primary=blocked, fallback=good)},
        coordinator=FakeCoordinator(),
    )
    result = bridge.generate("discovery", {"symbol": "MSFT", "public_evidence": [{"source_url": "https://www.sec.gov/test"}]}, now=NOW)
    assert result["status"] == "COMPLETED"
    assert result["route"] == "fallback"
    assert result["attempts"][0]["reason"] == "INFERENCE_CONTRACT_NOT_FREE_OR_LOCAL_AUTHORIZED"


def test_identical_underwriting_and_challenge_model_blocks_heterogeneity():
    bridge = OriginalResearchCallbackBridge(
        routes={
            "challenge": StageRoute(primary=_engine("challenge", {
                "verdict": "PASS_PUBLIC_RESEARCH_ONLY",
                "challenge_summary": "same model is invalid",
                "blockers": [],
                "next_action": "CONFIGURE_DIFFERENT_MODEL",
            }, model="same-model")),
        },
        coordinator=FakeCoordinator(),
    )
    result = bridge.challenge({"symbol": "MSFT"}, now=NOW, underwriting_model_identity="local-provider:same-model")
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "CHALLENGE_MODEL_NOT_HETEROGENEOUS"


def test_callback_final_private_output_rejected():
    bridge = OriginalResearchCallbackBridge(
        routes={
            "discovery": StageRoute(primary=_engine("discovery", {
                "candidate_sources": ["https://www.sec.gov/test"],
                "discovery_summary": "private path /home/user/secret.json",
                "missing_evidence": [],
            }, model="discover-a")),
        },
        coordinator=FakeCoordinator(),
    )
    result = bridge.generate("discovery", {"symbol": "MSFT", "public_evidence": [{"source_url": "https://www.sec.gov/test"}]}, now=NOW)
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "ALL_AUTHORIZED_INFERENCE_ROUTES_BLOCKED"
    assert "PUBLIC_OUTBOUND_VALUE_REJECTED" in result["attempts"][0]["reason"]


def _observation(symbol="VTI"):
    return {
        "symbol": symbol,
        "session_id": "existing-monitor-session",
        "official_material_ids": ["material-1"],
        "buy_zone": {"low": 100.0, "high": 110.0},
        "invalidation_condition": {"field": "last_price", "operator": "lt", "threshold": 90.0},
        "invalidation": "last_price below 90",
        "research_only": False,
    }


def _quote(symbol="VTI", at=NOW):
    return {
        "symbol": symbol,
        "source": "cnbc_nasdaq_last_sale",
        "quality": "public_reported_last_sale",
        "last_price": 105.0,
        "observed_at": at.isoformat(),
        "bar_time": at.isoformat(),
        "is_stale": False,
        "is_synthetic": False,
        "verified": True,
    }


def _expected():
    return {
        "execution_hash": "exec-1",
        "body_hash": "body-1",
        "job_id": "job-1",
        "platform": "telegram",
        "target": "main-cio-thread",
        "thread_id": "thread-7",
    }


def _contract(condition_id="entry", version="v1", observation_identity="obs-1"):
    return {
        "contract_id": "contract-vti",
        "condition_id": condition_id,
        "condition_version": version,
        "observation_identity": observation_identity,
        "symbol": "VTI",
        "observation": _observation(),
        "expected_receipt": _expected(),
    }


def test_position_bridge_vti_identity_siblings_unknown_stale_and_restart_pending_replay(tmp_path):
    root = tmp_path / "history"
    registry = {"contracts": [
        _contract("entry", "v1", "obs-a"),
        {**_contract("invalidate", "v2", "obs-b"), "contract_id": "contract-vti-sibling"},
    ]}
    quote_state = {"value": _quote()}
    receipt_state = {"value": None}

    def loader():
        return registry

    def quote_provider(symbol):
        return quote_state["value"]

    def receipt_provider(expected):
        return receipt_state["value"]

    first = LocalPositionReceiptBridge(
        registry_loader=loader,
        quote_provider=quote_provider,
        receipt_provider=receipt_provider,
        history=CIOSessionHistory(root, "issue16-monitor"),
    ).evaluate(now=NOW)
    assert first["vti_contract_covered"] is True
    assert first["private_positions_exported"] is False
    assert first["ack_store"] == "CIOSessionHistory/DeliveryReceiptConsumer"
    assert len(first["results"]) == 2
    assert first["results"][0]["condition_id"] == "entry"
    assert first["results"][1]["condition_id"] == "invalidate"
    assert first["results"][0]["contract_identity"] != first["results"][1]["contract_identity"]
    assert len(first["pending_receipts"]) == 2

    receipt_state["value"] = {
        **_expected(),
        "transport_status": "ACKNOWLEDGED",
        "delivered": True,
        "platform_message_id": "msg-real-1",
        "is_fixture": False,
    }
    restarted = LocalPositionReceiptBridge(
        registry_loader=loader,
        quote_provider=quote_provider,
        receipt_provider=receipt_provider,
        history=CIOSessionHistory(root, "issue16-monitor"),
    ).evaluate(now=NOW + timedelta(seconds=10))
    assert restarted["pending_receipts"] == []
    assert all(row["delivery"]["acknowledged"] for row in restarted["results"])

    quote_state["value"] = _quote(at=NOW - timedelta(hours=1))
    stale = LocalPositionReceiptBridge(
        registry_loader=loader,
        quote_provider=quote_provider,
        receipt_provider=receipt_provider,
        history=CIOSessionHistory(tmp_path / "stale", "issue16-monitor-stale"),
    ).evaluate(now=NOW)
    assert all(row["classification"] == "STALE" for row in stale["results"])

    quote_state["value"] = None
    unknown = LocalPositionReceiptBridge(
        registry_loader=loader,
        quote_provider=quote_provider,
        receipt_provider=receipt_provider,
        history=CIOSessionHistory(tmp_path / "unknown", "issue16-monitor-unknown"),
    ).evaluate(now=NOW)
    assert all(row["classification"] == "UNKNOWN" for row in unknown["results"])


def test_position_bridge_fixture_and_generation_never_ack(tmp_path):
    registry = {"contracts": [_contract()]}
    fixture_receipt = {
        **_expected(),
        "transport_status": "ACKNOWLEDGED",
        "delivered": True,
        "platform_message_id": "fixture-msg",
        "is_fixture": True,
    }
    bridge = LocalPositionReceiptBridge(
        registry_loader=lambda: registry,
        quote_provider=lambda symbol: _quote(),
        receipt_provider=lambda expected: fixture_receipt,
        history=CIOSessionHistory(tmp_path / "fixture", "issue16-monitor-fixture"),
    )
    result = bridge.evaluate(now=NOW)
    delivery = result["results"][0]["delivery"]
    assert delivery["acknowledged"] is False
    assert delivery["reason"] == "FIXTURE_RECEIPT_REJECTED"
    assert delivery["pending_receipt"] is True


def test_position_contract_rejects_private_or_unversioned_registry_rows(tmp_path):
    private = {**_contract(), "holding_quantity": 5}
    bridge = LocalPositionReceiptBridge(
        registry_loader=lambda: {"contracts": [private]},
        quote_provider=lambda symbol: _quote(),
        receipt_provider=lambda expected: None,
        history=CIOSessionHistory(tmp_path / "private", "issue16-monitor-private"),
    )
    result = bridge.evaluate(now=NOW)
    assert result["status"] == "BLOCKED"
    assert "ValidationError" in result["reason"]

    valid = PositionMonitorContract.model_validate(_contract())
    assert valid.condition_version == "v1"
