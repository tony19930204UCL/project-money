from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

PAGES = [
    ("Command Center", "command-center", "/api/health", "ok", '"paper_only": true'),
    ("Markets", "markets", "/api/watchlists", "ok", "2330.TW"),
    ("Chart", "chart", "/api/market/bars/2330.TW?timeframe=1D&limit=20", "ok", "TEST_ONLY_BROWSER_ADAPTER"),
    ("Paper Trade", "paper-trade", "/api/paper/orders", "ok", "TEST_ONLY_BROWSER_ORDER"),
    ("Portfolio", "portfolio", "/api/portfolio", "ok", "TEST_ONLY_BROWSER_ORDER"),
    ("Strategy Lab", "strategy-lab", "/api/strategies", "ok", "opening_range_breakout"),
    ("Research", "research", "/api/research/inbox", "ok", "TEST_ONLY_RESEARCH_FIXTURE"),
    ("Replay", "replay", "/api/paper/experiments", "ok", "TEST_ONLY_BROWSER_EXPERIMENT"),
    ("Risk", "risk", "/api/paper/risk-limits", "ok", "max_order_notional"),
    ("Diagnostics", "diagnostics", "/api/diagnostics", "ok", "TEST_ONLY_fixture_normal"),
]
VIEWPORTS = [(1280, 720), (1536, 864)]


def _recorders(page):
    console_errors: list[dict] = []
    page_errors: list[str] = []
    api_trace: list[dict] = []

    def record_console(msg):
        if msg.type != "error":
            return
        location = msg.location or {}
        console_errors.append({
            "type": msg.type,
            "text": msg.text,
            "url": location.get("url", ""),
            "line_number": location.get("lineNumber"),
            "column_number": location.get("columnNumber"),
        })

    page.on("console", record_console)

    def record_page_error(exc):
        detail = getattr(exc, "stack", None) or str(exc)
        page_errors.append(detail)

    page.on("pageerror", record_page_error)
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


def _wait_workspace(page, slug: str, expected_state: str):
    view = page.locator(f'[data-workspace-view="{slug}"]')
    view.wait_for(state="visible", timeout=5_000)
    page.wait_for_function(
        """([slug, state]) => {
            const el = document.querySelector(`[data-workspace-view="${slug}"]`);
            return el && el.getAttribute('data-api-status') === state;
        }""",
        arg=[slug, expected_state],
        timeout=5_000,
    )
    return view


def _assert_core_content(view, heading: str, expected_text: str) -> None:
    assert view.get_by_role("heading", name=heading, exact=True).count() == 1
    assert view.get_by_role("heading", name=heading, exact=True).is_visible()
    data_card = view.locator(".pm-data-card")
    assert data_card.count() == 1
    assert data_card.locator("pre").count() == 1
    assert expected_text in data_card.locator("pre").inner_text()


def _matching_responses(api_trace: list[dict], endpoint: str) -> list[dict]:
    target = endpoint.split("?", 1)[0]
    return [
        item for item in api_trace
        if urlsplit(item["url"]).path == target and item["method"] == "GET"
    ]


def _is_expected_chart_500_console(item: dict) -> bool:
    return (
        item.get("type") == "error"
        and urlsplit(item.get("url", "")).path == "/api/market/bars/2330.TW"
        and "Failed to load resource" in item.get("text", "")
        and "500" in item.get("text", "")
    )


