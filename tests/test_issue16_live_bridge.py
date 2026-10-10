from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from subprocess import TimeoutExpired

import cio_market_lab.research.issue16_live_bridge as live_bridge
from custom_scripts import issue16_bridge_candidate as candidate_cli

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


def _timeout_engine(
    stage,
    *,
    provider="local-provider",
    model="timeout-model",
    authorized=True,
    timeout_seconds=120,
):
    def transport(message, **kwargs):
        assert f"stage={stage}" in message
        raise TimeoutExpired(
            cmd=["/private/runtime/bin/hermes", "--workspace", "/home/user/private-workspace"],
            timeout=timeout_seconds,
            output=b"PRIVATE_PROMPT_MARKER /home/user/private-workspace " + (b"x" * 908),
            stderr=None,
        )

    return HermesLocalInference(
        InferenceContract(
            provider=provider,
            model=model,
            session_id=f"issue16-{stage}-{model}",
            workspace_root="/workspace",
            is_free_or_local_authorized=authorized,
            purpose=f"issue16 {stage}",
        ),
        transport=transport,
        timeout_seconds=timeout_seconds,
    )


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
        }, model="discover-a"), primary_model_family="discovery-family"),
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
    assert [row["stage"] for row in result["callback_evidence"]] == [
        "fetch", "discovery", "commercial", "underwriting", "challenge"
    ]
    discovery_evidence = result["callback_evidence"][1]
    assert discovery_evidence["model_identity"] == "local-provider:discover-a"
    assert discovery_evidence["runtime_receipt"] == {
        "resolved_provider": "local-provider",
        "resolved_model": "discover-a",
        "auth_verified": True,
        "is_success_response": True,
        "is_fixture": False,
        "returncode": 0,
    }
    assert result["callback_evidence"][-1]["challenge_model_distinct"] is True


def test_host_underwriting_timeout_exports_only_safe_typed_attempt_without_fallback():
    bridge = OriginalResearchCallbackBridge(
        routes={
            "underwriting": StageRoute(
                primary=_timeout_engine("underwriting", model="nemotron-timeout"),
                primary_model_family="nemotron",
            )
        },
        coordinator=FakeCoordinator(),
    )
    payload = bridge._host_stage_payload(
        "underwriting",
        {
            "documents": [{
                "url": "https://www.microsoft.com/en-us/Investor/test",
                "text": "Microsoft official earnings revenue and operating income disclosure.",
                "observed_at": NOW.isoformat(),
            }],
            "discovery": {
                "status": "PASS",
                "reason": "official public discovery complete",
                "source_urls": ["https://www.microsoft.com/en-us/Investor/test"],
            },
            "commercial": {
                "status": "PASS",
                "reason": "public commercial evidence complete",
                "source_urls": ["https://www.microsoft.com/en-us/Investor/test"],
            },
        },
        symbol="MSFT",
        seed_urls=[SEED_URL],
    )

    result = bridge._record_callback_evidence(
        bridge._infer_host_route(
            "underwriting",
            payload,
            now=NOW,
            seed_urls=["https://www.microsoft.com/en-us/Investor/test"],
        )
    )
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "HOST_UNDERWRITING_INFERENCE_TIMEOUT"
    assert len(result["attempts"]) == 1
    attempt = result["attempts"][0]
    assert attempt == {
        "stage": "underwriting",
        "route": "primary",
        "status": "BLOCKED",
        "reason": "HOST_UNDERWRITING_INFERENCE_TIMEOUT",
        "provider": "local-provider",
        "model": "nemotron-timeout",
        "model_family": "nemotron",
        "timeout_seconds": 120,
        "timeout_diagnostics": {
            "stdout_present": True,
            "stdout_bytes_seen": 959,
            "stderr_present": False,
            "stderr_bytes_seen": 0,
        },
    }
    exported = json.dumps(result, sort_keys=True)
    evidence_exported = json.dumps(bridge.callback_evidence, sort_keys=True)
    for private_marker in (
        "/private/runtime/bin/hermes",
        "/home/user/private-workspace",
        "PRIVATE_PROMPT_MARKER",
        "--workspace",
    ):
        assert private_marker not in exported
        assert private_marker not in evidence_exported
    assert bridge.callback_evidence[-1]["attempts"][0]["timeout_seconds"] == 120
    assert bridge.callback_evidence[-1]["attempts"][0]["timeout_diagnostics"]["stdout_bytes_seen"] == 959


def test_host_underwriting_timeout_preserved_before_existing_authorized_fallback_success():
    fallback_output = {
        "status": "INCOMPLETE",
        "reason": "public evidence insufficient after primary timeout; no values invented",
        "source_urls": ["https://www.microsoft.com/en-us/Investor/test"],
    }
    bridge = OriginalResearchCallbackBridge(
        routes={
            "underwriting": StageRoute(
                primary=_timeout_engine("underwriting", model="nemotron-timeout"),
                primary_model_family="nemotron",
                fallback=_engine(
                    "underwriting",
                    fallback_output,
                    provider="local-provider",
                    model="authorized-fallback",
                    authorized=True,
                ),
                fallback_model_family="fallback-family",
            )
        },
        coordinator=FakeCoordinator(),
    )
    payload = bridge._host_stage_payload(
        "underwriting",
        {
            "documents": [{
                "url": "https://www.microsoft.com/en-us/Investor/test",
                "text": "Microsoft official earnings public disclosure.",
                "observed_at": NOW.isoformat(),
            }],
            "discovery": {"status": "PASS", "reason": "done", "source_urls": ["https://www.microsoft.com/en-us/Investor/test"]},
            "commercial": {"status": "PASS", "reason": "done", "source_urls": ["https://www.microsoft.com/en-us/Investor/test"]},
        },
        symbol="MSFT",
        seed_urls=[SEED_URL],
    )

    result = bridge._record_callback_evidence(
        bridge._infer_host_route(
            "underwriting",
            payload,
            now=NOW,
            seed_urls=["https://www.microsoft.com/en-us/Investor/test"],
        )
    )
    assert result["status"] == "COMPLETED"
    assert result["route"] == "fallback"
    assert result["output"]["status"] == "INCOMPLETE"
    assert len(result["attempts"]) == 2
    assert result["attempts"][0]["reason"] == "HOST_UNDERWRITING_INFERENCE_TIMEOUT"
    assert result["attempts"][0]["timeout_seconds"] == 120
    assert result["attempts"][1]["status"] == "COMPLETED"
    assert result["attempts"][1]["model"] == "authorized-fallback"
    assert result["attempts"][1]["auth_verified"] is True
    assert result["attempts"][1]["is_success_response"] is True
    assert result["attempts"][1]["is_fixture"] is False
    assert result["attempts"][1]["returncode"] == 0
    exported = json.dumps(result, sort_keys=True)
    evidence_exported = json.dumps(bridge.callback_evidence, sort_keys=True)
    for private_marker in (
        "/private/runtime/bin/hermes",
        "/home/user/private-workspace",
        "PRIVATE_PROMPT_MARKER",
        "--workspace",
    ):
        assert private_marker not in exported
        assert private_marker not in evidence_exported


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


def _real_receipt(message_id="msg-real-1", **overrides):
    receipt = {
        **_expected(),
        "transport_status": "ACKNOWLEDGED",
        "delivered": True,
        "platform_message_id": message_id,
        "is_fixture": False,
    }
    receipt.update(overrides)
    return receipt


def _bridge(root, registry_state, quote_state, receipt_state, calls):
    def loader():
        return registry_state["value"]

    def quote_provider(symbol):
        return quote_state["value"]

    def receipt_provider(expected):
        calls.append(dict(expected))
        return receipt_state["value"]

    return LocalPositionReceiptBridge(
        registry_loader=loader,
        quote_provider=quote_provider,
        receipt_provider=receipt_provider,
        history=CIOSessionHistory(root, "issue16-monitor"),
    )


def test_pending_replay_resolves_after_restart_outside_original_buy_zone(tmp_path):
    root = tmp_path / "outside-zone"
    registry_state = {"value": {"contracts": [_contract()]}}
    quote_state = {"value": _quote()}
    receipt_state = {"value": None}
    calls = []

    first = _bridge(root, registry_state, quote_state, receipt_state, calls).evaluate(now=NOW)
    assert len(first["pending_receipts"]) == 1

    calls.clear()
    quote_state["value"] = {**_quote(), "last_price": 120.0}
    receipt_state["value"] = _real_receipt()
    restarted = _bridge(root, registry_state, quote_state, receipt_state, calls).evaluate(
        now=NOW + timedelta(seconds=10)
    )

    assert calls == [_expected()]
    assert restarted["pending_replay"]["resolved"] == 1
    assert restarted["pending_receipts"] == []
    assert restarted["results"][0]["triggered"] is False
    assert restarted["results"][0]["delivery"]["status"] == "NOT_TRIGGERED"
    history = CIOSessionHistory(root, "issue16-monitor").history()
    assert sum(row.get("kind") == "PLATFORM_ACK" for row in history) == 1
    assert sum(row.get("kind") == "MONITOR_PENDING_RESOLVED" for row in history) == 1


