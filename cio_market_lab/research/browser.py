"""Browser research interfaces and test adapters for CIO Market Lab.

Implements BrowserResearchAdapter protocol, ResearchItem domain model, and
FakeBrowserResearchAdapter for deterministic testing without external network or Chrome.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple, runtime_checkable
import uuid
from pydantic import BaseModel, Field


class ResearchItem(BaseModel):
    id: str = Field(default_factory=lambda: f"res-{uuid.uuid4().hex[:8]}")
    url: str
    title: str
    observed_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    claims: List[str] = Field(default_factory=list)
    source_mode: str = "deterministic_fetch"  # "chrome_cdp", "deterministic_fetch", "fake"
    status: str = "unverified"  # "unverified", "verified", "community_narrative", "contradicted"
    related_symbols: List[str] = Field(default_factory=list)
    provenance: Dict[str, Any] = Field(default_factory=dict)
    hypothesis: Optional[str] = None


@runtime_checkable
class BrowserResearchAdapter(Protocol):
    """Protocol for browsing public market research sources under strict domain allowlist."""

    def fetch_page(
        self, url: str, allowlist: Optional[Sequence[str]] = None
    ) -> ResearchItem:
        """Fetch and parse a research URL within allowlist boundaries."""
        ...

    def intake_item(self, item: ResearchItem) -> ResearchItem:
        """Store or stage a research item into the inbox."""
        ...

    def list_inbox(self) -> List[ResearchItem]:
        """Return all staged research items."""
        ...


class FakeBrowserResearchAdapter:
    """Deterministic fake research adapter for testing and offline development."""

    def __init__(self, initial_items: Optional[List[ResearchItem]] = None):
        self._inbox: Dict[str, ResearchItem] = {}
        if initial_items:
            for item in initial_items:
                self._inbox[item.id] = item
        else:
            # Seed with sample research items
            self._seed_sample_items()

    def _seed_sample_items(self) -> None:
        item1 = ResearchItem(
            id="res-twse-001",
            url="https://mops.twse.com.tw/mops/web/t05st01",
            title="TSMC (2330.TW) Monthly Revenue Report Aug 2026",
            claims=[
                "Consolidated revenue up 28.5% YoY",
                "Advanced 2nm process yields exceed internal targets",
            ],
            source_mode="fake",
            status="verified",
            related_symbols=["2330.TW", "NVDA"],
            provenance={"source": "Official TWSE MOPS", "reproduced": True},
            hypothesis="Revenue momentum supports breakout continuation",
        )
        item2 = ResearchItem(
            id="res-reddit-002",
            url="https://www.reddit.com/r/stocks/comments/sample_semis",
            title="Discussion on US semi equipment lead times and capex",
            claims=[
                "Capex cycle extending into late 2027",
                "Supplier constraints limiting packaging volume",
            ],
            source_mode="fake",
            status="community_narrative",
            related_symbols=["NVDA", "AAPL", "2330.TW"],
            provenance={"source": "Reddit r/stocks", "caution": "Community narrative only"},
            hypothesis="Check supplier delivery lead time data before swing sizing",
        )
        self._inbox[item1.id] = item1
        self._inbox[item2.id] = item2

    def fetch_page(
        self, url: str, allowlist: Optional[Sequence[str]] = None
    ) -> ResearchItem:
        """Simulate fetching a research page safely without live network calls."""
        if allowlist:
            matched = any(domain in url for domain in allowlist)
            if not matched:
                raise PermissionError(f"URL domain '{url}' not in allowlist: {list(allowlist)}")

        item = ResearchItem(
            url=url,
            title=f"Synthetic research snapshot for {url}",
            claims=["Simulated market signal observed", "No anomalous volume detected"],
            source_mode="fake",
            status="unverified",
            provenance={"mode": "FakeBrowserResearchAdapter", "url": url},
        )
        self._inbox[item.id] = item
        return item

    def intake_item(self, item: ResearchItem) -> ResearchItem:
        """Intake and persist an item into the fake research inbox."""
        self._inbox[item.id] = item
        return item

    def list_inbox(self) -> List[ResearchItem]:
        """Return all staged research items ordered by observed_at descending."""
        return sorted(self._inbox.values(), key=lambda x: x.observed_at, reverse=True)


class PublicResearchEvidence(BaseModel):
    """Sanitized public-only research evidence adhering strictly to verification criteria."""
    research_id: str
    symbol: str
    source_url: str
    source_tier: str = "unknown_tier"
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    published_at: Optional[str] = None
    is_fixture: bool = False
    verification_status: str = "verified"  # verified, unverified, community_narrative, contradicted
    verified_facts: List[str] = Field(default_factory=list)
    research_scope: str = "event_input_only_not_order"
    limitations: List[str] = Field(default_factory=list)
    raw_metadata: Dict[str, Any] = Field(default_factory=dict)


def validate_and_sanitize_evidence(
    data: Any,
    now: Optional[datetime] = None,
    max_age_seconds: float = 86400 * 7,
) -> Tuple[bool, Optional[PublicResearchEvidence], str]:
    """Validate and sanitize public research evidence under strict verification rules.
    
    Rejects fixtures, unverified narratives, community speculation, missing facts,
    and stale timestamps. No evidence means an explicit research gap, not fabricated content.
    """
    now_utc = now if now and now.tzinfo else (now.replace(tzinfo=timezone.utc) if now else datetime.now(timezone.utc))
    
    # 1. Convert to dictionary representation if needed
    if isinstance(data, BaseModel):
        d = data.model_dump(mode="json")
    elif isinstance(data, dict):
        d = dict(data)
    else:
        return False, None, "REJECTED_INVALID_TYPE: Evidence must be a dictionary or BaseModel"

    # 1.5. Strict research_id validation to prevent path traversal and arbitrary filesystem writes
    raw_rid = str(d.get("research_id") or d.get("id") or "").strip()
    if raw_rid:
        if (
            ".." in raw_rid
            or "/" in raw_rid
            or "\\" in raw_rid
            or any(c in raw_rid for c in '<>:"|?*\0')
            or not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", raw_rid)
        ):
            return False, None, "REJECTED_PATH_TRAVERSAL: Invalid research_id contains path traversal or illegal characters"

    # 2. Reject fixtures
    if d.get("is_fixture") is True:
        return False, None, "REJECTED_FIXTURE: Item explicitly labeled as test fixture"
    if d.get("is_synthetic") is True:
        return False, None, "REJECTED_FIXTURE: Item explicitly labeled as synthetic"
    source_mode = str(d.get("source_mode", "")).lower()
    if source_mode in ("fake", "synthetic", "mock", "fixture"):
        return False, None, f"REJECTED_FIXTURE: Source mode '{source_mode}' not permitted as verified evidence"
    r_id = str(d.get("research_id") or d.get("id") or "").lower()
    s_url = str(d.get("source_url") or d.get("url") or "").strip()
    if (
        r_id.startswith("fixture-")
        or r_id.startswith("fake-")
        or r_id.startswith("mock-")
        or r_id.startswith("synthetic-")
        or r_id == "res-twse-001"
        or r_id == "res-reddit-002"
        or "fixture" in s_url.lower()
        or "mock" in s_url.lower()
        or "synthetic" in s_url.lower()
        or "reddit.com" in s_url.lower()
    ):
        return False, None, "REJECTED_FIXTURE: Fixture or synthetic pattern detected in identifier or URL"

    # 3. Require valid source URL
    if not s_url or not (s_url.startswith("http://") or s_url.startswith("https://")):
        return False, None, f"REJECTED_INVALID_URL: Source URL '{s_url}' must start with http:// or https://"

    # 4. Require non-empty symbol
    sym = str(d.get("symbol") or (d.get("related_symbols", [None])[0] if d.get("related_symbols") else "")).strip().upper()
    if not sym:
        return False, None, "REJECTED_MISSING_SYMBOL: Symbol must be specified and non-empty"

    # 5. Require verified status; reject unverified narratives
    v_status = str(d.get("verification_status") or d.get("status") or "").strip().lower()
    if v_status != "verified":
        return False, None, f"REJECTED_UNVERIFIED_NARRATIVE: Status '{v_status}' is not verified (requires 'verified')"

    # 6. Require non-empty verified facts
    raw_facts = d.get("verified_facts") or d.get("claims") or []
    if not isinstance(raw_facts, list) or len(raw_facts) == 0:
        return False, None, "REJECTED_NO_VERIFIED_FACTS: Evidence must contain at least one verified fact"
    cleaned_facts = [str(f).strip() for f in raw_facts if str(f).strip()]
    if not cleaned_facts:
        return False, None, "REJECTED_NO_VERIFIED_FACTS: Verified facts list contains only blank entries"

    # 7. Check timestamp and freshness
    raw_obs = d.get("observed_at")
    if not raw_obs:
        return False, None, "REJECTED_MISSING_TIMESTAMP: observed_at timestamp is required"
    if isinstance(raw_obs, str):
        try:
            obs_dt = datetime.fromisoformat(raw_obs.replace("Z", "+00:00"))
        except Exception as exc:
            return False, None, f"REJECTED_INVALID_TIMESTAMP: Cannot parse observed_at '{raw_obs}': {exc}"
    elif isinstance(raw_obs, datetime):
        obs_dt = raw_obs
    else:
        return False, None, f"REJECTED_INVALID_TIMESTAMP: Unsupported timestamp type '{type(raw_obs)}'"

    obs_utc = obs_dt if obs_dt.tzinfo else obs_dt.replace(tzinfo=timezone.utc)
    age = (now_utc - obs_utc).total_seconds()
    if age < 0.0:
        return False, None, f"REJECTED_FUTURE_TIMESTAMP: observed_at ({obs_utc.isoformat()}) is in the future relative to decision time ({now_utc.isoformat()})"
    historical = d.get("research_scope") == "historical_company_facts_not_catalyst"
    if historical and not (
        d.get("source_tier") in {"official_exchange", "official_filing", "regulatory_filing"}
        and urlparse(s_url).hostname in {"openapi.twse.com.tw", "www.tpex.org.tw", "data.sec.gov"}
        and "official" in str((d.get("raw_metadata") or {}).get("source", "")).lower()
    ):
        return False, None, "REJECTED_HISTORICAL_SCOPE_PROVENANCE"
    allowed_age = 730 * 86400 if historical else max_age_seconds
    if age > allowed_age:
        return False, None, f"REJECTED_STALE_EVIDENCE: Evidence age ({age:.1f}s) exceeds maximum allowed age ({max_age_seconds:.1f}s)"
    if d.get("published_at"):
        try:
            publication = datetime.fromisoformat(str(d["published_at"]).replace("Z", "+00:00"))
            publication = publication if publication.tzinfo else publication.replace(tzinfo=timezone.utc)
        except ValueError:
            return False, None, "REJECTED_INVALID_PUBLICATION_DATE"
        publication_age = (now_utc - publication).total_seconds()
        if not 0 <= publication_age <= allowed_age:
            return False, None, "REJECTED_PUBLICATION_OUTSIDE_SCOPE_WINDOW"

    # 8. Source tier: do NOT default missing/unknown provenance to official_filing
    raw_tier = str(d.get("source_tier") or "").strip()
    raw_prov = d.get("provenance") or d.get("raw_metadata") or {}
    if not raw_tier or (raw_tier == "official_filing" and not (isinstance(raw_prov, dict) and raw_prov.get("source") and "official" in str(raw_prov.get("source")).lower())):
        source_tier = raw_tier if (raw_tier and raw_tier != "official_filing") else "unknown_tier"
    else:
        source_tier = raw_tier or "unknown_tier"

    # 9. Produce sanitized PublicResearchEvidence
    cleaned_limitations = [str(l).strip() for l in d.get("limitations", []) if str(l).strip()]
    sanitized = PublicResearchEvidence(
        research_id=d.get("research_id") or d.get("id") or f"res-{uuid.uuid4().hex[:8]}",
        symbol=sym,
        source_url=s_url,
        source_tier=source_tier,
        observed_at=obs_utc,
        published_at=str(d.get("published_at")) if d.get("published_at") else None,
        is_fixture=False,
        verification_status="verified",
        verified_facts=cleaned_facts,
        research_scope=str(d.get("research_scope") or "event_input_only_not_order"),
        limitations=cleaned_limitations,
        raw_metadata=d.get("raw_metadata") or d.get("provenance") or {},
    )
    return True, sanitized, "VERIFIED"


class PublicResearchInboxReader:
    """Configurable reader for sanitized public research evidence inbox.
    
    Reads from a configured file or directory (e.g. data/runtime/research_inbox
    or CIO_RESEARCH_INBOX_DIR/CIO_RESEARCH_INBOX_PATH).
    Rejects fixtures, stale evidence, and unverified narratives.
    Reports explicit research gaps for symbols lacking verified evidence.
    """
    def __init__(
        self,
        inbox_dir: Optional[Any] = None,
        max_age_seconds: float = 86400 * 7,
    ):
        from pathlib import Path
        self.inbox_dir = Path(inbox_dir) if inbox_dir is not None else None
        self.max_age_seconds = max_age_seconds
        self._staged_evidence: Dict[str, PublicResearchEvidence] = {}
        self._rejected_records: List[Dict[str, Any]] = []

    def set_inbox_dir(self, path: Any) -> None:
        from pathlib import Path
        self.inbox_dir = Path(path) if path is not None else None

    def add_evidence(
        self,
        evidence: Any,
        now: Optional[datetime] = None,
        persist: bool = True,
    ) -> Tuple[bool, str]:
        """Directly validate, sanitize, and stage a research evidence item."""
        is_valid, sanitized, reason = validate_and_sanitize_evidence(
            evidence, now=now, max_age_seconds=self.max_age_seconds
        )
        if not is_valid or sanitized is None:
            raw_d = evidence if isinstance(evidence, dict) else (
                evidence.model_dump(mode="json") if hasattr(evidence, "model_dump") else str(evidence)
            )
            self._rejected_records.append({
                "item": raw_d,
                "reason": reason,
                "timestamp": (now or datetime.now(timezone.utc)).isoformat(),
            })
            return False, reason

        # Persist genuine verified public structured evidence to canonical inbox
        if persist and self.inbox_dir is not None:
            temp_path = None
            try:
                inbox_resolved = Path(self.inbox_dir).resolve()
                inbox_resolved.mkdir(parents=True, exist_ok=True)

                target_filename = f"{sanitized.research_id}.json"
                out_path = (inbox_resolved / target_filename).resolve()
                if out_path.parent != inbox_resolved:
                    fail_reason = "REJECTED_PATH_TRAVERSAL: Target path escapes inbox directory"
                    self._rejected_records.append({
                        "item": sanitized.model_dump(mode="json"),
                        "reason": fail_reason,
                        "timestamp": (now or datetime.now(timezone.utc)).isoformat(),
                    })
                    return False, fail_reason

                # Atomic persistence: write to temporary file in the same directory, flush/fsync, then replace
                data_to_write = sanitized.model_dump(mode="json")
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=str(inbox_resolved),
                    prefix=f".tmp_{sanitized.research_id}_",
                    suffix=".tmp",
                    delete=False,
                ) as tf:
                    temp_path = Path(tf.name)
                    json.dump(data_to_write, tf, indent=2, ensure_ascii=False)
                    tf.flush()
                    os.fsync(tf.fileno())

                temp_path.replace(out_path)
            except Exception as exc:
                if temp_path is not None and temp_path.exists():
                    try:
                        temp_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                fail_reason = f"PERSISTENCE_FAILURE: Failed to persist evidence: {exc}"
                self._rejected_records.append({
                    "item": sanitized.model_dump(mode="json"),
                    "reason": fail_reason,
                    "timestamp": (now or datetime.now(timezone.utc)).isoformat(),
                })
                return False, fail_reason

        # Stage ONLY after successful persistence (or if persist=False)
        self._staged_evidence[sanitized.research_id] = sanitized
        return True, "VERIFIED"

    def load_from_path(self, path: Any, now: Optional[datetime] = None) -> int:
        """Load evidence from a single JSON file or directory of JSON files."""
        from pathlib import Path
        import json
        p = Path(path)
        if not p.exists():
            return 0
        loaded = 0
        if p.is_file():
            try:
                with p.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if isinstance(data, list):
                        for item in data:
                            ok, _ = self.add_evidence(item, now=now, persist=False)
                            if ok:
                                loaded += 1
                    elif isinstance(data, dict):
                        ok, _ = self.add_evidence(data, now=now, persist=False)
                        if ok:
                            loaded += 1
            except Exception as exc:
                self._rejected_records.append({"file": str(p), "reason": f"PARSE_ERROR: {exc}"})
        elif p.is_dir():
            for f in sorted(p.glob("*.json")):
                loaded += self.load_from_path(f, now=now)
        return loaded

    def scan_inbox(self, now: Optional[datetime] = None) -> int:
        """Scan configured inbox directory or environment inbox path."""
        import os
        from pathlib import Path
        loaded = 0
        scanned_paths = set()
        env_path = os.getenv("CIO_RESEARCH_INBOX_PATH") or os.getenv("CIO_RESEARCH_INBOX_DIR")
        if env_path:
            p = Path(env_path).resolve()
            if p.exists() and str(p) not in scanned_paths:
                loaded += self.load_from_path(p, now=now)
                scanned_paths.add(str(p))
        if self.inbox_dir:
            p_inbox = Path(self.inbox_dir).resolve()
            if p_inbox.exists() and str(p_inbox) not in scanned_paths:
                loaded += self.load_from_path(p_inbox, now=now)
                scanned_paths.add(str(p_inbox))
        return loaded

    def get_verified_research_for_symbols(
        self,
        symbols: Sequence[str],
        now: Optional[datetime] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Retrieve verified research and identify explicit research gaps.
        
        Returns:
            Tuple of (verified_research_list, research_gaps_list).
            No evidence means an explicit research gap, not fabricated content.
        """
        self.scan_inbox(now=now)
        now_utc = now if now and now.tzinfo else (now.replace(tzinfo=timezone.utc) if now else datetime.now(timezone.utc))
        verified: List[Dict[str, Any]] = []
        gaps: List[Dict[str, Any]] = []

        # Freshness filter at query time (must enforce observed_at <= decision time)
        active_by_symbol: Dict[str, List[PublicResearchEvidence]] = {}
        for ev in self._staged_evidence.values():
            obs = ev.observed_at if ev.observed_at.tzinfo else ev.observed_at.replace(tzinfo=timezone.utc)
            age = (now_utc - obs).total_seconds()
            if 0.0 <= age <= self.max_age_seconds:
                active_by_symbol.setdefault(ev.symbol.upper(), []).append(ev)

        for sym in symbols:
            s_clean = sym.strip().upper()
            items = active_by_symbol.get(s_clean, [])
            if items:
                for item in items:
                    verified.append(item.model_dump(mode="json"))
            else:
                gaps.append({
                    "symbol": s_clean,
                    "gap_status": "EXPLICIT_RESEARCH_GAP",
                    "reason": "NO_VERIFIED_RESEARCH",
                    "required_evidence": ["official_filings", "audited_financials", "ir_announcements"],
                    "observation_time": now_utc.isoformat(),
                })
        return verified, gaps

    def list_inbox(self) -> List[Dict[str, Any]]:
        """Return all staged verified research items."""
        self.scan_inbox()
        return [item.model_dump(mode="json") for item in self._staged_evidence.values()]

    def list_rejected(self) -> List[Dict[str, Any]]:
        """Return audit trail of rejected items."""
        return list(self._rejected_records)
