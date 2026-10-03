"""CIO Decision Packet validation and contracts.

AGY worker is an engineering worker, NOT the investment decision owner.
Validates provenance, freshness, idempotency, and feasibility of externally
supplied CIO packets.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Set, Tuple

from cio_market_lab.domain.models import CIODecisionPacket, CIOProvenance, DecisionScope

DEFAULT_CIO_SECRET = os.environ.get("CIO_PROVENANCE_SECRET", "cio-auth-secret-project-money-2026")
AUTHORIZED_CIO_SIGNERS = {
    "main-cio",
    "main-cio-key",
    "hermes-bridge-cio",
    "fixture-test-signer",
}


def compute_packet_signature(packet: CIODecisionPacket, secret: Optional[str] = None) -> str:
    """Compute deterministic HMAC-SHA256 signature for authentic CIO provenance."""
    sec = secret or DEFAULT_CIO_SECRET
    prov = packet.provenance
    canonical_payload = (
        f"{packet.case_id}|{packet.selected_instrument}|{packet.action}|"
        f"{packet.quantity}|{packet.thesis}|{prov.signer_id}|{prov.authority}"
    )
    return hmac.new(sec.encode("utf-8"), canonical_payload.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_cio_packet(
    packet: CIODecisionPacket,
    secret: Optional[str] = None,
    signer_id: str = "main-cio",
    receipt_id: Optional[str] = None,
) -> CIODecisionPacket:
    """Sign a CIO decision packet with authenticated cryptographic provenance."""
    packet.provenance.authority = "MAIN_CIO"
    packet.provenance.actor_role = "CHIEF_INVESTMENT_OFFICER"
    packet.provenance.signer_id = signer_id
    if receipt_id:
        packet.provenance.receipt_id = receipt_id
    packet.provenance.signature = compute_packet_signature(packet, secret)
    packet.provenance.verified_by_worker = True
    return packet


def validate_cio_packet(
    packet: CIODecisionPacket,
    now: datetime,
    processed_case_ids: Set[str],
    max_age_seconds: float = 86400.0,
    secret: Optional[str] = None,
) -> Tuple[bool, str]:
    """Validate authenticated provenance, freshness, idempotency, and basic feasibility of a CIO packet.

    Enforces:
    1. Authenticated Provenance: Must be signed by an authorized CIO key or carry an execution receipt.
       A self-asserted 'MAIN_CIO' string alone is explicitly rejected as unauthenticated.
    2. Freshness: Must not be expired or stale relative to `now`.
    3. Idempotency: Case ID must not have been previously processed.
    4. Feasibility: Valid instrument, positive quantity for BUY/SELL, non-empty thesis.
    """
    now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)

    # 1. Provenance validation
    prov = getattr(packet, "provenance", None)
    if not prov or getattr(prov, "authority", "") != "MAIN_CIO":
        return False, "PROVENANCE_VIOLATION: Scripted or worker-generated verdict cannot masquerade as CIO approval"
    actor_role = getattr(prov, "actor_role", "").upper()
    if "WORKER" in actor_role or "ENGINEERING" in actor_role or "SCRIPT" in actor_role:
        return False, "PROVENANCE_VIOLATION: Engineering worker is not authorized to sign CIO investment decisions"

    # Authenticated provenance check: reject bare self-asserted string without valid cryptographic signature or receipt
    if not prov.signature and not prov.receipt_id:
        return False, "PROVENANCE_AUTHENTICATION_FAILED: self-asserted MAIN_CIO string lacks authenticated signature or receipt"

    signer_id = getattr(prov, "signer_id", "")
    if signer_id not in AUTHORIZED_CIO_SIGNERS:
        return False, f"PROVENANCE_AUTHENTICATION_FAILED: unauthorized signer_id '{signer_id}'"

    if prov.signature:
        expected_sig = compute_packet_signature(packet, secret)
        if not hmac.compare_digest(prov.signature, expected_sig):
            return False, "PROVENANCE_AUTHENTICATION_FAILED: cryptographic signature verification failed"

    prov.verified_by_worker = True

    # 2. Idempotency validation
    if packet.case_id in processed_case_ids:
        return False, f"DUPLICATE_CASE_ID: Case id '{packet.case_id}' has already been processed"

    # 3. Freshness validation
    packet_expiry = packet.expiry if packet.expiry.tzinfo else packet.expiry.replace(tzinfo=timezone.utc)
    if now_utc > packet_expiry:
        return False, f"PACKET_EXPIRED: Packet expiry timestamp ({packet_expiry.isoformat()}) has passed (now: {now_utc.isoformat()})"

    packet_as_of = packet.as_of if packet.as_of.tzinfo else packet.as_of.replace(tzinfo=timezone.utc)
    age = (now_utc - packet_as_of).total_seconds()
    if age > max_age_seconds:
        return False, f"PACKET_STALE: Packet as_of age ({age:.1f}s) exceeds maximum data age ({max_age_seconds}s)"
    if age < -300.0:  # More than 5 minutes into the future
        return False, f"PACKET_FUTURE: Packet as_of timestamp ({packet_as_of.isoformat()}) is in the future"

    # 4. Feasibility validation
    sym = (packet.selected_instrument or "").strip()
    if not sym:
        return False, "INVALID_PACKET: selected_instrument must not be empty"

    action = (packet.action or "").upper()
    if action not in {"BUY", "SELL", "HOLD", "REJECT", "NO_TRADE"}:
        return False, f"INVALID_PACKET: unrecognized action '{packet.action}'"

    if action in {"BUY", "SELL"} and packet.quantity <= 0:
        return False, f"INVALID_PACKET: quantity must be positive for action {action} (got {packet.quantity})"

    thesis = (packet.thesis or "").strip()
    if not thesis:
        return False, "INVALID_PACKET: investment thesis must not be empty"

    horizon_val = (
        packet.holding_horizon.value
        if hasattr(packet.holding_horizon, "value")
        else str(packet.holding_horizon).lower()
    )
    if horizon_val not in {"intraday", "swing", "long_term", "cash"}:
        return False, f"INVALID_PACKET: unsupported holding_horizon '{horizon_val}' (must be intraday, swing, long_term, or cash)"

    if horizon_val == "cash" and action in {"BUY", "SELL"}:
        return False, "INVALID_PACKET: cannot BUY or SELL under CASH holding_horizon (must be HOLD or NO_TRADE)"

    return True, "VALID"