def test_pending_replay_resolves_with_stale_or_missing_quote_without_new_trigger(tmp_path):
    for name, quote in (
        ("stale", _quote(at=NOW - timedelta(hours=1))),
        ("missing", None),
    ):
        root = tmp_path / name
        registry_state = {"value": {"contracts": [_contract()]}}
        quote_state = {"value": _quote()}
        receipt_state = {"value": None}
        calls = []
        first = _bridge(root, registry_state, quote_state, receipt_state, calls).evaluate(now=NOW)
        assert len(first["pending_receipts"]) == 1

        calls.clear()
        quote_state["value"] = quote
        receipt_state["value"] = _real_receipt(message_id=f"msg-{name}")
        restarted = _bridge(root, registry_state, quote_state, receipt_state, calls).evaluate(
            now=NOW + timedelta(seconds=10)
        )
        assert calls == [_expected()]
        assert restarted["pending_replay"]["resolved"] == 1
        assert restarted["pending_receipts"] == []
        assert restarted["results"][0]["triggered"] is None
        assert restarted["results"][0]["classification"] in {"STALE", "UNKNOWN"}


def test_pending_replay_survives_condition_removal_and_does_not_require_registry_row(tmp_path):
    root = tmp_path / "removed"
    registry_state = {"value": {"contracts": [_contract()]}}
    quote_state = {"value": _quote()}
    receipt_state = {"value": None}
    calls = []
    first = _bridge(root, registry_state, quote_state, receipt_state, calls).evaluate(now=NOW)
    assert len(first["pending_receipts"]) == 1

    registry_state["value"] = {"contracts": []}
    quote_state["value"] = None
    receipt_state["value"] = _real_receipt()
    calls.clear()
    restarted = _bridge(root, registry_state, quote_state, receipt_state, calls).evaluate(
        now=NOW + timedelta(seconds=10)
    )
    assert calls == [_expected()]
    assert restarted["results"] == []
    assert restarted["pending_replay"]["resolved"] == 1
    assert restarted["pending_receipts"] == []


def test_pending_replay_uses_old_identity_only_and_mismatch_never_acks(tmp_path):
    root = tmp_path / "identity"
    registry_state = {"value": {"contracts": [_contract(version="v1", observation_identity="obs-old")]}}
    quote_state = {"value": _quote()}
    receipt_state = {"value": None}
    calls = []
    first = _bridge(root, registry_state, quote_state, receipt_state, calls).evaluate(now=NOW)
    old_pending = first["pending_receipts"][0]["pending_identity"]

    registry_state["value"] = {"contracts": [_contract(version="v2", observation_identity="obs-new")]}
    quote_state["value"] = {**_quote(), "last_price": 120.0}
    receipt_state["value"] = _real_receipt(execution_hash="wrong-exec")
    calls.clear()
    mismatch = _bridge(root, registry_state, quote_state, receipt_state, calls).evaluate(
        now=NOW + timedelta(seconds=10)
    )
    assert calls == [_expected()]
    assert mismatch["pending_replay"]["resolved"] == 0
    assert mismatch["pending_receipts"][0]["pending_identity"] == old_pending
    assert mismatch["results"][0]["condition_version"] == "v2"
    assert mismatch["results"][0]["observation_identity"] == "obs-new"
    history = CIOSessionHistory(root, "issue16-monitor").history()
    assert not any(row.get("kind") == "PLATFORM_ACK" for row in history)


def test_fixture_generation_failed_and_same_ack_replay_are_fail_closed_or_idempotent(tmp_path):
    root = tmp_path / "receipt-states"
    registry_state = {"value": {"contracts": [_contract()]}}
    quote_state = {"value": _quote()}
    receipt_state = {"value": None}
    calls = []
    first = _bridge(root, registry_state, quote_state, receipt_state, calls).evaluate(now=NOW)
    assert len(first["pending_receipts"]) == 1

    for bad in (
        _real_receipt(is_fixture=True),
        {
            **_expected(),
            "transport_status": "GENERATED",
            "delivered": False,
            "platform_message_id": "generated-only",
            "is_fixture": False,
        },
        _real_receipt(transport_status="FAILED", delivered=False),
    ):
        receipt_state["value"] = bad
        replayed = _bridge(root, registry_state, quote_state, receipt_state, calls).evaluate(
            now=NOW + timedelta(seconds=20)
        )
        assert replayed["pending_replay"]["resolved"] == 0
        assert len(replayed["pending_receipts"]) == 1

    quote_state["value"] = {**_quote(), "last_price": 120.0}
    receipt_state["value"] = _real_receipt()
    resolved = _bridge(root, registry_state, quote_state, receipt_state, calls).evaluate(
        now=NOW + timedelta(seconds=30)
    )
    assert resolved["pending_replay"]["resolved"] == 1
    assert resolved["pending_receipts"] == []

    before = CIOSessionHistory(root, "issue16-monitor").history()
    replay_again = _bridge(root, registry_state, quote_state, receipt_state, calls).evaluate(
        now=NOW + timedelta(seconds=40)
    )
    after = CIOSessionHistory(root, "issue16-monitor").history()
    assert replay_again["pending_replay"]["attempted"] == 0
    assert sum(row.get("kind") == "PLATFORM_ACK" for row in after) == 1
    assert sum(row.get("kind") == "MONITOR_PENDING_RESOLVED" for row in after) == 1
    assert len(after) == len(before)


def test_pending_history_persists_only_sanitized_immutable_linkage_snapshot(tmp_path):
    root = tmp_path / "snapshot"
    registry_state = {"value": {"contracts": [_contract()]}}
    quote_state = {"value": _quote()}
    receipt_state = {"value": None}
    calls = []
    _bridge(root, registry_state, quote_state, receipt_state, calls).evaluate(now=NOW)
    pending = [
        row
        for row in CIOSessionHistory(root, "issue16-monitor").history()
        if row.get("kind") == "MONITOR_PENDING_RECEIPT"
    ]
    assert len(pending) == 1
    row = pending[0]
    assert row["expected_receipt"] == _expected()
    assert set(row["expected_receipt"]) == {
        "execution_hash", "body_hash", "job_id", "platform", "target", "thread_id"
    }
    assert row["condition_id"] == "entry"
    assert row["condition_version"] == "v1"
    assert row["observation_identity"] == "obs-1"


def test_installed_run_case_mapping_uses_observed_host_signature_without_private_host_guess():
    bridge = OriginalResearchCallbackBridge(routes=_routes(), coordinator=FakeCoordinator())
    seen = {}

    def installed_run_case(case_id, ticker, seed_urls, directory, fetch, generate, challenge, max_attempts):
        seen.update(
            case_id=case_id,
            ticker=ticker,
            seed_urls=seed_urls,
            directory=directory,
            max_attempts=max_attempts,
            fetch=fetch,
            generate=generate,
            challenge=challenge,
        )
        return {"status": "BLOCKED", "reason": "HOST_CALLBACK_SHAPE_REQUIRES_MAIN_ACCEPTANCE"}

    result = bridge.run_installed_run_case(
        installed_run_case,
        case_id="case-16",
        symbol="MSFT",
        seed_urls=["https://www.sec.gov/test"],
        directory="/sanitized/candidate-dir",
        max_attempts=2,
        now=NOW,
    )
    assert seen["ticker"] == "MSFT"
    assert callable(seen["fetch"]) and callable(seen["generate"]) and callable(seen["challenge"])
    assert result["host_mapping_contract"] == "run_case_v1"
    assert result["live_acceptance_claimed"] is False


def test_installed_run_case_missing_entrypoint_stays_blocked_with_exact_next_action():
    bridge = OriginalResearchCallbackBridge(routes=_routes(), coordinator=FakeCoordinator())
    result = bridge.run_installed_run_case(
        None,
        case_id="case-16",
        symbol="MSFT",
        seed_urls=["https://www.sec.gov/test"],
        directory="/sanitized/candidate-dir",
        max_attempts=2,
        now=NOW,
    )
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "ORIGINAL_RESEARCH_ENGINE_ENTRYPOINT_MISSING"
    assert result["exact_next_action"] == "SUPPLY_INSTALLED_RUN_CASE_IMPORT_PATH"


SEED_URL = "https://www.microsoft.com/en-us/Investor/test"


def _public_document_reader(url):
    assert url == SEED_URL
    return "<html><body><p>Public issuer revenue and operating income disclosure for source-only contract testing.</p></body></html>"


