"""Issue #16 source-only bridges for the original research and position-monitor entrypoints.

This module is intentionally glue, not a replacement engine. The host owns the
original orchestration entrypoint and private contract registry. These adapters
supply bounded public-only callbacks and reuse the existing quote-edge and
receipt consumers without creating a second ACK store.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from subprocess import TimeoutExpired
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

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
from cio_market_lab.research.official import USER_AGENT
from cio_market_lab.research.official_documents import MAX_BYTES as OFFICIAL_DOCUMENT_MAX_BYTES, parse_official_document


class StageRoute(BaseModel):
    """Explicit primary/fallback route contract for one research stage."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")
    primary: HermesLocalInference
    primary_model_family: Optional[str] = Field(default=None, min_length=1)
    fallback: Optional[HermesLocalInference] = None
    fallback_model_family: Optional[str] = Field(default=None, min_length=1)

    def family_for(self, route_name: str) -> Optional[str]:
        if route_name == "primary":
            return self.primary_model_family
        if route_name == "fallback":
            return self.fallback_model_family
        return None


class OriginalHostStageOutput(BaseModel):
    """Strict original generate(stage,payload) contract."""

    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern="^(PASS|REJECT|INCOMPLETE)$")
    reason: str = Field(min_length=1)
    source_urls: list[str]


