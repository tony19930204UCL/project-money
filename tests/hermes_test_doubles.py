"""Hermes test doubles and fixtures for integration testing.

Strictly test-only. Every fixture and injected transport is marked fixture=True.
Never initialized in production.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
from typing import Any, Dict, Optional


class ExplicitFakeAgent:
    """Explicitly fake Hermes agent for integration testing of actual bridge path."""
    is_fixture: bool = True

    def __init__(
        self,
        provider: str = "openai-codex",
        model: str = "gpt-6-astra",
        base_url: str = "https://api.openai.com/v1",
        api_mode: str = "responses",
    ) -> None:
        self.provider = provider
        self.model = model
        self.requested_provider = provider
        self.base_url = base_url
        self._primary_runtime = {
            "provider": provider,
            "model": model,
            "api_mode": api_mode,
        }
        self.stream_delta_callback = None
        self.tool_progress_callback = None


def dummy_packet_dict() -> Dict[str, Any]:
    return {
        "case_id": "case-subprocess-bootstrap-001",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "evidence": ["fixture://provenance-test"],
        "thesis": "Test execution with fake agent fixture.",
        "selected_instrument": "2330.TW",
        "action": "BUY",
        "holding_horizon": "swing",
        "quantity": 10.0,
        "conditions": {},
        "risk_assessment": {},
        "alternatives_considered": [],
        "expiry": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
        "confidence": 0.9,
        "strategy_version": "test-v1",
        "provenance": {
            "authority": "MAIN_CIO",
            "actor_role": "CHIEF_INVESTMENT_OFFICER",
            "signer_id": "hermes-bridge-cio",
            "source": "hermes_bridge",
        },
        "is_fixture": True,
    }


def mock_injected_transport_for_test(
    message: str,
    *,
    session_id: str = "cio-market-lab",
    workspace_root: Optional[str] = None,
    timeout_seconds: int = 240,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    enforce_cio_pin: bool = False,
    extra_env: Optional[Dict[str, str]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Test transport verifying input reaches conversation and returning dynamic fixture response."""
    query_str = message if isinstance(message, str) else str(message)
    response_data = {
        "status": "decision_ready",
        "query_echo": query_str,
        "selected_instrument": "2330.TW",
        "action": "BUY",
        "confidence": 0.88,
        "thesis": f"Dynamically generated thesis for query: {query_str}",
        "case_id": "case-injected-test-001",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "evidence": ["fixture://provenance-test"],
        "holding_horizon": "swing",
        "quantity": 10.0,
        "conditions": {},
        "risk_assessment": {},
        "alternatives_considered": [],
        "expiry": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
        "strategy_version": "test-v1",
        "provenance": {
            "authority": "MAIN_CIO",
            "actor_role": "CHIEF_INVESTMENT_OFFICER",
            "signer_id": "hermes-bridge-cio",
            "source": "hermes_bridge",
        },
        "is_fixture": True,
    }
    response_text = json.dumps(response_data)
    runtime_metadata = {
        "resolved_provider": provider or "openai-codex",
        "resolved_model": model or "gpt-6-astra",
        "evidence_strength": "local-runtime",
        "is_fixture": True,
        "auth_verified": True,
        "is_success_response": True,
        "is_pre_auth_init": False,
        "exit_code": 0,
        "primary_runtime": {
            "provider": provider or "openai-codex",
            "model": model or "gpt-6-astra",
            "api_mode": "responses",
        },
    }
    return {
        "transport": "hermes_cli",
        "transport_identifier": "hermes-cli-cio-bridge",
        "status": "completed",
        "response": response_text,
        "session_id": session_id,
        "returncode": 0,
        "pinned_model": model,
        "requested_model": model,
        "requested_provider": provider,
        "runtime_metadata": runtime_metadata,
        "resolved_model": runtime_metadata["resolved_model"],
        "resolved_provider": runtime_metadata["resolved_provider"],
        "evidence_strength": "local-runtime",
        "is_fixture": True,
        "auth_verified": True,
        "is_success_response": True,
        "events": [
            {"type": "system", "subtype": "init", "model": model, "session_id": session_id},
            {"type": "runtime_metadata", "metadata": runtime_metadata},
            {"type": "result", "text": response_text, "exit_code": 0, "session_id": session_id, "is_fixture": True},
        ],
    }


def failing_injected_transport_for_test(
    message: str,
    *,
    session_id: str = "cio-market-lab",
    **kwargs: Any,
) -> Dict[str, Any]:
    """Test transport that simulates failure to verify nonzero status propagation."""
    raise RuntimeError("Hermes subprocess returned nonzero exit code 1: Simulated injected transport execution failure")