def _host_routes(
    *,
    missing_underwriting=False,
    same_challenge=False,
    same_challenge_family=False,
    outside_seed=False,
    challenge_status="PASS",
    challenge_objections=None,
    omit_challenge_objections=False,
):
    source_urls = ["https://example.com/not-a-seed"] if outside_seed else [SEED_URL]
    underwriting = {
        "status": "PASS",
        "reason": "public evidence supports bounded research review",
        "source_urls": source_urls,
        "financials": {"revenue": "public issuer disclosure"},
        "market_metrics": {"revenue_growth": "supported by supplied public disclosure"},
        "capital_structure": {"net_cash_context": "supported by supplied public disclosure"},
        "independent_source_mix": source_urls,
        "reflexivity_score": 0.5,
        "scenario_return_estimates": {
            "bear": "supported public-evidence estimate",
            "base": "supported public-evidence estimate",
            "bull": "supported public-evidence estimate"
        },
        "factor_labels": ["public-earnings", "valuation"],
        "business_maturity": "mature public operating business",
        "valuation_scenarios": {
            "bear": "public-evidence downside case",
            "base": "public-evidence base case",
            "bull": "public-evidence upside case",
        },
        "buy_zone": {"low": 100.0, "high": 110.0, "research_only": True},
        "invalidation_conditions": ["public operating facts materially deteriorate"],
        "review_by": "2026-10-08",
        "four_sentences": [
            "Sentence one summarizes public financial evidence.",
            "Sentence two summarizes business maturity.",
            "Sentence three summarizes valuation uncertainty.",
            "Sentence four states this is research-only review.",
        ],
    }
    if missing_underwriting:
        underwriting.pop("financials")
    challenge_model = "underwriter-a" if same_challenge else ("nemotron-variant-b" if same_challenge_family else "challenger-b")
    underwriting_family = "nemotron" if same_challenge_family else "underwriting-family"
    challenge_family = "nemotron" if same_challenge_family else ("underwriting-family" if same_challenge else "challenge-family")
    challenge_output = {
        "status": challenge_status,
        "reason": (
            "independent challenge completed"
            if challenge_status == "PASS"
            else "independent challenge blocks the candidate"
        ),
        "source_urls": source_urls,
    }
    if not omit_challenge_objections:
        challenge_output["objections"] = (
            ["Public-source challenge found no blocking contradiction."]
            if challenge_objections is None
            else challenge_objections
        )
    return {
        "discovery": StageRoute(primary=_engine("discovery", {
            "status": "PASS",
            "reason": "seed public source accepted",
            "source_urls": source_urls,
        }, model="discover-a"), primary_model_family="discovery-family"),
        "commercial": StageRoute(primary=_engine("commercial", {
            "status": "PASS",
            "reason": "commercial review completed from public seed source",
            "source_urls": source_urls,
        }, model="commercial-a"), primary_model_family="commercial-family"),
        "underwriting": StageRoute(primary=_engine(
            "underwriting",
            underwriting,
            model="underwriter-a",
        ), primary_model_family=underwriting_family),
        "challenge": StageRoute(primary=_engine(
            "challenge",
            challenge_output,
            model=challenge_model,
        ), primary_model_family=challenge_family),
    }


def _validate_original_stage_result(stage, result, seed_urls):
    """Sanitized mirror of the Main-reported original validate_stage_result contract."""
    assert isinstance(result, dict), f"{stage} result must be dict"
    assert isinstance(result.get("reason"), str) and result["reason"].strip(), (
        f"{stage} requires nonempty reason"
    )
    assert isinstance(result.get("source_urls"), list), f"{stage} requires list source_urls"
    assert all(isinstance(url, str) and url for url in result["source_urls"])
    assert set(result["source_urls"]) <= set(seed_urls), f"{stage} source_urls outside seeds"

    if stage == "challenge":
        assert result.get("status") in {"PASS", "BLOCK", "INCOMPLETE"}
        assert isinstance(result.get("objections"), list), "Challenge requires list objections"
        return result

    assert result.get("status") in {"PASS", "REJECT", "INCOMPLETE"}
    if stage == "underwriting" and result["status"] == "PASS":
        required = {
            "financials",
            "market_metrics",
            "capital_structure",
            "independent_source_mix",
            "reflexivity_score",
            "scenario_return_estimates",
            "factor_labels",
            "business_maturity",
            "valuation_scenarios",
            "buy_zone",
            "invalidation_conditions",
            "review_by",
            "four_sentences",
        }
        assert required <= set(result), "Underwriting PASS missing required facts"
        assert isinstance(result["financials"], dict) and result["financials"]
        assert isinstance(result["market_metrics"], dict) and result["market_metrics"]
        assert isinstance(result["capital_structure"], dict) and result["capital_structure"]
        assert isinstance(result["independent_source_mix"], list) and result["independent_source_mix"]
        assert set(result["independent_source_mix"]) <= set(seed_urls)
        assert result["reflexivity_score"] is not None
        assert isinstance(result["scenario_return_estimates"], dict) and result["scenario_return_estimates"]
        assert isinstance(result["factor_labels"], list) and result["factor_labels"]
        assert isinstance(result["business_maturity"], str) and result["business_maturity"].strip()
        assert isinstance(result["valuation_scenarios"], dict) and result["valuation_scenarios"]
        assert isinstance(result["buy_zone"], dict) and result["buy_zone"]
        assert isinstance(result["invalidation_conditions"], list) and result["invalidation_conditions"]
        assert isinstance(result["review_by"], str) and result["review_by"].strip()
        assert isinstance(result["four_sentences"], list) and len(result["four_sentences"]) == 4
        assert all(isinstance(sentence, str) and sentence.strip() for sentence in result["four_sentences"])
    return result


def _validate_original_terminal_result(result, seed_urls):
    assert isinstance(result, dict), "terminal result must be dict"
    assert result.get("status") in {"READY_FOR_CIO", "BLOCK", "INCOMPLETE", "FAILED"}
    assert isinstance(result.get("reason"), str) and result["reason"].strip()
    assert isinstance(result.get("source_urls"), list)
    assert set(result["source_urls"]) <= set(seed_urls)
    return result


def _faithful_original_run_case(
    case_id,
    ticker,
    seed_urls,
    directory,
    fetch,
    generate,
    challenge,
    max_attempts,
):
    assert case_id == "case-host-contract"
    assert ticker == "MSFT"
    assert directory == "/sanitized/candidate-dir"
    assert max_attempts == 2
    assert seed_urls == [SEED_URL]

    document = fetch(seed_urls[0])
    assert set(document) == {"url", "text", "observed_at"}
    assert document["url"] == seed_urls[0]
    assert document["observed_at"] == NOW.isoformat()
    assert isinstance(document["text"], str) and "Public issuer revenue" in document["text"]

    discovery = _validate_original_stage_result(
        "discovery",
        generate("discovery", {
            "documents": [document],
            "seed_urls": seed_urls,
        }),
        seed_urls,
    )
    commercial = _validate_original_stage_result(
        "commercial",
        generate("commercial", {
            "documents": [document],
            "discovery": discovery,
        }),
        seed_urls,
    )
    underwriting = _validate_original_stage_result(
        "underwriting",
        generate("underwriting", {
            "documents": [document],
            "discovery": discovery,
            "commercial": commercial,
        }),
        seed_urls,
    )
    challenged = _validate_original_stage_result(
        "challenge",
        challenge({
            "documents": [document],
            "underwriting": underwriting,
        }),
        seed_urls,
    )

    if any(row["status"] == "REJECT" for row in (discovery, commercial, underwriting)):
        terminal_status = "BLOCK"
        terminal_reason = "generate stage rejected candidate"
    elif challenged["status"] == "BLOCK":
        terminal_status = "BLOCK"
        terminal_reason = challenged["reason"]
    elif any(
        row["status"] == "INCOMPLETE"
        for row in (discovery, commercial, underwriting, challenged)
    ):
        terminal_status = "INCOMPLETE"
        terminal_reason = "original stage chain incomplete"
    else:
        terminal_status = "READY_FOR_CIO"
        terminal_reason = "original stage chain passed"

    return _validate_original_terminal_result(
        {
            "status": terminal_status,
            "reason": terminal_reason,
            "source_urls": list(seed_urls),
        },
        seed_urls,
    )


def test_default_public_reader_uses_official_document_byte_budget(monkeypatch):
    class FakeHeaders:
        def get(self, key):
            assert key == "Content-Type"
            return "text/html; charset=utf-8"

    class FakeResponse:
        status = 200
        headers = FakeHeaders()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, limit):
            assert limit == live_bridge.OFFICIAL_DOCUMENT_MAX_BYTES + 1
            return b"<html><body><p>Issuer revenue and operating income disclosure.</p></body></html>"

    monkeypatch.setattr(live_bridge, "urlopen", lambda request, timeout: FakeResponse())
    acquired = OriginalResearchCallbackBridge._default_public_text_reader(SEED_URL)
    assert acquired["content_type"].startswith("text/html")
    assert acquired["body"].startswith(b"<html>")


