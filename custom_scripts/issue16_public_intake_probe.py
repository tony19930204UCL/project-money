"""Genuine public Microsoft intake probe for Issue #16.

This probe exercises only the existing installed-run_case callback mapping through
fetch. It does not invoke inference, change providers, deploy, trade, or touch
private data.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from cio_market_lab.research.issue16_live_bridge import OriginalResearchCallbackBridge

MICROSOFT_FY26_Q4 = (
    "https://www.microsoft.com/en-us/Investor/earnings/"
    "FY-2026-Q4/press-release-webcast"
)


def _fetch_only(case_id, ticker, seed_urls, directory, fetch, generate, challenge, max_attempts):
    if ticker != "MSFT":
        raise RuntimeError("PUBLIC_INTAKE_PROBE_TICKER_MISMATCH")
    document = fetch(seed_urls[0])
    lowered = str(document.get("text") or "").lower()
    if set(document) != {"url", "text", "observed_at"}:
        raise RuntimeError("PUBLIC_INTAKE_PROBE_FETCH_ENVELOPE_INVALID")
    if document.get("url") != MICROSOFT_FY26_Q4:
        raise RuntimeError("PUBLIC_INTAKE_PROBE_SOURCE_URL_MISMATCH")
    if "revenue" not in lowered or "operating income" not in lowered:
        raise RuntimeError("PUBLIC_INTAKE_PROBE_DISCLOSURE_CONTENT_MISSING")
    return {
        "status": "INCOMPLETE",
        "reason": "genuine official fetch accepted; inference intentionally not invoked",
        "source_urls": list(seed_urls),
    }


def main() -> int:
    bridge = OriginalResearchCallbackBridge(routes={})
    result = bridge.run_installed_run_case(
        _fetch_only,
        case_id="microsoft-fy26-q4-public-intake",
        symbol="MSFT",
        seed_urls=[MICROSOFT_FY26_Q4],
        directory="/sanitized/public-intake-only",
        max_attempts=1,
        now=datetime.now(timezone.utc),
    )
    print(json.dumps(result, sort_keys=True, default=str))
    evidence = list(result.get("callback_evidence") or [])
    provenance = (
        evidence[0].get("provenance", [{}])[0]
        if evidence and evidence[0].get("stage") == "fetch"
        else {}
    )
    ok = (
        result.get("status") == "INCOMPLETE"
        and result.get("live_acceptance_claimed") is False
        and len(evidence) == 1
        and evidence[0].get("status") == "COMPLETED"
        and provenance.get("source_url") == MICROSOFT_FY26_Q4
        and provenance.get("extraction_succeeded") is True
        and isinstance(provenance.get("content_sha256"), str)
        and len(provenance["content_sha256"]) == 64
        and isinstance(provenance.get("extracted_content_sha256"), str)
        and len(provenance["extracted_content_sha256"]) == 64
    )
    return 0 if ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
