from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cio_market_lab.api.app import create_app
from cio_market_lab.domain.models import DecisionScope, Market
from cio_market_lab.engine.paper_orders import PaperExperimentSettings


ROOT = Path(__file__).resolve().parents[1]


def _payload(**overrides):
    payload = {
        "symbol": "AAPL",
        "market": "US",
        "bucket": "swing",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 10,
        "limit_price": 100,
        "origin": "MANUAL",
        "strategy_id": "native-usd-manual-fixture",
        "reason": "manual paper risk test",
        "audit_metadata": {"ticket_id": "test-ticket"},
        "data": {
            "source": "local_delayed_fixture",
            "age_seconds": 15,
            "last_price": 100,
            "is_stale": False,
            "is_fallback": False,
        },
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    # Persistent experiment state is a production feature. Each unit test must
    # use an isolated runtime root rather than inheriting a previous live run.
    monkeypatch.setenv("CIO_PORT", "21322")
    app = create_app(tmp_path)
    app.state.app_state.runner.configure(PaperExperimentSettings(
        strategy_id="native-usd-manual-fixture", enabled=True,
        base_currency="USD", market=Market.US, universe=["AAPL", "MSFT"],
        initial_cash=300_000, allowed_buckets=[DecisionScope.SWING, DecisionScope.INTRADAY],
    ))  # TEST_ONLY native USD paper funding, not TWD relabelled as USD
    return TestClient(app, base_url="http://127.0.0.1:21322")


def test_preview_and_submit_record_origin_reason_and_audit(client: TestClient):
    preview = client.post("/api/paper/orders/preview", json=_payload())
    assert preview.status_code == 200
    assert preview.json()["status"] == "APPROVED"
    assert preview.json()["risk_decision"]["data_status"] == "FRESH_NON_FALLBACK"

    submitted = client.post("/api/paper/orders", json=_payload())
    assert submitted.status_code == 200
    order = submitted.json()["order"]
    assert order["origin"] == "MANUAL"
    assert order["reason"] == "manual paper risk test"
    assert order["audit_metadata"]["ticket_id"] == "test-ticket"
    assert order["audit_metadata"]["data_source"] == "local_delayed_fixture"
    assert submitted.json()["paper_only"] is True


def test_stale_and_fallback_data_are_rejected(client: TestClient):
    stale = _payload(data={"source": "yahoo", "age_seconds": 301, "last_price": 100})
    response = client.post("/api/paper/orders", json=stale)
    assert response.status_code == 409
    assert "REJECTED_STALE_OR_FALLBACK" in response.json()["detail"]

    fallback = _payload(data={"source": "synthetic_replay", "age_seconds": 0, "last_price": 100, "is_fallback": True})
    response = client.post("/api/paper/orders", json=fallback)
    assert response.status_code == 409
    assert "REJECTED_STALE_OR_FALLBACK" in response.json()["detail"]


def test_kill_switch_blocks_new_orders_but_preserves_paper_boundary(client: TestClient):
    response = client.post("/api/paper/kill-switch", json={"enabled": True, "reason": "test halt"})
    assert response.status_code == 200
    assert response.json()["enabled"] is True
    blocked = client.post("/api/paper/orders", json=_payload())
    assert blocked.status_code == 409
    assert "KILL_SWITCH_ENABLED" in blocked.json()["detail"]
    assert blocked.json().get("broker_connected", False) is False

    client.post("/api/paper/kill-switch", json={"enabled": False, "reason": "resume paper tests"})
    assert client.post("/api/paper/orders", json=_payload()).status_code == 200


def test_strategy_experiment_defaults_off_then_requires_bounded_enabled_config(client: TestClient):
    default = client.get("/api/paper/experiments/orb").json()
    assert default["enabled"] is False
    assert default["strategy_id"] == "orb"

    strategy_payload = _payload(origin="STRATEGY", strategy_id="orb", strategy_version="1.2.3")
    blocked = client.post("/api/paper/orders", json=strategy_payload)
    assert blocked.status_code == 409
    assert "STRATEGY_AUTONOMOUS_PAPER_DISABLED" in blocked.json()["detail"]

    config = {
        "strategy_id": "orb",
        "base_currency": "USD",
        "enabled": True,
        "universe": ["AAPL"],
        "max_position_notional": 2_000,
        "max_daily_loss": 100,
        "max_open_positions": 2,
        "allowed_buckets": ["swing"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    }
    assert client.put("/api/paper/experiments/orb", json=config).status_code == 200
    accepted = client.post("/api/paper/orders", json=strategy_payload)
    assert accepted.status_code == 200
    assert accepted.json()["order"]["origin"] == "STRATEGY"


def test_expired_strategy_experiment_is_rejected(client: TestClient):
    config = {
        "strategy_id": "expired",
        "enabled": True,
        "universe": ["AAPL"],
        "max_position_notional": 2_000,
        "max_daily_loss": 100,
        "max_open_positions": 2,
        "allowed_buckets": ["swing"],
        "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    }
    client.put("/api/paper/experiments/expired", json=config)
    response = client.post("/api/paper/orders", json=_payload(origin="STRATEGY", strategy_id="expired"))
    assert response.status_code == 409
    assert "STRATEGY_EXPERIMENT_EXPIRED" in response.json()["detail"]


def test_main_cio_requires_explicit_user_instruction(client: TestClient):
    response = client.post("/api/paper/orders", json=_payload(origin="MAIN_CIO"))
    assert response.status_code == 409
    assert "MAIN_CIO_EXPLICIT_INSTRUCTION_REQUIRED" in response.json()["detail"]

    payload = _payload(origin="MAIN_CIO", explicit_user_instruction=True, reason="explicit user instruction for paper simulation")
    response = client.post("/api/paper/orders", json=payload)
    assert response.status_code == 200
    assert response.json()["order"]["origin"] == "MAIN_CIO"


def test_cancel_replace_and_risk_limit_are_local_and_audited(client: TestClient):
    order = client.post("/api/paper/orders", json=_payload()).json()["order"]
    replacement = _payload(quantity=5, limit_price=99, reason="replace stale paper ticket")
    response = client.post(f"/api/paper/orders/{order['order_id']}/cancel-replace", json=replacement)
    assert response.status_code == 200
    assert response.json()["status"] == "replaced"
    orders = client.get("/api/paper/orders").json()
    old = next(item for item in orders if item["order_id"] == order["order_id"])
    new = next(item for item in orders if item["order_id"] == response.json()["order"]["order_id"])
    assert old["status"] == "CANCELLED"
    assert new["audit_metadata"]["replaced_order_id"] == order["order_id"]

    limits = client.put("/api/paper/risk-limits", json={"max_order_notional": 50, "max_position_notional": 1000, "max_daily_loss": 10, "max_strategy_loss": 10, "max_open_positions": 2, "stale_data_threshold_seconds": 30})
    assert limits.status_code == 200
    blocked = client.post("/api/paper/orders", json=_payload(quantity=1, limit_price=100))
    assert blocked.status_code == 409
    assert "MAX_ORDER_NOTIONAL_EXCEEDED" in blocked.json()["detail"]


def test_swing_and_intraday_order_ledgers_remain_separate(client: TestClient):
    swing = client.post("/api/paper/orders", json=_payload(bucket="swing")).json()["order"]
    intraday = client.post("/api/paper/orders", json=_payload(bucket="intraday", symbol="MSFT")).json()["order"]
    # USD tickets stay on the USD strategy book, not the global TWD book.
    aggregate = client.get("/api/portfolios").json()
    assert aggregate["swing"]["orders"] == []
    assert aggregate["intraday"]["orders"] == []
    pm = client.app.state.app_state.portfolio_manager
    portfolios = {
        bucket: pm.get_strategy_portfolio("native-usd-manual-fixture", bucket).model_dump(mode="json")
        for bucket in ("swing", "intraday")
    }
    assert portfolios["swing"]["currency"] == portfolios["intraday"]["currency"] == "USD"
    assert len(portfolios["swing"]["orders"]) == 1
    assert len(portfolios["intraday"]["orders"]) == 1
    assert portfolios["swing"]["orders"][0]["order_id"] == swing["order_id"]
    assert portfolios["intraday"]["orders"][0]["order_id"] == intraday["order_id"]
    assert portfolios["swing"]["orders"][0]["bucket"] == "swing"
    assert portfolios["intraday"]["orders"][0]["bucket"] == "intraday"


def test_long_premium_option_cannot_bypass_unavailable_gate(client: TestClient):
    expiry = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    payload = _payload(
        symbol="AAPL_20261027_C_200",
        instrument_type="OPTION",
        side="BUY",
        quantity=2,
        limit_price=3.5,
        underlying_symbol="AAPL",
        option_right="CALL",
        strike=200,
        expiry=expiry,
        contract_multiplier=100,
        reason="bounded long-premium option simulation",
        data={
            "source": "local_option_fixture",
            "age_seconds": 5,
            "last_price": 3.5,
            "is_stale": False,
            "is_fallback": False,
        },
    )
    preview = client.post("/api/paper/orders/preview", json=payload)
    assert preview.status_code == 200
    assert preview.json()["status"] == "REJECTED"
    assert preview.json()["risk_decision"]["notional"] == 700
    assert "OPTIONS_UNAVAILABLE_PENDING_ADAPTER_ACCEPTANCE" in preview.json()["risk_decision"]["reasons"]

    submitted = client.post("/api/paper/orders", json=payload)
    assert submitted.status_code == 409
    assert "OPTIONS_UNAVAILABLE_PENDING_ADAPTER_ACCEPTANCE" in submitted.json()["detail"]


def test_option_scope_rejects_futures_expired_contracts_and_uncovered_short(client: TestClient):
    future = client.post(
        "/api/paper/orders",
        json=_payload(instrument_type="FUTURE", symbol="ESZ6"),
    )
    assert future.status_code == 409
    assert "FUTURES_NOT_PERMITTED" in future.json()["detail"]

    expired = client.post(
        "/api/paper/orders",
        json=_payload(
            symbol="AAPL_EXPIRED_C_200",
            instrument_type="OPTION",
            underlying_symbol="AAPL",
            option_right="CALL",
            strike=200,
            expiry=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            contract_multiplier=100,
        ),
    )
    assert expired.status_code == 409
    assert "OPTION_EXPIRED" in expired.json()["detail"]

    uncovered = client.post(
        "/api/paper/orders",
        json=_payload(
            symbol="AAPL_20261027_P_150",
            instrument_type="OPTION",
            side="SELL",
            quantity=1,
            underlying_symbol="AAPL",
            option_right="PUT",
            strike=150,
            expiry=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
            contract_multiplier=100,
        ),
    )
    assert uncovered.status_code == 409
    assert "UNCOVERED_SHORT_OPTION_FORBIDDEN" in uncovered.json()["detail"]
