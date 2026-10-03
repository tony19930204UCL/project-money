from __future__ import annotations

import json
from pathlib import Path

import pytest

VIEWS = [
    ("command", "Command Center", "#view-command", "Market pulse"),
    ("markets", "Markets", "#view-markets", "Markets"),
    ("chart", "Chart", "#view-chart", "Chart desk"),
    ("paper", "Paper Trade", "#view-paper", "Paper trade"),
    ("portfolio", "Portfolio", "#view-portfolio", "Portfolio"),
    ("strategy", "Strategy Lab", "#view-strategy", "Strategy lab"),
    ("research", "Research", "#view-research", "Research quarantine"),
    ("replay", "Replay", "#view-replay", "Historical replay"),
    ("risk", "Risk", "#view-risk", "Paper risk / observer"),
    ("diagnostics", "Diagnostics", "#view-diagnostics", "Diagnostics"),
]
VIEWPORTS = [(1280, 720), (1536, 864)]


def _safe_name(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")


def _recorded_page(browser, viewport, evidence_dir: Path, name: str):
    context = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
    page = context.new_page()
    evidence = {
        "name": name,
        "viewport": list(viewport),
        "console_errors": [],
        "page_errors": [],
        "requests": [],
        "responses": [],
    }
    page.on("console", lambda message: evidence["console_errors"].append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: evidence["page_errors"].append(str(error)))
    page.on("request", lambda request: evidence["requests"].append({
        "method": request.method,
        "url": request.url,
    }))
    page.on("response", lambda response: evidence["responses"].append({
        "status": response.status,
        "url": response.url,
    }))
    return context, page, evidence


def _finish(page, context, evidence, evidence_dir: Path, stem: str):
    page.screenshot(path=str(evidence_dir / f"{stem}.png"), full_page=True)
    (evidence_dir / f"{stem}.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    context.close()


@pytest.mark.parametrize("view,label,selector,title", VIEWS, ids=[item[1] for item in VIEWS])
@pytest.mark.parametrize("viewport", VIEWPORTS, ids=["1280x720", "1536x864"])
def test_workstation_named_view(
    chromium_browser,
    read_only_server,
    browser_evidence_dir,
    view,
    label,
    selector,
    title,
    viewport,
):
    stem = f"smoke-{_safe_name(label)}-{viewport[0]}x{viewport[1]}"
    context, page, evidence = _recorded_page(
        chromium_browser, viewport, browser_evidence_dir, stem
    )
    try:
        root = page.goto(read_only_server.base_url + "/", wait_until="domcontentloaded", timeout=15000)
        assert root is not None and root.status == 200
        page.get_by_text("Project Money", exact=False).first.wait_for(state="visible", timeout=10000)
        assert page.locator("#root").count() == 1
        assert any("/assets/" in item["url"] for item in evidence["responses"]), (
            "FastAPI root did not load the built SPA asset bundle"
        )

        legacy = page.goto(
            read_only_server.base_url + "/static/index.html",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        assert legacy is not None and legacy.status == 200
        page.locator(".nav-item", has_text=label).click()
        page.locator(selector).wait_for(state="visible", timeout=10000)
        assert page.locator(".nav-item.active").get_attribute("data-view") == view
        assert title.lower() in page.locator("#view-title").inner_text().lower()

        if view == "command":
            page.locator("#watchlist .watch-row").first.wait_for(state="visible", timeout=10000)
            assert page.locator("#watchlist .watch-row").count() > 0
        elif view == "research":
            page.locator("#research-list").wait_for(state="visible")
            assert page.locator("#research-list").inner_text().strip()
        elif view == "chart" and viewport[0] == 1280:
            page.locator("#chart-symbol-input").fill("AAPL")
            page.locator("#chart-symbol-load").click()
            page.wait_for_timeout(300)
            assert "fallback" in page.locator("#desk-chart-meta").inner_text().lower()
        elif view == "chart" and viewport[0] == 1536:
            page.locator("#chart-symbol-input").fill("ERROR.TW")
            page.locator("#chart-symbol-load").click()
            page.get_by_text("No bars available.", exact=True).wait_for(state="visible", timeout=10000)

        overflow = page.evaluate(
            "() => ({scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth})"
        )
        assert overflow["scrollWidth"] <= overflow["innerWidth"] + 1, overflow

        mutations = [
            item for item in evidence["requests"]
            if "/api/" in item["url"] and item["method"] not in {"GET", "HEAD", "OPTIONS"}
        ]
        assert mutations == [], f"read-only smoke emitted API mutations: {mutations}"

        allowed_negative = view == "chart" and viewport[0] == 1536
        bad_responses = [
            item for item in evidence["responses"]
            if "/api/" in item["url"] and item["status"] >= 400
            and not (
                allowed_negative
                and "/api/market/bars/ERROR.TW" in item["url"]
                and item["status"] == 500
            )
        ]
        assert bad_responses == [], f"fixture-critical API failures: {bad_responses}"
        assert evidence["page_errors"] == []
        assert evidence["console_errors"] == []
    finally:
        _finish(page, context, evidence, browser_evidence_dir, stem)
