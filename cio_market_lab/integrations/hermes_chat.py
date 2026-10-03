"""Hermes Chat Bridge integration for CIO Market Lab.

Builds inspectable Main-CIO drafts and exposes one guarded local Hermes CLI bridge.
The bridge never accepts a shell command, provider, credential, or delivery target
from the browser client.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

PAPER_DISCLAIMER = (
    "DISCLAIMER: CIO Market Lab is in simulation-only mode. "
    "No broker connection or live capital execution exists. "
    "All decisions are paper simulation and research only."
)
HERMES_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

BOOTSTRAP_SCRIPT = Path(__file__).resolve().parent / "cio_hermes_bootstrap.py"
DEFAULT_HERMES_WORKSPACE = os.getenv(
    "CIO_HERMES_WORKSPACE",
    str(Path(__file__).resolve().parent.parent.parent / "artifacts" / "hermes_workspace")
)


def _hermes_agent_path() -> Optional[str]:
    """Resolve installed Hermes agent path if configured in environment or default install."""
    configured = os.getenv("HERMES_AGENT_PATH")
    if configured and Path(configured).is_dir():
        return configured
    default_agent = Path.home() / ".hermes" / "hermes-agent"
    if default_agent.is_dir():
        return str(default_agent)
    try:
        candidate = Path(sys.executable).resolve().parents[2]
        if (candidate / "cli.py").is_file():
            return str(candidate)
    except Exception:
        pass
    return None


def _hermes_python() -> str:
    """Resolve Python interpreter for installed Hermes CLI / bootstrap."""
    configured = os.getenv("HERMES_PYTHON")
    if configured and Path(configured).is_file():
        return configured

    agent_path = _hermes_agent_path()
    candidate_roots: List[Path] = []
    hermes_home_env = os.getenv("HERMES_HOME")
    if hermes_home_env:
        candidate_roots.append(Path(hermes_home_env))
    candidate_roots.append(Path.home() / ".hermes")
    if agent_path:
        agent_p = Path(agent_path).resolve()
        candidate_roots.append(agent_p.parent)

    # 1. Discover via installed bootstrap runtime metadata (facts.json)
    if agent_path:
        canonical_agent = str(Path(agent_path).resolve())
        agent_install_key = hashlib.sha256(canonical_agent.encode("utf-8")).hexdigest()[:16]
        for root in candidate_roots:
            facts_candidates = [
                root / "installs" / agent_install_key / "facts.json",
            ]
            installs_dir = root / "installs"
            if installs_dir.is_dir():
                try:
                    for child in sorted(installs_dir.iterdir()):
                        if child.is_dir() and child.name != agent_install_key:
                            facts_candidates.append(child / "facts.json")
                except Exception:
                    pass

            for facts_path in facts_candidates:
                if facts_path.is_file():
                    try:
                        data = json.loads(facts_path.read_text(encoding="utf-8"))
                        env_path = data.get("packages", {}).get("venv", {}).get("environment")
                        if env_path:
                            for py_name in ("python", "python3", "python3.14"):
                                py_candidate = Path(env_path) / "bin" / py_name
                                if py_candidate.is_file() and os.access(py_candidate, os.X_OK):
                                    return str(py_candidate)
                    except Exception:
                        pass

    # 2. Discover via executable discovery across installed environments
    for root in candidate_roots:
        installs_dir = root / "installs"
        if installs_dir.is_dir():
            for py_pattern in (
                "*/environments/*/venv/bin/python3.14",
                "*/environments/*/venv/bin/python3",
                "*/environments/*/venv/bin/python",
            ):
                for py_candidate in sorted(installs_dir.glob(py_pattern)):
                    if py_candidate.is_file() and os.access(py_candidate, os.X_OK):
                        return str(py_candidate)

    # 3. Fallback to agent-local venv if present and valid
    if agent_path:
        for py_name in ("python3.14", "python3", "python"):
            agent_venv_py = Path(agent_path) / "venv" / "bin" / py_name
            if agent_venv_py.is_file() and os.access(agent_venv_py, os.X_OK):
                return str(agent_venv_py)

    for py_name in ("python3.14", "python3", "python"):
        default_venv_py = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / py_name
        if default_venv_py.is_file() and os.access(default_venv_py, os.X_OK):
            return str(default_venv_py)

    return sys.executable


def _hermes_executable() -> str:
    """Resolve Hermes executable or project-local bootstrap."""
    configured = os.getenv("HERMES_CHAT_EXECUTABLE")
    if configured:
        return configured
    if BOOTSTRAP_SCRIPT.is_file():
        return str(BOOTSTRAP_SCRIPT)
    discovered = shutil.which("hermes")
    if discovered:
        return discovered
    raise RuntimeError("Hermes CLI executable was not found")


class HermesChatDraft(BaseModel):
    """Structured, inspectable chat draft payload destined for Hermes Desktop host."""

    symbol: str
    run_id: Optional[str] = None
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    visible_metrics: Dict[str, Any] = Field(default_factory=dict)
    evidence_paths: List[str] = Field(default_factory=list)
    user_prompt: str
    formatted_message: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    auto_send: bool = False
    disclaimer: str = PAPER_DISCLAIMER

    def to_inspectable_dict(self) -> Dict[str, Any]:
        return self.model_dump()


def build_chat_draft(
    symbol: str,
    user_prompt: str,
    run_id: Optional[str] = None,
    strategy_id: Optional[str] = None,
    strategy_version: Optional[str] = None,
    visible_metrics: Optional[Dict[str, Any]] = None,
    evidence_paths: Optional[Sequence[str]] = None,
    extra_context: Optional[Dict[str, Any]] = None,
) -> HermesChatDraft:
    """Build a structured draft packet for Main CIO review. Never auto-send."""
    metrics = visible_metrics or {}
    evidence = list(evidence_paths or [])
    metrics_str = "\n".join(f"  - **{key}**: {value}" for key, value in metrics.items()) if metrics else "  - (None provided)"
    evidence_str = "\n".join(f"  - `{path}`" for path in evidence) if evidence else "  - (No attached evidence files)"
    formatted_message = (
        f"### CIO Market Lab Research Query: {symbol}\n\n"
        f"**User Request:** {user_prompt}\n\n"
        f"**Context Snapshot:**\n"
        f"- **Symbol:** `{symbol}`\n"
        f"- **Strategy:** `{strategy_id or 'N/A'}` (v{strategy_version or 'N/A'})\n"
        f"- **Simulation Run ID:** `{run_id or 'current'}`\n\n"
        f"**Visible Metrics:**\n{metrics_str}\n\n"
        f"**Evidence & Logs:**\n{evidence_str}\n\n"
        f"> ⚠️ *{PAPER_DISCLAIMER}*"
    )
    metadata: Dict[str, Any] = {
        "client": "cio-market-lab-v1",
        "authority": "Main CIO",
        "action_required": "manual_review",
        **(extra_context or {}),
    }
    return HermesChatDraft(
        symbol=symbol,
        run_id=run_id,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        visible_metrics=metrics,
        evidence_paths=evidence,
        user_prompt=user_prompt,
        formatted_message=formatted_message,
        metadata=metadata,
        auto_send=False,
    )


import hashlib
import uuid
from cio_market_lab.domain.models import CIODecisionPacket, CIOExecutionReceipt
from cio_market_lab.integrations.runtime_evidence import RuntimeEvidenceAdapter, RuntimeEvidenceRecord

UNSAFE_HERMES_DEFAULT_MODEL = "hermes-3-llama-3.1-8b"
TRANSPORT_IDENTIFIER = "hermes-cli-cio-bridge"
DEFAULT_PINNED_PROVIDER_ID = os.getenv("CIO_PROVIDER_ID", "openai-codex")
DEFAULT_CIO_MODEL_ID = "gpt-6.1-sol"
ESCALATION_CIO_MODEL_ID = "gpt-6-astra"
DEFAULT_PINNED_MODEL_ID = os.getenv("CIO_MODEL_ID", DEFAULT_CIO_MODEL_ID)


def resolve_cio_route(*, escalation_reason: Optional[str] = None, provider_id: Optional[str] = None) -> Dict[str, str]:
    """Resolve one explicitly pinned paper-CIO route; escalation is never inferred/retried."""
    provider = provider_id or os.getenv("CIO_PROVIDER_ID", DEFAULT_PINNED_PROVIDER_ID)
    reason = (escalation_reason or "").strip().lower()
    allowed = {"high_consequence", "thesis_conflict", "multiasset_complexity"}
    if reason and reason not in allowed:
        raise ValueError(f"Unsupported CIO escalation reason: {escalation_reason}")
    return {"provider_id": provider, "model_id": ESCALATION_CIO_MODEL_ID if reason else DEFAULT_CIO_MODEL_ID,
            "route": "exceptional_escalation" if reason else "default", "escalation_reason": reason}



def build_production_chat_command(
    *,
    session_id: str = "cio-market-lab",
    workspace_root: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    query_file: Optional[str] = "-",
    query: Optional[str] = None,
    format_type: str = "stream-json",
    reasoning: str = "medium",
    max_turns: int = 80,
    run_budget: float = 180.0,
    toolsets: str = "none",
    ignore_rules: bool = True,
    oneshot: bool = True,
    source: str = "tool",
    test_fixture_transport: bool = False,
) -> List[str]:
    """Build exact production command executing official Hermes bootstrap FIRST followed by entrypoint."""
    # Production chat commands may not bypass the semantic CIO router. The public
    # builder remains usable for fixture/custom executor tests, but any supplied
    # model/provider must match one of the canonical CIO route pairs.
    executable = _hermes_executable()
    effective_workspace = workspace_root or DEFAULT_HERMES_WORKSPACE

    if executable.endswith(".py"):
        agent_path = _hermes_agent_path()
        agent_path_code = (
            f"agent_path = {repr(agent_path)}\n"
            "if agent_path and agent_path not in sys.path:\n"
            "    sys.path.insert(0, agent_path)\n"
        ) if agent_path else ""

        bootstrap_activation_code = (
            "import sys\n"
            f"{agent_path_code}"
            "try:\n"
            "    import hermes_bootstrap\n"
            "except ModuleNotFoundError as exc:\n"
            "    if exc.name != 'hermes_bootstrap':\n"
            "        raise\n"
            "import runpy\n"
            "sys.argv = sys.argv[1:]\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n"
        )
        cmd_prefix = [
            _hermes_python(),
            "-c",
            bootstrap_activation_code,
            executable,
        ]
    else:
        cmd_prefix = [executable]

    command = [
        *cmd_prefix,
        "chat",
    ]
    if query_file:
        command.extend(["--query-file", query_file])
    elif query:
        command.extend(["--query", query])

    if oneshot:
        command.append("--oneshot")

    command.extend([
        "--format",
        format_type,
        "--session-id",
        session_id,
        "--toolsets",
        toolsets,
    ])

    if ignore_rules:
        command.append("--ignore-rules")

    command.extend([
        "--source",
        source,
        "--reasoning",
        reasoning,
        "--max-turns",
        str(int(max_turns)),
        "--run-budget",
        str(int(run_budget) if float(run_budget).is_integer() else run_budget),
        "--in",
        effective_workspace,
    ])

    if provider:
        command.extend(["--provider", provider])
    if model:
        command.extend(["--model", model])

    if test_fixture_transport:
        command.append("--test-fixture-transport")

    return command


def run_hermes_cli_chat(
    message: str,
    *,
    session_id: str = "cio-market-lab",
    workspace_root: Optional[str] = None,
    timeout_seconds: int = 240,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    enforce_cio_pin: bool = False,
    escalation_reason: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
    transport: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run one real Hermes Agent turn through project-local bootstrap subprocess with pinned model/provider."""
    clean_message = message.strip()
    if not clean_message:
        raise ValueError("message must not be empty")
    if not HERMES_SESSION_RE.fullmatch(session_id):
        raise ValueError("invalid Hermes session id")

    # If caller explicitly provided the unsafe default model, prohibit masquerading
    if model == UNSAFE_HERMES_DEFAULT_MODEL:
        raise ValueError(
            f"Explicit configured authenticated model required. '{UNSAFE_HERMES_DEFAULT_MODEL}' "
            "must not masquerade as Main CIO."
        )

    target_provider = provider
    target_model = model

    if enforce_cio_pin:
        route = resolve_cio_route(escalation_reason=escalation_reason, provider_id=provider or DEFAULT_PINNED_PROVIDER_ID)
        # Low-level fixture transports retain caller-supplied values. Production
        # routing is fixed by the executor's semantic route and cannot be overridden.
        if transport is None:
            if target_model is not None and target_model != route["model_id"]:
                raise ValueError("CIO route override rejected: model conflicts with semantic router")
            target_provider = route["provider_id"]
            target_model = route["model_id"]
        target_provider = target_provider or os.getenv("CIO_PROVIDER_ID", DEFAULT_PINNED_PROVIDER_ID)
        target_model = target_model or os.getenv("CIO_MODEL_ID", DEFAULT_PINNED_MODEL_ID)
        if not target_model or target_model == UNSAFE_HERMES_DEFAULT_MODEL:
            raise ValueError(
                f"Explicit configured authenticated model required. '{UNSAFE_HERMES_DEFAULT_MODEL}' "
                "must not masquerade as Main CIO."
            )
        if not target_provider or target_provider == TRANSPORT_IDENTIFIER:
            raise ValueError(
                f"Explicit configured authenticated provider required. Transport '{TRANSPORT_IDENTIFIER}' "
                "must not masquerade as inference provider."
            )

    effective_workspace = workspace_root or DEFAULT_HERMES_WORKSPACE

    # Explicit test transport injection via dependency injection (never through environment)
    if transport is not None:
        result = transport(
            clean_message,
            session_id=session_id,
            workspace_root=effective_workspace,
            timeout_seconds=timeout_seconds,
            model=target_model,
            provider=target_provider,
            enforce_cio_pin=enforce_cio_pin,
            escalation_reason=escalation_reason,
            extra_env=extra_env,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Injected transport returned non-dict response")
        # Every injected result is marked fixture and strictly rejected by production verifier
        result["is_fixture"] = True
        if isinstance(result.get("runtime_metadata"), dict):
            result["runtime_metadata"]["is_fixture"] = True
        return result

    command = build_production_chat_command(
        session_id=session_id,
        workspace_root=effective_workspace,
        provider=target_provider,
        model=target_model,
        query_file="-",
        format_type="stream-json",
        reasoning="medium",
        max_turns=80,
        run_budget=180.0,
        toolsets="none",
        ignore_rules=True,
        oneshot=True,
        source="tool",
    )

    run_env = dict(os.environ)
    agent_path = _hermes_agent_path()
    if agent_path:
        run_env.setdefault("HERMES_AGENT_PATH", agent_path)
    hermes_py = _hermes_python()
    if hermes_py:
        run_env.setdefault("HERMES_PYTHON", hermes_py)
    run_env.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")
    if extra_env:
        run_env.update(extra_env)

    child = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=run_env,
        start_new_session=True,
    )
    try:
        stdout, stderr = child.communicate(input=clean_message, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        # Partial stream-json output survives communicate() timeout as bytes,
        # even with text=True. Whitelist metadata only, never prompt or stderr.
        event_types = {"system", "runtime_metadata", "text", "result"}
        partial = exc.stdout or b""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        observed_events: List[str] = []
        for line in partial.splitlines():
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(event, dict) and event.get("type") in event_types:
                observed_events.append(event["type"])
        exc.bridge_diagnostic = {
            "event_types": observed_events,
            "stderr_present": bool(exc.stderr),
            "stdout_bytes_seen": len(partial.encode("utf-8")),
        }
        # subprocess.run kills only the direct child. Bootstrap descendants may
        # inherit stdout/stderr and survive; terminate this call's private group.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.communicate()
        raise
    completed = subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
    if completed.returncode != 0:
        events = []
        for raw_line in (completed.stdout or "").splitlines():
            line = raw_line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
        err_msg = ""
        for ev in reversed(events):
            if ev.get("error"):
                err_msg = str(ev["error"])
                break
        detail = err_msg or (completed.stderr or completed.stdout or "Hermes chat failed").strip()
        raise RuntimeError(f"Hermes subprocess returned nonzero exit code {completed.returncode}: {detail[-2000:]}")

    response = ""
    resolved_session_id = session_id
    runtime_metadata: Dict[str, Any] = {}
    events: List[Dict[str, Any]] = []

    has_explicit_post_auth = False
    has_terminal_result = False
    terminal_result_failed = False
    terminal_error: Optional[str] = None
    terminal_exit_code = 0

    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(event)
        event_type = event.get("type")

        if event_type == "system" and event.get("subtype") == "init":
            # Note: stream_json emits init BEFORE auth; model is unverified CLI argument echo
            # NEVER trust pre-auth init as provider source or mark auth_verified.
            if "model" in event:
                runtime_metadata.setdefault("resolved_model", event["model"])
            if "session_id" in event:
                resolved_session_id = str(event["session_id"])
            if not has_explicit_post_auth:
                runtime_metadata["is_pre_auth_init"] = True
                runtime_metadata["auth_verified"] = False

        elif event_type == "runtime_metadata":
            # Explicit post-auth source
            has_explicit_post_auth = True
            runtime_metadata["is_pre_auth_init"] = False
            runtime_metadata["auth_verified"] = True
            if isinstance(event.get("metadata"), dict):
                runtime_metadata.update(event["metadata"])
            for k in (
                "resolved_provider",
                "resolved_model",
                "provider",
                "model",
                "evidence_strength",
                "attestation_type",
                "is_cryptographic_attestation",
                "primary_runtime",
                "base_url",
                "requested_provider",
                "is_fixture",
                "fallback_active",
            ):
                if k in event:
                    runtime_metadata[k] = event[k]

        elif event_type == "result":
            has_terminal_result = True
            response = str(event.get("text") or event.get("content") or "").strip()
            if event.get("session_id"):
                resolved_session_id = str(event["session_id"])
            if "tokens" in event and isinstance(event["tokens"], dict):
                runtime_metadata["tokens"] = event["tokens"]
            terminal_exit_code = event.get("exit_code", 0)
            runtime_metadata["exit_code"] = terminal_exit_code
            if event.get("failed"):
                terminal_result_failed = True
                runtime_metadata["failed"] = True
            if event.get("error"):
                terminal_error = str(event["error"])
                runtime_metadata["error"] = terminal_error
            # Catch mid-turn fallback from terminal response
            if "runtime_metadata" in event and isinstance(event["runtime_metadata"], dict):
                has_explicit_post_auth = True
                runtime_metadata["is_pre_auth_init"] = False
                runtime_metadata["auth_verified"] = True
                runtime_metadata.update(event["runtime_metadata"])
            if "fallback_active" in event:
                runtime_metadata["fallback_active"] = bool(event["fallback_active"])

    # Fail closed on pre-auth only stream even if it contains provider/model
    if not has_explicit_post_auth:
        runtime_metadata["is_pre_auth_init"] = True
        runtime_metadata["auth_verified"] = False
        runtime_metadata.pop("resolved_provider", None)

    # Validate terminal result existence and status
    if not has_terminal_result:
        runtime_metadata["is_success_response"] = False
    else:
        runtime_metadata["is_success_response"] = (
            terminal_exit_code == 0
            and not terminal_result_failed
            and not terminal_error
        )

    if enforce_cio_pin:
        if not has_terminal_result:
            raise RuntimeError(
                "READBACK_VERIFICATION_FAILED: Missing terminal result event. Successful terminal result required."
            )
        if terminal_exit_code != 0:
            raise RuntimeError(
                f"READBACK_VERIFICATION_FAILED: Terminal result reported nonzero exit code {terminal_exit_code}"
            )
        if terminal_result_failed or terminal_error:
            raise RuntimeError(
                f"READBACK_VERIFICATION_FAILED: Terminal result reported error/failed: {terminal_error or 'failed=True'}"
            )

    if not response:
        raise RuntimeError("Hermes chat returned no final response")

    return {
        "transport": "hermes_cli",
        "transport_identifier": TRANSPORT_IDENTIFIER,
        "status": "completed" if runtime_metadata.get("is_success_response", True) else "failed",
        "response": response,
        "session_id": resolved_session_id,
        "returncode": completed.returncode,
        "pinned_model": target_model,
        "requested_model": target_model,
        "requested_provider": target_provider,
        "runtime_metadata": runtime_metadata,
        "resolved_model": runtime_metadata.get("resolved_model"),
        "resolved_provider": runtime_metadata.get("resolved_provider"),
        "evidence_strength": runtime_metadata.get("evidence_strength", "local-runtime"),
        "is_fixture": runtime_metadata.get("is_fixture", False),
        "auth_verified": runtime_metadata.get("auth_verified", False),
        "is_success_response": runtime_metadata.get("is_success_response", False),
        "events": events,
    }


class HermesCIORuntimeContract(BaseModel):
    """Runtime contract declaring bounded Hermes bridge interface for Main CIO."""

    provider_id: str = Field(default_factory=lambda: os.getenv("CIO_PROVIDER_ID", DEFAULT_PINNED_PROVIDER_ID))
    model_id: str = Field(default_factory=lambda: os.getenv("CIO_MODEL_ID", DEFAULT_PINNED_MODEL_ID))
    pinned_provider: str = Field(default_factory=lambda: os.getenv("CIO_PROVIDER_ID", DEFAULT_PINNED_PROVIDER_ID))
    pinned_model: str = Field(default_factory=lambda: os.getenv("CIO_MODEL_ID", DEFAULT_PINNED_MODEL_ID))
    transport_identifier: str = TRANSPORT_IDENTIFIER
    authority: str = "MAIN_CIO"
    worker_role: str = "ENGINEERING_WORKER_ONLY"
    transport: str = "local_cli_stream_json"
    transport_name: str = "hermes_cli"
    execution_mode: str = "subprocess_bridge"
    execution_command: str = "hermes chat --query-file - --oneshot --format stream-json --session-id <session_id> --toolsets none --ignore-rules --provider <pinned_provider> --model <pinned_model>"
    input_contract: Dict[str, Any] = Field(default_factory=lambda: {
        "required_fields": [
            "symbol",
            "current_market_phase",
            "candidate_signals",
            "portfolio_context",
            "retrieved_prior_lessons",
            "past_outcomes",
            "rejected_opportunities",
            "tactical_risk_bounds",
        ],
        "format": "Markdown or JSON CIO decision context request",
    })
    output_contract: Dict[str, Any] = Field(default_factory=lambda: {
        "required_schema": "CIODecisionPacket",
        "full_schema": CIODecisionPacket.model_json_schema(),
        "required_fields": list(CIODecisionPacket.model_fields.keys()),
        "provenance_invariant": "provenance.authority must be 'MAIN_CIO'. Scripted or AGY verdicts rejected.",
        "pinned_provider": DEFAULT_PINNED_PROVIDER_ID,
        "pinned_model": DEFAULT_PINNED_MODEL_ID,
    })
    supported_states: List[str] = Field(default_factory=lambda: [
        "READY",
        "DRAFT_INSPECTABLE",
    ])
    unavailable_states: Dict[str, str] = Field(default_factory=lambda: {
        "UNCONFIGURED_OR_UNSAFE_MODEL": "Default hermes-3-llama-3.1-8b prohibited from masquerading as Main CIO; explicit authenticated provider/model required.",
        "UNCONFIGURED_OR_TRANSPORT_PROVIDER": "Transport identifier hermes-cli-cio-bridge prohibited from masquerading as inference provider; explicit authenticated provider required.",
        "NO_CONFIGURED_EXECUTOR": "No CIO decision provider is registered; runner produces explicit blocked status.",
        "HERMES_CLI_NOT_FOUND": "Hermes executable is not installed or discoverable on the host filesystem.",
        "HERMES_TIMEOUT": "Subprocess execution exceeded timeout (default 240 seconds).",
        "HERMES_NONZERO_EXIT": "Process exited with non-zero status; fail closed to NO_TRADE.",
        "MALFORMED_CIO_PACKET": "Response is unparseable or violates CIODecisionPacket schema or provenance.",
        "READBACK_VERIFICATION_FAILED": "Runtime metadata missing or mismatched with pinned provider/model; fail closed without verified receipt.",
        "PROVENANCE_AUTHENTICATION_FAILED": "Self-asserted MAIN_CIO string lacks authenticated signature or receipt.",
    })


def get_hermes_runtime_contract() -> HermesCIORuntimeContract:
    return HermesCIORuntimeContract()


class HermesCIODecisionExecutor:
    """Executes CIO decision request against local Hermes CLI bridge when configured."""

    def __init__(
        self,
        session_id: str = "cio-market-lab",
        workspace_root: Optional[str] = None,
        timeout_seconds: int = 240,
        provider_id: Optional[str] = None,
        model_id: Optional[str] = None,
        allow_fixture: bool = True,
        is_production: bool = False,
        transport: Optional[Callable[..., Dict[str, Any]]] = None,
        escalation_reason: Optional[str] = None,
    ) -> None:
        self.session_id = session_id
        self.workspace_root = workspace_root or DEFAULT_HERMES_WORKSPACE
        self.timeout_seconds = timeout_seconds
        self.provider_id = (
            provider_id if provider_id is not None
            else os.getenv("CIO_PROVIDER_ID", DEFAULT_PINNED_PROVIDER_ID)
        )
        route = resolve_cio_route(escalation_reason=escalation_reason, provider_id=self.provider_id)
        self.routing_receipt = route
        self.model_id = model_id if model_id is not None else route["model_id"]
        if is_production and (self.provider_id, self.model_id) != (route["provider_id"], route["model_id"]):
            raise ValueError("LEGACY_MODEL_OVERRIDE_BLOCKED: explicit model/provider conflicts with canonical CIO route")
        if escalation_reason and model_id is not None and model_id != route["model_id"]:
            raise ValueError("Explicit model pin conflicts with requested escalation route")
        if is_production or os.getenv("CIO_PRODUCTION_EXECUTOR") == "1":
            self.allow_fixture = False
        else:
            self.allow_fixture = allow_fixture
        self.is_production = not self.allow_fixture
        if self.is_production and self.model_id != route["model_id"]:
            raise ValueError("LEGACY_MODEL_OVERRIDE_BLOCKED: explicit model conflicts with canonical CIO route")
        if self.is_production and self.provider_id != "openai-codex":
            raise ValueError("LEGACY_PROVIDER_OVERRIDE_BLOCKED: subscription CIO requires openai-codex")
        self.transport = transport
        self.last_receipt: Optional[CIOExecutionReceipt] = None
        self.runtime_adapter = RuntimeEvidenceAdapter(
            pinned_provider=self.provider_id,
            pinned_model=self.model_id,
        )

    def is_available(self) -> bool:
        if self.transport is None:
            try:
                _hermes_executable()
            except Exception:
                return False
        # Fail closed: must have an explicitly configured, non-empty model that is NOT the unsafe default
        if not self.model_id or self.model_id == UNSAFE_HERMES_DEFAULT_MODEL:
            return False
        # Must have an explicitly configured, non-empty provider that is NOT the transport identifier
        if not self.provider_id or self.provider_id == TRANSPORT_IDENTIFIER:
            return False
        return True

    def request_decision(self, context_request: Any) -> Optional[CIODecisionPacket]:
        """Request CIO decision. If unavailable, fails closed without fabricating trades."""
        if not self.is_available():
            if not self.model_id or self.model_id == UNSAFE_HERMES_DEFAULT_MODEL:
                raise RuntimeError(
                    f"EXECUTOR_UNAVAILABLE: Unsafe or unconfigured CIO model '{self.model_id}'. "
                    f"Explicit configured authenticated provider/model required. "
                    f"'{UNSAFE_HERMES_DEFAULT_MODEL}' must not masquerade as Main CIO."
                )
            if not self.provider_id or self.provider_id == TRANSPORT_IDENTIFIER:
                raise RuntimeError(
                    f"EXECUTOR_UNAVAILABLE: Unsafe or unconfigured CIO provider '{self.provider_id}'. "
                    f"Transport identifier '{TRANSPORT_IDENTIFIER}' is not an inference provider. "
                    f"Explicit configured authenticated inference provider required."
                )
            raise RuntimeError("EXECUTOR_UNAVAILABLE: Hermes CLI executable was not found on host")

        from cio_market_lab.engine.cio_packet import sign_cio_packet

        schema_json = json.dumps(CIODecisionPacket.model_json_schema(), indent=2)

        paper_mandate = (
            "PROJECT MONEY PAPER-ONLY MANDATE:\n"
            "- Objective: Autonomous one-month terminal NAV growth on isolated paper simulation ledger.\n"
            "- Capital Boundaries: Absolutely NO client real holdings, NO broker connection, and NO real money.\n"
            "- Worker Role: Antigravity (AGY) engineering only; worker handles execution, accounting, and verification.\n"
            "- Governance: Approved paper tool scope is NOT a live-account mandate. Real broker/holding mutation is strictly forbidden.\n"
            "- Authority: You are the authoritative Main CIO making paper portfolio investment decisions."
        )

        prompt = (
            f"You are the MAIN CIO.\n"
            f"{paper_mandate}\n\n"
            f"Review the attached research, quotes, and prior lessons:\n"
            f"{json.dumps(context_request.model_dump(mode='json'), indent=2)}\n\n"
            f"You must make an authoritative investment decision for the paper portfolio.\n"
            f"Output ONLY a valid JSON object strictly conforming to the following CIODecisionPacket JSON schema:\n"
            f"```json\n{schema_json}\n```\n"
            f"Required invariants:\n"
            f"- provenance.authority must be 'MAIN_CIO'\n"
            f"- provenance.actor_role must be 'CHIEF_INVESTMENT_OFFICER'\n"
            f"- provenance.signer_id must be an authorized CIO signer ('hermes-bridge-cio' or 'main-cio')\n"
            f"- action must be one of: BUY, SELL, HOLD, REJECT, NO_TRADE\n"
            f"- expiry must be a valid future ISO-8601 timestamp\n"
            f"- Output MUST be pure JSON with no markdown wrapping or additional commentary.\n"
            f"- Return a CONCISE decision, target at most 2500 characters. Keep each rationale to one sentence.\n"
            f"- DO NOT echo canonical_portfolio, verified_quotes, fx_accounting, tool_eligibility, or predecision_snapshot.\n"
            f"- Omit optional predecision_snapshot and predecision_version: the authenticated bridge binds the exact input snapshot.\n"
            f"- Do not copy the schema or context into the response. No tools are available; use supplied evidence only.\n"
            f"- A justified BUY/SELL is permitted; do not default to NO_TRADE merely because this is a paper test.\n"
            f"- Use the request's symbol as selected_instrument, including NO_TRADE/HOLD; cash preservation is action NO_TRADE, not a fabricated CASH security.\n"
            f"- For BUY/SELL include conditions.instrument_type='EQUITY' or 'ETF' (the runtime enum), limit_price, "
            f"risk bounds and a future review condition. Missing/stale required evidence must still fail closed."
        )
        result = run_hermes_cli_chat(
            prompt,
            session_id=self.session_id,
            workspace_root=self.workspace_root,
            timeout_seconds=self.timeout_seconds,
            model=self.model_id,
            provider=self.provider_id,
            enforce_cio_pin=True,
            transport=self.transport,
            escalation_reason=self.routing_receipt.get("escalation_reason") or None,
        )

        # 1. Parse and validate runtime readback metadata via isolated project-local adapter
        raw_metadata = result.get("runtime_metadata")
        if not isinstance(raw_metadata, dict):
            raise RuntimeError("MALFORMED_RUNTIME_METADATA: CLI output missing valid runtime metadata dict")

        if "resolved_provider" not in raw_metadata and result.get("resolved_provider"):
            raw_metadata["resolved_provider"] = result["resolved_provider"]
        if "resolved_model" not in raw_metadata and result.get("resolved_model"):
            raw_metadata["resolved_model"] = result["resolved_model"]

        # Production executor must reject subprocess/metadata errors even if subprocess return code was zero
        if result.get("returncode", 0) != 0:
            raise RuntimeError(
                f"READBACK_VERIFICATION_FAILED: Process exited with non-zero exit code {result.get('returncode')}"
            )
        if result.get("failed") is True or raw_metadata.get("failed") is True:
            raise RuntimeError("READBACK_VERIFICATION_FAILED: Result reported failed=True")
        if result.get("error") or raw_metadata.get("error"):
            err_msg = result.get("error") or raw_metadata.get("error")
            raise RuntimeError(f"READBACK_VERIFICATION_FAILED: Result reported error: {err_msg}")

        resp_text = result.get("response", "")
        evidence = self.runtime_adapter.verify_runtime_evidence(
            metadata=raw_metadata,
            response_text=resp_text,
            exit_code=result.get("returncode", 0),
            pinned_provider=self.provider_id,
            pinned_model=self.model_id,
            allow_fixture=self.allow_fixture,
        )

        # Production executor must reject fixture=true
        if not self.allow_fixture and (evidence.is_fixture or raw_metadata.get("is_fixture") or result.get("is_fixture")):
            raise RuntimeError("PRODUCTION_EXECUTOR_REJECTED_FIXTURE: Production executor rejected fixture=True")

        # Production executor must reject auth_verified=false
        if not evidence.auth_verified or raw_metadata.get("auth_verified") is False:
            raise RuntimeError("READBACK_VERIFICATION_FAILED: Authentication not verified (auth_verified=False)")

        # Production executor must reject is_success_response=false
        if not evidence.is_success_response or raw_metadata.get("is_success_response") is False:
            raise RuntimeError("READBACK_VERIFICATION_FAILED: Response marked unsuccessful (is_success_response=False)")

        # Production executor must reject metadata nonzero return code
        meta_rc = raw_metadata.get("exit_code") if raw_metadata.get("exit_code") is not None else raw_metadata.get("returncode")
        if meta_rc is not None and meta_rc != 0:
            raise RuntimeError(f"READBACK_VERIFICATION_FAILED: Nonzero exit code reported in metadata: {meta_rc}")

        resolved_provider = evidence.resolved_provider
        resolved_model = evidence.resolved_model

        # 2. Parse response text and validate packet schema
        m = re.search(r"\{.*\}", resp_text, re.DOTALL)
        if not m:
            raise RuntimeError(f"MALFORMED_CIO_PACKET: No JSON found in Hermes response: {resp_text[:200]}")
        try:
            data = json.loads(m.group(0))
        except Exception as exc:
            raise RuntimeError(f"MALFORMED_CIO_PACKET: Invalid JSON structure in Hermes response: {exc}")
        try:
            packet = CIODecisionPacket.model_validate(data)
        except Exception as exc:
            raise RuntimeError(f"MALFORMED_CIO_PACKET: Failed CIODecisionPacket schema validation: {exc}")

        # Reject fixture packet if production executor
        if not self.allow_fixture and getattr(packet, "is_fixture", False):
            raise RuntimeError("PRODUCTION_EXECUTOR_REJECTED_FIXTURE: Production executor rejected fixture=True packet")

        # Identity belongs to immutable caller context, not model prose.
        # Different daily contexts must not collapse into one evaluable case;
        # retries of one request remain idempotent. No economic decision changes.
        request_identity = context_request.model_dump(mode="json").get("request_id")
        if request_identity:
            case_identity = self.session_id + "\0" + str(request_identity)
            packet.case_id = "case-" + hashlib.sha256(case_identity.encode("utf-8")).hexdigest()[:24]

        # The source snapshot is already frozen input, not model-generated text.
        # Bind it locally after authenticating the response. Never trust an echoed
        # snapshot to rewrite quotes/NAV/FX, or spend latency generating it again.
        import copy
        packet.predecision_snapshot = copy.deepcopy(getattr(context_request, "predecision_snapshot", {}))
        packet.predecision_version = getattr(context_request, "predecision_version", "v1")

        # 3. Generate execution readback receipt with verified runtime values (NEVER requested strings alone)
        receipt_id = f"receipt-{uuid.uuid4()}"
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        response_hash = hashlib.sha256(resp_text.encode("utf-8")).hexdigest()
        receipt = CIOExecutionReceipt(
            receipt_id=receipt_id,
            case_id=packet.case_id,
            session_id=self.session_id,
            provider_id=resolved_provider,
            model_id=resolved_model,
            timestamp=datetime.now(timezone.utc),
            raw_prompt_hash=prompt_hash,
            raw_response_hash=response_hash,
            readback_verified=True,
            authority="MAIN_CIO",
            evidence_strength=evidence.evidence_strength,
            transport=TRANSPORT_IDENTIFIER,
            requested_provider=self.provider_id,
            requested_model=self.model_id,
        )
        self.last_receipt = receipt

        # 4. Sign with authenticated bridge provenance linking to readback receipt
        sign_cio_packet(packet, signer_id="hermes-bridge-cio", receipt_id=receipt_id)
        return packet