def test_host_fetch_parses_genuine_microsoft_style_html_and_records_dual_hash_provenance():
    bridge = OriginalResearchCallbackBridge(routes=_host_routes(), coordinator=FakeCoordinator())
    raw = (
        b"<html><body>"
        b"<p>Microsoft revenue increased while operating income also increased according to the official quarterly disclosure.</p>"
        b"<table><tr><th>Revenue</th><th>Operating income</th></tr><tr><td>100</td><td>50</td></tr></table>"
        b"</body></html>"
    )
    result = bridge._fetch_host_public_document(
        SEED_URL,
        now=NOW,
        reader=lambda url: {"body": raw, "content_type": "text/html"},
    )
    assert set(result) == {"url", "text", "observed_at"}
    assert "Microsoft revenue increased" in result["text"]
    assert "Revenue" in result["text"]
    evidence = bridge.callback_evidence[-1]
    assert evidence["status"] == "COMPLETED"
    provenance = evidence["provenance"][0]
    assert provenance["source_url"] == SEED_URL
    assert provenance["extraction_succeeded"] is True
    assert len(provenance["content_sha256"]) == 64
    assert len(provenance["extracted_content_sha256"]) == 64
    assert provenance["content_sha256"] != provenance["extracted_content_sha256"]


def test_host_fetch_actual_oversized_html_fixture_fails_closed_and_keeps_failure_diagnostic():
    bridge = OriginalResearchCallbackBridge(routes=_host_routes(), coordinator=FakeCoordinator())
    raw = b"<html><body>" + (b"x" * live_bridge.OFFICIAL_DOCUMENT_MAX_BYTES) + b"</body></html>"
    try:
        bridge._fetch_host_public_document(
            SEED_URL,
            now=NOW,
            reader=lambda url: {"body": raw, "content_type": "text/html"},
        )
    except RuntimeError as exc:
        assert "HOST_FETCH_PUBLIC_DOCUMENT_OVERSIZE" in str(exc)
    else:
        raise AssertionError("oversized official HTML must fail closed")
    evidence = bridge.callback_evidence[-1]
    assert evidence["status"] == "BLOCKED"
    assert "HOST_FETCH_PUBLIC_DOCUMENT_OVERSIZE" in evidence["reason"]
    assert evidence["provenance"][0]["extraction_succeeded"] is False


def test_host_fetch_nonofficial_host_rejected_by_existing_official_parser_with_diagnostic():
    bridge = OriginalResearchCallbackBridge(routes=_host_routes(), coordinator=FakeCoordinator())
    nonofficial = "https://example.com/investor/earnings"
    try:
        bridge._fetch_host_public_document(
            nonofficial,
            now=NOW,
            reader=lambda url: {
                "body": b"<html><body><p>Revenue disclosure text from a nonofficial host must not pass.</p></body></html>",
                "content_type": "text/html",
            },
        )
    except RuntimeError as exc:
        assert "UNAPPROVED_DISCLOSURE_HOST" in str(exc)
    else:
        raise AssertionError("nonofficial disclosure host must fail closed")
    evidence = bridge.callback_evidence[-1]
    assert evidence["status"] == "BLOCKED"
    assert "UNAPPROVED_DISCLOSURE_HOST" in evidence["reason"]


def test_host_route_fallback_same_family_is_rejected_before_inference():
    primary = _engine("challenge", {
        "status": "PASS",
        "reason": "primary challenge",
        "source_urls": [SEED_URL],
        "objections": ["primary"],
    }, model="nemotron-a")
    fallback = _engine("challenge", {
        "status": "PASS",
        "reason": "fallback challenge",
        "source_urls": [SEED_URL],
        "objections": ["fallback"],
    }, model="nemotron-b")
    bridge = OriginalResearchCallbackBridge(
        routes={
            "challenge": StageRoute(
                primary=primary,
                primary_model_family="nemotron",
                fallback=fallback,
                fallback_model_family="nemotron",
            )
        },
        coordinator=FakeCoordinator(),
    )
    result = bridge._infer_host_route(
        "challenge",
        {"documents": []},
        now=NOW,
        seed_urls=[SEED_URL],
    )
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "INFERENCE_ROUTE_FALLBACK_MODEL_FAMILY_CONFLICT"
    assert result["attempts"] == []


def test_original_run_case_faithful_contract_executes_all_five_stages():
    bridge = OriginalResearchCallbackBridge(routes=_host_routes(), coordinator=FakeCoordinator())
    result = bridge.run_installed_run_case(
        _faithful_original_run_case,
        case_id="case-host-contract",
        symbol="MSFT",
        seed_urls=[SEED_URL],
        directory="/sanitized/candidate-dir",
        max_attempts=2,
        now=NOW,
        reader=_public_document_reader,
    )

    assert result["status"] == "READY_FOR_CIO"
    assert result["live_acceptance_claimed"] is False
    assert [row["stage"] for row in result["callback_evidence"]] == [
        "fetch", "discovery", "commercial", "underwriting", "challenge"
    ]
    fetch_evidence = result["callback_evidence"][0]
    assert set(fetch_evidence["provenance"][0]) == {
        "source_url", "observed_at", "content_sha256", "extracted_content_sha256", "extraction_succeeded"
    }
    assert fetch_evidence["provenance"][0]["extraction_succeeded"] is True
    assert len(fetch_evidence["provenance"][0]["content_sha256"]) == 64
    assert result["callback_evidence"][3]["model_identity"] == "local-provider:underwriter-a"
    assert result["callback_evidence"][4]["model_identity"] == "local-provider:challenger-b"
    assert result["callback_evidence"][4]["challenge_model_distinct"] is True


def test_underwriting_schema_advertises_complete_pass_contract_to_inference_model():
    schema = live_bridge.OriginalHostUnderwritingOutput.model_json_schema()
    conditional = schema.get("allOf")
    assert isinstance(conditional, list) and conditional
    pass_rule = conditional[0]
    assert pass_rule["if"]["properties"]["status"]["const"] == "PASS"
    required = set(pass_rule["then"]["required"])
    assert required == set(live_bridge.HOST_UNDERWRITING_REQUIRED_FIELDS)

    status_description = schema["properties"]["status"]["description"]
    assert "PASS only when every schema-listed underwriting field" in status_description
    assert "INCOMPLETE" in status_description
    assert "never invent values" in status_description

    incomplete = live_bridge.OriginalHostUnderwritingOutput.model_validate({
        "status": "INCOMPLETE",
        "reason": "public evidence does not support required underwriting facts",
        "source_urls": [SEED_URL],
    })
    assert incomplete.status == "INCOMPLETE"
    assert incomplete.financials is None
    assert incomplete.scenario_return_estimates is None


def test_underwriting_allowed_source_urls_are_intersection_of_seed_list_and_task_documents():
    bridge = OriginalResearchCallbackBridge(routes={}, coordinator=FakeCoordinator())
    second_seed = "https://www.microsoft.com/en-us/Investor/second"
    payload = bridge._host_stage_payload(
        "underwriting",
        {
            "documents": [{
                "url": SEED_URL,
                "text": "Microsoft official earnings public disclosure.",
                "observed_at": NOW.isoformat(),
            }],
            "discovery": {"status": "PASS", "reason": "done", "source_urls": [SEED_URL]},
            "commercial": {"status": "PASS", "reason": "done", "source_urls": [SEED_URL]},
        },
        symbol="MSFT",
        seed_urls=[SEED_URL, second_seed],
    )
    assert payload["underwriting_contract"]["allowed_source_urls"] == [SEED_URL]


