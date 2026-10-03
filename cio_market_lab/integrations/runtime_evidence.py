"""Project-local runtime evidence adapter for Hermes CIO decision execution.

Collects allowlisted post-auth initialized runtime provider and model evidence
from process-local runtime or instrumented stream-json emitter.
Never relies on unauthenticated CLI strings, pre-auth init echoes, or LLM self-claims.
Labels evidence strictly as 'local-runtime' (not cryptographic server attestation).
Strictly allowlists nonsecret provider/model/api_mode ONLY, never credentials/headers/tokens.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

TRANSPORT_IDENTIFIER = "hermes-cli-cio-bridge"
EVIDENCE_STRENGTH_LOCAL_RUNTIME = "local-runtime"
UNSAFE_HERMES_DEFAULT_MODEL = "hermes-3-llama-3.1-8b"

ALLOWLISTED_PRIMARY_RUNTIME_FIELDS = ("provider", "model", "api_mode")

ALLOWLISTED_SOURCE_FIELDS = [
    "agent.provider",
    "agent.model",
    "agent._primary_runtime.provider",
    "agent._primary_runtime.model",
    "agent._primary_runtime.api_mode",
    "agent.base_url",
    "agent.requested_provider",
]


class RuntimeEvidenceRecord(BaseModel):
    """Allowlisted initialized runtime provider/model evidence record."""

    evidence_strength: str = Field(
        default=EVIDENCE_STRENGTH_LOCAL_RUNTIME,
        description="Explicit label: local-runtime, NOT cryptographic server attestation",
    )
    attestation_type: str = "process_local_runtime_inspection"
    is_cryptographic_attestation: bool = False
    source_fields: List[str] = Field(default_factory=lambda: list(ALLOWLISTED_SOURCE_FIELDS))
    resolved_provider: str
    resolved_model: str
    primary_runtime: Dict[str, Any] = Field(default_factory=dict)
    requested_provider: Optional[str] = None
    requested_model: Optional[str] = None
    base_url: Optional[str] = None
    exit_code: int = 0
    is_success_response: bool = True
    auth_verified: bool = True
    is_pre_auth_init: bool = False
    is_fixture: bool = False
    fallback_active: bool = False
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RuntimeEvidenceAdapter:
    """Project-local runtime evidence adapter.

    Extracts allowlisted post-auth runtime evidence from initialized agent instances
    or stream events. Rejects pre-auth init, prefix-inferred provider identity,
    missing metadata, route mismatches, fallback masquerading, and failed executions.
    """

    def __init__(
        self,
        pinned_provider: Optional[str] = None,
        pinned_model: Optional[str] = None,
    ) -> None:
        self.pinned_provider = pinned_provider
        self.pinned_model = pinned_model

    def extract_from_agent(self, agent: Any) -> RuntimeEvidenceRecord:
        """Extract allowlisted post-auth initialized runtime fields directly from agent."""
        if agent is None:
            raise RuntimeError("AGENT_NOT_INITIALIZED: Cannot extract runtime evidence from None agent")

        # Allowlisted fields from initialized agent
        provider = getattr(agent, "provider", None)
        model = getattr(agent, "model", None)

        # STRICT ALLOWLIST: only copy nonsecret provider/model/api_mode ONLY.
        # NEVER copy credentials, headers, tokens, api_key, or arbitrary dict.
        raw_primary = getattr(agent, "_primary_runtime", None)
        primary_runtime: Dict[str, Any] = {}
        if isinstance(raw_primary, dict):
            for field in ALLOWLISTED_PRIMARY_RUNTIME_FIELDS:
                if field in raw_primary and raw_primary[field] is not None:
                    primary_runtime[field] = str(raw_primary[field]).strip()

        requested_provider = getattr(agent, "requested_provider", None)
        base_url = getattr(agent, "base_url", None)
        is_fixture = bool(getattr(agent, "is_fixture", False))

        if not provider or not str(provider).strip():
            raise RuntimeError(
                "PRE_AUTH_OR_UNINITIALIZED_AGENT: Agent has no post-auth initialized provider"
            )
        if not model or not str(model).strip():
            raise RuntimeError(
                "PRE_AUTH_OR_UNINITIALIZED_AGENT: Agent has no post-auth initialized model"
            )

        # Detect fallback masquerading: if primary_runtime is recorded and differs from active
        if primary_runtime:
            primary_p = primary_runtime.get("provider")
            primary_m = primary_runtime.get("model")
            if primary_p and primary_p != provider:
                raise RuntimeError(
                    f"FALLBACK_MASQUERADING_DETECTED: Active provider '{provider}' "
                    f"diverged from primary runtime provider '{primary_p}'"
                )
            if primary_m and primary_m != model:
                raise RuntimeError(
                    f"FALLBACK_MASQUERADING_DETECTED: Active model '{model}' "
                    f"diverged from primary runtime model '{primary_m}'"
                )

        return RuntimeEvidenceRecord(
            evidence_strength=EVIDENCE_STRENGTH_LOCAL_RUNTIME,
            attestation_type="process_local_runtime_inspection",
            is_cryptographic_attestation=False,
            source_fields=list(ALLOWLISTED_SOURCE_FIELDS),
            resolved_provider=str(provider).strip(),
            resolved_model=str(model).strip(),
            primary_runtime=primary_runtime,
            requested_provider=requested_provider,
            requested_model=model,
            base_url=base_url,
            exit_code=0,
            is_success_response=True,
            auth_verified=True,
            is_pre_auth_init=False,
            is_fixture=is_fixture,
            fallback_active=False,
        )

    def verify_runtime_evidence(
        self,
        metadata: Any,
        response_text: str = "",
        exit_code: int = 0,
        pinned_provider: Optional[str] = None,
        pinned_model: Optional[str] = None,
        allow_fixture: bool = False,
    ) -> RuntimeEvidenceRecord:
        """Validate collected runtime evidence against pins, schema, and response success."""
        if not isinstance(metadata, dict):
            raise RuntimeError("MALFORMED_RUNTIME_METADATA: CLI output missing valid runtime metadata dict")

        # 1. Reject pre-auth init events
        if metadata.get("is_pre_auth_init") is True or (
            metadata.get("type") == "system" and metadata.get("subtype") == "init"
        ):
            raise RuntimeError(
                "READBACK_VERIFICATION_FAILED: Pre-auth init record rejected. "
                "Hermes stream_json emits init before authentication; post-auth runtime evidence required."
            )

        # Reject unverified authentication
        if metadata.get("auth_verified") is False:
            raise RuntimeError(
                "READBACK_VERIFICATION_FAILED: Authentication not verified (auth_verified=False); "
                "explicit authenticated post-auth runtime evidence required."
            )

        # Reject unsuccessful response flag
        if metadata.get("is_success_response") is False:
            raise RuntimeError(
                "READBACK_VERIFICATION_FAILED: Response marked unsuccessful (is_success_response=False)."
            )

        # Reject fixture in production (unless explicitly allowed by fixture test caller)
        if not allow_fixture and (metadata.get("is_fixture") is True or metadata.get("fixture") is True):
            raise RuntimeError(
                "READBACK_VERIFICATION_FAILED: Fixture runtime evidence rejected by production executor "
                "(fixture=True prohibited in production)."
            )

        # Reject nonzero returncode in metadata even if subprocess return code was zero
        meta_exit = metadata.get("exit_code") if metadata.get("exit_code") is not None else metadata.get("returncode")
        if meta_exit is not None and meta_exit != 0:
            raise RuntimeError(
                f"READBACK_VERIFICATION_FAILED: Metadata reported nonzero exit code: {meta_exit}"
            )

        # Reject reported errors or failed flags in result/metadata
        if metadata.get("failed") is True or metadata.get("error") or metadata.get("is_error") is True:
            err_msg = metadata.get("error") or "failed=True"
            raise RuntimeError(
                f"READBACK_VERIFICATION_FAILED: Runtime reported error/failure: {err_msg}"
            )

        # 2. Reject prefix-inferred provider identity (e.g. splitting model 'openai-codex/gpt-6-astra')
        if metadata.get("prefix_inferred") is True:
            raise RuntimeError(
                "READBACK_VERIFICATION_FAILED: Prefix-inferred identity rejected. "
                "Provider identity must never be derived from model prefix."
            )

        resolved_provider = metadata.get("resolved_provider") or metadata.get("provider")
        resolved_model = metadata.get("resolved_model") or metadata.get("model")

        # Check for model-prefix masquerading where resolved_provider is absent or was guessed
        if not resolved_provider:
            raise RuntimeError("READBACK_VERIFICATION_FAILED: Missing resolved provider in runtime metadata")
        if not resolved_model:
            raise RuntimeError("READBACK_VERIFICATION_FAILED: Missing resolved model in runtime metadata")

        # Transport cannot masquerade as inference provider
        if resolved_provider == TRANSPORT_IDENTIFIER:
            raise RuntimeError(
                f"READBACK_VERIFICATION_FAILED: Transport identifier '{TRANSPORT_IDENTIFIER}' "
                f"cannot masquerade as actual inference provider"
            )

        # Prohibit unsafe default model
        if resolved_model == UNSAFE_HERMES_DEFAULT_MODEL:
            raise RuntimeError(
                f"READBACK_VERIFICATION_FAILED: Default model '{UNSAFE_HERMES_DEFAULT_MODEL}' "
                f"prohibited from masquerading as Main CIO"
            )

        # 3. Compare runtime route against pins
        target_provider = pinned_provider or self.pinned_provider
        target_model = pinned_model or self.pinned_model

        if target_provider and resolved_provider != target_provider:
            raise RuntimeError(
                f"READBACK_VERIFICATION_FAILED: Provider mismatch "
                f"(requested pin '{target_provider}', runtime resolved '{resolved_provider}')"
            )
        if target_model and resolved_model != target_model:
            raise RuntimeError(
                f"READBACK_VERIFICATION_FAILED: Model mismatch "
                f"(requested pin '{target_model}', runtime resolved '{resolved_model}')"
            )

        # 4. Reject fallback masquerading
        if metadata.get("fallback_active") is True:
            raise RuntimeError(
                "READBACK_VERIFICATION_FAILED: Fallback masquerading rejected. "
                "Runtime route diverged from pinned route."
            )
        primary = metadata.get("primary_runtime")
        sanitized_primary: Dict[str, Any] = {}
        if isinstance(primary, dict) and primary:
            for field in ALLOWLISTED_PRIMARY_RUNTIME_FIELDS:
                if field in primary and primary[field] is not None:
                    sanitized_primary[field] = str(primary[field]).strip()
            p_provider = sanitized_primary.get("provider")
            p_model = sanitized_primary.get("model")
            if target_provider and p_provider and p_provider != target_provider:
                raise RuntimeError(
                    f"READBACK_VERIFICATION_FAILED: Primary provider '{p_provider}' "
                    f"mismatches pinned provider '{target_provider}'"
                )
            if target_model and p_model and p_model != target_model:
                raise RuntimeError(
                    f"READBACK_VERIFICATION_FAILED: Primary model '{p_model}' "
                    f"mismatches pinned model '{target_model}'"
                )

        # 5. Require successful final response
        if exit_code != 0:
            raise RuntimeError(
                f"READBACK_VERIFICATION_FAILED: Process exited with non-zero exit code {exit_code}"
            )
        if not response_text or not response_text.strip():
            raise RuntimeError(
                "READBACK_VERIFICATION_FAILED: Missing or empty final response text"
            )

        evidence_strength = metadata.get("evidence_strength") or EVIDENCE_STRENGTH_LOCAL_RUNTIME
        is_fixture = bool(metadata.get("is_fixture", False))

        return RuntimeEvidenceRecord(
            evidence_strength=evidence_strength,
            attestation_type="process_local_runtime_inspection",
            is_cryptographic_attestation=False,
            source_fields=list(ALLOWLISTED_SOURCE_FIELDS),
            resolved_provider=str(resolved_provider).strip(),
            resolved_model=str(resolved_model).strip(),
            primary_runtime=sanitized_primary,
            requested_provider=target_provider,
            requested_model=target_model,
            base_url=metadata.get("base_url"),
            exit_code=exit_code,
            is_success_response=True,
            auth_verified=True,
            is_pre_auth_init=False,
            is_fixture=is_fixture,
            fallback_active=False,
        )


class RuntimeEvidenceStreamJsonEmitter:
    """Process-local emitter instrumentation for stream-json with post-auth runtime evidence.

    Instruments the stream-json lifecycle:
    1. init is emitted before auth per stream-json specification.
    2. attach(agent) captures allowlisted initialized runtime fields from agent and
       emits a 'runtime_metadata' event with evidence_strength='local-runtime'.
    3. emit_result re-extracts runtime evidence at terminal response to catch mid-turn fallback,
       and writes the terminal 'result' envelope.
    """

    def __init__(
        self,
        model: str = "",
        session_id: str = "",
        adapter: Optional[RuntimeEvidenceAdapter] = None,
        emit_fn: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._session_id = session_id
        self._start = time.time()
        self._tool_started: Dict[str, float] = {}
        self._adapter = adapter or RuntimeEvidenceAdapter()
        self._emit_fn = emit_fn or self._default_emit
        self._attached_agent: Any = None
        self._runtime_evidence: Optional[RuntimeEvidenceRecord] = None
        self._captured_events: List[Dict[str, Any]] = []

        # Standard pre-auth init event (model echoed from CLI args before auth)
        self._emit({
            "type": "system",
            "subtype": "init",
            "model": model,
            "session_id": session_id,
            "is_pre_auth_init": True,
            "auth_verified": False,
        })

    def attach(self, agent: Any) -> "RuntimeEvidenceStreamJsonEmitter":
        """Route callbacks into emitter AND capture authenticated post-init runtime evidence."""
        self._attached_agent = agent
        agent.stream_delta_callback = self.on_text_delta
        agent.tool_progress_callback = self.on_tool_progress

        # Extract allowlisted post-auth runtime evidence from initialized agent
        evidence = self._adapter.extract_from_agent(agent)
        self._runtime_evidence = evidence

        # Emit post-auth runtime metadata event into stream
        self._emit({
            "type": "runtime_metadata",
            "subtype": "attach",
            "evidence_strength": evidence.evidence_strength,
            "attestation_type": evidence.attestation_type,
            "resolved_provider": evidence.resolved_provider,
            "resolved_model": evidence.resolved_model,
            "primary_runtime": evidence.primary_runtime,
            "base_url": evidence.base_url,
            "is_fixture": evidence.is_fixture,
            "auth_verified": True,
            "is_pre_auth_init": False,
            "fallback_active": False,
        })
        return self

    def on_text_delta(self, text: Optional[str]) -> None:
        if text:
            self._emit({"type": "text", "text": str(text)})

    def on_tool_progress(
        self,
        event_type: str,
        tool_name: Optional[str] = None,
        preview: Any = None,
        args: Any = None,
        **kwargs: Any,
    ) -> None:
        name = tool_name or "unknown"
        key = kwargs.get("tool_call_id") or name
        if event_type == "tool.started":
            self._tool_started[key] = time.time()
            payload: Dict[str, Any] = {"type": "tool_use", "name": name}
            if kwargs.get("tool_call_id"):
                payload["tool_call_id"] = kwargs["tool_call_id"]
            if isinstance(args, dict):
                payload["input"] = args
            self._emit(payload)
        elif event_type == "tool.completed":
            duration = kwargs.get("duration") or (time.time() - self._tool_started.pop(key, time.time()))
            output = str(kwargs.get("result") or "")
            self._emit({
                "type": "tool_result",
                "name": name,
                **({"tool_call_id": kwargs["tool_call_id"]} if kwargs.get("tool_call_id") else {}),
                "output": output if len(output) <= 5000 else output[:5000] + "...",
                "duration_ms": int(float(duration) * 1000),
                "is_error": bool(kwargs.get("is_error", False)),
            })

    def emit_result(self, result: Any, session_id: str = "", exit_code: int = 0) -> int:
        """Write terminal result record and return process exit code.

        Re-extracts runtime at terminal response to catch mid-turn fallback
        (since attach snapshot alone is insufficient).
        """
        data = result if isinstance(result, dict) else {"final_response": "" if result is None else str(result)}
        exit_code = exit_code or (1 if data.get("failed") else 0)

        # RE-EXTRACT RUNTIME AT TERMINAL RESPONSE to catch mid-turn fallback
        mid_turn_fallback_detected = False
        if self._attached_agent is not None:
            try:
                terminal_evidence = self._adapter.extract_from_agent(self._attached_agent)
                if self._runtime_evidence is not None:
                    if (
                        terminal_evidence.resolved_provider != self._runtime_evidence.resolved_provider
                        or terminal_evidence.resolved_model != self._runtime_evidence.resolved_model
                    ):
                        mid_turn_fallback_detected = True
                        terminal_evidence.fallback_active = True
                self._runtime_evidence = terminal_evidence

                self._emit({
                    "type": "runtime_metadata",
                    "subtype": "terminal",
                    "evidence_strength": terminal_evidence.evidence_strength,
                    "attestation_type": terminal_evidence.attestation_type,
                    "resolved_provider": terminal_evidence.resolved_provider,
                    "resolved_model": terminal_evidence.resolved_model,
                    "primary_runtime": terminal_evidence.primary_runtime,
                    "base_url": terminal_evidence.base_url,
                    "is_fixture": terminal_evidence.is_fixture,
                    "auth_verified": True,
                    "is_pre_auth_init": False,
                    "fallback_active": mid_turn_fallback_detected,
                })
            except Exception as exc:
                mid_turn_fallback_detected = True
                self._emit({
                    "type": "runtime_metadata",
                    "subtype": "terminal",
                    "error": str(exc),
                    "auth_verified": False,
                    "is_pre_auth_init": False,
                    "fallback_active": True,
                    "resolved_provider": getattr(self._attached_agent, "provider", None),
                    "resolved_model": getattr(self._attached_agent, "model", None),
                })

        is_success = (exit_code == 0 and not bool(data.get("failed")) and not bool(data.get("error")))

        payload: Dict[str, Any] = {
            "type": "result",
            "session_id": session_id or self._session_id,
            "exit_code": exit_code,
            "text": data.get("final_response") or "",
            "tokens": {
                "input": data.get("input_tokens") or 0,
                "output": data.get("output_tokens") or 0,
                "total": data.get("total_tokens") or 0,
                "cache_read": data.get("cache_read_tokens") or 0,
                "cache_write": data.get("cache_write_tokens") or 0,
            },
            "duration_ms": int((time.time() - self._start) * 1000),
            "auth_verified": self._runtime_evidence is not None,
            "is_success_response": is_success,
            "is_fixture": self._runtime_evidence.is_fixture if self._runtime_evidence else False,
            "fallback_active": mid_turn_fallback_detected or bool(data.get("fallback_active", False)),
        }
        if self._runtime_evidence is not None:
            payload["resolved_provider"] = self._runtime_evidence.resolved_provider
            payload["resolved_model"] = self._runtime_evidence.resolved_model
            payload["primary_runtime"] = self._runtime_evidence.primary_runtime
        if data.get("failed"):
            payload["failed"] = True
        if data.get("error"):
            payload["error"] = str(data["error"])

        self._emit(payload)
        return exit_code

    def _default_emit(self, obj: Dict[str, Any]) -> None:
        try:
            sys.stdout.write(
                json.dumps({**obj, "timestamp": int(time.time() * 1000)}, ensure_ascii=False) + "\n"
            )
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            pass

    def _emit(self, obj: Dict[str, Any]) -> None:
        self._captured_events.append(obj)
        self._emit_fn(obj)

    @property
    def runtime_evidence(self) -> Optional[RuntimeEvidenceRecord]:
        return self._runtime_evidence

    @property
    def captured_events(self) -> List[Dict[str, Any]]:
        return list(self._captured_events)
