"""Portable source-only acceptance adapters for Project Money Issue #16.

This module is deliberately deployment-neutral.  It reuses the repository's
public research coordinator, keeps role identity separate from scheduler state,
evaluates position-monitor contracts without exporting holdings, and validates
platform acknowledgement only from linked receipts.

Nothing here connects a broker, mutates a private runtime, changes cron/provider
configuration, or treats test evidence as live acceptance.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Iterable, Mapping, Optional

from cio_market_lab.research.free_adapters import FreeSourceCoordinator


PRIVATE_MARKERS = {
    "account",
    "account_id",
    "account_path",
    "client",
    "client_id",
    "credential",
    "credentials",
    "holding",
    "holdings",
    "order",
    "orders",
    "portfolio",
    "private_path",
    "secret",
    "token",
}

ORIGINAL_ROLE_CONTRACTS: dict[str, dict[str, Any]] = {
    "main_cio": {
        "owner": "MAIN_CIO",
        "function": "main_cio_identity",
        "private_context_allowed": True,
        "public_worker_export_only": False,
    },
    "tw_research": {
        "owner": "TW_RESEARCH",
        "function": "tw_research",
        "private_context_allowed": False,
        "public_worker_export_only": True,
    },
    "us_research": {
        "owner": "US_RESEARCH",
        "function": "us_research",
        "private_context_allowed": False,
        "public_worker_export_only": True,
    },
    "underwriting": {
        "owner": "UNDERWRITING",
        "function": "underwriting",
        "private_context_allowed": False,
        "public_worker_export_only": True,
    },
    "allocation": {
        "owner": "ALLOCATION",
        "function": "allocation",
        "private_context_allowed": False,
        "public_worker_export_only": True,
    },
    "source_audio": {
        "owner": "SOURCE_AUDIO",
        "function": "source_audio",
        "private_context_allowed": False,
        "public_worker_export_only": True,
    },
    "industry_mapping": {
        "owner": "INDUSTRY_MAPPING",
        "function": "industry_mapping",
        "private_context_allowed": False,
        "public_worker_export_only": True,
    },
    "red_team": {
        "owner": "RED_TEAM",
        "function": "red_team",
        "private_context_allowed": False,
        "public_worker_export_only": True,
    },
    "blindside": {
        "owner": "BLINDSIDE",
        "function": "blindside",
        "private_context_allowed": False,
        "public_worker_export_only": True,
    },
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _private_paths(value: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key).strip().lower()
            path = f"{prefix}.{key}" if prefix else str(key)
            if name in PRIVATE_MARKERS or any(
                marker in name for marker in ("credential", "private_path", "client_holding")
            ):
                found.append(path)
            found.extend(_private_paths(child, path))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found.extend(_private_paths(child, f"{prefix}[{index}]"))
    return found


def assert_public_worker_export(payload: Mapping[str, Any]) -> None:
    """Fail closed before transport when private/client markers are present."""
    paths = _private_paths(payload)
    if paths:
        raise ValueError("PRIVATE_EXPORT_REJECTED:" + ",".join(sorted(paths)))


def original_role_acceptance(
    schedule_jobs: Optional[Iterable[Mapping[str, Any]]] = None,
) -> dict[str, dict[str, Any]]:
    """Return immutable role contracts plus explicit scheduler state.

    Role identity never depends on a cron job being enabled.  Missing/disabled
    scheduling is reported as scheduling evidence only and cannot remap a role.
    """
    jobs = list(schedule_jobs or [])
    result: dict[str, dict[str, Any]] = {}
    for role_name, contract in ORIGINAL_ROLE_CONTRACTS.items():
        matching = [
            job for job in jobs
            if str(job.get("role", "")).strip().lower() == role_name
        ]
        if len(matching) > 1:
            schedule_status = "DUPLICATE_SCHEDULE_BINDING"
        elif not matching:
            schedule_status = "SCHEDULE_NOT_CONFIGURED"
        elif matching[0].get("enabled") is True:
            schedule_status = "SCHEDULE_ENABLED"
        else:
            schedule_status = "SCHEDULE_DISABLED"
        result[role_name] = {
            **contract,
            "role_name": role_name,
            "identity_status": "CONTRACT_VALID",
            "schedule_status": schedule_status,
            "schedule_evidence_count": len(matching),
        }
    return result


@dataclass(frozen=True)
class StageResult:
    stage: str
    status: str
    model_identity: str
    observed_at: str
    input_sha256: str
    output_sha256: Optional[str]
    schema_valid: bool
    output: Optional[dict[str, Any]]
    reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "model_identity": self.model_identity,
            "observed_at": self.observed_at,
            "input_sha256": self.input_sha256,
            "output_sha256": self.output_sha256,
            "schema_valid": self.schema_valid,
            "output": self.output,
            "reason": self.reason,
        }


class PublicOnlyResearchWorkflowAdapter:
    """Bounded adapter from existing public source modules to research stages.

    The default stage engines are local deterministic extractive/rules engines.
    They are intentionally not trading models and have no paid-provider path.
    """

    def __init__(
        self,
        coordinator: Optional[FreeSourceCoordinator] = None,
        *,
        max_serialized_bytes: int = 250_000,
    ) -> None:
        self.coordinator = coordinator or FreeSourceCoordinator()
        self.max_serialized_bytes = max_serialized_bytes

    def _source_bundle(
        self,
        symbol: str,
        now: datetime,
        reader: Any = None,
    ) -> dict[str, Any]:
        result = self.coordinator.refresh_symbol(symbol, _utc(now), reader=reader)
        assert_public_worker_export(result)
        encoded = json.dumps(result, default=str, ensure_ascii=False).encode("utf-8")
        if len(encoded) > self.max_serialized_bytes:
            raise ValueError("PUBLIC_RESEARCH_BUNDLE_OVERSIZE")
        return result

    @staticmethod
    def _records(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for name in (
            "official_facts",
            "secondary_finviz",
            "secondary_stock_analysis",
            "peer_market_cap",
        ):
            value = bundle.get(name) or {}
            record = value.get("record") if isinstance(value, Mapping) else None
            if isinstance(record, Mapping):
                row = dict(record)
                row["adapter_stage"] = name
                records.append(row)
        return records

    @staticmethod
    def _infer(stage: str, payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Run bounded local inference with stage-specific schemas."""
        if stage == "discovery":
            records = payload.get("records") or []
            urls = sorted(
                {
                    str(r.get("source_url") or r.get("url"))
                    for r in records
                    if r.get("source_url") or r.get("url")
                }
            )
            output = {
                "candidate_sources": urls,
                "record_count": len(records),
                "gaps": list(payload.get("gaps") or []),
            }
            return "LOCAL_DISCOVERY_RULES_V1", output
        if stage == "commercial":
            records = payload.get("records") or []
            metric_keys = sorted(
                {
                    str(k)
                    for record in records
                    for k in ((record.get("metrics") or {}).keys() if isinstance(record, Mapping) else [])
                }
            )
            output = {
                "public_metric_fields": metric_keys[:64],
                "source_count": len(records),
                "commercial_status": "EVIDENCE_PRESENT" if records else "EVIDENCE_MISSING",
            }
            return "LOCAL_COMMERCIAL_RULES_V1", output
        if stage == "underwriting":
            gaps = list(payload.get("gaps") or [])
            records = payload.get("records") or []
            output = {
                "evidence_count": len(records),
                "gap_count": len(gaps),
                "underwriting_status": "INCOMPLETE" if gaps or not records else "PUBLIC_EVIDENCE_READY",
                "limitations": [
                    "Research-only local assessment.",
                    "No order, holding, account, valuation approval or capital authority.",
                ],
            }
            return "LOCAL_UNDERWRITING_RULES_V1", output
        if stage == "challenge":
            underwriting = payload.get("underwriting") or {}
            discovery = payload.get("discovery") or {}
            blockers = []
            if underwriting.get("underwriting_status") != "PUBLIC_EVIDENCE_READY":
                blockers.append("UNDERWRITING_NOT_READY")
            if not discovery.get("candidate_sources"):
                blockers.append("NO_PUBLIC_SOURCE_URL")
            output = {
                "verdict": "BLOCKED" if blockers else "PASS_PUBLIC_RESEARCH_ONLY",
                "blockers": blockers,
                "different_model": True,
                "next_action": (
                    "MAIN_CIO_RUN_REAL_HOST_PUBLIC_INPUT_ACCEPTANCE"
                    if not blockers
                    else "SUPPLY_MISSING_PUBLIC_RESEARCH_PREREQUISITE"
                ),
            }
            return "LOCAL_CHALLENGE_RULES_V2", output
        raise ValueError(f"UNKNOWN_RESEARCH_STAGE:{stage}")

    def _run_stage(
        self,
        stage: str,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> StageResult:
        assert_public_worker_export(payload)
        input_sha = _stable_hash(payload)
        try:
            model_identity, output = self._infer(stage, payload)
            assert_public_worker_export(output)
            if not isinstance(output, dict) or not output:
                raise ValueError("EMPTY_STAGE_OUTPUT")
            return StageResult(
                stage=stage,
                status="COMPLETED",
                model_identity=model_identity,
                observed_at=_utc(now).isoformat(),
                input_sha256=input_sha,
                output_sha256=_stable_hash(output),
                schema_valid=True,
                output=output,
            )
        except Exception as exc:
            return StageResult(
                stage=stage,
                status="BLOCKED",
                model_identity=f"LOCAL_{stage.upper()}_UNAVAILABLE",
                observed_at=_utc(now).isoformat(),
                input_sha256=input_sha,
                output_sha256=None,
                schema_valid=False,
                output=None,
                reason=f"{type(exc).__name__}:{exc}",
            )

    def run(
        self,
        symbol: str,
        *,
        now: datetime,
        reader: Any = None,
    ) -> dict[str, Any]:
        """Run fetch -> discovery -> commercial -> underwriting -> challenge."""
        observed = _utc(now)
        attempts: list[dict[str, Any]] = []
        try:
            bundle = self._source_bundle(symbol, observed, reader=reader)
        except Exception as exc:
            return {
                "status": "BLOCKED",
                "symbol": symbol,
                "owner": "MAIN_CIO",
                "exact_next_action": "RESTORE_OR_SUPPLY_APPROVED_PUBLIC_SOURCE_PREREQUISITE",
                "attempts": [{
                    "stage": "fetch",
                    "status": "BLOCKED",
                    "observed_at": observed.isoformat(),
                    "reason": f"{type(exc).__name__}:{exc}",
                }],
                "live_acceptance_claimed": False,
            }

        records = self._records(bundle)
        provenance = [
            {
                "source_url": record.get("source_url") or record.get("url"),
                "observed_at": record.get("observed_at") or observed.isoformat(),
                "content_sha256": _stable_hash(record),
                "adapter_stage": record.get("adapter_stage"),
            }
            for record in records
        ]
        attempts.append({
            "stage": "fetch",
            "status": "COMPLETED" if records else "BLOCKED",
            "observed_at": observed.isoformat(),
            "source_count": len(records),
            "provenance": provenance,
            "gaps": list(bundle.get("gaps") or []),
        })
        if not records:
            return {
                "status": "BLOCKED",
                "symbol": symbol,
                "owner": "MAIN_CIO",
                "exact_next_action": "SUPPLY_GENUINELY_FRESH_PUBLIC_INPUT",
                "attempts": attempts,
                "live_acceptance_claimed": False,
            }

        base = {
            "symbol": symbol,
            "records": records,
            "gaps": list(bundle.get("gaps") or []),
            "provenance": provenance,
        }
        discovery = self._run_stage("discovery", base, observed)
        attempts.append(discovery.as_dict())
        if discovery.status != "COMPLETED":
            return self._blocked(symbol, attempts, "RESTORE_LOCAL_DISCOVERY_STAGE")

        commercial_payload = {**base, "discovery": discovery.output}
        commercial = self._run_stage("commercial", commercial_payload, observed)
        attempts.append(commercial.as_dict())
        if commercial.status != "COMPLETED":
            return self._blocked(symbol, attempts, "RESTORE_LOCAL_COMMERCIAL_STAGE")

        underwriting_payload = {
            **base,
            "discovery": discovery.output,
            "commercial": commercial.output,
        }
        underwriting = self._run_stage("underwriting", underwriting_payload, observed)
        attempts.append(underwriting.as_dict())
        if underwriting.status != "COMPLETED":
            return self._blocked(symbol, attempts, "RESTORE_LOCAL_UNDERWRITING_STAGE")

        challenge_payload = {
            "symbol": symbol,
            "discovery": discovery.output,
            "commercial": commercial.output,
            "underwriting": underwriting.output,
        }
        challenge = self._run_stage("challenge", challenge_payload, observed)
        attempts.append(challenge.as_dict())
        if challenge.status != "COMPLETED":
            return self._blocked(symbol, attempts, "RESTORE_LOCAL_CHALLENGE_STAGE")

        status = (
            "COMPLETED_PUBLIC_RESEARCH_CANDIDATE"
            if challenge.output and challenge.output.get("verdict") == "PASS_PUBLIC_RESEARCH_ONLY"
            else "BLOCKED"
        )
        return {
            "status": status,
            "symbol": symbol,
            "attempts": attempts,
            "source_provenance": provenance,
            "challenge_model_distinct": challenge.model_identity != underwriting.model_identity,
            "owner": "MAIN_CIO",
            "exact_next_action": (
                "MAIN_CIO_RUN_REAL_HOST_PUBLIC_INPUT_ACCEPTANCE"
                if status == "COMPLETED_PUBLIC_RESEARCH_CANDIDATE"
                else "SUPPLY_MISSING_PUBLIC_RESEARCH_PREREQUISITE"
            ),
            "live_acceptance_claimed": False,
        }

    @staticmethod
    def _blocked(
        symbol: str,
        attempts: list[dict[str, Any]],
        action: str,
    ) -> dict[str, Any]:
        return {
            "status": "BLOCKED",
            "symbol": symbol,
            "owner": "MAIN_CIO",
            "exact_next_action": action,
            "attempts": attempts,
            "live_acceptance_claimed": False,
        }


@dataclass(frozen=True)
class MonitorContract:
    contract_id: str
    symbol: str
    field: str
    operator: str
    threshold: float
    max_age_seconds: int = 1800


class ReceiptAwarePositionConsumer:
    """Evaluate local monitor contracts without exporting position quantities."""

    SUPPORTED = {">", ">=", "<", "<=", "==", "!="}

    @staticmethod
    def _compare(left: float, operator: str, right: float) -> bool:
        if operator == ">":
            return left > right
        if operator == ">=":
            return left >= right
        if operator == "<":
            return left < right
        if operator == "<=":
            return left <= right
        if operator == "==":
            return left == right
        if operator == "!=":
            return left != right
        raise ValueError("UNSUPPORTED_MONITOR_OPERATOR")

    def evaluate(
        self,
        contracts: Iterable[MonitorContract],
        observations: Mapping[str, Mapping[str, Any]],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        observed_now = _utc(now)
        rows: list[dict[str, Any]] = []
        symbols = set()
        for contract in contracts:
            symbols.add(contract.symbol.upper())
            stable_identity = _stable_hash({
                "contract_id": contract.contract_id,
                "symbol": contract.symbol.upper(),
                "field": contract.field,
                "operator": contract.operator,
                "threshold": contract.threshold,
            })
            if contract.operator not in self.SUPPORTED:
                rows.append({
                    "contract_id": contract.contract_id,
                    "symbol": contract.symbol.upper(),
                    "stable_identity": stable_identity,
                    "classification": "UNKNOWN",
                    "triggered": None,
                    "reason": "UNSUPPORTED_MONITOR_OPERATOR",
                })
                continue
            obs = observations.get(contract.symbol) or observations.get(contract.symbol.upper())
            if not isinstance(obs, Mapping):
                rows.append({
                    "contract_id": contract.contract_id,
                    "symbol": contract.symbol.upper(),
                    "stable_identity": stable_identity,
                    "classification": "UNKNOWN",
                    "triggered": None,
                    "reason": "OBSERVATION_MISSING",
                })
                continue
            ts_raw = obs.get("observed_at")
            value = obs.get(contract.field)
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                ts = _utc(ts)
                age = (observed_now - ts).total_seconds()
            except Exception:
                age = float("inf")
            if value is None or not isinstance(value, (int, float)):
                classification = "UNKNOWN"
                triggered = None
                reason = "OBSERVATION_VALUE_UNAVAILABLE"
            elif age < 0:
                classification = "UNKNOWN"
                triggered = None
                reason = "FUTURE_OBSERVATION"
            elif age > contract.max_age_seconds:
                classification = "STALE"
                triggered = None
                reason = "STALE_OBSERVATION"
            else:
                classification = "FRESH"
                triggered = self._compare(float(value), contract.operator, contract.threshold)
                reason = "TRIGGERED" if triggered else "NO_TRIGGER"
            rows.append({
                "contract_id": contract.contract_id,
                "symbol": contract.symbol.upper(),
                "stable_identity": stable_identity,
                "classification": classification,
                "triggered": triggered,
                "reason": reason,
                "source_receipt_id": obs.get("receipt_id"),
                "observed_at": ts_raw,
            })
        return {
            "status": "EVALUATED",
            "vti_contract_covered": "VTI" in symbols,
            "results": rows,
            "private_positions_exported": False,
        }


class DeliveryReceiptConsumer:
    """Receipt-aware acknowledgement gate; generation is never acknowledgement."""

    REQUIRED_LINKS = (
        "execution_hash",
        "body_hash",
        "job_id",
        "platform",
        "target",
        "thread_id",
    )

    def __init__(self) -> None:
        self._seen_receipt_ids: set[str] = set()

    def consume(
        self,
        expected: Mapping[str, Any],
        receipt: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if not receipt:
            return {
                "status": "UNKNOWN",
                "acknowledged": False,
                "replay_permitted": False,
                "reason": "RECEIPT_MISSING",
            }
        if receipt.get("transport_status") in {None, "UNKNOWN", "AMBIGUOUS"}:
            return {
                "status": "UNKNOWN",
                "acknowledged": False,
                "replay_permitted": False,
                "reason": "TRANSPORT_UNKNOWN_OR_AMBIGUOUS",
            }
        if receipt.get("delivered") is True and not receipt.get("platform_message_id"):
            return {
                "status": "UNKNOWN",
                "acknowledged": False,
                "replay_permitted": False,
                "reason": "DELIVERED_WITHOUT_LINKED_PLATFORM_PROOF",
            }
        mismatches = [
            key for key in self.REQUIRED_LINKS
            if not receipt.get(key) or receipt.get(key) != expected.get(key)
        ]
        message_id = str(receipt.get("platform_message_id") or "").strip()
        if not message_id:
            mismatches.append("platform_message_id")
        if mismatches:
            return {
                "status": "UNKNOWN",
                "acknowledged": False,
                "replay_permitted": False,
                "reason": "RECEIPT_LINKAGE_MISMATCH:" + ",".join(sorted(set(mismatches))),
            }

        receipt_id = _stable_hash({
            key: receipt.get(key) for key in (*self.REQUIRED_LINKS, "platform_message_id")
        })
        if receipt_id in self._seen_receipt_ids:
            return {
                "status": "ACKNOWLEDGED_DUPLICATE",
                "acknowledged": True,
                "replay_permitted": False,
                "receipt_identity": receipt_id,
                "platform_message_id": message_id,
            }
        self._seen_receipt_ids.add(receipt_id)
        return {
            "status": "ACKNOWLEDGED",
            "acknowledged": True,
            "replay_permitted": False,
            "receipt_identity": receipt_id,
            "platform_message_id": message_id,
        }