def test_underwriting_pass_rejects_seed_url_not_present_in_task_documents():
    second_seed = "https://www.microsoft.com/en-us/Investor/second"
    output = {
        "status": "PASS",
        "reason": "incorrectly cites an unfetched seed",
        "source_urls": [second_seed],
        "financials": {"revenue": "supported"},
        "market_metrics": {"growth": "supported"},
        "capital_structure": {"cash": "supported"},
        "independent_source_mix": [second_seed],
        "reflexivity_score": 0.5,
        "scenario_return_estimates": {"base": "supported"},
        "factor_labels": ["earnings"],
        "business_maturity": "mature",
        "valuation_scenarios": {"base": "supported"},
        "buy_zone": {"research_only": True},
        "invalidation_conditions": ["facts deteriorate"],
        "review_by": "2026-10-08",
        "four_sentences": [
            "One.",
            "Two.",
            "Three.",
            "Four.",
        ],
    }
    bridge = OriginalResearchCallbackBridge(
        routes={
            "underwriting": StageRoute(
                primary=_engine("underwriting", output, model="underwriter-task-evidence"),
                primary_model_family="underwriting-family",
            )
        },
        coordinator=FakeCoordinator(),
    )
    payload = bridge._host_stage_payload(
        "underwriting",
        {
            "documents": [{
                "url": SEED_URL,
                "text": "Microsoft official earnings public disclosure.",
                "observed_at": NOW.isoformat(),
            }],
            "discovery": {"status": "PASS", "reason": "done", "source_urls": [SEED_URL]},
            "commercial": {"status": "PASS", "reason": "done", "source_urls": [SEED_URL]},
        },
        symbol="MSFT",
        seed_urls=[SEED_URL, second_seed],
    )
    result = bridge._infer_host_route(
        "underwriting",
        payload,
        now=NOW,
        seed_urls=[SEED_URL, second_seed],
    )
    assert result["status"] == "BLOCKED"
    assert result["reason"].endswith("HOST_UNDERWRITING_INDEPENDENT_SOURCE_NOT_IN_TASK_EVIDENCE")


def test_underwriting_task_contract_requires_explicit_allowed_seed_urls():
    bridge = OriginalResearchCallbackBridge(routes={}, coordinator=FakeCoordinator())
    try:
        bridge._host_stage_payload(
            "underwriting",
            {
                "documents": [],
                "discovery": {"status": "PASS", "reason": "done", "source_urls": [SEED_URL]},
                "commercial": {"status": "PASS", "reason": "done", "source_urls": [SEED_URL]},
            },
            symbol="MSFT",
            seed_urls=[],
        )
    except RuntimeError as exc:
        assert str(exc) == "HOST_UNDERWRITING_ALLOWED_SOURCE_URLS_REQUIRED"
    else:
        raise AssertionError("underwriting task contract must fail closed without explicit allowed seed URLs")


def test_underwriting_prompt_contains_schema_visible_pass_requirements_without_fabrication():
    seen = {}
    def transport(message, **kwargs):
        seen["message"] = message
        return {
            "response": json.dumps({
                "status": "INCOMPLETE",
                "reason": "MISSING_UNDERWRITING_FIELDS:financials",
                "source_urls": [SEED_URL],
            }),
            "returncode": 0,
            "runtime_metadata": {
                "resolved_provider": "local-provider",
                "resolved_model": "underwriter-schema",
                "auth_verified": True,
                "is_success_response": True,
                "is_fixture": False,
                "fallback_active": False,
            },
        }

    engine = HermesLocalInference(
        InferenceContract(
            provider="local-provider",
            model="underwriter-schema",
            session_id="issue16-underwriting-schema",
            workspace_root="/workspace",
            is_free_or_local_authorized=True,
            purpose="issue16 underwriting schema visibility",
        ),
        transport=transport,
    )
    bridge = OriginalResearchCallbackBridge(
        routes={
            "underwriting": StageRoute(
                primary=engine,
                primary_model_family="underwriting-family",
            )
        },
        coordinator=FakeCoordinator(),
    )
    payload = bridge._host_stage_payload(
        "underwriting",
        {
            "documents": [{
                "url": SEED_URL,
                "text": "Microsoft official earnings public disclosure.",
                "observed_at": NOW.isoformat(),
            }],
            "discovery": {"status": "PASS", "reason": "done", "source_urls": [SEED_URL]},
            "commercial": {"status": "PASS", "reason": "done", "source_urls": [SEED_URL]},
        },
        symbol="MSFT",
        seed_urls=[SEED_URL],
    )
    result = bridge._infer_host_route(
        "underwriting",
        payload,
        now=NOW,
        seed_urls=[SEED_URL],
    )
    assert result["status"] == "COMPLETED"
    assert result["output"]["status"] == "INCOMPLETE"
    prompt = seen["message"]
    assert '"allOf"' in prompt
    for field in live_bridge.HOST_UNDERWRITING_REQUIRED_FIELDS:
        assert f'"{field}"' in prompt
    assert "never invent values" in prompt
    assert "MISSING_UNDERWRITING_FIELDS" in prompt
    assert '"required_output_fields_for_pass"' in prompt
    assert '"allowed_source_urls"' in prompt
    assert SEED_URL in prompt


def test_microsoft_official_false_pass_missing_main_underwriting_fields_downgrades_incomplete():
    """Regression for Main's real-host false PASS observed on Microsoft earnings input."""
    missing_fields = {
        "financials",
        "market_metrics",
        "capital_structure",
        "independent_source_mix",
        "reflexivity_score",
        "scenario_return_estimates",
        "factor_labels",
    }
    false_pass = {
        "status": "PASS",
        "reason": "model incorrectly promoted incomplete Microsoft public evidence",
        "source_urls": [SEED_URL],
        "business_maturity": "mature public operating business",
        "valuation_scenarios": {
            "bear": "qualitative downside case only",
            "base": "qualitative base case only",
            "bull": "qualitative upside case only",
        },
        "buy_zone": {"research_only": True},
        "invalidation_conditions": ["public operating facts materially deteriorate"],
        "review_by": "2026-10-08",
        "four_sentences": [
            "Microsoft public earnings evidence was available.",
            "Discovery and commercial stages produced public-source artifacts.",
            "Required underwriting fields were not supported by the supplied evidence.",
            "This candidate must remain incomplete rather than invent missing values.",
        ],
    }
    route = StageRoute(
        primary=_engine("underwriting", false_pass, model="underwriter-false-pass"),
        primary_model_family="underwriting-family",
    )
    bridge = OriginalResearchCallbackBridge(
        routes={"underwriting": route},
        coordinator=FakeCoordinator(),
    )
    payload = bridge._host_stage_payload(
        "underwriting",
        {
            "documents": [{
                "url": SEED_URL,
                "text": "Microsoft official earnings revenue and operating income disclosure.",
                "observed_at": NOW.isoformat(),
            }],
            "discovery": {
                "status": "PASS",
                "reason": "official Microsoft disclosure discovered",
                "source_urls": [SEED_URL],
            },
            "commercial": {
                "status": "PASS",
                "reason": "commercial evidence produced",
                "source_urls": [SEED_URL],
            },
        },
        symbol="MSFT",
        seed_urls=[SEED_URL],
    )
    contract = payload["underwriting_contract"]
    assert set(contract["required_output_fields_for_pass"]) == set(
        live_bridge.HOST_UNDERWRITING_REQUIRED_FIELDS
    )
    assert contract["allowed_source_urls"] == [SEED_URL]
    assert "Do not infer or invent missing numerical values" in contract["pass_semantics"]
    assert "MISSING_UNDERWRITING_FIELDS" in contract["insufficient_evidence_semantics"]

    result = bridge._infer_host_route(
        "underwriting",
        payload,
        now=NOW,
        seed_urls=[SEED_URL],
    )
    assert result["status"] == "COMPLETED"
    assert result["output"]["status"] == "INCOMPLETE"
    assert result["schema_valid"] is True
    reason = result["output"]["reason"]
    assert reason.startswith("MISSING_UNDERWRITING_FIELDS:")
    assert set(reason.split(":", 1)[1].split(",")) == missing_fields
    for field in missing_fields:
        assert field not in result["output"]


def test_original_run_case_missing_underwriting_facts_downgrades_incomplete():
    bridge = OriginalResearchCallbackBridge(
        routes=_host_routes(missing_underwriting=True),
        coordinator=FakeCoordinator(),
    )
    seen = {}

    def host(case_id, ticker, seed_urls, directory, fetch, generate, challenge, max_attempts):
        document = fetch(seed_urls[0])
        discovery = _validate_original_stage_result(
            "discovery", generate("discovery", {"documents": [document]}), seed_urls
        )
        commercial = _validate_original_stage_result(
            "commercial",
            generate("commercial", {"documents": [document], "discovery": discovery}),
            seed_urls,
        )
        underwriting = _validate_original_stage_result(
            "underwriting",
            generate("underwriting", {
                "documents": [document],
                "discovery": discovery,
                "commercial": commercial,
            }),
            seed_urls,
        )
        seen["underwriting"] = underwriting
        assert underwriting["status"] == "INCOMPLETE"
        assert underwriting["reason"] == "MISSING_UNDERWRITING_FIELDS:financials"
        challenged = _validate_original_stage_result(
            "challenge",
            challenge({"documents": [document], "underwriting": underwriting}),
            seed_urls,
        )
        return _validate_original_terminal_result(
            {
                "status": "INCOMPLETE",
                "reason": "underwriting incomplete",
                "source_urls": seed_urls,
            },
            seed_urls,
        )

    result = bridge.run_installed_run_case(
        host,
        case_id="missing-facts",
        symbol="MSFT",
        seed_urls=[SEED_URL],
        directory="/sanitized/candidate-dir",
        max_attempts=2,
        now=NOW,
        reader=_public_document_reader,
    )
    assert seen["underwriting"]["status"] == "INCOMPLETE"
    assert result["status"] == "INCOMPLETE"


