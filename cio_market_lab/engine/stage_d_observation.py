"""Persisted official-research packet loader for the opt-in Stage D material gate.

This module performs no network access and never treats a packet's `verified` bit
as sufficient provenance. Packets are inputs only; fixture packets are rejected.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import math
from typing import Any
from urllib.parse import urlparse

from cio_market_lab.engine.cio_session import FrozenDecisionContext
from cio_market_lab.research.browser import validate_and_sanitize_evidence

_BASE_HOSTS = {
    "twse.com.tw", "www.twse.com.tw", "openapi.twse.com.tw", "mops.twse.com.tw",
    "tpex.org.tw", "www.tpex.org.tw", "mops.tpex.org.tw",
    "sec.gov", "www.sec.gov", "data.sec.gov",
}
_ALLOWED_TIERS = {
    "official_exchange", "official_filing", "regulatory_filing", "official_company_ir",
}


class PacketUnavailableError(ValueError):
    """No admissible current persisted packet exists for the requested symbol."""


def _hosts() -> set[str]:
    configured = os.environ.get("CIO_STAGE_D_OFFICIAL_HOSTS", "")
    return _BASE_HOSTS | {x.strip().lower().rstrip(".") for x in configured.split(",") if x.strip()}


def _official_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme == "https" and any(host == allowed or host.endswith("." + allowed) for allowed in _hosts())


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PacketUnavailableError("missing observed_at")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PacketUnavailableError("invalid observed_at") from exc
    if dt.tzinfo is None:
        raise PacketUnavailableError("observed_at requires timezone")
    return dt.astimezone(timezone.utc)


def _validate_packet(packet: Any, symbol: str, now: datetime, max_age_seconds: float, *, allow_fixture: bool = False) -> dict[str, Any]:
    if not isinstance(packet, dict):
        raise PacketUnavailableError("packet must be a JSON object")
    if not allow_fixture and (packet.get("is_fixture") is True or packet.get("test_only") is True or packet.get("source_mode") in {"fake", "fixture", "synthetic", "mock"}):
        raise PacketUnavailableError("TEST_ONLY/fixture packets are not accepted")
    if packet.get("verified") is not True or packet.get("verification_status") != "verified":
        raise PacketUnavailableError("packet is not explicitly verified")
    if str(packet.get("symbol", "")).strip().upper() != symbol.upper():
        raise PacketUnavailableError("packet symbol mismatch")
    market = str(packet.get("market", "")).upper()
    if market not in {"TW", "US"}:
        raise PacketUnavailableError("market must be TW or US")
    tier = str(packet.get("source_tier", ""))
    if tier not in _ALLOWED_TIERS:
        raise PacketUnavailableError("source_tier is not an accepted official tier")
    source_url = str(packet.get("source_url", ""))
    if not _official_url(source_url):
        raise PacketUnavailableError("source URL host is not on the official allowlist")
    observed = _parse_time(packet.get("observed_at"))
    age = (now - observed).total_seconds()
    if age < 0 or age > max_age_seconds:
        raise PacketUnavailableError("packet is future-dated or stale")
    facts = packet.get("verified_facts")
    if not isinstance(facts, list) or not facts or not all(isinstance(x, str) and x.strip() for x in facts):
        raise PacketUnavailableError("verified_facts must contain non-empty strings")
    for field in ("thesis", "invalidation"):
        if not isinstance(packet.get(field), str) or not packet[field].strip():
            raise PacketUnavailableError(f"missing {field}")
    zone = packet.get("buy_zone")
    unarmed = (packet.get("research_only") is True and packet.get("stance") in {"WAIT", "REJECT"}
               and packet.get("exposure_ceiling") == 0 and bool(packet.get("missing_evidence")))
    if unarmed:
        if zone is not None or packet.get("invalidation_condition") is not None:
            raise PacketUnavailableError("research-only plan must not claim numeric price triggers")
    elif not isinstance(zone, dict) or not all(isinstance(zone.get(k), (float, int)) and not isinstance(zone.get(k), bool) and math.isfinite(zone[k]) and zone[k] > 0 for k in ("low", "high")) or zone["low"] > zone["high"]:
        raise PacketUnavailableError("missing buy_zone")
    # Reuse canonical sanitizer for symbol/facts/time/id checks, while adding a
    # strict host allowlist above (the existing general inbox schema is broader).
    evidence = {**packet, "research_id": packet.get("research_id", ""), "research_scope": packet.get("research_scope", "event_input_only_not_order")}
    if allow_fixture:
        evidence = {**evidence, "is_fixture": False, "test_only": False, "source_mode": "offline_test"}
    ok, sanitized, reason = validate_and_sanitize_evidence(evidence, now=now, max_age_seconds=max_age_seconds)
    if not ok or sanitized is None:
        raise PacketUnavailableError(reason)
    return {**packet, "symbol": symbol.upper(), "market": market, "observed_at": observed.isoformat(),
            "source_url": source_url, "source_tier": tier, "verified_facts": [x.strip() for x in facts]}


class PersistedResearchPacketLoader:
    def __init__(self, packet_root: Path, max_age_seconds: float = 36 * 3600, *, trusted_manifest: Path | None = None, allow_fixture: bool = False) -> None:
        self.packet_root = Path(packet_root).resolve()
        self.max_age_seconds = max_age_seconds
        self.allow_fixture = allow_fixture
        self.trusted_manifest = Path(trusted_manifest).resolve() if trusted_manifest else None
        if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise PacketUnavailableError("invalid freshness limit")
        if self.trusted_manifest and self.trusted_manifest.is_relative_to(self.packet_root):
            raise PacketUnavailableError("trust manifest must be outside untrusted packet root")

    def _verify_approval(self, packet: dict[str, Any]) -> None:
        # Hashes bind an independently approved source capture to the exact note;
        # they do NOT by themselves establish web-source truth or fetch authenticity.
        if self.allow_fixture:
            if packet.get("test_only") is not True:
                raise PacketUnavailableError("fixture mode requires explicit TEST_ONLY packet")
            return
        if self.trusted_manifest is None:
            raise PacketUnavailableError("independent trusted source approval is required")
        try:
            approvals = json.loads(self.trusted_manifest.read_text())
            canonical = json.dumps(packet, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            digest = hashlib.sha256(canonical).hexdigest()
            approval = approvals["approved_packets"][digest]
            if approvals.get("test_only") or approval.get("approved_by") != "MAIN_CIO" or approval.get("source_verified") is not True:
                raise PacketUnavailableError("source approval missing or fixture-only")
            if approval.get("source_url") != packet.get("source_url"):
                raise PacketUnavailableError("source approval URL mismatch")
            base = self.trusted_manifest.parent
            source = (base / approval["capture_file"]).resolve()
            if not source.is_relative_to(base):
                raise PacketUnavailableError("capture path escapes trusted source root")
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != approval["capture_sha256"]:
                raise PacketUnavailableError("source capture digest mismatch")
            excerpts = packet.get("source_excerpts")
            if not isinstance(excerpts, list) or not excerpts or not all(isinstance(x, str) and x.strip() and x in data.decode("utf-8") for x in excerpts):
                raise PacketUnavailableError("source excerpts not bound to capture")
        except PacketUnavailableError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PacketUnavailableError("trusted approval unavailable or packet not approved") from exc

    def load(self, symbol: str, now: datetime) -> dict[str, Any]:
        if not self.packet_root.is_dir():
            raise PacketUnavailableError("packet root is missing")
        matches: list[dict[str, Any]] = []
        for path in sorted(self.packet_root.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            candidates = raw if isinstance(raw, list) else [raw]
            for candidate in candidates:
                if isinstance(candidate, dict) and str(candidate.get("symbol", "")).upper() == symbol.upper():
                    validated = _validate_packet(candidate, symbol, now, self.max_age_seconds, allow_fixture=self.allow_fixture)
                    self._verify_approval(candidate)
                    matches.append(validated)
        if not matches:
            raise PacketUnavailableError(f"no persisted verified packet for {symbol}")
        # Prefer newest valid packet; reject ambiguous equal-time records rather
        # than depending on filesystem order.
        matches.sort(key=lambda x: x["observed_at"], reverse=True)
        if len(matches) > 1 and matches[0]["observed_at"] == matches[1]["observed_at"] and matches[0] != matches[1]:
            raise PacketUnavailableError("ambiguous equal-time packets")
        return matches[0]


def build_canonical_observation(packet: dict[str, Any], symbol: str, session_id: str) -> tuple[dict[str, Any], FrozenDecisionContext]:
    """Create stable semantic material identity; timestamps/quotes do not enter the digest."""
    semantic = {k: packet[k] for k in ("symbol", "market", "source_url", "source_tier", "verified_facts", "thesis", "buy_zone", "invalidation")}
    for field in ("valuation_scenarios", "catalysts", "exposure_ceiling", "stance", "missing_evidence"):
        if field in packet:
            semantic[field] = packet[field]
    material_id = hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    # Numeric rules are explicit source-approved inputs, never parsed from prose.
    condition = packet.get("invalidation_condition")
    if condition is not None:
        semantic["invalidation_condition"] = condition
        material_id = hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    context_date = packet.get("plan_session_date", packet["observed_at"][:10])
    context_id = f"{session_id}-{symbol.upper()}-{context_date}-{material_id[:12]}"
    context = FrozenDecisionContext(
        context_id=context_id, session_date=context_date,
        official_source_lineage=[{"research_id": packet.get("research_id"), "source_url": packet["source_url"],
                                  "source_tier": packet["source_tier"], "observed_at": packet["observed_at"],
                                  "verified_facts": packet["verified_facts"]}],
        thesis=packet["thesis"], valuation_scenarios=packet.get("valuation_scenarios", {}),
        catalysts=packet.get("catalysts", []), entry_zone=packet["buy_zone"] or {},
        invalidation={"rule": packet["invalidation"], "condition": condition}, exposure_ceiling=float(packet.get("exposure_ceiling", 0.02)),
    )
    observation = {"symbol": symbol.upper(), "session_id": session_id,
                   "official_material_ids": [material_id], "packet_observed_at": packet["observed_at"],
                   "market": packet["market"], "source_url": packet["source_url"],
                   "source_tier": packet["source_tier"], "verified_facts": packet["verified_facts"],
                   "thesis": packet["thesis"], "buy_zone": packet["buy_zone"], "invalidation": packet["invalidation"],
                   "research_only": packet.get("research_only", False),
                   "stance": packet.get("stance", "WAIT"),
                   "missing_evidence": list(packet.get("missing_evidence", []))}
    if packet.get("research_only"):
        observation["entry_edge"] = {"condition":"NOT_ARMED_MISSING_EVIDENCE","triggered":False,"quote":None}
    else:
        observation["entry_edge"] = {"condition": "price_within_buy_zone", "low": packet["buy_zone"]["low"], "high": packet["buy_zone"]["high"], "triggered": None, "quote": None}
    observation["invalidation_edge"] = {"condition": "unsupported_freeform_rule", "triggered": None, "reason": "structured price condition unavailable", "rule": packet["invalidation"]}
    observation["invalidation_condition"] = condition
    return observation, context


def apply_verified_quote_edges(observation: dict[str, Any], quote: Any, now: datetime, *,
                               max_age_seconds: float = 300, allow_fixture: bool = False) -> dict[str, Any]:
    """Evaluate explicit price rules; no inference, network or prose interpretation.

    A passed quote is already sourced by the canonical adapter. Provenance,
    symbol, source-time and observed-time are independently checked here.
    TEST_ONLY inputs require an explicit isolated caller opt-in.
    """
    result = dict(observation)
    result.pop("entry_triggered", None)
    result.pop("invalidation_triggered", None)
    raw = quote.model_dump(mode="json") if hasattr(quote, "model_dump") else quote
    try:
        if not isinstance(raw, dict):
            raise PacketUnavailableError("quote missing")
        if raw.get("symbol") != observation.get("symbol"):
            raise PacketUnavailableError("quote symbol mismatch")
        source = str(raw.get("source", "")).lower()
        quality = str(raw.get("quality", "")).lower()
        fixture = any(x in source for x in ("fixture", "mock", "test")) or raw.get("test_only") is True or raw.get("is_fixture") is True
        if fixture and not allow_fixture:
            raise PacketUnavailableError("TEST_ONLY quote rejected")
        if raw.get("is_stale") or raw.get("is_synthetic") or raw.get("verified") is False:
            raise PacketUnavailableError("stale/synthetic/unverified quote")
        if not source or source == "missing" or any(x in source for x in ("synthetic", "fallback", "replay")):
            raise PacketUnavailableError("untrusted quote source")
        # The public last-sale adapter can supply review signals, never proof
        # of an executable bid/ask or a fill. Qualify this exact adapter pair.
        qualified_public_last_sale = (
            source == "cnbc_nasdaq_last_sale" and quality == "public_reported_last_sale"
        )
        if quality not in {"good", "fresh", "delayed", "delayed_valid", "authoritative"} and not qualified_public_last_sale:
            raise PacketUnavailableError("unverified quote quality")
        if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise PacketUnavailableError("invalid quote freshness limit")
        for stamp in (raw.get("observed_at"), raw.get("bar_time") or raw.get("timestamp")):
            age = (now - _parse_time(stamp)).total_seconds()
            if age < 0 or age > max_age_seconds:
                raise PacketUnavailableError("quote source/observation timestamp stale or future")
        price = raw.get("last_price")
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            raise PacketUnavailableError("invalid quote price")
        zone = observation["buy_zone"]
        if observation.get("research_only") and zone is None:
            result["quote_edge_status"] = "RESEARCH_ONLY_NO_PRICE_TRIGGER"
            result["entry_triggered"] = False
            result["invalidation_triggered"] = False
            return result
        low, high = zone["low"], zone["high"]
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0 for x in (low, high)) or low > high:
            raise PacketUnavailableError("invalid entry zone")
    except (PacketUnavailableError, KeyError, TypeError, ValueError) as exc:
        result["quote_edge_status"] = "BLOCKED_QUOTE_UNAVAILABLE"
        result["quote_edge_reason"] = str(exc)
        return result
    result["quote_edge_status"] = "TEST_ONLY" if fixture else "VALIDATED_ADAPTER_QUOTE"
    result["entry_triggered"] = low <= price <= high
    result["entry_edge"] = {"condition": "price_within_buy_zone", "low": low, "high": high,
                            "triggered": result["entry_triggered"], "quote": raw}
    rule = observation.get("invalidation_condition")
    operators = {"lt": lambda a, b: a < b, "lte": lambda a, b: a <= b,
                 "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b}
    threshold = rule.get("threshold") if isinstance(rule, dict) else None
    if (isinstance(rule, dict) and rule.get("field") == "last_price" and rule.get("operator") in operators
            and isinstance(threshold, (int, float)) and not isinstance(threshold, bool)
            and math.isfinite(threshold) and threshold > 0):
        result["invalidation_triggered"] = operators[rule["operator"]](price, threshold)
        result["invalidation_edge"] = {"condition": rule, "triggered": result["invalidation_triggered"], "quote": raw}
    else:
        result["invalidation_edge"] = {"condition": "unsupported_freeform_rule", "triggered": None,
                                       "reason": "explicit supported last_price rule required",
                                       "rule": observation.get("invalidation")}
    return result


def make_packet_observation_provider(packet_root: Path, max_age_seconds: float = 36 * 3600, *, trusted_manifest: Path | None = None, allow_fixture: bool = False):
    loader = PersistedResearchPacketLoader(packet_root, max_age_seconds, trusted_manifest=trusted_manifest, allow_fixture=allow_fixture)
    contexts: dict[str, FrozenDecisionContext] = {}

    def provide(*, symbol: str, inputs: dict[str, Any], now: datetime) -> dict[str, Any]:
        packet = loader.load(symbol, now)
        observation, context = build_canonical_observation(packet, symbol, "project-money-main-cio")
        contexts[symbol.upper()] = context
        return observation

    provide.context_for = lambda symbol: contexts.get(symbol.upper())
    return provide
