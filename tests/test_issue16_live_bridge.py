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


def _public_document_reader(url):
    assert url == "https://www.microsoft.com/en-us/Investor/test"
    return [
        {
            "document_part": "HTML paragraph 1",
            "text": "Public issuer revenue and operating income disclosure for source-only contract testing.",
        }
    ]


def test_original_run_case_signature_executes_fetch_generate_challenge_end_to_end():
    bridge = OriginalResearchCallbackBridge(routes=_routes(), coordinator=FakeCoordinator())
    seen = {}

    def original_run_case(case_id, ticker, seed_urls, directory, fetch, generate, challenge, max_attempts):
        assert case_id == "case-host-contract"
        assert ticker == "MSFT"
        assert directory == "/sanitized/candidate-dir"
        assert max_attempts == 2

        document = fetch(seed_urls[0])
        assert document["source_url"] == seed_urls[0]
        assert document["observed_at"] == NOW.isoformat()
        assert len(document["content_sha256"]) == 64
        assert document["content"][0]["document_part"] == "HTML paragraph 1"

        discovery = generate("discovery", {
            "documents": [document],
            "seed_urls": seed_urls,
        })
        assert set(discovery) == {
            "candidate_sources", "discovery_summary", "missing_evidence"
        }

        commercial = generate("commercial", {
            "documents": [document],
            "discovery": discovery,
        })
        assert set(commercial) == {
            "commercial_summary", "evidence_used", "missing_evidence"
        }

        underwriting = generate("underwriting", {
            "documents": [document],
            "discovery": discovery,
            "commercial": commercial,
        })
        assert underwriting["underwriting_status"] == "PUBLIC_EVIDENCE_READY"

        challenged = challenge({
            "documents": [document],
            "underwriting": underwriting,
        })
        assert challenged["verdict"] == "PASS_PUBLIC_RESEARCH_ONLY"
        seen["complete"] = True
        return {
            "status": "COMPLETED_PUBLIC_RESEARCH_CANDIDATE",
            "underwriting_status": underwriting["underwriting_status"],
            "challenge_verdict": challenged["verdict"],
        }

    result = bridge.run_installed_run_case(
        original_run_case,
        case_id="case-host-contract",
        symbol="MSFT",
        seed_urls=["https://www.microsoft.com/en-us/Investor/test"],
        directory="/sanitized/candidate-dir",
        max_attempts=2,
        now=NOW,
        reader=_public_document_reader,
    )

    assert seen["complete"] is True
    assert result["status"] == "COMPLETED_PUBLIC_RESEARCH_CANDIDATE"
    assert result["live_acceptance_claimed"] is False
    assert [row["stage"] for row in result["callback_evidence"]] == [
        "fetch", "discovery", "commercial", "underwriting", "challenge"
    ]
    assert result["callback_evidence"][3]["model_identity"] == "local-provider:underwriter-a"
    assert result["callback_evidence"][4]["model_identity"] == "local-provider:challenger-b"
    assert result["callback_evidence"][4]["challenge_model_distinct"] is True


def test_original_run_case_unavailable_fetch_is_blocked_and_invocation_evidence_resets():
    bridge = OriginalResearchCallbackBridge(routes=_routes(), coordinator=FakeCoordinator())

    def successful_host(case_id, ticker, seed_urls, directory, fetch, generate, challenge, max_attempts):
        fetch(seed_urls[0])
        return {"status": "BLOCKED", "reason": "STOP_AFTER_FETCH"}

    first = bridge.run_installed_run_case(
        successful_host,
        case_id="first",
        symbol="MSFT",
        seed_urls=["https://www.microsoft.com/en-us/Investor/test"],
        directory="/sanitized/candidate-dir",
        max_attempts=1,
        now=NOW,
        reader=_public_document_reader,
    )
    assert [row["stage"] for row in first["callback_evidence"]] == ["fetch"]

    def unavailable_reader(url):
        raise RuntimeError("PUBLIC_SOURCE_OFFLINE")

    second = bridge.run_installed_run_case(
        successful_host,
        case_id="second",
        symbol="MSFT",
        seed_urls=["https://www.microsoft.com/en-us/Investor/test"],
        directory="/sanitized/candidate-dir",
        max_attempts=1,
        now=NOW,
        reader=unavailable_reader,
    )
    assert second["status"] == "BLOCKED"
    assert "HOST_FETCH_PUBLIC_DOCUMENT_UNAVAILABLE" in second["reason"]
    assert second["callback_evidence"] == []


def test_original_run_case_private_public_document_is_rejected_before_model_transport():
    bridge = OriginalResearchCallbackBridge(routes=_routes(), coordinator=FakeCoordinator())

    def host(case_id, ticker, seed_urls, directory, fetch, generate, challenge, max_attempts):
        fetch(seed_urls[0])
        raise AssertionError("private document should not pass fetch")

    result = bridge.run_installed_run_case(
        host,
        case_id="private-negative",
        symbol="MSFT",
        seed_urls=["https://www.microsoft.com/en-us/Investor/test"],
        directory="/sanitized/candidate-dir",
        max_attempts=1,
        now=NOW,
        reader=lambda url: {"private_path": "/home/user/secret.json"},
    )
    assert result["status"] == "BLOCKED"
    assert "PUBLIC_OUTBOUND_FIELD_REJECTED" in result["reason"]
    assert result["callback_evidence"] == []


def test_original_run_case_same_model_challenge_is_blocked_using_actual_underwriting_identity():
    routes = _routes()
    routes["challenge"] = StageRoute(primary=_engine("challenge", {
        "verdict": "PASS_PUBLIC_RESEARCH_ONLY",
        "challenge_summary": "same-model challenge must be rejected",
        "blockers": [],
        "next_action": "CONFIGURE_DIFFERENT_MODEL",
    }, model="underwriter-a"))
    bridge = OriginalResearchCallbackBridge(routes=routes, coordinator=FakeCoordinator())

    def host(case_id, ticker, seed_urls, directory, fetch, generate, challenge, max_attempts):
        document = fetch(seed_urls[0])
        discovery = generate("discovery", {"documents": [document]})
        commercial = generate("commercial", {
            "documents": [document], "discovery": discovery
        })
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
        seed_urls=["https://www.microsoft.com/en-us/Investor/test"],
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
    bridge = OriginalResearchCallbackBridge(routes=_routes(), coordinator=FakeCoordinator())

    def host(case_id, ticker, seed_urls, directory, fetch, generate, challenge, max_attempts):
        document = fetch(seed_urls[0])
        challenge({"documents": [document]})
        raise AssertionError("challenge without underwriting identity must not return")

    result = bridge.run_installed_run_case(
        host,
        case_id="missing-underwriting-negative",
        symbol="MSFT",
        seed_urls=["https://www.microsoft.com/en-us/Investor/test"],
        directory="/sanitized/candidate-dir",
        max_attempts=1,
        now=NOW,
        reader=_public_document_reader,
    )
    assert result["status"] == "BLOCKED"
    assert "HOST_UNDERWRITING_MODEL_IDENTITY_REQUIRED" in result["reason"]
