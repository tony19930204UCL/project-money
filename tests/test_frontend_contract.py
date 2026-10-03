"""Static/API contract coverage for the CIO Market Lab V2 first-slice UI."""
from pathlib import Path

from fastapi.testclient import TestClient

from cio_market_lab.api.app import app


ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "plugin" / "ui"


def test_v2_static_assets_exist_and_are_linked():
    if not UI.exists():
        import pytest
        pytest.skip("Frontend UI assets not present in backend staging packet")
    index = (UI / "index.html").read_text(encoding="utf-8")
    css = (UI / "style.css").read_text(encoding="utf-8")
    js = (UI / "app.js").read_text(encoding="utf-8")

    assert 'href="/static/style.css"' in index
    assert 'src="/static/app.js"' in index
    for marker in ("PAPER ONLY", "Watchlist radar", "Paper order ticket", "Main CIO", "Strategy Lab", "Research"):
        assert marker in index
    for marker in ("market-strip", "command-grid", "cio-drawer", "@media(max-width:1280px)", "@media(max-width:700px)"):
        assert marker in css
    for marker in ("/api/overview", "/api/watchlists", "/api/market/bars/", "/api/paper/orders/preview", "/api/paper/orders", "/api/paper/kill-switch", "/api/markets/breadth", "/api/markets/leaders", "/api/markets/search", "/api/paper/experiments"):
        assert marker in js
    for marker in ("timeframe", "loadBars(state.selectedSymbol", "renderMarkets", "applyPreset", "setupMarketControls", "renderExperiments", "loadExperimentRuntime", "run-one-cycle"):
        assert marker in js
    assert "data-timeframe" in index
    for control in ("experiment-mode", "experiment-start", "experiment-cycle", "experiment-stop", "experiment-runtime"):
        assert f'id="{control}"' in index
    assert "No depth source connected" in index
    for marker in ("MANUAL", "STRATEGY", "MAIN_CIO"):
        assert marker in index


def test_static_routes_serve_restored_upstream_workstation():
    if not UI.exists():
        import pytest
        pytest.skip("Frontend UI assets not present in backend staging packet")
    client = TestClient(app)
    index = client.get("/")

    assert index.status_code == 200
    assert '<div id="root"></div>' in index.text
    assert "main-" in index.text
    assert ".js" in index.text
    assert ".css" in index.text



def test_frontend_contract_uses_existing_and_optional_api_surfaces():
    client = TestClient(app)
    health = client.get("/api/health")
    overview = client.get("/api/overview")
    watchlists = client.get("/api/watchlists")
    portfolios = client.get("/api/portfolios")
    strategies = client.get("/api/strategies")
    research = client.get("/api/research/inbox")

    assert health.status_code == 200
    assert health.json()["paper_only"] is True
    assert health.json()["broker_connected"] is False
    assert overview.status_code == 200
    assert set((watchlists.json() or {}).keys()) >= {"TW", "US"}
    assert portfolios.status_code == 200
    assert strategies.status_code == 200
    assert research.status_code == 200


def test_frontend_paper_ticket_contract_matches_backend_models():
    if not UI.exists():
        import pytest
        pytest.skip("Frontend UI assets not present in backend staging packet")
    js = (UI / "app.js").read_text(encoding="utf-8")

    for field in ("order_type", "limit_price", "stop_price", "explicit_user_instruction", "audit_metadata", "age_seconds", "is_fallback"):
        assert field in js
    assert "two_step_confirmation:true" in js
    assert "Submit simulated order" in js
    assert 'api("/api/paper/orders", {method:"POST"' in js


def test_frontend_does_not_claim_live_depth_or_broker_execution():
    if not UI.exists():
        import pytest
        pytest.skip("Frontend UI assets not present in backend staging packet")
    index = (UI / "index.html").read_text(encoding="utf-8").lower()
    js = (UI / "app.js").read_text(encoding="utf-8").lower()

    assert "no depth source connected" in index
    assert "five-level" not in index
    assert "websocket" not in js
    assert "broker" in index
    assert "paper_only" in js