class OriginalHostUnderwritingOutput(OriginalHostStageOutput):
    """Original-host underwriting result with schema-visible PASS requirements.

    INCOMPLETE/REJECT may omit unsupported fields. PASS is advertised to the
    inference model as requiring every underwriting fact below; the bridge
    still independently downgrades incomplete PASS output after inference.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"status": {"const": "PASS"}},
                        "required": ["status"],
                    },
                    "then": {
                        "required": [
                            "financials",
                            "market_metrics",
                            "capital_structure",
                            "independent_source_mix",
                            "reflexivity_score",
                            "scenario_return_estimates",
                            "factor_labels",
                            "business_maturity",
                            "valuation_scenarios",
                            "buy_zone",
                            "invalidation_conditions",
                            "review_by",
                            "four_sentences",
                        ]
                    },
                }
            ]
        },
    )
    status: str = Field(
        pattern="^(PASS|REJECT|INCOMPLETE)$",
        description=(
            "PASS only when every schema-listed underwriting field is supported by supplied "
            "public evidence; use INCOMPLETE when evidence is insufficient and never invent values."
        ),
    )
    financials: Optional[dict[str, Any]] = Field(
        default=None,
        description="Public-evidence financial facts; never infer missing numeric values.",
    )
    market_metrics: Optional[dict[str, Any]] = Field(
        default=None,
        description="Public-evidence market metrics actually supported by supplied documents.",
    )
    capital_structure: Optional[dict[str, Any]] = Field(
        default=None,
        description="Public-evidence capital-structure facts actually supported by supplied documents.",
    )
    independent_source_mix: Optional[list[str]] = Field(
        default=None,
        description="Non-empty subset of supplied seed URLs used as underwriting evidence.",
    )
    reflexivity_score: Optional[float] = Field(
        default=None,
        description="Only provide when supportable from supplied evidence; otherwise return INCOMPLETE.",
    )
    scenario_return_estimates: Optional[dict[str, Any]] = Field(
        default=None,
        description="Scenario return estimates supported by supplied evidence; do not fabricate numbers.",
    )
    factor_labels: Optional[list[str]] = Field(
        default=None,
        description="Evidence-backed factor labels relevant to the underwriting case.",
    )
    business_maturity: Optional[str] = None
    valuation_scenarios: Optional[dict[str, Any]] = None
    buy_zone: Optional[dict[str, Any]] = None
    invalidation_conditions: Optional[list[str]] = None
    review_by: Optional[str] = None
    four_sentences: Optional[list[str]] = Field(default=None, min_length=4, max_length=4)


class OriginalHostChallengeOutput(BaseModel):
    """Strict original challenge(payload) contract.

    Objections are model-produced output. The bridge never inserts an empty
    list to turn malformed challenge output into acceptance.
    """

    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern="^(PASS|BLOCK|INCOMPLETE)$")
    reason: str = Field(min_length=1)
    source_urls: list[str]
    objections: list[Any]


HOST_STAGE_SCHEMAS: dict[str, type[BaseModel]] = {
    "discovery": OriginalHostStageOutput,
    "commercial": OriginalHostStageOutput,
    "underwriting": OriginalHostUnderwritingOutput,
    "challenge": OriginalHostChallengeOutput,
}


HOST_UNDERWRITING_REQUIRED_FIELDS = (
    "financials",
    "market_metrics",
    "capital_structure",
    "independent_source_mix",
    "reflexivity_score",
    "scenario_return_estimates",
    "factor_labels",
    "business_maturity",
    "valuation_scenarios",
    "buy_zone",
    "invalidation_conditions",
    "review_by",
    "four_sentences",
)

HOST_UNDERWRITING_PROMPT_CONTRACT = {
    "required_output_fields_for_pass": list(HOST_UNDERWRITING_REQUIRED_FIELDS),
    "pass_semantics": (
        "Return PASS only when every required field is non-empty and supported by the supplied "
        "public documents/source_urls. Do not infer or invent missing numerical values."
    ),
    "insufficient_evidence_semantics": (
        "When any required field cannot be supported by supplied public evidence, return INCOMPLETE "
        "and name every missing field in reason using MISSING_UNDERWRITING_FIELDS:<comma-separated-fields>."
    ),
    "public_evidence_semantics": (
        "source_urls must contain only supplied seed URLs actually used as public evidence."
    ),
}

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
                        "stage",
                        "route",
                        "status",
                        "reason",
                        "provider",
                        "model",
                        "model_family",
                        "timeout_seconds",
                        "timeout_diagnostics",
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
            "model_family": result.get("model_family"),
            "runtime_receipt": receipt_evidence,
            "attempts": attempts,
            "schema_valid": result.get("schema_valid"),
            "challenge_model_distinct": result.get("challenge_model_distinct"),
            "observed_at": result.get("observed_at"),
            "provenance": result.get("provenance") if result.get("stage") == "fetch" else None,
            "fetch_diagnostics": result.get("fetch_diagnostics") if result.get("stage") == "fetch" else None,
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


    @staticmethod
    def _default_public_text_reader(url: str) -> dict[str, Any]:
        """Acquire one bounded official document for parser-backed extraction."""
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            raise RuntimeError("HOST_FETCH_PUBLIC_HTTP_URL_REQUIRED")
        if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
            raise RuntimeError("HOST_FETCH_NONPUBLIC_HOST_REJECTED")
        request = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain,*/*;q=0.5",
            },
        )
        with urlopen(request, timeout=25) as response:
            raw = response.read(OFFICIAL_DOCUMENT_MAX_BYTES + 1)
            if response.status != 200:
                raise RuntimeError(f"HOST_FETCH_HTTP_{response.status}")
            if len(raw) > OFFICIAL_DOCUMENT_MAX_BYTES:
                raise RuntimeError("HOST_FETCH_PUBLIC_DOCUMENT_OVERSIZE")
            return {
                "body": raw,
                "content_type": str(response.headers.get("Content-Type") or "application/octet-stream"),
            }

    def _fetch_host_public_document(
        self,
        url: str,
        *,
        now: datetime,
        reader: Any = None,
    ) -> dict[str, Any]:
        """Adapt original host fetch(url) to exactly {url,text,observed_at}."""
        observed = _utc(now)
        if not isinstance(url, str) or not url.strip():
            raise RuntimeError("HOST_FETCH_URL_REQUIRED")
        url = url.strip()
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            raise RuntimeError("HOST_FETCH_PUBLIC_HTTP_URL_REQUIRED")
        if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
            raise RuntimeError("HOST_FETCH_NONPUBLIC_HOST_REJECTED")
        _reject_private_content(url, "host_fetch.url")

        diagnostic = {
            "status": "BLOCKED",
            "stage": "fetch",
            "reason": None,
            "observed_at": observed.isoformat(),
            "provenance": [{
                "source_url": url,
                "observed_at": observed.isoformat(),
                "content_sha256": None,
                "extracted_content_sha256": None,
                "extraction_succeeded": False,
            }],
        }
        try:
            acquired = reader(url) if callable(reader) else self._default_public_text_reader(url)
            content_type = "text/html"
            if isinstance(acquired, Mapping):
                raw = acquired.get("body")
                content_type = str(acquired.get("content_type") or content_type)
            else:
                raw = acquired
            if isinstance(raw, str):
                raw_bytes = raw.encode("utf-8")
                if not raw.lstrip().startswith("<"):
                    content_type = "text/plain"
            elif isinstance(raw, bytes):
                raw_bytes = raw
            else:
                raise RuntimeError("HOST_FETCH_PUBLIC_DOCUMENT_BODY_INVALID")
            if len(raw_bytes) > OFFICIAL_DOCUMENT_MAX_BYTES:
                raise RuntimeError("HOST_FETCH_PUBLIC_DOCUMENT_OVERSIZE")

            raw_hash = hashlib.sha256(raw_bytes).hexdigest()
            diagnostic["provenance"][0]["content_sha256"] = raw_hash
            rows = parse_official_document(url, raw_bytes, content_type)
            extracted_parts = []
            rejected_parts: list[str] = []
            first_rejection: Optional[ValueError] = None
            for index, row in enumerate(rows):
                if not isinstance(row, Mapping):
                    continue
                part = str(row.get("document_part") or "").strip()
                text = str(row.get("text") or "").strip()
                if not text:
                    continue
                candidate = f"[{part}] {text}" if part else text
                try:
                    _reject_private_content(candidate, f"host_fetch.document.parts[{index}]")
                except ValueError as exc:
                    if first_rejection is None:
                        first_rejection = exc
                    rejected_parts.append(f"{part or 'part'}:{exc}")
                    continue
                extracted_parts.append(candidate)
            diagnostic["fetch_diagnostics"] = {
                "parsed_parts": len(rows),
                "accepted_parts": len(extracted_parts),
                "rejected_parts": len(rejected_parts),
                "rejection_reasons": rejected_parts[:8],
            }
            if not extracted_parts:
                if first_rejection is not None:
                    raise first_rejection
                raise RuntimeError("HOST_FETCH_OFFICIAL_EXTRACTION_EMPTY")
            text_value = "\n\n".join(extracted_parts)
            _reject_private_content(text_value, "host_fetch.document.text")
            if len(text_value.encode("utf-8")) > self.max_serialized_bytes:
                raise RuntimeError("HOST_FETCH_EXTRACTED_DOCUMENT_OVERSIZE")

            extracted_hash = hashlib.sha256(text_value.encode("utf-8")).hexdigest()
            diagnostic = {
                **diagnostic,
                "status": "COMPLETED",
                "reason": None,
                "provenance": [{
                    "source_url": url,
                    "observed_at": observed.isoformat(),
                    "content_sha256": raw_hash,
                    "extracted_content_sha256": extracted_hash,
                    "extraction_succeeded": True,
                }],
            }
            self._record_callback_evidence(diagnostic)
            return {
                "url": url,
                "text": text_value,
                "observed_at": observed.isoformat(),
            }
        except Exception as exc:
            diagnostic["reason"] = f"{type(exc).__name__}:{exc}"
            self._record_callback_evidence(diagnostic)
            if isinstance(exc, RuntimeError) and str(exc).startswith("HOST_FETCH_"):
                raise
            raise RuntimeError(
                f"HOST_FETCH_PUBLIC_DOCUMENT_UNAVAILABLE:{type(exc).__name__}:{exc}"
            ) from exc

    @staticmethod
    def _host_stage_payload(
        stage: str,
        payload: Mapping[str, Any],
        *,
        symbol: str,
        seed_urls: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise RuntimeError(f"HOST_{stage.upper()}_PAYLOAD_MAPPING_REQUIRED")
        normalized = dict(payload)
        normalized.setdefault("symbol", symbol)
        if stage == "underwriting":
            seeds = {str(url).strip() for url in (seed_urls or []) if str(url).strip()}
            documents = normalized.get("documents")
            if not isinstance(documents, list):
                raise RuntimeError("HOST_UNDERWRITING_DOCUMENTS_REQUIRED")
            allowed = []
            for row in documents:
                if not isinstance(row, Mapping):
                    continue
                url = str(row.get("url") or "").strip()
                if url and url in seeds and url not in allowed:
                    allowed.append(url)
            if not allowed:
                raise RuntimeError("HOST_UNDERWRITING_ALLOWED_SOURCE_URLS_REQUIRED")
            normalized["underwriting_contract"] = {
                **HOST_UNDERWRITING_PROMPT_CONTRACT,
                "allowed_source_urls": allowed,
            }
        _reject_private_content(normalized, f"host_{stage}.payload")
        return normalized

    @staticmethod
    def _validate_host_source_urls(
        source_urls: list[str],
        *,
        seed_urls: list[str],
        stage: str,
    ) -> None:
        if not source_urls:
            raise RuntimeError(f"HOST_{stage.upper()}_SOURCE_URLS_REQUIRED")
        seeds = set(seed_urls)
        if any(url not in seeds for url in source_urls):
            raise RuntimeError(f"HOST_{stage.upper()}_SOURCE_URL_OUTSIDE_SEEDS")

    @staticmethod
    def _validate_host_underwriting_evidence(
        output: OriginalHostUnderwritingOutput,
        *,
        seed_urls: list[str],
        allowed_source_urls: list[str],
    ) -> None:
        if output.status != "PASS":
            return
        mix = list(output.independent_source_mix or [])
        if not mix:
            raise RuntimeError("HOST_UNDERWRITING_INDEPENDENT_SOURCE_MIX_REQUIRED")
        seeds = set(seed_urls)
        allowed = set(allowed_source_urls)
        if any(url not in seeds for url in mix):
            raise RuntimeError("HOST_UNDERWRITING_INDEPENDENT_SOURCE_OUTSIDE_SEEDS")
        if any(url not in allowed for url in mix):
            raise RuntimeError("HOST_UNDERWRITING_INDEPENDENT_SOURCE_NOT_IN_TASK_EVIDENCE")
        if any(url not in allowed for url in output.source_urls):
            raise RuntimeError("HOST_UNDERWRITING_SOURCE_URL_NOT_IN_TASK_EVIDENCE")

    @staticmethod
    def _downgrade_incomplete_underwriting(
        output: OriginalHostUnderwritingOutput,
    ) -> OriginalHostUnderwritingOutput:
        if output.status != "PASS":
            return output
        values = {
            field: getattr(output, field)
            for field in HOST_UNDERWRITING_REQUIRED_FIELDS
        }
        missing = [
            key for key, value in values.items()
            if value is None or value == "" or value == {} or value == []
        ]
        if not output.source_urls:
            missing.append("public_source_evidence")
        if missing:
            payload = output.model_dump()
            payload["status"] = "INCOMPLETE"
            payload["reason"] = "MISSING_UNDERWRITING_FIELDS:" + ",".join(sorted(set(missing)))
            return OriginalHostUnderwritingOutput.model_validate(payload)
        return output

    @staticmethod
    def _safe_timeout_diagnostics(
        exc: TimeoutExpired,
        *,
        configured_timeout_seconds: Any,
    ) -> dict[str, Any]:
        """Project a subprocess timeout into public-safe primitive diagnostics only."""
        def byte_count(value: Any) -> int:
            if isinstance(value, bytes):
                return len(value)
            if isinstance(value, str):
                return len(value.encode("utf-8", errors="replace"))
            return 0

        timeout_value = exc.timeout
        if isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float)):
            timeout_value = configured_timeout_seconds
        if isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float)):
            timeout_value = 0

        stdout_value = getattr(exc, "output", None)
        stderr_value = getattr(exc, "stderr", None)
        return {
            "timeout_seconds": timeout_value,
            "stdout_present": stdout_value is not None,
            "stdout_bytes_seen": byte_count(stdout_value),
            "stderr_present": stderr_value is not None,
            "stderr_bytes_seen": byte_count(stderr_value),
        }

    def _infer_host_route(
        self,
        stage: str,
        payload: Mapping[str, Any],
        *,
        now: datetime,
        seed_urls: list[str],
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
        schema = HOST_STAGE_SCHEMAS[stage]
        attempts: list[dict[str, Any]] = []
        if (
            route.fallback is not None
            and route.primary_model_family
            and route.fallback_model_family
            and route.primary_model_family.strip().lower() == route.fallback_model_family.strip().lower()
        ):
            return {
                "status": "BLOCKED",
                "stage": stage,
                "reason": "INFERENCE_ROUTE_FALLBACK_MODEL_FAMILY_CONFLICT",
                "attempts": [],
                "observed_at": _utc(now).isoformat(),
            }
        for route_name, engine in (("primary", route.primary), ("fallback", route.fallback)):
            if engine is None:
                continue
            model_family = route.family_for(route_name)
            if not model_family:
                attempts.append({
                    "route": route_name,
                    "status": "BLOCKED",
                    "reason": "INFERENCE_MODEL_FAMILY_REQUIRED",
                    "provider": engine.contract.provider,
                    "model": engine.contract.model,
                })
                continue
            if engine.contract.is_free_or_local_authorized is not True:
                attempts.append({
                    "route": route_name,
                    "status": "BLOCKED",
                    "reason": "INFERENCE_CONTRACT_NOT_FREE_OR_LOCAL_AUTHORIZED",
                    "provider": engine.contract.provider,
                    "model": engine.contract.model,
                })
                continue
            try:
                _reject_private_content(payload)
                model_identity, raw_output = engine.infer(stage, payload, schema)
                validated_model = schema.model_validate(raw_output)
                if stage == "underwriting":
                    validated_model = self._downgrade_incomplete_underwriting(validated_model)
                    contract = payload.get("underwriting_contract")
                    allowed_source_urls = (
                        list(contract.get("allowed_source_urls") or [])
                        if isinstance(contract, Mapping)
                        else []
                    )
                    self._validate_host_underwriting_evidence(
                        validated_model,
                        seed_urls=seed_urls,
                        allowed_source_urls=allowed_source_urls,
                    )
                validated = validated_model.model_dump(mode="json", exclude_none=True)
                self._validate_host_source_urls(
                    list(validated.get("source_urls") or []),
                    seed_urls=seed_urls,
                    stage=stage,
                )
                _reject_private_content(validated)
                receipt = getattr(engine, "last_runtime_receipt", None)
                if not isinstance(receipt, Mapping):
                    raise RuntimeError("INFERENCE_RUNTIME_RECEIPT_MISSING")
                attempts.append({
                    "route": route_name,
                    "status": "COMPLETED",
                    "provider": receipt.get("resolved_provider"),
                    "model": receipt.get("resolved_model"),
                    "model_family": model_family,
                    "returncode": receipt.get("returncode"),
                    "auth_verified": receipt.get("auth_verified"),
                    "is_success_response": receipt.get("is_success_response"),
                    "is_fixture": receipt.get("is_fixture"),
                })
                return {
                    "status": "COMPLETED",
                    "stage": stage,
                    "route": route_name,
                    "model_identity": model_identity,
                    "model_family": model_family,
                    "runtime_receipt": dict(receipt),
                    "schema_valid": True,
                    "input_sha256": _stable_hash(payload),
                    "output_sha256": _stable_hash(validated),
                    "output": validated,
                    "attempts": attempts,
                    "observed_at": _utc(now).isoformat(),
                }
            except TimeoutExpired as exc:
                safe_timeout = self._safe_timeout_diagnostics(
                    exc,
                    configured_timeout_seconds=getattr(engine, "timeout_seconds", None),
                )
                attempts.append({
                    "stage": stage,
                    "route": route_name,
                    "status": "BLOCKED",
                    "reason": f"HOST_{stage.upper()}_INFERENCE_TIMEOUT",
                    "provider": engine.contract.provider,
                    "model": engine.contract.model,
                    "model_family": model_family,
                    "timeout_seconds": safe_timeout["timeout_seconds"],
                    "timeout_diagnostics": {
                        key: safe_timeout[key]
                        for key in (
                            "stdout_present",
                            "stdout_bytes_seen",
                            "stderr_present",
                            "stderr_bytes_seen",
                        )
                    },
                })
                # The raw TimeoutExpired (including cmd/output/stderr) remains local to
                # this exception scope and is never copied into public callback evidence.
                continue
            except (ValidationError, ValueError, RuntimeError) as exc:
                attempts.append({
                    "route": route_name,
                    "status": "BLOCKED",
                    "reason": f"{type(exc).__name__}:{exc}",
                    "provider": engine.contract.provider,
                    "model": engine.contract.model,
                })
        return {
            "status": "BLOCKED",
            "stage": stage,
            "reason": (
                str(attempts[-1].get("reason"))
                if attempts and attempts[-1].get("reason")
                else "ALL_AUTHORIZED_INFERENCE_ROUTES_BLOCKED"
            ),
            "attempts": attempts,
            "observed_at": _utc(now).isoformat(),
        }

    def _installed_run_case_callbacks(
        self,
        *,
        symbol: str,
        seed_urls: list[str],
        now: datetime,
        reader: Any = None,
    ) -> dict[str, Callable[..., dict[str, Any]]]:
        """Bind host callback arities while keeping inference identity in bridge state."""
        underwriting_identity: dict[str, Optional[str]] = {"value": None, "family": None}

        def fetch_callback(url: str) -> dict[str, Any]:
            return self._fetch_host_public_document(url, now=now, reader=reader)

        def generate_callback(stage: str, payload: Mapping[str, Any]) -> dict[str, Any]:
            normalized = self._host_stage_payload(
                stage,
                payload,
                symbol=symbol,
                seed_urls=seed_urls,
            )
            if stage not in self.GENERATE_STAGES:
                raise RuntimeError("HOST_GENERATE_STAGE_MISMATCH")
            result = self._record_callback_evidence(
                self._infer_host_route(
                    stage,
                    normalized,
                    now=now,
                    seed_urls=seed_urls,
                )
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
                underwriting_identity["family"] = str(result.get("model_family") or "").strip() or None
            # Original run_case consumes the stage schema object, not bridge audit metadata.
            return dict(output)

        def challenge_callback(payload: Mapping[str, Any]) -> dict[str, Any]:
            identity = underwriting_identity["value"]
            if not identity:
                raise RuntimeError("HOST_UNDERWRITING_MODEL_IDENTITY_REQUIRED")
            normalized = self._host_stage_payload("challenge", payload, symbol=symbol)
            result = self._record_callback_evidence(
                self._infer_host_route(
                    "challenge",
                    normalized,
                    now=now,
                    seed_urls=seed_urls,
                )
            )
            underwriting_family = underwriting_identity.get("family")
            challenge_family = str(result.get("model_family") or "").strip() or None
            family_conflict = (
                result.get("status") == "COMPLETED"
                and underwriting_family is not None
                and challenge_family is not None
                and underwriting_family.lower() == challenge_family.lower()
            )
            identity_conflict = result.get("status") == "COMPLETED" and result.get("model_identity") == identity
            if family_conflict or identity_conflict:
                reason = (
                    "CHALLENGE_MODEL_NOT_HETEROGENEOUS"
                    if identity_conflict
                    else "CHALLENGE_MODEL_FAMILY_NOT_HETEROGENEOUS"
                )
                result = {
                    **result,
                    "status": "BLOCKED",
                    "reason": reason,
                    "schema_valid": False,
                }
                self.callback_evidence[-1] = {
                    **self.callback_evidence[-1],
                    "status": "BLOCKED",
                    "reason": reason,
                    "challenge_model_distinct": False,
                }
            elif result.get("status") == "COMPLETED":
                result["challenge_model_distinct"] = True
                self.callback_evidence[-1]["challenge_model_distinct"] = True
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
            seed_urls=list(seed_urls),
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
