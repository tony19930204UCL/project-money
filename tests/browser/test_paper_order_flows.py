from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from tests.browser.conftest import PROJECT_ROOT, HELPER, run_helper_command


def _page(browser, viewport=(1280, 720)):
    context = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    return context, page, errors


def _orders(base_url: str):
    return httpx.get(base_url + "/api/paper/orders", timeout=5).json()


def _fills(base_url: str):
    return httpx.get(base_url + "/api/fills", timeout=5).json()


def _events(base_url: str):
    return httpx.get(base_url + "/api/events?since_id=0&limit=1000", timeout=5).json()["events"]


def _submit_manual(page, *, symbol="2330.TW", market="TW", qty="2", price="105", reason="TEST_ONLY manual browser order"):
    page.locator("#order-symbol").fill(symbol)
    page.locator("#order-market").select_option(market)
    page.locator("#order-side").select_option("BUY")
    page.locator("#order-type").select_option("LIMIT")
    page.locator("#order-qty").fill(qty)
    page.locator("#order-price").fill(price)
    page.locator("#order-bucket").select_option("swing")
    page.locator("#order-origin").select_option("MANUAL")
    page.locator("#order-reason").fill(reason)
    page.locator(".submit-order").click()
    page.get_by_text("Preview approved.", exact=False).wait_for(state="visible", timeout=10000)
    page.locator(".submit-order").click()
    page.get_by_text("Simulated order accepted:", exact=False).wait_for(state="visible", timeout=10000)


def test_manual_ui_submit_cancel_replace_fill_and_restart(
    chromium_browser,
    fresh_runtime,
    server_factory,
    tmp_path,
):
    control = tmp_path / "control-a"
    with server_factory(fresh_runtime, control, read_only=False) as server:
        context, page, errors = _page(chromium_browser)
        try:
            page.goto(server.base_url + "/static/index.html", wait_until="domcontentloaded")
            page.locator("#watchlist .watch-row").first.wait_for(state="visible", timeout=10000)

            _submit_manual(page, reason="TEST_ONLY pending cancel")
            page.locator(".nav-item", has_text="Paper Trade").click()
            page.locator("[data-order-cancel]").first.click()
            page.wait_for_timeout(250)
            assert any(order["status"] == "CANCELLED" for order in _orders(server.base_url))

            page.locator(".nav-item", has_text="Command Center").click()
            _submit_manual(page, qty="2", price="105", reason="TEST_ONLY pending replace")
            before_replace = [o for o in _orders(server.base_url) if o["status"] == "PENDING" and o["origin"] == "MANUAL"]
            assert before_replace
            old_id = before_replace[-1]["order_id"]

            page.locator(".nav-item", has_text="Paper Trade").click()
            page.locator(f'[data-order-replace="{old_id}"]').click()
            page.locator("#order-qty").fill("3")
            page.locator(".submit-order").click()
            page.get_by_text("Preview approved.", exact=False).wait_for(state="visible", timeout=10000)
            page.locator(".submit-order").click()
            page.get_by_text("Replacement accepted:", exact=False).wait_for(state="visible", timeout=10000)
            replaced = _orders(server.base_url)
            assert next(o for o in replaced if o["order_id"] == old_id)["status"] == "CANCELLED"
            replacement = [o for o in replaced if o["status"] == "PENDING" and o["origin"] == "MANUAL"][-1]
            replacement_id = replacement["order_id"]

            count_before_invalid = len(replaced)
            page.locator("#order-qty").fill("0")
            page.locator("#order-reason").fill("TEST_ONLY invalid quantity")
            page.locator(".submit-order").click()
            page.wait_for_timeout(250)
            assert len(_orders(server.base_url)) == count_before_invalid

            page.locator("#order-qty").fill("1")
            page.locator("#order-reason").fill("")
            page.locator(".submit-order").click()
            page.get_by_text("Reason/evidence is required", exact=False).wait_for(state="visible")
            assert len(_orders(server.base_url)) == count_before_invalid

            page.locator("#kill-toggle").click()
            page.wait_for_timeout(200)
            page.locator("#order-reason").fill("TEST_ONLY kill switch rejection")
            page.locator(".submit-order").click()
            page.get_by_text("Kill switch is ON", exact=False).wait_for(state="visible")
            assert len(_orders(server.base_url)) == count_before_invalid
            page.locator("#kill-toggle").click()

            page.locator("#order-symbol").fill("AAPL")
            page.locator("#order-market").select_option("US")
            page.locator("#order-qty").fill("1")
            page.locator("#order-price").fill("105")
            page.locator("#order-reason").fill("TEST_ONLY stale quote rejection")
            page.locator(".submit-order").click()
            page.get_by_text("Blocked:", exact=False).wait_for(state="visible", timeout=10000)
            assert len(_orders(server.base_url)) == count_before_invalid

            assert errors == []
        finally:
            context.close()

    advanced = run_helper_command("advance-manual", fresh_runtime)
    assert advanced["order_id"] == replacement_id
    assert advanced["status"] == "FILLED"
    assert advanced["fill_ids"]

    with server_factory(fresh_runtime, tmp_path / "control-b", read_only=False) as restarted:
        context, page, errors = _page(chromium_browser)
        try:
            page.goto(restarted.base_url + "/static/index.html", wait_until="domcontentloaded")
            page.locator(".nav-item", has_text="Paper Trade").click()
            page.get_by_text("FILLED", exact=True).first.wait_for(state="visible", timeout=10000)
            page.locator(".nav-item", has_text="Portfolio").click()
            assert "positions" in page.locator("#portfolio-cards").inner_text().lower()

            orders = _orders(restarted.base_url)
            fills = _fills(restarted.base_url)
            events = _events(restarted.base_url)
            assert sum(o["order_id"] == replacement_id for o in orders) == 1
            assert sum(f["order_id"] == replacement_id for f in fills) == 1
            assert sum(
                e["event"]["event_type"] == "ORDER_FILLED"
                and e["event"]["aggregate_id"] == replacement_id
                for e in events
            ) == 1
            assert errors == []
        finally:
            context.close()


