"""Public browser research adapter sidecar module for constrained public retrieval."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import ipaddress
import re
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from cio_market_lab.domain.models import ResearchItem

_ALLOWED_OFFICIAL_HOSTS = {
    "www.sec.gov",
    "sec.gov",
    "www.twse.com.tw",
    "twse.com.tw",
    "mops.twse.com.tw",
    "investor.nvidia.com",
}

_ALLOWED_COMMUNITY_HOSTS = {
    "www.reddit.com",
    "reddit.com",
    "old.reddit.com",
    "www.ptt.cc",
}

_ALLOWED_HOSTS = _ALLOWED_OFFICIAL_HOSTS | _ALLOWED_COMMUNITY_HOSTS

_CHALLENGE_PATTERNS = [
    re.compile(r"verify you are human", re.IGNORECASE),
    re.compile(r"captcha", re.IGNORECASE),
    re.compile(r"access denied", re.IGNORECASE),
    re.compile(r"attention required", re.IGNORECASE),
    re.compile(r"security check", re.IGNORECASE),
    re.compile(r"checking your browser", re.IGNORECASE),
]


def _validate_public_url(url: str) -> None:
    if not isinstance(url, str):
        raise ValueError("URL must be a string")

    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"Disallowed scheme: {parsed.scheme}. Only HTTPS is permitted.")

    if parsed.username or parsed.password:
        raise ValueError("User credentials in URL are strictly prohibited.")

    if parsed.port not in (None, 443):
        raise ValueError(f"Non-standard HTTPS port: {parsed.port}. Only port 443 is permitted.")

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError("URL hostname is empty or invalid.")

    if hostname == "localhost":
        raise ValueError("Localhost access is disallowed.")

    try:
        ipaddress.ip_address(hostname)
        raise ValueError("IP literals are disallowed.")
    except ValueError as e:
        if "IP literals are disallowed" in str(e):
            raise

    if hostname not in _ALLOWED_HOSTS:
        raise ValueError(f"Hostname '{hostname}' is not in approved public host list.")


class PublicBrowserResearchAdapter:
    """Adapter performing sandboxed, bounded public web research for approved hosts."""

    def __init__(
        self,
        reader: Optional[Callable[[str], Dict[str, Any]]] = None,
        executable_path: Optional[str] = None,
        timeout_ms: int = 15000,
    ) -> None:
        self.reader = reader
        self.executable_path = executable_path
        self.timeout_ms = timeout_ms
        self.last_receipt: Optional[Dict[str, Any]] = None

    def _playwright_read(self, url: str) -> Dict[str, Any]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is required when no custom reader is provided.") from exc

        with sync_playwright() as p:
            launch_kwargs: Dict[str, Any] = {"headless": True}
            if self.executable_path:
                launch_kwargs["executable_path"] = self.executable_path

            browser = p.chromium.launch(**launch_kwargs)
            try:
                context = browser.new_context(
                    ignore_https_errors=False,
                    java_script_enabled=True,
                    service_workers="block",
                )
                try:
                    def route_handler(route: Any, request: Any) -> None:
                        method = getattr(request, "method", "GET")
                        if str(method).upper() not in ("GET", "HEAD"):
                            route.abort()
                            return
                        req_url = request.url
                        try:
                            _validate_public_url(req_url)
                            route.continue_()
                        except Exception:
                            route.abort()

                    context.route("**/*", route_handler)
                    page = context.new_page()
                    if hasattr(page, "set_default_timeout"):
                        page.set_default_timeout(self.timeout_ms)

                    response = page.goto(
                        url,
                        timeout=self.timeout_ms,
                        wait_until="domcontentloaded",
                    )
                    if response is None:
                        raise RuntimeError(f"No response received navigating to {url}")
                    status = response.status

                    final_url = page.url
                    _validate_public_url(final_url)

                    initial_parsed = urlparse(url)
                    initial_host = (initial_parsed.hostname or "").lower()
                    is_initial_official = initial_host in _ALLOWED_OFFICIAL_HOSTS
                    final_parsed = urlparse(final_url)
                    final_host = (final_parsed.hostname or "").lower()
                    if is_initial_official and final_host in _ALLOWED_COMMUNITY_HOSTS:
                        raise ValueError(
                            f"Cross-tier redirect rejected: official host '{initial_host}' redirected to community host '{final_host}'."
                        )

                    title = page.title() or ""
                    try:
                        text = page.inner_text("body", timeout=min(self.timeout_ms, 5000))
                    except Exception:
                        text = ""

                    return {
                        "url": final_url,
                        "title": title,
                        "text": text,
                        "status": status,
                    }
                finally:
                    context.close()
            finally:
                browser.close()

    def fetch_page(self, url: str, now: Optional[datetime] = None) -> List[ResearchItem]:
        self.last_receipt = None
        _validate_public_url(url)

        initial_parsed = urlparse(url)
        initial_host = (initial_parsed.hostname or "").lower()
        is_initial_official = initial_host in _ALLOWED_OFFICIAL_HOSTS

        if self.reader is not None:
            data = self.reader(url)
        else:
            data = self._playwright_read(url)

        final_url = data.get("url", url)
        _validate_public_url(final_url)

        final_parsed = urlparse(final_url)
        final_host = (final_parsed.hostname or "").lower()
        if is_initial_official and final_host in _ALLOWED_COMMUNITY_HOSTS:
            raise ValueError(
                f"Cross-tier redirect rejected: official host '{initial_host}' redirected to community host '{final_host}'."
            )

        status = data.get("status", 200)
        if status >= 400:
            raise ValueError(f"Page fetch failed with HTTP status {status}")

        raw_text = data.get("text") or ""
        normalized_text = " ".join(raw_text.split())
        if not normalized_text:
            raise ValueError("Page body rendered empty text or could not be loaded")

        for pattern in _CHALLENGE_PATTERNS:
            if pattern.search(normalized_text):
                raise ValueError("Page content indicates human verification, captcha, or access denied challenge")

        if final_host in _ALLOWED_COMMUNITY_HOSTS:
            verification_status = "COMMUNITY_NARRATIVE"
            source_type = "community_forum"
        else:
            verification_status = "UNVERIFIED"
            source_type = "official_public_filing"

        summary = normalized_text[:1000]
        extracted_claims = [summary] if summary else []

        content_bytes = normalized_text.encode("utf-8")
        content_sha256 = hashlib.sha256(content_bytes).hexdigest()

        id_material = f"{final_url}|{content_sha256}".encode("utf-8")
        deterministic_id = hashlib.sha256(id_material).hexdigest()

        observed_at = now if now is not None else datetime.now(timezone.utc)

        self.last_receipt = {
            "url": final_url,
            "observed_at": observed_at.isoformat(),
            "content_sha256": content_sha256,
            "status": status,
            "source_type": source_type,
            "limitations": "unauthenticated_bounded_render_only",
        }

        item = ResearchItem(
            id=deterministic_id,
            url=final_url,
            title=data.get("title") or "",
            observed_at=observed_at,
            extracted_claims=extracted_claims,
            provenance="public_browser_sidecar",
            related_symbols=[],
            verification_status=verification_status,
            promoted_to_experiment=False,
        )

        return [item]