def test_original_run_case_source_urls_must_be_seed_subset():
    bridge = OriginalResearchCallbackBridge(
        routes=_host_routes(outside_seed=True),
        coordinator=FakeCoordinator(),
    )

    def host(case_id, ticker, seed_urls, directory, fetch, generate, challenge, max_attempts):
        document = fetch(seed_urls[0])
        generate("discovery", {"documents": [document]})
        raise AssertionError("outside-seed source must not pass")

    result = bridge.run_installed_run_case(
        host,
        case_id="outside-seed",
        symbol="MSFT",
        seed_urls=[SEED_URL],
        directory="/sanitized/candidate-dir",
        max_attempts=1,
        now=NOW,
        reader=_public_document_reader,
    )
    assert result["status"] == "BLOCKED"
    assert "HOST_DISCOVERY_SOURCE_URL_OUTSIDE_SEEDS" in result["reason"]


def test_original_run_case_challenge_missing_objections_fails_closed():
    bridge = OriginalResearchCallbackBridge(
        routes=_host_routes(omit_challenge_objections=True),
        coordinator=FakeCoordinator(),
    )
    result = bridge.run_installed_run_case(
        _faithful_original_run_case,
        case_id="case-host-contract",
        symbol="MSFT",
        seed_urls=[SEED_URL],
        directory="/sanitized/candidate-dir",
        max_attempts=2,
        now=NOW,
        reader=_public_document_reader,
    )
    assert result["status"] == "BLOCKED"
    assert "HOST_CHALLENGE_CALLBACK_BLOCKED" in result["reason"]
    assert "objections" in result["reason"] or "objections" in json.dumps(result["callback_evidence"])


def test_original_run_case_challenge_malformed_objections_fails_closed():
    bridge = OriginalResearchCallbackBridge(
        routes=_host_routes(challenge_objections="not-a-list"),
        coordinator=FakeCoordinator(),
    )
    result = bridge.run_installed_run_case(
        _faithful_original_run_case,
        case_id="case-host-contract",
        symbol="MSFT",
        seed_urls=[SEED_URL],
        directory="/sanitized/candidate-dir",
        max_attempts=2,
        now=NOW,
        reader=_public_document_reader,
    )
    assert result["status"] == "BLOCKED"
    assert "HOST_CHALLENGE_CALLBACK_BLOCKED" in result["reason"]


def test_original_run_case_challenge_block_maps_terminal_to_block_and_preserves_objections():
    objections = [
        "Valuation sensitivity remains too wide for a PASS.",
        "Public evidence does not resolve the downside case.",
    ]
    bridge = OriginalResearchCallbackBridge(
        routes=_host_routes(
            challenge_status="BLOCK",
            challenge_objections=objections,
        ),
        coordinator=FakeCoordinator(),
    )
    seen = {}

    def host(case_id, ticker, seed_urls, directory, fetch, generate, challenge, max_attempts):
        document = fetch(seed_urls[0])
        discovery = _validate_original_stage_result(
            "discovery", generate("discovery", {"documents": [document]}), seed_urls
        )
        commercial = _validate_original_stage_result(
            "commercial",
            generate("commercial", {"documents": [document], "discovery": discovery}),
            seed_urls,
        )
        underwriting = _validate_original_stage_result(
            "underwriting",
            generate("underwriting", {
                "documents": [document],
                "discovery": discovery,
                "commercial": commercial,
            }),
            seed_urls,
        )
        challenged = _validate_original_stage_result(
            "challenge",
            challenge({"documents": [document], "underwriting": underwriting}),
            seed_urls,
        )
        seen["challenge"] = challenged
        assert challenged["status"] == "BLOCK"
        return _validate_original_terminal_result(
            {
                "status": "BLOCK",
                "reason": challenged["reason"],
                "source_urls": challenged["source_urls"],
            },
            seed_urls,
        )

    result = bridge.run_installed_run_case(
        host,
        case_id="blocked-challenge",
        symbol="MSFT",
        seed_urls=[SEED_URL],
        directory="/sanitized/candidate-dir",
        max_attempts=2,
        now=NOW,
        reader=_public_document_reader,
    )
    assert result["status"] == "BLOCK"
    assert seen["challenge"]["objections"] == objections
    assert result["callback_evidence"][-1]["model_identity"] == "local-provider:challenger-b"
    assert result["callback_evidence"][-1]["challenge_model_distinct"] is True


def _run_candidate_cli(monkeypatch, capsys, *, routes, entrypoint):
    monkeypatch.setattr(candidate_cli, "_routes", lambda path: routes)
    monkeypatch.setattr(candidate_cli, "_load_entrypoint", lambda spec: entrypoint)
    monkeypatch.setattr(
        OriginalResearchCallbackBridge,
        "_default_public_text_reader",
        staticmethod(_public_document_reader),
    )
    exit_code = candidate_cli.main([
        "research",
        "--symbol", "MSFT",
        "--case-id", "case-host-contract",
        "--seed-url", SEED_URL,
        "--directory", "/sanitized/candidate-dir",
        "--routes", "/sanitized/routes.json",
        "--entrypoint", "synthetic_original:run_case",
        "--max-attempts", "2",
        "--now", NOW.isoformat(),
    ])
    output = json.loads(capsys.readouterr().out.strip())
    return exit_code, output


def test_candidate_cli_ready_for_cio_exits_zero_and_preserves_host_output(monkeypatch, capsys):
    exit_code, output = _run_candidate_cli(
        monkeypatch,
        capsys,
        routes=_host_routes(),
        entrypoint=_faithful_original_run_case,
    )
    assert exit_code == 0
    assert output["status"] == "READY_FOR_CIO"
    assert output["reason"] == "original stage chain passed"
    assert output["source_urls"] == [SEED_URL]
    assert output["live_acceptance_claimed"] is False
    assert output["candidate_cli"] is True
    assert output["host_entrypoint_spec"] == "synthetic_original:run_case"
    assert [row["stage"] for row in output["callback_evidence"]] == [
        "fetch", "discovery", "commercial", "underwriting", "challenge"
    ]


def test_candidate_cli_block_terminal_is_nonzero_and_preserved(monkeypatch, capsys):
    exit_code, output = _run_candidate_cli(
        monkeypatch,
        capsys,
        routes=_host_routes(
            challenge_status="BLOCK",
            challenge_objections=["Synthetic objection blocks candidate."],
        ),
        entrypoint=_faithful_original_run_case,
    )
    assert exit_code != 0
    assert output["status"] == "BLOCK"
    assert output["reason"] == "independent challenge blocks the candidate"
    assert output["live_acceptance_claimed"] is False


def test_candidate_cli_incomplete_terminal_is_nonzero_and_preserved(monkeypatch, capsys):
    exit_code, output = _run_candidate_cli(
        monkeypatch,
        capsys,
        routes=_host_routes(missing_underwriting=True),
        entrypoint=_faithful_original_run_case,
    )
    assert exit_code != 0
    assert output["status"] == "INCOMPLETE"
    assert output["reason"] == "original stage chain incomplete"
    assert output["live_acceptance_claimed"] is False


def test_candidate_cli_failed_terminal_is_nonzero_and_preserved(monkeypatch, capsys):
    def failed_after_full_contract(*args, **kwargs):
        completed = _faithful_original_run_case(*args, **kwargs)
        assert completed["status"] == "READY_FOR_CIO"
        return _validate_original_terminal_result(
            {
                "status": "FAILED",
                "reason": "synthetic terminal failure after full contract traversal",
                "source_urls": completed["source_urls"],
            },
            completed["source_urls"],
        )

    exit_code, output = _run_candidate_cli(
        monkeypatch,
        capsys,
        routes=_host_routes(),
        entrypoint=failed_after_full_contract,
    )
    assert exit_code != 0
    assert output["status"] == "FAILED"
    assert output["reason"] == "synthetic terminal failure after full contract traversal"
    assert output["live_acceptance_claimed"] is False
    assert [row["stage"] for row in output["callback_evidence"]] == [
        "fetch", "discovery", "commercial", "underwriting", "challenge"
    ]


