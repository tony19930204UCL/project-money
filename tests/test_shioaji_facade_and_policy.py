from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from cio_market_lab.api import shioaji_facade
from cio_market_lab.data import tw_official
from cio_market_lab.api.app import create_app
from cio_market_lab.domain.models import DecisionScope
from cio_market_lab.engine.automation_policy import (
    AutomationRiskPolicy,
    ExitAction,
    LLMPolicyBoundary,
    LLMVerdict,
)


def test_shioaji_facade_is_paper_only_and_maps_orders(tmp_path, monkeypatch):
    monkeypatch.setenv("CIO_MARKET_LAB_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("CIO_PORT", "21322")
    with TestClient(create_app(), base_url="http://127.0.0.1:21322") as client:
        health = client.get("/api/v1/health").json()
        assert health["paper_only"] is True
        assert health["broker_connected"] is False

        accounts = client.get("/api/v1/auth/accounts").json()
        assert accounts and accounts[0]["signed"] is True

        contracts = client.get("/api/v1/data/contracts", params={"keyword": "2330"})
        assert contracts.status_code == 200
        assert any(item["code"] == "2330" for item in contracts.json()["contracts"])

        placed = client.post(
            "/api/v1/order/place_order",
            json={
                "contract": {"code": "2330", "exchange": "TSE", "security_type": "STK"},
                "order": {
                    "action": "Buy",
                    "price": 100,
                    "quantity": 1,
                    "price_type": "LMT",
                    "order_type": "ROD",
                    "custom_field": "idem-2330-1",
                },
            },
        )
        assert placed.status_code == 200
        first = placed.json()
        assert first["status"]["status"] == "Submitted"

        repeated = client.post(
            "/api/v1/order/place_order",
            json={
                "contract": {"code": "2330", "exchange": "TSE", "security_type": "STK"},
                "order": {
                    "action": "Buy",
                    "price": 100,
                    "quantity": 1,
                    "price_type": "LMT",
                    "order_type": "ROD",
                    "custom_field": "idem-2330-1",
                },
            },
        )
        assert repeated.json()["order"]["id"] == first["order"]["id"]
        assert len(client.post("/api/v1/order/trades", json={}).json()) == 1

        cancelled = client.post("/api/v1/order/cancel_order", json={"trade": first})
        assert cancelled.status_code == 200
        assert cancelled.json()["status"]["status"] == "Cancelled"

        positions = client.post("/api/v1/portfolio/position_unit", json={}).json()
        assert isinstance(positions, list)
        account_balance = client.post("/api/v1/portfolio/account_balance", json={})
        assert account_balance.status_code == 200
        canonical = client.app.state.app_state.runner.get_canonical_team_ops()
        assert account_balance.json()["acc_balance"] == canonical["portfolio"]["cash"]
        assert account_balance.json()["acc_balance"] == canonical["portfolio"]["initial_cash"]
        assert account_balance.json()["reporting_currency"] == "TWD"
        margin = client.post("/api/v1/portfolio/margin", json={})
        assert margin.status_code == 200
        margin_body = margin.json()
        assert margin_body["available_margin"] == canonical["portfolio"]["cash"]
        assert margin_body["nav_status"] in {"OK", "NAV_UNAVAILABLE"}
        if margin_body["nav_status"] == "NAV_UNAVAILABLE":
            assert margin_body["equity"] is None
            assert margin_body["nav_reason"]

        policy = client.get("/api/paper/automation/policy").json()
        assert policy["paper_only"] is True
        assert policy["broker_connected"] is False
        assert policy["intraday"]["llm_authority"] == "veto_only"
        assert policy["swing"]["may_place_order"] is False


def test_order_mutation_endpoints_reject_missing_or_invalid_fields_without_500(tmp_path, monkeypatch):
    monkeypatch.setenv("CIO_MARKET_LAB_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("CIO_PORT", "21322")
    with TestClient(create_app(), base_url="http://127.0.0.1:21322") as client:
        cases = [
            ("/api/v1/order/cancel_order", {}, "trade_id is required"),
            ("/api/v1/order/update_price", {}, "trade_id is required"),
            ("/api/v1/order/update_price", {"trade_id": "missing"}, "price is required"),
            ("/api/v1/order/update_price", {"trade_id": "missing", "price": "bad"}, "price must be numeric"),
            ("/api/v1/order/update_price", {"trade_id": "missing", "price": 0}, "price must be greater than zero"),
            ("/api/v1/order/update_qty", {}, "trade_id is required"),
            ("/api/v1/order/update_qty", {"trade_id": "missing"}, "quantity is required"),
            ("/api/v1/order/update_qty", {"trade_id": "missing", "quantity": "bad"}, "quantity must be numeric"),
            ("/api/v1/order/update_qty", {"trade_id": "missing", "quantity": 0}, "quantity must be greater than zero"),
        ]
        for path, payload, detail in cases:
            response = client.post(path, json=payload)
            assert response.status_code == 422, (path, payload, response.text)
            assert response.json()["detail"] == detail


def test_facade_read_only_fallback_endpoints_do_not_claim_broker_data(tmp_path, monkeypatch):
    monkeypatch.setenv("CIO_MARKET_LAB_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("CIO_PORT", "21322")
    # Hermetic upstream outage, including empty caches; scanner cannot touch
    # a live market adapter and the index must disclose missing official data.
    monkeypatch.setattr(shioaji_facade, "_stock_contracts", lambda: [])
    monkeypatch.setattr(tw_official, "_QUOTE_CACHE", {"expires_at": 0.0, "rows": {}, "error": None})
    monkeypatch.setattr(tw_official, "_PROFILE_CACHE", {"expires_at": 0.0, "rows": {}, "error": None})
    def unavailable(*args, **kwargs):
        raise ConnectionError("fixture upstream unavailable")
    monkeypatch.setattr(tw_official, "_fetch_json", unavailable)
    with TestClient(create_app(), base_url="http://127.0.0.1:21322") as client:
        for method, path, payload in [
            ("get", "/api/v1/data/scanner", None),
            ("post", "/api/v1/data/credit_enquire", {}),
            ("post", "/api/v1/data/short_stock_sources", {}),
            ("get", "/api/v1/data/index_components", None),
            ("get", "/api/v1/data/regulatory_punish", None),
            ("post", "/api/v1/order/trade_cache_health", {"refresh": False}),
            ("post", "/api/v1/monitor/quota", {}),
            ("post", "/api/v1/monitor/usage", {}),
        ]:
            response = getattr(client, method)(path, json=payload) if payload is not None else getattr(client, method)(path)
            assert response.status_code == 200, path
            if path == "/api/v1/data/index_components":
                assert response.json()["refresh_state"] == "unavailable"
                assert response.json()["reference_date"] is None
                assert response.json()["info"] == []

        rejected = client.post("/api/v1/order/reserve_stock", json={})
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["broker_connected"] is False


def test_combo_and_capability_routes_remain_paper_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CIO_MARKET_LAB_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("CIO_PORT", "21322")
    shioaji_facade._TAIFEX_CACHE.update({
        "expires_at": float("inf"),
        "rows": [
            {"Date": "20260923", "Contract": "TX", "ContractMonth(Week)": "202610", "Open": "28000", "High": "28100", "Low": "27900", "Last": "28050", "Change": "50", "%": "0.18%", "Volume": "100", "BestBid": "28049", "BestAsk": "28051", "TradingSession": "一般"},
            {"Date": "20260923", "Contract": "TX", "ContractMonth(Week)": "202611", "Open": "28020", "High": "28120", "Low": "27920", "Last": "28070", "Change": "45", "%": "0.16%", "Volume": "80", "BestBid": "28069", "BestAsk": "28071", "TradingSession": "一般"},
        ],
    })
    with TestClient(create_app(), base_url="http://127.0.0.1:21322") as client:
        combos = client.get("/api/v1/data/contracts/combo/futures", params={"root": "TXF"})
        assert combos.status_code == 200
        combo = combos.json()[0]
        assert combo["managed"] is True
        assert combo["combo_type"] == "TimeSpread"

        built = client.post("/api/v1/data/contracts/combo", json={"legs": combo["legs"]})
        assert built.status_code == 200
        assert built.json()["code"] == combo["code"]

        snapshots = client.post("/api/v1/data/snapshots", json={"contracts": [combo]})
        assert snapshots.status_code == 200
        assert snapshots.json()[0]["source"] == "paper_combo_derived"
        assert snapshots.json()[0]["close"] == -20

        placed = client.post(
            "/api/v1/order/place_comboorder",
            json={
                "combo_contract": combo,
                "order": {"action": "Buy", "price": 10, "quantity": 1, "price_type": "LMT", "order_type": "ROD"},
            },
        )
        assert placed.status_code == 200
        trade = placed.json()
        assert trade["paper_only"] is True
        assert trade["broker_connected"] is False
        assert trade["status"]["status"] == "Submitted"
        assert len(client.post("/api/v1/order/combotrades", json={}).json()) == 1

        cancelled = client.post("/api/v1/order/cancel_comboorder", json={"trade_id": trade["order"]["id"]})
        assert cancelled.status_code == 200
        assert cancelled.json()["status"]["status"] == "Cancelled"

        subscribed = client.post("/api/v1/stream/subscribe/calculated_index", json={"code": "IX0001"})
        assert subscribed.status_code == 200
        assert subscribed.json()["paper_only"] is True
        assert subscribed.json()["broker_connected"] is False
        assert client.post("/api/v1/stream/unsubscribe/calculated_index", json={"code": "IX0001"}).status_code == 200


def test_deterministic_exit_policy_and_llm_authority_boundary():
    policy = AutomationRiskPolicy()
    stop = policy.evaluate(
        symbol="2330.TW",
        bucket=DecisionScope.SWING,
        quantity=10,
        average_entry_price=100,
        current_price=95,
        now=datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc),
    )
    assert stop.action == ExitAction.EXIT
    assert stop.reason == "STOP_LOSS"

    forced_flat = policy.evaluate(
        symbol="2330.TW",
        bucket=DecisionScope.INTRADAY,
        quantity=10,
        average_entry_price=100,
        current_price=100,
        now=datetime(2026, 9, 24, 5, 26, tzinfo=timezone.utc),
    )
    assert forced_flat.action == ExitAction.FORCE_FLATTEN
    assert forced_flat.reason == "INTRADAY_SESSION_FLATTEN"

    unavailable = LLMPolicyBoundary().review(DecisionScope.INTRADAY, {"quantity": 10})
    assert unavailable.verdict == LLMVerdict.VETO
    assert unavailable.may_change_quantity is False
    assert unavailable.may_place_order is False

    review = LLMPolicyBoundary(lambda ctx: {"verdict": "ALLOW", "quantity": 999, "price": 1, "reason": "context clear"}).review(
        DecisionScope.SWING, {"quantity": 10}
    )
    assert review.verdict == LLMVerdict.REVIEW_ONLY
    assert "quantity" not in review.model_output
    assert "price" not in review.model_output