def _cio_payload(case_id: str, *, expiry: datetime, provenance: dict):
    now = datetime.now(timezone.utc)
    return {
        "case_id": case_id,
        "as_of": now.isoformat(),
        "evidence": ["fixture://TEST_ONLY_BROWSER_NEGATIVE"],
        "thesis": "TEST_ONLY authority negative",
        "selected_instrument": "2330.TW",
        "action": "BUY",
        "holding_horizon": "swing",
        "quantity": 1,
        "conditions": {"strategy_id": "TEST_ONLY_browser_cio"},
        "risk_assessment": {"test_only": True},
        "alternatives_considered": [],
        "expiry": expiry.isoformat(),
        "confidence": 0.5,
        "strategy_version": "TEST_ONLY-browser-v1",
        "provenance": provenance,
        "is_fixture": True,
    }


def test_strategy_and_cio_authority_browser_readback_and_negative_gates(
    chromium_browser,
    fresh_runtime,
    server_factory,
    tmp_path,
):
    seeded = run_helper_command("seed-authorities", fresh_runtime)
    assert seeded["strategy_order_id"]
    assert seeded["cio_order_id"]
    assert seeded["cio_case_id"] == "TEST_ONLY_browser_cio_case"

    with server_factory(fresh_runtime, tmp_path / "control-auth-a", read_only=False) as server:
        context, page, errors = _page(chromium_browser, (1536, 864))
        try:
            page.goto(server.base_url + "/static/index.html", wait_until="domcontentloaded")
            page.locator(".nav-item", has_text="Paper Trade").click()
            ledger = page.locator("#paper-ledger")
            ledger.get_by_text("STRATEGY", exact=True).first.wait_for(state="visible", timeout=10000)
            ledger.get_by_text("MAIN_CIO", exact=True).first.wait_for(state="visible", timeout=10000)
            assert "TEST_ONLY-browser-v1" in ledger.inner_text()

            page.locator(".nav-item", has_text="Command Center").click()
            assert page.locator('#order-origin option[value="MAIN_CIO"]').is_disabled()

            before = _orders(server.base_url)
            before_ids = {item["order_id"] for item in before}

            forged = {
                "symbol": "2330.TW", "market": "TW", "bucket": "swing",
                "side": "BUY", "order_type": "LIMIT", "quantity": 1,
                "limit_price": 105, "origin": "MAIN_CIO",
                "reason": "TEST_ONLY forged UI authority",
                "explicit_user_instruction": True,
                "data": {
                    "source": "TEST_ONLY_BROWSER_FIXTURE",
                    "last_price": 103,
                    "age_seconds": 0,
                    "is_stale": False,
                    "is_fallback": False,
                },
            }
            result = page.evaluate("""async payload => {
                const r = await fetch('/api/paper/orders', {
                  method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)
                });
                return {status:r.status, body:await r.json()};
            }""", forged)
            assert result["status"] == 403
            assert result["body"]["detail"] == "MAIN_CIO_REQUIRES_AUTHENTICATED_DECISION_PACKET"
            assert {item["order_id"] for item in _orders(server.base_url)} == before_ids

            now = datetime.now(timezone.utc)
            negatives = [
                _cio_payload(
                    "TEST_ONLY_invalid_signature",
                    expiry=now + timedelta(hours=1),
                    provenance={
                        "authority": "MAIN_CIO",
                        "actor_role": "CHIEF_INVESTMENT_OFFICER",
                        "signer_id": "fixture-test-signer",
                        "source": "TEST_ONLY_BROWSER",
                        "signature": "invalid",
                    },
                ),
                _cio_payload(
                    "TEST_ONLY_expired_receipt",
                    expiry=now - timedelta(minutes=1),
                    provenance={
                        "authority": "MAIN_CIO",
                        "actor_role": "CHIEF_INVESTMENT_OFFICER",
                        "signer_id": "fixture-test-signer",
                        "source": "TEST_ONLY_BROWSER",
                        "receipt_id": "TEST_ONLY_EXPIRED_RECEIPT",
                    },
                ),
                _cio_payload(
                    "TEST_ONLY_worker_authority",
                    expiry=now + timedelta(hours=1),
                    provenance={
                        "authority": "MAIN_CIO",
                        "actor_role": "ENGINEERING_WORKER",
                        "signer_id": "fixture-test-signer",
                        "source": "TEST_ONLY_BROWSER",
                        "receipt_id": "TEST_ONLY_WORKER_RECEIPT",
                    },
                ),
            ]
            for payload in negatives:
                response = page.evaluate("""async payload => {
                    const r = await fetch('/api/paper/cio/decision-packets', {
                      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)
                    });
                    return {status:r.status, body:await r.json()};
                }""", payload)
                assert response["status"] in (200, 409)
                if response["status"] == 200:
                    assert response["body"]["decision"]["action"] == "NO_TRADE"
                    assert "REJECT" in response["body"]["decision"]["reason"].upper() or "EXPIRED" in response["body"]["decision"]["reason"].upper()
                assert {item["order_id"] for item in _orders(server.base_url)} == before_ids

            events_before = _events(server.base_url)
            fills_before = _fills(server.base_url)
            portfolios_before = httpx.get(server.base_url + "/api/portfolios", timeout=5).json()
            assert errors == []
        finally:
            context.close()

    with server_factory(fresh_runtime, tmp_path / "control-auth-b", read_only=False) as restarted:
        orders_after = _orders(restarted.base_url)
        events_after = _events(restarted.base_url)
        fills_after = _fills(restarted.base_url)
        portfolios_after = httpx.get(restarted.base_url + "/api/portfolios", timeout=5).json()
        assert len({item["order_id"] for item in orders_after}) == len(orders_after)
        assert len({item["fill_id"] for item in fills_after}) == len(fills_after)
        assert len({item["sequence"] for item in events_after}) == len(events_after)
        assert fills_after == fills_before
        assert portfolios_after == portfolios_before
        assert len(events_after) >= len(events_before)


def test_no_quote_stays_unavailable_without_new_order(
    chromium_browser,
    fresh_runtime,
    server_factory,
    tmp_path,
):
    with server_factory(fresh_runtime, tmp_path / "control-missing", read_only=False) as server:
        context, page, errors = _page(chromium_browser)
        try:
            page.goto(server.base_url + "/static/index.html", wait_until="domcontentloaded")
            before = len(_orders(server.base_url))
            page.locator("#order-symbol").fill("MISSING.TW")
            page.locator("#order-market").select_option("TW")
            page.locator("#order-type").select_option("MARKET")
            page.locator("#order-qty").fill("1")
            page.locator("#order-reason").fill("TEST_ONLY missing quote")
            page.locator(".submit-order").click()
            page.get_by_text("Blocked:", exact=False).wait_for(state="visible", timeout=10000)
            assert len(_orders(server.base_url)) == before
            assert errors == []
        finally:
            context.close()
