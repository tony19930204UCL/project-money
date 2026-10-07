"""Issue #16 source-only bridges for the original research and position-monitor entrypoints.

This module is intentionally glue, not a replacement engine. The host owns the
original orchestration entrypoint and private contract registry. These adapters
supply bounded public-only callbacks and reuse the existing quote-edge and
receipt consumers without creating a second ACK store.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

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

    def callbacks(self) -> dict[str, Callable[..., dict[str, Any]]]:
        return {
            "fetch": self.fetch,
            "generate": self.generate,
            "challenge": self.challenge,
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
        try:
            result = entrypoint(
                symbol=symbol,
                now=_utc(now),
                fetch_callback=lambda requested_symbol=symbol, **kwargs: self.fetch(
                    requested_symbol,
                    now=kwargs.pop("now", now),
                    reader=kwargs.pop("reader", reader),
                ),
                generate_callback=lambda stage, payload, **kwargs: self.generate(
                    stage,
                    payload,
                    now=kwargs.pop("now", now),
                ),
                challenge_callback=lambda payload, underwriting_model_identity, **kwargs: self.challenge(
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
    """Bridge a caller-supplied local contract registry to existing consumers."""

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

    def _pending_identity(self, contract: PositionMonitorContract, monitor_row: Mapping[str, Any]) -> str:
        return _stable_hash(
            {
                "contract_id": contract.contract_id,
                "condition_id": contract.condition_id,
                "condition_version": contract.condition_version,
                "observation_identity": contract.observation_identity,
                "stable_identity": monitor_row.get("stable_identity"),
                "expected_receipt": contract.expected_receipt,
            }
        )

    def _persist_pending_once(self, identity: str, contract: PositionMonitorContract) -> None:
        for row in self.history.history():
            if row.get("kind") == "MONITOR_PENDING_RECEIPT" and row.get("pending_identity") == identity:
                return
        self.history.append(
            {
                "kind": "MONITOR_PENDING_RECEIPT",
                "pending_identity": identity,
                "contract_id": contract.contract_id,
                "condition_id": contract.condition_id,
                "condition_version": contract.condition_version,
                "observation_identity": contract.observation_identity,
            }
        )

    def evaluate(self, *, now: datetime) -> dict[str, Any]:
        observed = _utc(now)
        try:
            contracts = self._contracts()
        except (ValidationError, ValueError) as exc:
            return {
                "status": "BLOCKED",
                "reason": f"{type(exc).__name__}:{exc}",
                "results": [],
                "private_positions_exported": False,
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
            contract_identity = _stable_hash(
                {
                    "contract_id": contract.contract_id,
                    "condition_id": contract.condition_id,
                    "condition_version": contract.condition_version,
                    "observation_identity": contract.observation_identity,
                    "monitor_stable_identity": monitor_row.get("stable_identity"),
                }
            )
            delivery = {
                "status": "NOT_TRIGGERED",
                "acknowledged": False,
                "replay_permitted": False,
            }
            if monitor_row.get("triggered") is True and contract.expected_receipt is not None:
                expected = dict(contract.expected_receipt)
                receipt = self.receipt_provider(expected)
                delivery = self.receipt_consumer.consume(expected, receipt)
                if delivery.get("acknowledged") is not True:
                    pending_identity = self._pending_identity(contract, monitor_row)
                    self._persist_pending_once(pending_identity, contract)
                    delivery = {
                        **delivery,
                        "pending_receipt": True,
                        "pending_identity": pending_identity,
                    }

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

        pending = [
            row
            for row in self.history.history()
            if row.get("kind") == "MONITOR_PENDING_RECEIPT"
        ]
        return {
            "status": "EVALUATED",
            "vti_contract_covered": any(c.symbol.upper() == "VTI" for c in contracts),
            "results": results,
            "pending_receipts": [
                {
                    "pending_identity": row.get("pending_identity"),
                    "contract_id": row.get("contract_id"),
                    "condition_id": row.get("condition_id"),
                    "condition_version": row.get("condition_version"),
                    "observation_identity": row.get("observation_identity"),
                }
                for row in pending
            ],
            "private_positions_exported": False,
            "ack_store": "CIOSessionHistory/DeliveryReceiptConsumer",
        }
