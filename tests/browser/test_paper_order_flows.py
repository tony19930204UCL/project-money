from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest


def _recorders(page):
    console_errors: list[dict] = []
    page_errors: list[str] = []
    api_trace: list[dict] = []

    def on_console(msg):
        if msg.type == "error":
            location = msg.location or {}
            console_errors.append({
                "text": msg.text,
                "url": location.get("url", ""),
                "line_number": location.get("lineNumber"),
            })

    page.on("console", on_console)
    page.on("pageerror", lambda exc: page_errors.append(getattr(exc, "stack", None) or str(exc)))
    page.on(
        "response",
        lambda response: api_trace.append({
            "method": response.request.method,
            "url": response.url,
            "status": response.status,
        }) if "/api/" in response.url else None,
    )
    return console_errors, page_errors, api_trace


def _write_evidence(page, evidence_dir: Path, stem: str, payload: dict) -> None:
    page.screenshot(path=str(evidence_dir / f"{stem}.png"), full_page=True)
    (evidence_dir / f"{stem}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _open_paper_trade(page, base_url: str):
    response = page.goto(base_url, wait_until="domcontentloaded", timeout=15_000)
    assert response is not None and response.ok
    page.get_by_role("button", name="Paper Trade", exact=True).click()
    view = page.locator('[data-workspace-view="paper-trade"]')
    view.wait_for(state="visible", timeout=5_000)
    page.wait_for_function(
        """() => {
            const el = document.querySelector('[data-workspace-view="paper-trade"]');
            return el && el.getAttribute('data-api-status') !== 'loading';
        }""",
        timeout=5_000,
    )
    return view


def _canonical(page, base_url: str) -> dict:
    health = page.request.get(f"{base_url}/api/health").json()
    assert health["paper_only"] is True
    assert health["broker_connected"] is False
    readback = page.request.get(f"{base_url}/api/paper/readback").json()
    assert readback["paper_only"] is True
    assert readback["broker_connected"] is False
    return {
        "orders": page.request.get(f"{base_url}/api/paper/orders").json(),
        "fills": readback["fills"],
        "portfolios": readback["portfolios"],
        "portfolio": page.request.get(f"{base_url}/api/portfolio").json(),
        "events": page.request.get(f"{base_url}/api/events?since_id=0&limit=1000").json(),
    }


def _set_manual_form(page, *, quantity="1", reason="manual browser paper order", order_type="LIMIT", data_state="fresh", limit_price="101"):
    page.get_by_label("Paper quantity").fill(str(quantity))
    page.get_by_label("Paper reason").fill(reason)
    page.get_by_label("Paper order type").select_option(order_type)
    if order_type == "LIMIT":
        page.get_by_label("Paper limit price").fill(str(limit_price))
    page.get_by_label("Paper data state").select_option(data_state)


def _preview(page):
    page.get_by_role("button", name="Preview order", exact=True).click()
    page.locator('[data-testid="paper-preview"]').wait_for(state="visible", timeout=5_000)
    return page.locator('[data-testid="paper-preview"]').inner_text()


def test_manual_ui_preview_confirm_cancel_replace_and_rejections(
    order_flow_server,
    chromium,
    evidence_dir: Path,
):
    base_url, _, _ = order_flow_server
    context = chromium.new_context(viewport={"width": 1536, "height": 864})
    page = context.new_page()
    console_errors, page_errors, api_trace = _recorders(page)
    completed = False
    canonical_after = None
    action_order_ids: dict[str, str] = {}
    try:
        view = _open_paper_trade(page, base_url)
        readback = view.locator(".pm-data-card pre").inner_text()
        assert "TEST_ONLY_MANUAL_FILLED" in readback
        assert "TEST_ONLY_MANUAL_PARTIAL" in readback
        assert "TEST_ONLY_FILL_MANUAL_FULL" in readback
        assert "TEST_ONLY_FILL_MANUAL_PARTIAL" in readback
        assert "FILLED" in readback
        assert "PARTIALLY_FILLED" in readback

        before = _canonical(page, base_url)
        before_count = len(before["orders"])
        expected_qty = sum(
            (fill["quantity"] if fill["side"] == "BUY" else -fill["quantity"])
            for fill in before["fills"]
            if fill["symbol"] == "2330.TW" and fill["bucket"] == "swing"
        )
        assert expected_qty > 0
        assert before["portfolio"]["swing"]["positions"]["2330.TW"]["quantity"] == expected_qty
        assert before["portfolio"]["swing"]["cash"] < before["portfolio"]["swing"]["initial_cash"]

        _set_manual_form(page, reason="TEST_ONLY_UI_PENDING_CANCEL")
        preview = _preview(page)
        assert '"status": "APPROVED"' in preview
        assert '"data_status": "FRESH_NON_FALLBACK"' in preview
        after_preview = _canonical(page, base_url)
        assert after_preview["orders"] == before["orders"]
        assert after_preview["fills"] == before["fills"]
        assert after_preview["portfolio"]["swing"]["cash"] == before["portfolio"]["swing"]["cash"]
        confirm = page.get_by_role("button", name="Confirm paper order", exact=True)
        assert confirm.is_enabled()
        confirm.click()
        page.get_by_role("status").filter(has_text="CONFIRMED").wait_for(timeout=5_000)

        submitted = _canonical(page, base_url)
        assert len(submitted["orders"]) == before_count + 1
        manual = next(item for item in submitted["orders"] if item["reason"] == "TEST_ONLY_UI_PENDING_CANCEL")
        action_order_ids["confirmed_then_cancelled"] = manual["order_id"]
        assert manual["origin"] == "MANUAL"
        assert manual["status"] == "PENDING"
        row = page.locator(f'tr[data-order-id="{manual["order_id"]}"]')
        row.get_by_role("button", name="Cancel", exact=True).click()
        page.get_by_role("status").filter(has_text="CANCELLED").wait_for(timeout=5_000)
        cancelled = _canonical(page, base_url)
        cancelled_order = next(item for item in cancelled["orders"] if item["order_id"] == manual["order_id"])
        assert cancelled_order["status"] == "CANCELLED"

        _set_manual_form(page, quantity="1", reason="TEST_ONLY_UI_PENDING_REPLACE", limit_price="101")
        assert '"status": "APPROVED"' in _preview(page)
        page.get_by_role("button", name="Confirm paper order", exact=True).click()
        page.get_by_role("status").filter(has_text="CONFIRMED").wait_for(timeout=5_000)
        pending = _canonical(page, base_url)
        old = next(item for item in pending["orders"] if item["reason"] == "TEST_ONLY_UI_PENDING_REPLACE")
        _set_manual_form(page, quantity="2", reason="TEST_ONLY_UI_REPLACEMENT", limit_price="99")
        old_row = page.locator(f'tr[data-order-id="{old["order_id"]}"]')
        old_row.get_by_role("button", name="Replace", exact=True).click()
        page.get_by_role("status").filter(has_text="REPLACED").wait_for(timeout=5_000)
        replaced = _canonical(page, base_url)
        old_after = next(item for item in replaced["orders"] if item["order_id"] == old["order_id"])
        replacement = next(item for item in replaced["orders"] if item["reason"] == "TEST_ONLY_UI_REPLACEMENT")
        action_order_ids["replaced_old"] = old["order_id"]
        action_order_ids["replacement_new"] = replacement["order_id"]
        assert old_after["status"] == "CANCELLED"
        assert replacement["status"] == "PENDING"
        assert replacement["quantity"] == 2
        assert replacement["audit_metadata"]["replaced_order_id"] == old["order_id"]

        stable_count = len(replaced["orders"])
        forbidden_baseline_fill_ids = [item["fill_id"] for item in replaced["fills"]]
        forbidden_baseline_cash = replaced["portfolio"]["swing"]["cash"]
        _set_manual_form(page, quantity="0", reason="TEST_ONLY_INVALID_QUANTITY")
        page.get_by_role("button", name="Preview order", exact=True).click()
        page.get_by_role("status").filter(has_text="quantity").wait_for(timeout=5_000)
        assert len(_canonical(page, base_url)["orders"]) == stable_count

        _set_manual_form(page, quantity="1", reason=" ")
        page.get_by_role("button", name="Preview order", exact=True).click()
        page.get_by_role("status").filter(has_text="reason").wait_for(timeout=5_000)
        assert len(_canonical(page, base_url)["orders"]) == stable_count

        kill = page.request.post(
            f"{base_url}/api/paper/kill-switch",
            data={"enabled": True, "reason": "TEST_ONLY browser kill-switch rejection"},
        )
        assert kill.status == 200
        _set_manual_form(page, quantity="1", reason="TEST_ONLY_KILL_SWITCH")
        assert "KILL_SWITCH_ENABLED" in _preview(page)
        assert len(_canonical(page, base_url)["orders"]) == stable_count
        resume = page.request.post(
            f"{base_url}/api/paper/kill-switch",
            data={"enabled": False, "reason": "TEST_ONLY browser resume"},
        )
        assert resume.status == 200

        _set_manual_form(page, quantity="1", reason="TEST_ONLY_STALE", data_state="stale")
        assert "REJECTED_STALE_OR_FALLBACK" in _preview(page)
        assert len(_canonical(page, base_url)["orders"]) == stable_count

        _set_manual_form(
            page,
            quantity="1",
            reason="TEST_ONLY_NO_QUOTE",
            order_type="MARKET",
            data_state="noquote",
        )
        assert "MISSING_REFERENCE_PRICE" in _preview(page)
        forbidden_after = _canonical(page, base_url)
        assert len(forbidden_after["orders"]) == stable_count
        assert [item["fill_id"] for item in forbidden_after["fills"]] == forbidden_baseline_fill_ids
        assert forbidden_after["portfolio"]["swing"]["cash"] == forbidden_baseline_cash

        event_types = [
            item["event"]["event_type"]
            for item in forbidden_after["events"]["events"]
        ]
        assert "ORDER_CREATED" in event_types
        assert "ORDER_CANCELLED" in event_types
        assert "ORDER_REPLACED" in event_types

        assert not page_errors
        validation_responses = [
            item for item in api_trace
            if urlsplit(item["url"]).path == "/api/paper/orders/preview"
            and item["status"] >= 400
        ]
        assert len(validation_responses) == 2
        assert all(item["status"] == 422 for item in validation_responses)
        assert all(
            urlsplit(item["url"]).path == "/api/paper/orders/preview"
            and "Failed to load resource" in item["text"]
            and "422" in item["text"]
            for item in console_errors
        )
        canonical_after = forbidden_after
        completed = True
    finally:
        _write_evidence(page, evidence_dir, "paper-order-manual-flow", {
            "completed": completed,
            "action_order_ids": action_order_ids,
            "canonical_order_statuses": (
                {item["order_id"]: item["status"] for item in canonical_after["orders"]}
                if canonical_after is not None else None
            ),
            "canonical_fill_ids": (
                [item["fill_id"] for item in canonical_after["fills"]]
                if canonical_after is not None else None
            ),
            "canonical_swing_cash": (
                canonical_after["portfolio"]["swing"]["cash"]
                if canonical_after is not None else None
            ),
            "canonical_event_types": (
                [item["event"]["event_type"] for item in canonical_after["events"]["events"]]
                if canonical_after is not None else None
            ),
            "console_errors": console_errors,
            "page_errors": page_errors,
            "api_trace": api_trace,
        })
        context.close()


def test_strategy_and_main_cio_readback_and_authority_boundaries(
    order_flow_server,
    chromium,
    evidence_dir: Path,
):
    base_url, _, _ = order_flow_server
    context = chromium.new_context(viewport={"width": 1536, "height": 864})
    page = context.new_page()
    console_errors, page_errors, api_trace = _recorders(page)
    completed = False
    authority_payload = None
    strategy_order_ids: set[str] = set()
    cio_order_ids: set[str] = set()
    try:
        view = _open_paper_trade(page, base_url)
        readback = view.locator(".pm-data-card pre").inner_text()
        assert '"origin": "STRATEGY"' in readback
        assert "opening_range_breakout" in readback
        assert "TEST_ONLY_FILL_STRATEGY" in readback
        assert '"origin": "MAIN_CIO"' in readback
        assert "TEST_ONLY_CIO_V1" in readback
        assert "TEST_ONLY_CIO_POSITIVE" in readback

        canonical = _canonical(page, base_url)
        strategy_orders = [item for item in canonical["orders"] if item["origin"] == "STRATEGY"]
        cio_orders = [item for item in canonical["orders"] if item["origin"] == "MAIN_CIO"]
        assert strategy_orders
        assert cio_orders
        assert all(item["strategy_id"] == "opening_range_breakout" for item in strategy_orders)
        assert all(item["strategy_version"] == "runner-v2" for item in strategy_orders)
        assert all(item["status"] in {"PARTIALLY_FILLED", "FILLED"} for item in strategy_orders)
        assert all(item["strategy_version"] == "TEST_ONLY_CIO_V1" for item in cio_orders)
        assert all(
            item.get("audit_metadata", {}).get("case_id") == "TEST_ONLY_CIO_POSITIVE"
            for item in cio_orders
        )

        strategy_order_ids = {item["order_id"] for item in strategy_orders}
        cio_order_ids = {item["order_id"] for item in cio_orders}
        strategy_fill_ids = {
            fill["fill_id"] for fill in canonical["fills"]
            if fill["order_id"] in strategy_order_ids
        }
        assert "TEST_ONLY_FILL_STRATEGY" in strategy_fill_ids

        canonical_events = canonical["events"]["events"]
        assert any(
            item["event"]["event_type"] == "ORDER_CREATED"
            and item["event"]["aggregate_id"] in strategy_order_ids
            for item in canonical_events
        )
        assert any(
            item["event"]["event_type"] == "ORDER_FILLED"
            and item["event"]["aggregate_id"] in strategy_order_ids
            for item in canonical_events
        )
        assert any(
            item["event"]["event_type"] == "ORDER_CREATED"
            and item["event"]["aggregate_id"] in cio_order_ids
            for item in canonical_events
        )

        authority = page.request.get(f"{base_url}/api/test-only/order-flow")
        assert authority.status == 200
        authority_payload = authority.json()
        assert authority_payload["paper_only"] is True
        assert authority_payload["broker_connected"] is False
        assert authority_payload["positive"]["action"] == "BUY_PENDING"
        assert "PENDING" in authority_payload["positive"]["reason"]
        assert authority_payload["positive"]["order_id"] in cio_order_ids
        assert authority_payload["negative_invariants"]["fills_before"] == authority_payload["negative_invariants"]["fills_after"]
        assert authority_payload["negative_invariants"]["cash_before"] == authority_payload["negative_invariants"]["cash_after"]

        expected = {
            "missing_signature": "PROVENANCE_AUTHENTICATION_FAILED",
            "invalid_signature": "PROVENANCE_AUTHENTICATION_FAILED",
            "expired": "PACKET_EXPIRED",
            "worker_authority": "PROVENANCE_VIOLATION",
        }
        for key, marker in expected.items():
            decision = authority_payload["negative"][key]
            assert decision["action"] == "NO_TRADE"
            assert marker in decision["reason"]

        assert not [
            item for item in canonical["orders"]
            if item["origin"] == "MAIN_CIO"
            and item.get("audit_metadata", {}).get("case_id") != "TEST_ONLY_CIO_POSITIVE"
        ]
        script_sources = page.locator('script[src]').evaluate_all(
            "(nodes) => nodes.map((node) => node.getAttribute('src')).filter(Boolean)"
        )
        assert script_sources
        bundle_text = "\n".join(
            page.request.get(
                source if source.startswith("http") else base_url + (source if source.startswith("/") else "/" + source)
            ).text()
            for source in script_sources
        )
        assert "CIO_PROVENANCE_SECRET" not in bundle_text
        assert "fixture-test-signer" not in bundle_text
        assert "sign_cio_packet" not in bundle_text
        assert page.get_by_label("Order origin").count() == 0

        page.get_by_role("button", name="Research", exact=True).click()
        research = page.locator('[data-workspace-view="research"]')
        research.wait_for(state="visible", timeout=5_000)
        assert research.get_by_role("button", name="Preview order", exact=True).count() == 0
        assert research.get_by_role("button", name="Confirm paper order", exact=True).count() == 0
        assert research.get_by_label("Order origin").count() == 0

        assert not page_errors
        assert not console_errors
        completed = True
    finally:
        _write_evidence(page, evidence_dir, "paper-order-authority-flow", {
            "completed": completed,
            "strategy_order_ids": sorted(strategy_order_ids),
            "cio_order_ids": sorted(cio_order_ids),
            "negative_reasons": (
                {
                    key: value["reason"]
                    for key, value in authority_payload["negative"].items()
                }
                if authority_payload is not None else None
            ),
            "negative_invariants": (
                authority_payload["negative_invariants"]
                if authority_payload is not None else None
            ),
            "console_errors": console_errors,
            "page_errors": page_errors,
            "api_trace": api_trace,
        })
        context.close()


def test_same_runtime_actual_process_restart_preserves_order_fill_case_and_nav(
    order_flow_restart_factory,
    chromium,
    evidence_dir: Path,
):
    first_snapshot = None
    first_pid = None
    with order_flow_restart_factory() as first:
        base_url, runtime, first_pid = first
        context = chromium.new_context(viewport={"width": 1280, "height": 720})
        page = context.new_page()
        _open_paper_trade(page, base_url)
        first_snapshot = _canonical(page, base_url)
        learning = page.request.get(f"{base_url}/api/paper/cio/learning-cases").json()
        first_snapshot["case_ids"] = sorted(
            item["case_id"] for item in learning.get("cases", [])
            if item.get("case_id", "").startswith("TEST_ONLY_CIO")
        )
        first_snapshot["runtime"] = str(runtime)
        first_snapshot["pid"] = first_pid
        context.close()

    assert first_snapshot is not None
    with order_flow_restart_factory() as second:
        base_url, runtime, second_pid = second
        assert second_pid != first_pid
        assert str(runtime) == first_snapshot["runtime"]
        context = chromium.new_context(viewport={"width": 1280, "height": 720})
        page = context.new_page()
        view = _open_paper_trade(page, base_url)
        second_snapshot = _canonical(page, base_url)
        learning = page.request.get(f"{base_url}/api/paper/cio/learning-cases").json()
        second_snapshot["case_ids"] = sorted(
            item["case_id"] for item in learning.get("cases", [])
            if item.get("case_id", "").startswith("TEST_ONLY_CIO")
        )

        assert [item["order_id"] for item in second_snapshot["orders"]] == [
            item["order_id"] for item in first_snapshot["orders"]
        ]
        assert [item["fill_id"] for item in second_snapshot["fills"]] == [
            item["fill_id"] for item in first_snapshot["fills"]
        ]
        assert second_snapshot["case_ids"] == first_snapshot["case_ids"]
        assert len(set(second_snapshot["case_ids"])) == len(second_snapshot["case_ids"])
        assert second_snapshot["events"] == first_snapshot["events"]
        assert second_snapshot["portfolio"]["swing"]["cash"] == first_snapshot["portfolio"]["swing"]["cash"]
        assert second_snapshot["portfolio"]["swing"]["equity"] == first_snapshot["portfolio"]["swing"]["equity"]
        assert second_snapshot["portfolios"] == first_snapshot["portfolios"]
        assert len(set(item["order_id"] for item in second_snapshot["orders"])) == len(second_snapshot["orders"])
        assert len(set(item["fill_id"] for item in second_snapshot["fills"])) == len(second_snapshot["fills"])
        assert "TEST_ONLY_CIO_POSITIVE" in view.locator(".pm-data-card pre").inner_text()

        _write_evidence(page, evidence_dir, "paper-order-process-restart", {
            "completed": True,
            "first_pid": first_pid,
            "second_pid": second_pid,
            "same_runtime": str(runtime) == first_snapshot["runtime"],
            "order_ids": [item["order_id"] for item in second_snapshot["orders"]],
            "fill_ids": [item["fill_id"] for item in second_snapshot["fills"]],
            "case_ids": second_snapshot["case_ids"],
            "cash": second_snapshot["portfolio"]["swing"]["cash"],
            "equity": second_snapshot["portfolio"]["swing"]["equity"],
            "canonical_portfolios_equal": second_snapshot["portfolios"] == first_snapshot["portfolios"],
        })
        context.close()
