from __future__ import annotations

import json
from pathlib import Path

import pytest

PAGES = [
    ("Command Center", "command-center"),
    ("Markets", "markets"),
    ("Chart", "chart"),
    ("Paper Trade", "paper-trade"),
    ("Portfolio", "portfolio"),
    ("Strategy Lab", "strategy-lab"),
    ("Research", "research"),
    ("Replay", "replay"),
    ("Risk", "risk"),
    ("Diagnostics", "diagnostics"),
]
VIEWPORTS = [(1280, 720), (1536, 864)]  # ABC C01/C02/C03/C09/B16 viewport corpus


@pytest.mark.parametrize("label,slug", PAGES, ids=[item[1] for item in PAGES])
@pytest.mark.parametrize("viewport", VIEWPORTS, ids=["1280x720", "1536x864"])
def test_workstation_page_viewport(
    browser_server,
    chromium,
    evidence_dir: Path,
    label: str,
    slug: str,
    viewport: tuple[int, int],
):
    base_url, _, _ = browser_server
    width, height = viewport
    context = chromium.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    console_errors: list[str] = []
    page_errors: list[str] = []
    api_trace: list[dict] = []

    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    def record_page_error(exc):
        detail = getattr(exc, "stack", None) or str(exc)
        page_errors.append(detail)
        print(f"BROWSER_PAGE_ERROR: {detail}", flush=True)

    page.on("pageerror", record_page_error)
    page.on(
        "response",
        lambda response: api_trace.append({
            "method": response.request.method,
            "url": response.url,
            "status": response.status,
        }) if "/api/" in response.url else None,
    )

    stem = f"{slug}-{width}x{height}"
    try:
        response = page.goto(base_url, wait_until="domcontentloaded", timeout=15_000)
        assert response is not None and response.ok
        page.locator("#root").wait_for(state="attached", timeout=5_000)
        health = page.request.get(f"{base_url}/api/health").json()
        assert health["paper_only"] is True
        assert health["broker_connected"] is False

        nav = page.get_by_role("button", name=label, exact=True)
        nav.wait_for(state="visible", timeout=5_000)
        assert nav.count() == 1, f"missing actual SPA navigation control: {label}"
        nav.click()
        page.locator(f'[data-workspace-view="{slug}"]').wait_for(state="visible", timeout=5_000)
        view = page.locator(f'[data-workspace-view="{slug}"]')
        page.wait_for_function(
            """(slug) => {
                const el = document.querySelector(`[data-workspace-view="${slug}"]`);
                return el && el.getAttribute('data-api-status') !== 'loading';
            }""",
            arg=slug,
            timeout=5_000,
        )
        assert view.get_attribute("data-api-status") in {"ok", "empty", "stale", "error"}
        if slug == "command-center":
            assert page.get_by_role("button", name="Open Trading Terminal", exact=True).count() == 1

        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 1, f"page-level horizontal overflow={overflow}px"
        assert page.locator("body").is_visible()
        assert not page_errors

        critical = [
            item for item in api_trace
            if item["status"] >= 400 and "/api/" in item["url"]
        ]
        assert not critical, f"fixture-critical API failures: {critical}"
        assert not console_errors, f"console errors: {console_errors}"
    finally:
        page.screenshot(path=str(evidence_dir / f"{stem}.png"), full_page=True)
        (evidence_dir / f"{stem}.json").write_text(
            json.dumps({
                "page": label,
                "viewport": [width, height],
                "console_errors": console_errors,
                "page_errors": page_errors,
                "api_trace": api_trace,
            }, indent=2),
            encoding="utf-8",
        )
        context.close()


@pytest.mark.parametrize(
    "market_mode,expected_state",
    [
        ("empty", "empty"),
        ("stale", "stale"),
        ("error", "error"),
    ],
    ids=["empty-response", "stale-response", "error-response"],
)
def test_smoke_negative_state_contracts(
    browser_server_factory,
    chromium,
    market_mode,
    expected_state,
):
    """Negative states must come through the real FastAPI + TEST_ONLY adapter."""
    with browser_server_factory(market_mode=market_mode) as server:
        base_url, _, _ = server
        context = chromium.new_context(viewport={"width": 1280, "height": 720})
        page = context.new_page()
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=15_000)
            chart_nav = page.get_by_role("button", name="Chart", exact=True)
            chart_nav.wait_for(state="visible", timeout=5_000)
            chart_nav.click()
            view = page.locator('[data-workspace-view="chart"]')
            page.wait_for_function(
                """(state) =>
                    document.querySelector('[data-workspace-view="chart"]')
                        ?.getAttribute('data-api-status') === state
                """,
                arg=expected_state,
                timeout=5_000,
            )
            assert view.get_attribute("data-api-status") == expected_state
            assert view.locator(f'[data-state-kind="{expected_state}"]').count() == 1
        finally:
            context.close()


def test_smoke_is_get_only_without_operator_actions(browser_server, chromium):
    base_url, _, _ = browser_server
    context = chromium.new_context(viewport={"width": 1280, "height": 720})
    page = context.new_page()
    mutations: list[str] = []
    page.on(
        "request",
        lambda req: mutations.append(f"{req.method} {req.url}")
        if "/api/" in req.url and req.method in {"POST", "PUT", "DELETE", "PATCH"} else None,
    )
    page.goto(base_url, wait_until="domcontentloaded", timeout=15_000)
    page.get_by_role("button", name="Command Center", exact=True).wait_for(
        state="visible", timeout=5_000
    )
    for label, slug in PAGES:
        page.get_by_role("button", name=label, exact=True).click()
        page.locator(f'[data-workspace-view="{slug}"]').wait_for(
            state="visible", timeout=5_000
        )
        page.wait_for_function(
            """(slug) => {
                const el = document.querySelector(`[data-workspace-view="${slug}"]`);
                return el && el.getAttribute('data-api-status') !== 'loading';
            }""",
            arg=slug,
            timeout=5_000,
        )
    context.close()
    assert not mutations, f"observer navigation emitted mutation requests: {mutations}"