def test_original_run_case_unavailable_fetch_is_blocked_and_invocation_evidence_resets():
    bridge = OriginalResearchCallbackBridge(routes=_host_routes(), coordinator=FakeCoordinator())

    def fetch_only(case_id, ticker, seed_urls, directory, fetch, generate, challenge, max_attempts):
        fetch(seed_urls[0])
        return {"status": "INCOMPLETE", "reason": "stop after fetch", "source_urls": seed_urls}

    first = bridge.run_installed_run_case(
        fetch_only,
        case_id="first",
        symbol="MSFT",
        seed_urls=[SEED_URL],
        directory="/sanitized/candidate-dir",
        max_attempts=1,
        now=NOW,
        reader=_public_document_reader,
    )
    assert [row["stage"] for row in first["callback_evidence"]] == ["fetch"]

    def unavailable_reader(url):
        raise RuntimeError("PUBLIC_SOURCE_OFFLINE")

    second = bridge.run_installed_run_case(
        fetch_only,
        case_id="second",
        symbol="MSFT",
        seed_urls=[SEED_URL],
        directory="/sanitized/candidate-dir",
        max_attempts=1,
        now=NOW,
        reader=unavailable_reader,
    )
    assert second["status"] == "BLOCKED"
    assert "HOST_FETCH_PUBLIC_DOCUMENT_UNAVAILABLE" in second["reason"]
    assert len(second["callback_evidence"]) == 1
    assert second["callback_evidence"][0]["stage"] == "fetch"
    assert second["callback_evidence"][0]["status"] == "BLOCKED"
    assert "PUBLIC_SOURCE_OFFLINE" in second["callback_evidence"][0]["reason"]
    assert second["callback_evidence"][0]["provenance"][0]["extraction_succeeded"] is False


def test_original_run_case_private_public_document_is_rejected_before_model_transport():
    bridge = OriginalResearchCallbackBridge(routes=_host_routes(), coordinator=FakeCoordinator())

    def host(case_id, ticker, seed_urls, directory, fetch, generate, challenge, max_attempts):
        fetch(seed_urls[0])
        raise AssertionError("private document should not pass fetch")

    result = bridge.run_installed_run_case(
        host,
        case_id="private-negative",
        symbol="MSFT",
        seed_urls=[SEED_URL],
        directory="/sanitized/candidate-dir",
        max_attempts=1,
        now=NOW,
        reader=lambda url: "<html><body><p>Official revenue disclosure references private runtime path /home/user/secret.json and must be rejected.</p></body></html>",
    )
    assert result["status"] == "BLOCKED"
    assert "PUBLIC_OUTBOUND_VALUE_REJECTED" in result["reason"]
    assert len(result["callback_evidence"]) == 1
    assert result["callback_evidence"][0]["stage"] == "fetch"
    assert result["callback_evidence"][0]["status"] == "BLOCKED"
    assert "PUBLIC_OUTBOUND_VALUE_REJECTED" in result["callback_evidence"][0]["reason"]
    assert result["callback_evidence"][0]["provenance"][0]["extraction_succeeded"] is False


def test_original_run_case_same_model_family_challenge_is_blocked_even_when_model_names_differ():
    bridge = OriginalResearchCallbackBridge(
        routes=_host_routes(same_challenge_family=True),
        coordinator=FakeCoordinator(),
    )

    def host(case_id, ticker, seed_urls, directory, fetch, generate, challenge, max_attempts):
        document = fetch(seed_urls[0])
        discovery = generate("discovery", {"documents": [document]})
        commercial = generate("commercial", {"documents": [document], "discovery": discovery})
        underwriting = generate("underwriting", {
            "documents": [document],
            "discovery": discovery,
            "commercial": commercial,
        })
        challenge({"documents": [document], "underwriting": underwriting})
        raise AssertionError("same-family challenge should not return successfully")

    result = bridge.run_installed_run_case(
        host,
        case_id="same-family-negative",
        symbol="MSFT",
        seed_urls=[SEED_URL],
        directory="/sanitized/candidate-dir",
        max_attempts=1,
        now=NOW,
        reader=_public_document_reader,
    )
    assert result["status"] == "BLOCKED"
    assert "CHALLENGE_MODEL_FAMILY_NOT_HETEROGENEOUS" in result["reason"]
    assert result["callback_evidence"][-1]["challenge_model_distinct"] is False
    assert result["callback_evidence"][-1]["model_family"] == "nemotron"


def test_original_run_case_same_model_challenge_is_blocked_using_actual_underwriting_identity():
    bridge = OriginalResearchCallbackBridge(
        routes=_host_routes(same_challenge=True),
        coordinator=FakeCoordinator(),
    )

    def host(case_id, ticker, seed_urls, directory, fetch, generate, challenge, max_attempts):
        document = fetch(seed_urls[0])
        discovery = generate("discovery", {"documents": [document]})
        commercial = generate("commercial", {"documents": [document], "discovery": discovery})
        underwriting = generate("underwriting", {
            "documents": [document],
            "discovery": discovery,
            "commercial": commercial,
        })
        challenge({"documents": [document], "underwriting": underwriting})
        raise AssertionError("same-model challenge should not return successfully")

    result = bridge.run_installed_run_case(
        host,
        case_id="same-model-negative",
        symbol="MSFT",
        seed_urls=[SEED_URL],
        directory="/sanitized/candidate-dir",
        max_attempts=2,
        now=NOW,
        reader=_public_document_reader,
    )
    assert result["status"] == "BLOCKED"
    assert "CHALLENGE_MODEL_NOT_HETEROGENEOUS" in result["reason"]
    challenge_evidence = result["callback_evidence"][-1]
    assert challenge_evidence["stage"] == "challenge"
    assert challenge_evidence["model_identity"] == "local-provider:underwriter-a"
    assert challenge_evidence["status"] == "BLOCKED"


def test_original_run_case_challenge_before_underwriting_is_blocked():
    bridge = OriginalResearchCallbackBridge(routes=_host_routes(), coordinator=FakeCoordinator())

    def host(case_id, ticker, seed_urls, directory, fetch, generate, challenge, max_attempts):
        document = fetch(seed_urls[0])
        challenge({"documents": [document]})
        raise AssertionError("challenge without underwriting identity must not return")

    result = bridge.run_installed_run_case(
        host,
        case_id="missing-underwriting-negative",
        symbol="MSFT",
        seed_urls=[SEED_URL],
        directory="/sanitized/candidate-dir",
        max_attempts=1,
        now=NOW,
        reader=_public_document_reader,
    )
    assert result["status"] == "BLOCKED"
    assert "HOST_UNDERWRITING_MODEL_IDENTITY_REQUIRED" in result["reason"]


def test_underwriting_payload_accepts_installed_host_official_documents_key():
    """Installed run_case sends `official_documents`, not `documents` (live PR28 failure)."""
    payload = OriginalResearchCallbackBridge._host_stage_payload(
        "underwriting",
        {"ticker": "MSFT", "official_documents": [{"url": SEED_URL, "text": "x"}], "prior_public_results": {}},
        symbol="MSFT",
        seed_urls=[SEED_URL],
    )
    assert payload["underwriting_contract"]["allowed_source_urls"] == [SEED_URL]


# Judgment layer regression: host status must remain INCOMPLETE until Main CIO review.
def _judgment_snapshot(**changes):
    snapshot = {
        "price": 100.0, "as_of": NOW.isoformat(), "source": "public market quote",
        "week52_high": 120.0, "week52_low": 80.0,
    }
    snapshot.update(changes)
    return snapshot


def _judgment_draft(**changes):
    from datetime import timedelta
    draft = {
        "reflexivity_score": 0.5,
        "scenario_return_estimates": {"bear": -0.2, "base": 0.1, "bull": 0.3},
        "business_maturity": "mature",
        "valuation_scenarios": {"bear": 80.0, "base": 110.0, "bull": 130.0},
        "buy_zone": {"low": 90.0, "high": 105.0},
        "invalidation_conditions": ["material deterioration"],
        "review_by": (NOW + timedelta(days=30)).date().isoformat(),
    }
    draft.update(changes)
    return draft


def test_judgment_snapshot_stale_tzless_future_and_missing_never_pass():
    from datetime import timedelta
    for snapshot in (
        None,
        _judgment_snapshot(as_of=(NOW - timedelta(hours=37)).isoformat()),
        _judgment_snapshot(as_of=NOW.replace(tzinfo=None).isoformat()),
        _judgment_snapshot(as_of=(NOW + timedelta(seconds=1)).isoformat()),
    ):
        assert live_bridge._valid_market_snapshot(snapshot, NOW) is False


def test_judgment_valid_snapshot_and_draft_requires_main_cio():
    snapshot = _judgment_snapshot()
    draft = _judgment_draft()
    assert live_bridge._valid_market_snapshot(snapshot, NOW)
    assert live_bridge._validate_judgment_draft(draft, snapshot["price"], NOW) is None
    assert "PASS_PENDING_MAIN_CIO_REVIEW" not in live_bridge.OriginalHostUnderwritingOutput.model_fields["status"].metadata[0].pattern


