"""Issue #16 source-only bridges for the original research and position-monitor entrypoints.

This module is intentionally glue, not a replacement engine. The host owns the
original orchestration entrypoint and private contract registry. These adapters
supply bounded public-only callbacks and reuse the existing quote-edge and
receipt consumers without creating a second ACK store.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cio_market_lab.engine.cio_session import CIOSessionHistory
from cio_market_lab.research.issue16_acceptance import (
    DeliveryReceiptConsumer,
    HermesLocalInference,
    PublicOnlyResearchWorkflowAdapter,
    ReceiptAwarePositionConsumer,
    STAGE_SCHEMAS,
    _reject_private_content,
    _stable_hash,
    _utc,
)
from cio_market_lab.research.free_adapters import FreeSourceCoordinator
from cio_market_lab.research.official import _public_json


class StageRoute(BaseModel):
    """Explicit primary/fallback route contract for one research stage."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")
    primary: HermesLocalInference
    fallback: Optional[HermesLocalInference] = None


class OriginalResearchCallbackBridge:
    """Bind verified source/inference adapters into the original callback entrypoint.

    The entrypoint remains authoritative for orchestration. This class only
    supplies fetch/generate/challenge callbacks and preserves fail-closed route
    evidence. It never turns a fixture or missing route into live success.
    """

    GENERATE_STAGES = ("discovery", "commercial", "underwriting")

    def __init__(
        self,
        *,
        routes: Mapping[str, StageRoute],
        coordinator: Optional[FreeSourceCoordinator] = None,
        max_serialized_bytes: int = 250_000,
    ) -> None:
        self.routes = dict(routes)
        self.coordinator = coordinator or FreeSourceCoordinator()
        self._projection = PublicOnlyResearchWorkflowAdapter(
            coordinator=self.coordinator,
            max_serialized_bytes=max_serialized_bytes,
        )
        self.max_serialized_bytes = max_serialized_bytes
        self.callback_evidence: list[dict[str, Any]] = []

    def fetch(self, symbol: str, *, now: datetime, reader: Any = None) -> dict[str, Any]:
        observed = _utc(now)
        try:
            bundle = self.coordinator.refresh_symbol(symbol, observed, reader=reader)
            import json

            serialized = json.dumps(bundle, default=str, ensure_ascii=False).encode()
            if len(serialized) > self.max_serialized_bytes:
                raise ValueError("PUBLIC_RESEARCH_BUNDLE_OVERSIZE")
            records = self._projection._records(bundle, observed)
            official = any(
                row["source_tier"] in {
                    "official_filing",
                    "regulatory_filing",
                    "official_exchange",
                }
                for row in records
            )
            if not records or not official:
                return {
                    "status": "BLOCKED",
                    "stage": "fetch",
                    "reason": "FRESH_OFFICIAL_PUBLIC_INPUT_REQUIRED",
                    "public_evidence": records,
                    "gaps": list(bundle.get("gaps") or []),
                    "observed_at": observed.isoformat(),
                }
            return {
                "status": "COMPLETED",
                "stage": "fetch",
                "public_evidence": records,
                "gaps": list(bundle.get("gaps") or []),
                "observed_at": observed.isoformat(),
                "provenance": [
                    {
                        "source_url": row["source_url"],
                        "observed_at": row["observed_at"],
                        "content_sha256": _stable_hash(row),
                    }
                    for row in records
                ],
            }
        except Exception as exc:
            return {
                "status": "BLOCKED",
                "stage": "fetch",
                "reason": f"{type(exc).__name__}:{exc}",
                "observed_at": observed.isoformat(),
            }

    def _infer_route(
        self,
        stage: str,
        payload: Mapping[str, Any],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        route = self.routes.get(stage)
        if route is None:
            return {
                "status": "BLOCKED",
                "stage": stage,
                "reason": "INFERENCE_ROUTE_MISSING",
                "attempts": [],
                "observed_at": _utc(now).isoformat(),
            }
        schema = STAGE_SCHEMAS[stage]
        attempts: list[dict[str, Any]] = []
        for route_name, engine in (("primary", route.primary), ("fallback", route.fallback)):
            if engine is None:
                continue
            if engine.contract.is_free_or_local_authorized is not True:
                attempts.append(
                    {
                        "route": route_name,
                        "status": "BLOCKED",
                        "reason": "INFERENCE_CONTRACT_NOT_FREE_OR_LOCAL_AUTHORIZED",
                        "provider": engine.contract.provider,
                        "model": engine.contract.model,
                    }
                )
                continue
            try:
                _reject_private_content(payload)
                model_identity, output = engine.infer(stage, payload, schema)
                validated = schema.model_validate(output).model_dump(mode="json")
                _reject_private_content(validated)
                receipt = getattr(engine, "last_runtime_receipt", None)
                if not isinstance(receipt, Mapping):
                    raise RuntimeError("INFERENCE_RUNTIME_RECEIPT_MISSING")
                attempts.append(
                    {
                        "route": route_name,
                        "status": "COMPLETED",
                        "provider": receipt.get("resolved_provider"),
                        "model": receipt.get("resolved_model"),
                        "returncode": receipt.get("returncode"),
                        "auth_verified": receipt.get("auth_verified"),
                        "is_success_response": receipt.get("is_success_response"),
                        "is_fixture": receipt.get("is_fixture"),
                    }
                )
                return {
                    "status": "COMPLETED",
                    "stage": stage,
                    "route": route_name,
                    "model_identity": model_identity,
                    "runtime_receipt": dict(receipt),
                    "schema_valid": True,
                    "input_sha256": _stable_hash(payload),
                    "output_sha256": _stable_hash(validated),
                    "output": validated,
                    "attempts": attempts,
                    "observed_at": _utc(now).isoformat(),
                }
            except (ValidationError, ValueError, RuntimeError) as exc:
                attempts.append(
                    {
                        "route": route_name,
                        "status": "BLOCKED",
                        "reason": f"{type(exc).__name__}:{exc}",
                        "provider": engine.contract.provider,
                        "model": engine.contract.model,
                    }
                )
        return {
            "status": "BLOCKED",
            "stage": stage,
            "reason": "ALL_AUTHORIZED_INFERENCE_ROUTES_BLOCKED",
            "attempts": attempts,
            "observed_at": _utc(now).isoformat(),
        }

    def generate(
        self,
        stage: str,
        payload: Mapping[str, Any],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        if stage not in self.GENERATE_STAGES:
            return {
                "status": "BLOCKED",
                "stage": stage,
                "reason": "GENERATE_STAGE_MISMATCH",
                "attempts": [],
                "observed_at": _utc(now).isoformat(),
            }
        return self._infer_route(stage, payload, now=now)

    def challenge(
        self,
        payload: Mapping[str, Any],
        *,
        now: datetime,
        underwriting_model_identity: str,
    ) -> dict[str, Any]:
        result = self._infer_route("challenge", payload, now=now)
        if result["status"] != "COMPLETED":
            return result
        if result["model_identity"] == underwriting_model_identity:
            return {
                **result,
                "status": "BLOCKED",
                "reason": "CHALLENGE_MODEL_NOT_HETEROGENEOUS",
                "schema_valid": False,
            }
        result["challenge_model_distinct"] = True
        return result

    def _record_callback_evidence(self, result: Mapping[str, Any]) -> dict[str, Any]:
        receipt = result.get("runtime_receipt")
        receipt_evidence = None
        if isinstance(receipt, Mapping):
            receipt_evidence = {
                key: receipt.get(key)
                for key in (
                    "resolved_provider",
                    "resolved_model",
                    "auth_verified",
                    "is_success_response",
                    "is_fixture",
                    "returncode",
                )
            }
        attempts = []
        for row in result.get("attempts") or []:
            if not isinstance(row, Mapping):
                continue
            attempts.append(
                {
                    key: row.get(key)
                    for key in (
                        "route",
                        "status",
                        "reason",
                        "provider",
                        "model",
                        "returncode",
                        "auth_verified",
                        "is_success_response",
                        "is_fixture",
                    )
                    if row.get(key) is not None
                }
            )
        evidence = {
            "stage": result.get("stage"),
            "status": result.get("status"),
            "reason": result.get("reason"),
            "route": result.get("route"),
            "model_identity": result.get("model_identity"),
            "runtime_receipt": receipt_evidence,
            "attempts": attempts,
            "schema_valid": result.get("schema_valid"),
            "challenge_model_distinct": result.get("challenge_model_distinct"),
            "observed_at": result.get("observed_at"),
            "provenance": result.get("provenance") if result.get("stage") == "fetch" else None,
        }
        _reject_private_content(evidence)
        self.callback_evidence.append(evidence)
        return dict(result)

    def callbacks(self) -> dict[str, Callable[..., dict[str, Any]]]:
        def fetch_callback(symbol: str, **kwargs: Any) -> dict[str, Any]:
            return self._record_callback_evidence(self.fetch(symbol, **kwargs))

        def generate_callback(stage: str, payload: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
            return self._record_callback_evidence(self.generate(stage, payload, **kwargs))

        def challenge_callback(
            payload: Mapping[str, Any],
            underwriting_model_identity: str,
            **kwargs: Any,
        ) -> dict[str, Any]:
            return self._record_callback_evidence(
                self.challenge(
                    payload,
                    underwriting_model_identity=underwriting_model_identity,
                    **kwargs,
                )
            )

        return {
            "fetch": fetch_callback,
            "generate": generate_callback,
            "challenge": challenge_callback,
        }

    def run_original_entrypoint(
        self,
        entrypoint: Optional[Callable[..., Any]],
        *,
        symbol: str,
        now: datetime,
        reader: Any = None,
    ) -> dict[str, Any]:
        """Inject callbacks into the host/original engine without duplicating its DAG."""

        if not callable(entrypoint):
            return {
                "status": "BLOCKED",
                "reason": "ORIGINAL_RESEARCH_ENGINE_ENTRYPOINT_MISSING",
                "owner": "MAIN_CIO",
                "exact_next_action": "INSTALL_OR_MAP_ORIGINAL_RESEARCH_ENGINE_ENTRYPOINT",
                "live_acceptance_claimed": False,
            }
        self.callback_evidence = []
        callbacks = self.callbacks()
        try:
            result = entrypoint(
                symbol=symbol,
                now=_utc(now),
                fetch_callback=lambda requested_symbol=symbol, **kwargs: callbacks["fetch"](
                    requested_symbol,
                    now=kwargs.pop("now", now),
                    reader=kwargs.pop("reader", reader),
                ),
                generate_callback=lambda stage, payload, **kwargs: callbacks["generate"](
                    stage,
                    payload,
                    now=kwargs.pop("now", now),
                ),
                challenge_callback=lambda payload, underwriting_model_identity, **kwargs: callbacks["challenge"](
                    payload,
                    now=kwargs.pop("now", now),
                    underwriting_model_identity=underwriting_model_identity,
                ),
            )
        except TypeError as exc:
            return {
                "status": "BLOCKED",
                "reason": f"ORIGINAL_ENTRYPOINT_CALLBACK_CONTRACT_MISMATCH:{exc}",
                "owner": "MAIN_CIO",
                "exact_next_action": "MAP_HOST_ENTRYPOINT_TO_FETCH_GENERATE_CHALLENGE_CALLBACKS",
                "live_acceptance_claimed": False,
            }
        if not isinstance(result, Mapping):
            return {
                "status": "BLOCKED",
                "reason": "ORIGINAL_ENTRYPOINT_RESULT_INVALID",
                "owner": "MAIN_CIO",
                "live_acceptance_claimed": False,
            }
        output = dict(result)
        output.setdefault("live_acceptance_claimed", False)
        output.setdefault("owner", "MAIN_CIO")
        output["callback_evidence"] = list(self.callback_evidence)
        return output


    def _fetch_host_public_document(
        self,
        url: str,
        *,
        now: datetime,
        reader: Any = None,
    ) -> dict[str, Any]:
        """Adapt original host fetch(url) into a bounded public-document envelope."""
        observed = _utc(now)
        if not isinstance(url, str) or not url.strip():
            raise RuntimeError("HOST_FETCH_URL_REQUIRED")
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("HOST_FETCH_PUBLIC_HTTP_URL_REQUIRED")
        _reject_private_content(url, "host_fetch.url")
        try:
            raw = reader(url) if callable(reader) else _public_json(url)
        except Exception as exc:
            raise RuntimeError(f"HOST_FETCH_PUBLIC_DOCUMENT_UNAVAILABLE:{type(exc).__name__}:{exc}") from exc
        document = {
            "source_url": url,
            "observed_at": observed.isoformat(),
            "content_sha256": _stable_hash(raw),
            "content": raw,
        }
        _reject_private_content(document, "host_fetch.document")
        encoded = __import__("json").dumps(
            document, default=str, ensure_ascii=False
        ).encode()
        if len(encoded) > self.max_serialized_bytes:
            raise RuntimeError("HOST_FETCH_PUBLIC_DOCUMENT_OVERSIZE")
        evidence = {
            "status": "COMPLETED",
            "stage": "fetch",
            "observed_at": observed.isoformat(),
            "provenance": [{
                "source_url": url,
                "observed_at": observed.isoformat(),
                "content_sha256": document["content_sha256"],
            }],
        }
        self._record_callback_evidence(evidence)
        return document

    @staticmethod
    def _host_stage_payload(
        stage: str,
        payload: Mapping[str, Any],
        *,
        symbol: str,
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise RuntimeError(f"HOST_{stage.upper()}_PAYLOAD_MAPPING_REQUIRED")
        normalized = dict(payload)
        normalized.setdefault("symbol", symbol)
        _reject_private_content(normalized, f"host_{stage}.payload")
        return normalized

    def _installed_run_case_callbacks(
        self,
        *,
        symbol: str,
        now: datetime,
        reader: Any = None,
    ) -> dict[str, Callable[..., dict[str, Any]]]:
        """Bind host callback arities while keeping inference identity in bridge state."""
        underwriting_identity: dict[str, Optional[str]] = {"value": None}

        def fetch_callback(url: str) -> dict[str, Any]:
            return self._fetch_host_public_document(url, now=now, reader=reader)

        def generate_callback(stage: str, payload: Mapping[str, Any]) -> dict[str, Any]:
            normalized = self._host_stage_payload(stage, payload, symbol=symbol)
            result = self._record_callback_evidence(
                self.generate(stage, normalized, now=now)
            )
            if result.get("status") != "COMPLETED":
                raise RuntimeError(
                    f"HOST_{str(stage).upper()}_CALLBACK_BLOCKED:{result.get('reason','UNKNOWN')}"
                )
            output = result.get("output")
            if not isinstance(output, Mapping):
                raise RuntimeError(f"HOST_{str(stage).upper()}_OUTPUT_ENVELOPE_INVALID")
            if stage == "underwriting":
                identity = str(result.get("model_identity") or "").strip()
                if not identity:
                    raise RuntimeError("HOST_UNDERWRITING_MODEL_IDENTITY_REQUIRED")
                underwriting_identity["value"] = identity
            # Original run_case consumes the stage schema object, not bridge audit metadata.
            return dict(output)

        def challenge_callback(payload: Mapping[str, Any]) -> dict[str, Any]:
            identity = underwriting_identity["value"]
            if not identity:
                raise RuntimeError("HOST_UNDERWRITING_MODEL_IDENTITY_REQUIRED")
            normalized = self._host_stage_payload("challenge", payload, symbol=symbol)
            result = self._record_callback_evidence(
                self.challenge(
                    normalized,
                    now=now,
                    underwriting_model_identity=identity,
                )
            )
            if result.get("status") != "COMPLETED":
                raise RuntimeError(
                    f"HOST_CHALLENGE_CALLBACK_BLOCKED:{result.get('reason','UNKNOWN')}"
                )
            output = result.get("output")
            if not isinstance(output, Mapping):
                raise RuntimeError("HOST_CHALLENGE_OUTPUT_ENVELOPE_INVALID")
            return dict(output)

        return {
            "fetch": fetch_callback,
            "generate": generate_callback,
            "challenge": challenge_callback,
        }

    def run_installed_run_case(
        self,
        entrypoint: Optional[Callable[..., Any]],
        *,
        case_id: str,
        symbol: str,
        seed_urls: list[str],
        directory: str,
        max_attempts: int,
        now: datetime,
        reader: Any = None,
    ) -> dict[str, Any]:
        """Map the observed installed run_case contract without inventing a host module."""
        if not callable(entrypoint):
            return {
                "status": "BLOCKED",
                "reason": "ORIGINAL_RESEARCH_ENGINE_ENTRYPOINT_MISSING",
                "owner": "MAIN_CIO",
                "exact_next_action": "SUPPLY_INSTALLED_RUN_CASE_IMPORT_PATH",
                "live_acceptance_claimed": False,
            }
        if not case_id.strip() or not symbol.strip() or not seed_urls or max_attempts < 1:
            return {
                "status": "BLOCKED",
                "reason": "ORIGINAL_RUN_CASE_HOST_MAPPING_ARGUMENTS_INVALID",
                "owner": "MAIN_CIO",
                "exact_next_action": "SUPPLY_CASE_ID_TICKER_SEED_URLS_DIRECTORY_AND_MAX_ATTEMPTS",
                "live_acceptance_claimed": False,
            }
        self.callback_evidence = []
        callbacks = self._installed_run_case_callbacks(
            symbol=symbol,
            now=now,
            reader=reader,
        )
        try:
            result = entrypoint(
                case_id=case_id,
                ticker=symbol,
                seed_urls=list(seed_urls),
                directory=directory,
                fetch=callbacks["fetch"],
                generate=callbacks["generate"],
                challenge=callbacks["challenge"],
                max_attempts=max_attempts,
            )
        except TypeError as exc:
            return {
                "status": "BLOCKED",
                "reason": f"ORIGINAL_RUN_CASE_CALLBACK_CONTRACT_MISMATCH:{exc}",
                "owner": "MAIN_CIO",
                "exact_next_action": "MAIN_MAP_INSTALLED_RUN_CASE_CALLBACK_SHAPES",
                "live_acceptance_claimed": False,
                "callback_evidence": list(self.callback_evidence),
            }
        except (RuntimeError, ValueError) as exc:
            return {
                "status": "BLOCKED",
                "reason": f"ORIGINAL_RUN_CASE_CALLBACK_BLOCKED:{exc}",
                "owner": "MAIN_CIO",
                "exact_next_action": "RESTORE_AUTHORIZED_PUBLIC_CALLBACK_PREREQUISITE",
                "live_acceptance_claimed": False,
                "callback_evidence": list(self.callback_evidence),
            }
        if not isinstance(result, Mapping):
            return {
                "status": "BLOCKED",
                "reason": "ORIGINAL_ENTRYPOINT_RESULT_INVALID",
                "owner": "MAIN_CIO",
                "live_acceptance_claimed": False,
            }
        try:
            _reject_private_content(result)
        except ValueError as exc:
            return {
                "status": "BLOCKED",
                "reason": f"ORIGINAL_ENTRYPOINT_RESULT_PRIVATE:{exc}",
                "owner": "MAIN_CIO",
                "live_acceptance_claimed": False,
            }
        output = dict(result)
        output.setdefault("live_acceptance_claimed", False)
        output.setdefault("owner", "MAIN_CIO")
        output.setdefault("host_mapping_contract", "run_case_v1")
        output.setdefault("host_mapping_signature", "run_case(case_id,ticker,seed_urls,directory,fetch,generate,challenge,max_attempts)")
        output.setdefault("host_mapping_observed_at", _utc(now).isoformat())
        output["callback_evidence"] = list(self.callback_evidence)
        return output


class PositionMonitorContract(BaseModel):
    """Sanitized local monitor contract; holdings/account fields are intentionally absent."""

    model_config = ConfigDict(extra="forbid")
    contract_id: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    condition_version: str = Field(min_length=1)
    observation_identity: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    observation: dict[str, Any]
    expected_receipt: Optional[dict[str, Any]] = None


class LocalPositionReceiptBridge:
    """Bridge a caller-supplied local contract registry to existing consumers.

    Pending delivery reconciliation is independent from the current quote and
    current registry. Pending rows store only sanitized immutable receipt
    linkage plus the original versioned monitor identity.
    """

    def __init__(
        self,
        *,
        registry_loader: Callable[[], Any],
        quote_provider: Callable[[str], Any],
        receipt_provider: Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]],
        history: CIOSessionHistory,
        max_age_seconds: float = 300,
        allow_fixture_quotes: bool = False,
    ) -> None:
        self.registry_loader = registry_loader
        self.quote_provider = quote_provider
        self.receipt_provider = receipt_provider
        self.history = history
        self.position_consumer = ReceiptAwarePositionConsumer()
        self.receipt_consumer = DeliveryReceiptConsumer(history)
        self.max_age_seconds = max_age_seconds
        self.allow_fixture_quotes = allow_fixture_quotes

    def _contracts(self) -> list[PositionMonitorContract]:
        raw = self.registry_loader()
        if isinstance(raw, Mapping):
            raw = raw.get("contracts")
        if not isinstance(raw, list):
            raise ValueError("POSITION_CONTRACT_REGISTRY_INVALID")
        contracts = [PositionMonitorContract.model_validate(row) for row in raw]
        seen: set[tuple[str, str, str]] = set()
        for contract in contracts:
            key = (contract.contract_id, contract.condition_id, contract.condition_version)
            if key in seen:
                raise ValueError("POSITION_CONTRACT_DUPLICATE_IDENTITY")
            seen.add(key)
        return contracts

    def _expected_snapshot(self, expected: Mapping[str, Any]) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for key in DeliveryReceiptConsumer.REQUIRED_LINKS:
            value = expected.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"EXPECTED_RECEIPT_LINKAGE_INVALID:{key}")
            snapshot[key] = value.strip()
        _reject_private_content(snapshot)
        return snapshot

    def _pending_identity(
        self,
        contract: PositionMonitorContract,
        monitor_row: Mapping[str, Any],
        expected: Mapping[str, Any],
    ) -> str:
        return _stable_hash(
            {
                "contract_id": contract.contract_id,
                "condition_id": contract.condition_id,
                "condition_version": contract.condition_version,
                "observation_identity": contract.observation_identity,
                "stable_identity": monitor_row.get("stable_identity"),
                "expected_receipt": self._expected_snapshot(expected),
            }
        )

    def _persist_pending_once(
        self,
        identity: str,
        contract: PositionMonitorContract,
        expected: Mapping[str, Any],
        monitor_row: Mapping[str, Any],
    ) -> None:
        if any(
            row.get("kind") == "MONITOR_PENDING_RECEIPT"
            and row.get("pending_identity") == identity
            for row in self.history.history()
        ):
            return
        self.history.append(
            {
                "kind": "MONITOR_PENDING_RECEIPT",
                "pending_identity": identity,
                "contract_id": contract.contract_id,
                "condition_id": contract.condition_id,
                "condition_version": contract.condition_version,
                "observation_identity": contract.observation_identity,
                "monitor_stable_identity": monitor_row.get("stable_identity"),
                "expected_receipt": self._expected_snapshot(expected),
            }
        )

    def _mark_resolved_once(self, pending_identity: str, delivery: Mapping[str, Any]) -> None:
        if any(
            row.get("kind") == "MONITOR_PENDING_RESOLVED"
            and row.get("pending_identity") == pending_identity
            for row in self.history.history()
        ):
            return
        self.history.append(
            {
                "kind": "MONITOR_PENDING_RESOLVED",
                "pending_identity": pending_identity,
                "receipt_identity": delivery.get("receipt_identity"),
            }
        )

    def _unresolved_pending(self) -> list[dict[str, Any]]:
        rows = self.history.history()
        resolved = {
            row.get("pending_identity")
            for row in rows
            if row.get("kind") == "MONITOR_PENDING_RESOLVED"
        }
        return [
            dict(row)
            for row in rows
            if row.get("kind") == "MONITOR_PENDING_RECEIPT"
            and row.get("pending_identity") not in resolved
        ]

    def _replay_pending(self) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        for row in self._unresolved_pending():
            pending_identity = str(row.get("pending_identity") or "")
            expected = row.get("expected_receipt")
            if not isinstance(expected, Mapping):
                attempts.append(
                    {
                        "pending_identity": pending_identity,
                        "status": "BLOCKED",
                        "reason": "PENDING_EXPECTED_RECEIPT_LINKAGE_MISSING",
                    }
                )
                continue
            try:
                snapshot = self._expected_snapshot(expected)
            except ValueError as exc:
                attempts.append(
                    {
                        "pending_identity": pending_identity,
                        "status": "BLOCKED",
                        "reason": f"{type(exc).__name__}:{exc}",
                    }
                )
                continue
            receipt = self.receipt_provider(snapshot)
            delivery = self.receipt_consumer.consume(snapshot, receipt)
            attempts.append(
                {
                    "pending_identity": pending_identity,
                    "status": delivery.get("status"),
                    "acknowledged": delivery.get("acknowledged") is True,
                    "reason": delivery.get("reason"),
                    "receipt_identity": delivery.get("receipt_identity"),
                }
            )
            if delivery.get("acknowledged") is True:
                self._mark_resolved_once(pending_identity, delivery)
        return {
            "attempted": len(attempts),
            "resolved": sum(1 for row in attempts if row.get("acknowledged") is True),
            "attempts": attempts,
        }

    def _pending_public_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "pending_identity": row.get("pending_identity"),
                "contract_id": row.get("contract_id"),
                "condition_id": row.get("condition_id"),
                "condition_version": row.get("condition_version"),
                "observation_identity": row.get("observation_identity"),
            }
            for row in self._unresolved_pending()
        ]

    def evaluate(self, *, now: datetime) -> dict[str, Any]:
        observed = _utc(now)

        # Replay historical pending receipts before current registry/quote
        # evaluation. This never re-evaluates or resends an old capital action.
        replay = self._replay_pending()
        replay_by_identity = {
            row["pending_identity"]: row
            for row in replay["attempts"]
            if row.get("acknowledged") is True
        }

        try:
            contracts = self._contracts()
        except (ValidationError, ValueError) as exc:
            return {
                "status": "BLOCKED",
                "reason": f"{type(exc).__name__}:{exc}",
                "results": [],
                "pending_replay": replay,
                "pending_receipts": self._pending_public_rows(),
                "private_positions_exported": False,
                "ack_store": "CIOSessionHistory/DeliveryReceiptConsumer",
            }

        results: list[dict[str, Any]] = []
        for contract in contracts:
            observation = dict(contract.observation)
            observation.update(
                {
                    "symbol": contract.symbol,
                    "condition_id": contract.condition_id,
                    "condition_version": contract.condition_version,
                    "observation_identity": contract.observation_identity,
                }
            )
            try:
                _reject_private_content(observation)
            except ValueError as exc:
                results.append(
                    {
                        "contract_id": contract.contract_id,
                        "condition_id": contract.condition_id,
                        "condition_version": contract.condition_version,
                        "observation_identity": contract.observation_identity,
                        "symbol": contract.symbol,
                        "classification": "UNKNOWN",
                        "triggered": None,
                        "reason": f"{type(exc).__name__}:{exc}",
                    }
                )
                continue

            quote = self.quote_provider(contract.symbol)
            quote_map = {} if quote is None else {contract.symbol: quote}
            evaluated = self.position_consumer.evaluate(
                {contract.symbol: observation},
                quote_map,
                now=observed,
                max_age_seconds=self.max_age_seconds,
                allow_fixture=self.allow_fixture_quotes,
            )
            monitor_row = dict(evaluated["results"][0])
            if monitor_row.get("classification") == "UNKNOWN" and "stale" in str(monitor_row.get("reason", "")).lower():
                monitor_row["classification"] = "STALE"
            contract_identity = _stable_hash(
                {
                    "contract_id": contract.contract_id,
                    "condition_id": contract.condition_id,
                    "condition_version": contract.condition_version,
                    "observation_identity": contract.observation_identity,
                    "monitor_stable_identity": monitor_row.get("stable_identity"),
                }
            )
            delivery: dict[str, Any] = {
                "status": "NOT_TRIGGERED",
                "acknowledged": False,
                "replay_permitted": False,
            }

            if monitor_row.get("triggered") is True and contract.expected_receipt is not None:
                try:
                    expected = self._expected_snapshot(contract.expected_receipt)
                    pending_identity = self._pending_identity(contract, monitor_row, expected)
                except ValueError as exc:
                    delivery = {
                        "status": "UNKNOWN",
                        "acknowledged": False,
                        "replay_permitted": False,
                        "reason": f"{type(exc).__name__}:{exc}",
                    }
                else:
                    if pending_identity in replay_by_identity:
                        prior = replay_by_identity[pending_identity]
                        delivery = {
                            "status": "ACKNOWLEDGED_REPLAY",
                            "acknowledged": True,
                            "replay_permitted": False,
                            "receipt_identity": prior.get("receipt_identity"),
                            "pending_identity": pending_identity,
                        }
                    else:
                        receipt = self.receipt_provider(expected)
                        delivery = self.receipt_consumer.consume(expected, receipt)
                        if delivery.get("acknowledged") is not True:
                            self._persist_pending_once(
                                pending_identity,
                                contract,
                                expected,
                                monitor_row,
                            )
                            delivery = {
                                **delivery,
                                "pending_receipt": True,
                                "pending_identity": pending_identity,
                            }
                        elif any(
                            row.get("kind") == "MONITOR_PENDING_RECEIPT"
                            and row.get("pending_identity") == pending_identity
                            for row in self.history.history()
                        ):
                            self._mark_resolved_once(pending_identity, delivery)

            results.append(
                {
                    "contract_id": contract.contract_id,
                    "condition_id": contract.condition_id,
                    "condition_version": contract.condition_version,
                    "observation_identity": contract.observation_identity,
                    "contract_identity": contract_identity,
                    **monitor_row,
                    "delivery": delivery,
                }
            )

        return {
            "status": "EVALUATED",
            "vti_contract_covered": any(c.symbol.upper() == "VTI" for c in contracts),
            "results": results,
            "pending_replay": replay,
            "pending_receipts": self._pending_public_rows(),
            "private_positions_exported": False,
            "ack_store": "CIOSessionHistory/DeliveryReceiptConsumer",
        }