@pytest.mark.parametrize(
    "label,slug,endpoint,expected_state,expected_text",
    PAGES,
    ids=[item[1] for item in PAGES],
)
@pytest.mark.parametrize("viewport", VIEWPORTS, ids=["1280x720", "1536x864"])
def test_workstation_page_viewport(
    browser_server,
    chromium,
    evidence_dir: Path,
    label: str,
    slug: str,
    endpoint: str,
    expected_state: str,
    expected_text: str,
    viewport: tuple[int, int],
):
    base_url, _, _ = browser_server
    width, height = viewport
    context = chromium.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    console_errors, page_errors, api_trace = _recorders(page)
    stem = f"{slug}-{width}x{height}"
    try:
        response = page.goto(base_url, wait_until="domcontentloaded", timeout=15_000)
        assert response is not None and response.ok
        health = page.request.get(f"{base_url}/api/health").json()
        assert health["paper_only"] is True
        assert health["broker_connected"] is False

        nav = page.get_by_role("button", name=label, exact=True)
        nav.wait_for(state="visible", timeout=5_000)
        assert nav.count() == 1
        nav.click()
        view = _wait_workspace(page, slug, expected_state)
        assert view.get_attribute("data-api-status") == expected_state
        _assert_core_content(view, label, expected_text)

        completed = _matching_responses(api_trace, endpoint)
        assert completed, f"expected GET did not complete: {endpoint}"
        assert completed[-1]["status"] == 200

        if slug == "command-center":
            assert page.get_by_role("button", name="Open Trading Terminal", exact=True).count() == 1

        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 1
        assert not page_errors
        assert not [item for item in api_trace if item["status"] >= 400]
        assert not console_errors
    finally:
        _write_evidence(page, evidence_dir, stem, {
            "page": label,
            "viewport": [width, height],
            "console_errors": console_errors,
            "page_errors": page_errors,
            "api_trace": api_trace,
        })
        context.close()


def test_core_content_assertion_rejects_missing_heading_and_payload(browser_server, chromium):
    """Mutation guard for Main's exact missing-h1 + missing-pre regression."""
    base_url, _, _ = browser_server
    context = chromium.new_context(viewport={"width": 1280, "height": 720})
    page = context.new_page()
    try:
        page.goto(base_url, wait_until="domcontentloaded", timeout=15_000)
        page.get_by_role("button", name="Markets", exact=True).click()
        view = _wait_workspace(page, "markets", "ok")
        _assert_core_content(view, "Markets", "2330.TW")
        page.evaluate(
            """() => {
                const view = document.querySelector('[data-workspace-view="markets"]');
                view?.querySelector('h1')?.remove();
                view?.querySelector('pre')?.remove();
            }"""
        )
        with pytest.raises(AssertionError):
            _assert_core_content(view, "Markets", "2330.TW")
    finally:
        context.close()


@pytest.mark.parametrize(
    "market_mode,expected_state,expected_http",
    [("empty", "empty", 200), ("stale", "stale", 200), ("error", "error", 500)],
    ids=["empty-response", "stale-response", "error-response"],
)
def test_smoke_negative_state_contracts(
    browser_server_factory,
    chromium,
    evidence_dir: Path,
    market_mode,
    expected_state,
    expected_http,
):
    with browser_server_factory(market_mode=market_mode) as server:
        base_url, _, _ = server
        context = chromium.new_context(viewport={"width": 1280, "height": 720})
        page = context.new_page()
        console_errors, page_errors, api_trace = _recorders(page)
        stem = f"negative-{market_mode}"
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=15_000)
            page.get_by_role("button", name="Chart", exact=True).click()
            view = _wait_workspace(page, "chart", expected_state)
            assert view.locator(f'[data-state-kind="{expected_state}"]').count() == 1
            assert view.get_by_role("heading", name="Chart", exact=True).is_visible()

            completed = _matching_responses(
                api_trace, "/api/market/bars/2330.TW?timeframe=1D&limit=20"
            )
            assert completed
            assert completed[-1]["status"] == expected_http
            unrelated = [
                item for item in api_trace
                if item["status"] >= 400
                and not (
                    market_mode == "error"
                    and urlsplit(item["url"]).path == "/api/market/bars/2330.TW"
                    and item["status"] == 500
                )
            ]
            assert not unrelated
            assert not page_errors
            if market_mode == "error":
                expected_console_errors = [
                    item for item in console_errors
                    if _is_expected_chart_500_console(item)
                ]
                assert expected_console_errors, console_errors
                assert len(expected_console_errors) == len(console_errors), console_errors
            else:
                assert not console_errors
        finally:
            _write_evidence(page, evidence_dir, stem, {
                "market_mode": market_mode,
                "expected_state": expected_state,
                "console_errors": console_errors,
                "page_errors": page_errors,
                "api_trace": api_trace,
            })
            context.close()