def test_judgment_buy_zone_out_of_scale_rejected():
    assert live_bridge._validate_judgment_draft(
        _judgment_draft(buy_zone={"low": 10.0, "high": 20.0}), 100.0, NOW
    ) == "buy_zone"


def test_judgment_valuation_and_review_deadline_rejected():
    from datetime import timedelta
    assert live_bridge._validate_judgment_draft(
        _judgment_draft(valuation_scenarios={"bear": 80, "base": "unsupported", "bull": 120}), 100, NOW
    ) == "valuation_scenarios"
    assert live_bridge._validate_judgment_draft(
        _judgment_draft(review_by=(NOW + timedelta(days=121)).date().isoformat()), 100, NOW
    ) == "review_by"


def test_judgment_official_disclosure_allowlist_unchanged():
    import pytest
    from cio_market_lab.research.official_documents import parse_official_document
    with pytest.raises(ValueError, match="UNAPPROVED_DISCLOSURE_HOST"):
        parse_official_document(
            "https://finance.yahoo.com/quote/MSFT",
            b"<html><body><p>Revenue disclosure</p></body></html>",
            "text/html",
        )


def test_judgment_snapshot_private_content_rejected():
    import pytest
    with pytest.raises((ValueError, RuntimeError)):
        live_bridge._reject_private_content(
            {"price": 100, "source": "private account", "account_number": "123456789"},
            "market_snapshot",
        )


def test_judgment_no_snapshot_preserves_legacy_complete_pass():
    """No market_snapshot key must preserve the installed original host PASS semantics."""
    bridge = OriginalResearchCallbackBridge(routes=_host_routes(), coordinator=FakeCoordinator())
    payload = bridge._host_stage_payload(
        "underwriting",
        {"documents": [{"url": SEED_URL, "text": "Official issuer disclosure", "observed_at": NOW.isoformat()}]},
        symbol="MSFT", seed_urls=[SEED_URL],
    )
    assert "market_snapshot" not in payload
    result = bridge._infer_host_route("underwriting", payload, now=NOW, seed_urls=[SEED_URL])
    assert result["status"] == "COMPLETED"
    assert result["output"]["status"] == "PASS"
    assert "judgment_draft" not in result["output"]


def _snapshot_injection_run(snapshot, draft_overrides=None, judgment_status="DRAFT_FOR_MAIN_CIO"):
    out = {
        "status": "PASS", "reason": "drafted", "source_urls": [SEED_URL],
        "financials": {"revenue": "supported"}, "market_metrics": {"growth": "supported"},
        "capital_structure": {"cash": "supported"}, "independent_source_mix": [SEED_URL],
        "factor_labels": ["earnings"],
        "four_sentences": ["One.", "Two.", "Three.", "Four."],
        **_judgment_draft(**(draft_overrides or {})),
    }
    if judgment_status:
        out["judgment_status"] = judgment_status
    bridge = OriginalResearchCallbackBridge(
        routes={"underwriting": StageRoute(
            primary=_engine("underwriting", out, model="underwriter-snapshot"),
            primary_model_family="underwriting-family")},
        coordinator=FakeCoordinator(),
        market_snapshot=snapshot,
    )
    payload = bridge._host_stage_payload(
        "underwriting",
        {
            "documents": [{"url": SEED_URL, "text": "Microsoft official earnings public disclosure.",
                           "observed_at": NOW.isoformat()}],
            "discovery": {"status": "PASS", "reason": "done", "source_urls": [SEED_URL]},
            "commercial": {"status": "PASS", "reason": "done", "source_urls": [SEED_URL]},
        },
        symbol="MSFT", seed_urls=[SEED_URL],
    )
    return bridge._infer_host_route("underwriting", payload, now=NOW, seed_urls=[SEED_URL])


def test_constructor_snapshot_valid_yields_draft_never_pass():
    result = _snapshot_injection_run(_judgment_snapshot())
    out = result["output"] if "output" in result else result
    assert result["status"] != "PASS"
    assert out.get("status") == "INCOMPLETE"
    assert out.get("judgment_status") == "DRAFT_FOR_MAIN_CIO"
    assert out["judgment_draft"]["buy_zone"] == {"low": 90.0, "high": 105.0}


def test_constructor_snapshot_stale_never_pass():
    from datetime import timedelta
    result = _snapshot_injection_run(_judgment_snapshot(as_of=(NOW - timedelta(hours=40)).isoformat()))
    out = result["output"] if "output" in result else result
    assert result["status"] != "PASS"
    assert "MARKET_SNAPSHOT_STALE_OR_MISSING" in str(out.get("reason"))
    assert not out.get("judgment_draft")


def test_constructor_snapshot_out_of_scale_buy_zone_rejected():
    result = _snapshot_injection_run(_judgment_snapshot(), {"buy_zone": {"low": 5.0, "high": 9.0}})
    out = result["output"] if "output" in result else result
    assert result["status"] != "PASS"
    assert "INVALID_JUDGMENT_FIELD:buy_zone" in str(out.get("reason"))


def test_snapshot_judgment_output_format_and_legacy_absence():
    from cio_market_lab.research.issue16_live_bridge import OriginalResearchCallbackBridge
    document = {"url": "https://example.com/official"}
    base = {"official_documents": [document]}
    kwargs = {"symbol": "MSFT", "seed_urls": [document["url"]]}
    legacy = OriginalResearchCallbackBridge._host_stage_payload("underwriting", base, **kwargs)
    assert "judgment_output_format" not in legacy["underwriting_contract"]
    snapshot = {**base, "market_snapshot": {"price": 535.07, "source": "public", "as_of": NOW.isoformat()}}
    enriched = OriginalResearchCallbackBridge._host_stage_payload("underwriting", snapshot, **kwargs)
    fmt = enriched["underwriting_contract"]["judgment_output_format"]
    assert set(live_bridge.HOST_UNDERWRITING_JUDGMENT_FIELDS).issubset(fmt)
    assert "DRAFT_FOR_MAIN_CIO" in fmt["judgment_status"]
    assert legacy["underwriting_contract"] == {
        **live_bridge.HOST_UNDERWRITING_PROMPT_CONTRACT,
        "allowed_source_urls": [document["url"]],
    }


def test_judgment_prompt_example_cannot_validate_as_real_draft():
    from cio_market_lab.research.issue16_live_bridge import OriginalResearchCallbackBridge
    document = {"url": "https://example.com/official"}
    payload = {"official_documents": [document], "market_snapshot": {"price": 535.07}}
    normalized = OriginalResearchCallbackBridge._host_stage_payload(
        "underwriting", payload, symbol="MSFT", seed_urls=[document["url"]]
    )
    example = normalized["underwriting_contract"]["judgment_output_format"]["worked_example"]
    assert example["label"].startswith("EXAMPLE_ONLY")
    assert live_bridge._validate_judgment_draft(example, 535.07, NOW) is not None


def test_constructor_snapshot_injection_ships_judgment_format_to_model():
    captured = {}
    real = HermesLocalInference.infer

    def spy(self, stage, payload, schema):
        captured["contract"] = payload.get("underwriting_contract")
        captured["snapshot"] = payload.get("market_snapshot")
        return real(self, stage, payload, schema)

    HermesLocalInference.infer = spy
    try:
        _snapshot_injection_run(_judgment_snapshot())
    finally:
        HermesLocalInference.infer = real
    assert captured["snapshot"]["price"] == 100.0
    fmt = captured["contract"]["judgment_output_format"]
    assert "reflexivity_score" in fmt and "buy_zone" in fmt and "review_by" in fmt


def test_four_sentences_shape_only_with_market_snapshot():
    bridge = OriginalResearchCallbackBridge(routes=_host_routes(), coordinator=FakeCoordinator())
    base = {"documents": [{"url": SEED_URL, "text": "Official issuer disclosure", "observed_at": NOW.isoformat()}]}
    legacy = bridge._host_stage_payload("underwriting", base, symbol="MSFT", seed_urls=[SEED_URL])
    assert "four_sentences_shape" not in legacy["underwriting_contract"]
    assert "judgment_output_format" not in legacy["underwriting_contract"]
    with_snapshot = bridge._host_stage_payload(
        "underwriting", {**base, "market_snapshot": _judgment_snapshot()},
        symbol="MSFT", seed_urls=[SEED_URL],
    )
    shape = with_snapshot["underwriting_contract"]["four_sentences_shape"]
    assert "EXACTLY 4 separate strings" in shape["rule"]
    assert "one sentence per element" in shape["rule"]
    assert shape["example_label"].startswith("EXAMPLE_ONLY")
    assert shape["example"] == ["S1.", "S2.", "S3.", "S4."]
    assert len(shape["example"]) == 4
    assert all(isinstance(sentence, str) for sentence in shape["example"])
