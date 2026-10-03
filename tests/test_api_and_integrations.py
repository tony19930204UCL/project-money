"""Tests for CIO Market Lab API, integrations, and Hermes plugin registration.

Uses FastAPI TestClient and monkeypatch to guarantee zero live Jev, Hermes, or Chrome dependencies.
Enforces simulation-only and paper-only invariants.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any, Dict
from fastapi.testclient import TestClient
import pytest

from cio_market_lab.api.app import app, create_app
from cio_market_lab.integrations.hermes_chat import (
    PAPER_DISCLAIMER,
    HermesChatDraft,
    build_chat_draft,
)
from cio_market_lab.integrations.jev import (
    CandidateAction,
    JevChoiceRequest,
    JevChoiceResponse,
    JevDecisionProvider,
)
from cio_market_lab.research.browser import (
    BrowserResearchAdapter,
    FakeBrowserResearchAdapter,
    ResearchItem,
)
try:
    from plugin.register import check_server_health, load_manifest, register
except ImportError:
    check_server_health = None
    load_manifest = None
    register = None



@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("CIO_PORT", "21322")
    return TestClient(app, base_url="http://127.0.0.1:21322")


# --- 1. API Endpoints Tests ---

def test_api_health_endpoint(client: TestClient):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["mode"] == "simulation_only"
    assert data["paper_only"] is True
    assert data["broker_connected"] is False
    assert data["autonomous_capital_decisions"] is False
    assert data["autonomous_live_capital_decisions"] is False
    assert data["autonomous_paper_execution"] is True
    assert data["capabilities"]["derivative_trading_supported"] is False
    assert data["capabilities"]["option_scope"] == "UNAVAILABLE_IMPLEMENTED_COMPONENTS_NOT_ACTIVATED"
    assert data["capabilities"]["intraday_paper_trading_supported"] is True
    assert "LONG_PREMIUM_OPTION" not in data["capabilities"]["supported_instruments"]
    assert "LONG_PREMIUM_OPTIONS" in data["capabilities"]["unsupported_capabilities"]
    assert "UNCOVERED_SHORT_OPTIONS" in data["capabilities"]["unsupported_capabilities"]
    assert "simulation-only mode" in data["disclaimer"]


def test_project_money_mandate_is_truthful_and_bounded(client: TestClient):
    resp = client.get("/api/paper/mandate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ACTIVE_PAPER_EXPERIMENT"
    assert data["client_role"] == "OBSERVER_ONLY"
    assert data["decision_owner"] == "MAIN_CIO"
    assert data["evaluation_window"]["ends_on"] == "2026-10-26"
    assert data["execution_scope"]["paper_only"] is True
    assert data["execution_scope"]["real_money_permitted"] is False
    assert "LONG_PREMIUM_OPTION" in data["execution_scope"]["supported_instruments"]
    assert "UNCOVERED_SHORT_OPTIONS" in data["execution_scope"]["unsupported_capabilities"]
    assert data["live_promotion"]["automatic"] is False
    assert data["live_promotion"]["requires_new_client_authorization"] is True


def test_api_overview_endpoint(client: TestClient):
    resp = client.get("/api/overview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "simulation_only"
    assert "TW" in data["market_regimes"]
    assert "US" in data["market_regimes"]
    for bucket in ("swing", "intraday"):
        summary = data["portfolios_summary"][bucket]
        if summary["equity"] is None:
            assert summary["nav_status"] == "NAV_UNAVAILABLE"
            assert summary["nav_reason"]
        else:
            assert summary["equity"] >= 100_000.0
            assert summary["nav_status"] == "OK"
            assert summary["nav_reason"] is None
    assert data["strategies_summary"]["total"] >= 1


def test_api_watchlists_endpoint(client: TestClient):
    resp = client.get("/api/watchlists")
    assert resp.status_code == 200
    data = resp.json()
    assert "TW" in data and len(data["TW"]) > 0
    assert "US" in data and len(data["US"]) > 0
    symbols_tw = [x["symbol"] for x in data["TW"]]
    assert "2330.TW" in symbols_tw


def test_api_strategies_and_activation_lifecycle(client: TestClient):
    # List strategies
    resp = client.get("/api/strategies")
    assert resp.status_code == 200
    strats = resp.json()
    assert len(strats) >= 1

    first_id = strats[0]["id"]

    # Pause strategy
    p_resp = client.post(f"/api/strategies/{first_id}/pause", json={"authority": "Main CIO"})
    assert p_resp.status_code == 200
    assert p_resp.json()["strategy"]["status"] == "PAUSED"

    # Activate strategy
    a_resp = client.post(f"/api/strategies/{first_id}/activate", json={"authority": "Main CIO"})
    assert a_resp.status_code == 200
    assert a_resp.json()["strategy"]["status"] == "PAPER_ACTIVE"


def test_api_portfolios_endpoint(client: TestClient):
    resp = client.get("/api/portfolios")
    assert resp.status_code == 200
    data = resp.json()
    assert data["paper_only"] is True
    assert "swing" in data
    assert "intraday" in data
    assert "assumptions" in data
    assert data["assumptions"]["reject_stale_data"] is True


def test_api_signals_and_fills(client: TestClient):
    sig_resp = client.get("/api/signals")
    assert sig_resp.status_code == 200
    sigs = sig_resp.json()
    assert isinstance(sigs, list)

    fills_resp = client.get("/api/fills")
    assert fills_resp.status_code == 200
    fills = fills_resp.json()
    assert isinstance(fills, list)


def test_api_diagnostics_safety_invariants(client: TestClient):
    resp = client.get("/api/diagnostics")
    assert resp.status_code == 200
    data = resp.json()
    invariants = data["safety_invariants"]
    assert invariants["broker_integration_permitted"] is False
    assert invariants["broker_credentials_configured"] is False
    assert invariants["autonomous_capital_decision_authority"] is False
    assert invariants["autonomous_paper_execution_enabled"] is True
    assert invariants["derivative_trading_permitted"] is False
    assert invariants["option_scope"] == "UNAVAILABLE_IMPLEMENTED_COMPONENTS_NOT_ACTIVATED"
    assert data["derivative_capabilities"]["capabilities"]["LONG_PREMIUM_OPTIONS"]["status"] == "UNAVAILABLE"
    assert "LONG_PREMIUM_OPTION" not in invariants["supported_instruments"]
    assert invariants["ledgers_isolated"] is True


def test_api_research_inbox_and_intake(client: TestClient):
    # Get initial inbox
    resp = client.get("/api/research/inbox")
    assert resp.status_code == 200
    items = resp.json()
    initial_count = len(items)

    # Post new research item
    intake_payload = {
        "url": "https://mops.twse.com.tw/sample_notice",
        "title": "TWSE Sample Notice for Testing",
        "claims": ["Capex guidance affirmed", "Fab expansion on schedule"],
        "related_symbols": ["2330.TW"],
        "hypothesis": "Continued expansion supports multi-month swing thesis",
    }
    post_resp = client.post("/api/research/intake", json=intake_payload)
    assert post_resp.status_code == 200
    created = post_resp.json()["item"]
    assert created["title"] == "TWSE Sample Notice for Testing"

    # Verify item is in inbox
    resp2 = client.get("/api/research/inbox")
    items2 = resp2.json()
    assert len(items2) == initial_count + 1


def test_api_ui_static_index(client: TestClient):
    root = Path(__file__).resolve().parents[1]
    ui_dir = root / "vendor" / "shioaji-pro-app" / "dist"
    legacy_ui_dir = root / "plugin" / "ui"
    if not ui_dir.exists() and not legacy_ui_dir.exists():
        pytest.skip("Frontend UI assets not present in backend staging packet")
    resp = client.get("/")
    assert resp.status_code == 200
    assert '<div id="root"></div>' in resp.text
    assert "main-" in resp.text



# --- 2. Jev Integration Tests ---

def test_jev_choice_request_validation():
    # Valid request
    req = JevChoiceRequest(
        schema="jev.action_choice_request_v1",
        goal="Select optimal entry checkpoint for 2330.TW",
        candidates=[
            CandidateAction(id="next_open", description="Enter on next open"),
            CandidateAction(id="pullback_limit", description="Enter on first pullback"),
            CandidateAction(id="reobserve", description="Wait for more bar data"),
            CandidateAction(id="abstain", description="Do not enter"),
        ],
    )
    assert len(req.candidates) == 4

    # Invalid: missing 'reobserve' or 'abstain'
    with pytest.raises(ValueError, match="reobserve"):
        JevChoiceRequest(
            schema="jev.action_choice_request_v1",
            goal="Test invalid candidates",
            candidates=[
                CandidateAction(id="opt1", description="Option 1"),
                CandidateAction(id="opt2", description="Option 2"),
            ],
        )

    # Invalid: candidate ID format
    with pytest.raises(ValueError):
        CandidateAction(id="invalid space id", description="Bad id")


def test_jev_provider_choose_success(monkeypatch: pytest.MonkeyPatch):
    provider = JevDecisionProvider()

    def mock_subprocess_run(cmd, input, stdout, stderr, timeout, check):
        out = {
            "schema": "jev.action_choice_v1",
            "selected_id": "next_open",
            "confidence": 0.88,
            "reason": "Clear breakout acceptance",
            "observation_id": "obs-001",
            "probabilities": {"next_open": 0.88, "reobserve": 0.12},
        }
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps(out).encode("utf-8"),
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", mock_subprocess_run)

    req = JevChoiceRequest(
        goal="Choose entry model",
        observation_id="obs-001",
        candidates=[
            CandidateAction(id="next_open", description="Enter next open"),
            CandidateAction(id="reobserve", description="Wait and observe"),
            CandidateAction(id="abstain", description="Abstain completely"),
        ],
    )
    resp = provider.choose(req)
    assert resp.selected_id == "next_open"
    assert resp.confidence == 0.88
    assert resp.is_fallback is False


def test_jev_provider_enforces_allowlist(monkeypatch: pytest.MonkeyPatch):
    """If Jev returns an action NOT in the prevalidated candidates list, fallback must trigger."""
    provider = JevDecisionProvider()

    def mock_subprocess_run(cmd, input, stdout, stderr, timeout, check):
        # Returned action is hallucinated or unallowed
        out = {
            "schema": "jev.action_choice_v1",
            "selected_id": "unallowed_hack_action",
            "confidence": 0.95,
            "reason": "Unsafe action",
        }
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps(out).encode("utf-8"),
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", mock_subprocess_run)

    req = JevChoiceRequest(
        goal="Choose entry model",
        candidates=[
            CandidateAction(id="reobserve", description="Wait and observe"),
            CandidateAction(id="abstain", description="Abstain"),
        ],
    )
    resp = provider.choose(req)
    assert resp.is_fallback is True
    assert resp.selected_id == "reobserve"
    assert "not in prevalidated allowlist" in resp.reason


def test_jev_provider_handles_timeout(monkeypatch: pytest.MonkeyPatch):
    provider = JevDecisionProvider()

    def mock_subprocess_run(cmd, input, stdout, stderr, timeout, check):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", mock_subprocess_run)

    req = JevChoiceRequest(
        goal="Test timeout",
        candidates=[
            CandidateAction(id="action1", description="Action 1"),
            CandidateAction(id="reobserve", description="Reobserve"),
            CandidateAction(id="abstain", description="Abstain"),
        ],
    )
    resp = provider.choose(req, timeout=1.0)
    assert resp.is_fallback is True
    assert resp.selected_id == "reobserve"
    assert "Timeout expired" in resp.reason


def test_jev_api_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    def mock_subprocess_run(cmd, input, stdout, stderr, timeout, check):
        out = {
            "schema": "jev.action_choice_v1",
            "selected_id": "reobserve",
            "confidence": 0.75,
            "reason": "Wait for next bar close",
        }
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps(out).encode("utf-8"),
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", mock_subprocess_run)

    req_payload = {
        "schema": "jev.action_choice_request_v1",
        "goal": "Test API Jev endpoint",
        "candidates": [
            {"id": "reobserve", "description": "Observe again"},
            {"id": "abstain", "description": "Do nothing"},
        ],
    }
    resp = client.post("/api/integrations/jev/choose", json=req_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["selected_id"] == "reobserve"
    assert data["confidence"] == 0.75


# --- 3. Hermes Chat Bridge Tests ---

def test_hermes_chat_draft_structure():
    draft = build_chat_draft(
        symbol="2330.TW",
        user_prompt="Analyze compression and breakout parameters",
        strategy_id="volatility_contraction",
        strategy_version="1.0.0",
        visible_metrics={"Compression": "0.62", "RVOL": "1.45"},
        evidence_paths=["cio_market_lab/events/store.py"],
    )

    assert isinstance(draft, HermesChatDraft)
    # Hard invariant: never auto-sends
    assert draft.auto_send is False
    assert draft.symbol == "2330.TW"
    assert "2330.TW" in draft.formatted_message
    assert "DISCLAIMER" in draft.disclaimer
    assert PAPER_DISCLAIMER in draft.formatted_message

    dict_repr = draft.to_inspectable_dict()
    assert dict_repr["auto_send"] is False
    assert dict_repr["symbol"] == "2330.TW"


def test_api_hermes_chat_draft_endpoint(client: TestClient):
    payload = {
        "symbol": "AAPL",
        "user_prompt": "Evaluate opening range breakout quality",
        "strategy_id": "opening_range_breakout",
        "strategy_version": "1.0.0",
        "visible_metrics": {"RVOL": 1.8},
        "evidence_paths": ["tests/fixtures/synthetic.parquet"],
    }
    resp = client.post("/api/integrations/hermes/chat-draft", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["auto_send"] is False
    assert data["symbol"] == "AAPL"
    assert "CIO Market Lab Research Query: AAPL" in data["formatted_message"]
    assert data["visible_metrics"]["RVOL"] == 1.8


# --- 4. Browser Research Adapter Tests ---

def test_browser_research_adapter_protocol():
    adapter = FakeBrowserResearchAdapter()
    assert isinstance(adapter, BrowserResearchAdapter)

    inbox = adapter.list_inbox()
    assert len(inbox) >= 2

    # Intake a new item
    new_item = ResearchItem(
        url="https://example.com/research",
        title="Test Research Title",
        claims=["Claim A", "Claim B"],
        related_symbols=["AAPL"],
    )
    adapter.intake_item(new_item)
    assert any(i.id == new_item.id for i in adapter.list_inbox())

    # Fetch page with allowlist enforcement
    fetched = adapter.fetch_page("https://mops.twse.com.tw/doc", allowlist=["twse.com.tw"])
    assert fetched.url == "https://mops.twse.com.tw/doc"

    with pytest.raises(PermissionError):
        adapter.fetch_page("https://malicious.example.com", allowlist=["twse.com.tw"])


# --- 5. Plugin Manifest & Register Tests ---

def test_plugin_manifest_validation():
    if load_manifest is None:
        pytest.skip("plugin.register not available in backend staging packet")
    manifest = load_manifest()
    assert manifest["id"] == "cio-market-lab"
    assert manifest["plugin_type"] == "server_plugin"
    assert manifest["server"]["port"] == 8765
    assert manifest["safety"]["simulation_only"] is True
    assert manifest["safety"]["broker_credentials"] is False
    assert manifest["safety"]["autonomous_capital_decisions"] is False


def test_plugin_register_function(monkeypatch: pytest.MonkeyPatch):
    if register is None:
        pytest.skip("plugin.register not available in backend staging packet")
    # Mock check_server_health to avoid network requirement during unit testing
    monkeypatch.setattr(
        "plugin.register.check_server_health",
        lambda host, port: {"online": True, "data": {"status": "ok"}},
    )

    reg_info = register()
    assert reg_info["status"] == "registered"
    assert reg_info["plugin_id"] == "cio-market-lab"
    assert reg_info["server_status"] == "online"
    assert reg_info["safety"]["simulation_only"] is True