def test_smoke_is_get_only_without_operator_actions(
    browser_server,
    chromium,
    evidence_dir: Path,
):
    base_url, _, _ = browser_server
    context = chromium.new_context(viewport={"width": 1280, "height": 720})
    page = context.new_page()
    console_errors, page_errors, api_trace = _recorders(page)
    issued_requests: list[dict] = []
    mutations: list[str] = []
    visited_pages: list[str] = []
    traversal_completed = False
    orders_after = None
    events_after = None
    orders_equal = False
    events_equal = False

    def record_request(req):
        if "/api/" not in req.url:
            return
        item = {"method": req.method, "url": req.url}
        issued_requests.append(item)
        if req.method in {"POST", "PUT", "DELETE", "PATCH"}:
            mutations.append(f"{req.method} {req.url}")

    page.on("request", record_request)

    orders_before = page.request.get(f"{base_url}/api/paper/orders").json()
    events_before = page.request.get(f"{base_url}/api/events?since_id=0&limit=1000").json()
    assert any(
        item.get("audit_metadata", {}).get("fixture_receipt") == "TEST_ONLY_BROWSER_ORDER"
        for item in orders_before
    )
    try:
        page.goto(base_url, wait_until="domcontentloaded", timeout=15_000)
        health = page.request.get(f"{base_url}/api/health").json()
        assert health["paper_only"] is True
        assert health["broker_connected"] is False

        for label, slug, endpoint, expected_state, expected_text in PAGES:
            page.get_by_role("button", name=label, exact=True).click()
            view = _wait_workspace(page, slug, expected_state)
            _assert_core_content(view, label, expected_text)
            completed = _matching_responses(api_trace, endpoint)
            assert completed, endpoint
            assert completed[-1]["status"] == 200
            visited_pages.append(slug)

        assert visited_pages == [item[1] for item in PAGES]
        expected_paths = {item[2].split("?", 1)[0] for item in PAGES}
        completed_paths = {
            urlsplit(item["url"]).path
            for item in api_trace
            if item["method"] == "GET" and item["status"] == 200
        }
        assert expected_paths.issubset(completed_paths)

        orders_after = page.request.get(f"{base_url}/api/paper/orders").json()
        events_after = page.request.get(f"{base_url}/api/events?since_id=0&limit=1000").json()
        orders_equal = orders_after == orders_before
        events_equal = events_after == events_before

        assert orders_equal
        assert events_equal
        assert not mutations
        assert not page_errors
        assert not console_errors
        assert issued_requests
        assert all(item["method"] == "GET" for item in issued_requests)
        assert all(item["method"] == "GET" for item in api_trace)
        traversal_completed = True
    finally:
        _write_evidence(page, evidence_dir, "get-only-full-traversal", {
            "completed": traversal_completed,
            "visited_pages": visited_pages,
            "expected_pages": [item[1] for item in PAGES],
            "console_errors": console_errors,
            "page_errors": page_errors,
            "api_trace": api_trace,
            "issued_requests": issued_requests,
            "mutations": mutations,
            "orders_equal": orders_equal,
            "events_equal": events_equal,
            "orders_before_count": len(orders_before),
            "orders_after_count": len(orders_after) if orders_after is not None else None,
            "events_before_count": len(events_before.get("events", [])),
            "events_after_count": (
                len(events_after.get("events", []))
                if events_after is not None
                else None
            ),
            "orders_before": orders_before,
            "orders_after": orders_after,
            "events_before": events_before,
            "events_after": events_after,
        })
        context.close()
