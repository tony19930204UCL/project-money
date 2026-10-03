#!/usr/bin/env python3
"""Project-local Hermes bootstrap for CIO bridge.

Delivers a project-local entrypoint used by actual bridge using installed Hermes CLI
dependency tree only. Staged source (artifacts/hermes_source_readonly) is reference-only
and is NEVER added to sys.path or imported in production.
Installs RuntimeEvidenceStreamJsonEmitter during subprocess execution,
re-extracts runtime at terminal response, and supports genuine chat conversation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Optional

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Configured installed agent path if provided or discoverable from defaults
_configured_agent_path = os.environ.get("HERMES_AGENT_PATH")
if not _configured_agent_path or not Path(_configured_agent_path).is_dir():
    _default_agent = Path.home() / ".hermes" / "hermes-agent"
    if _default_agent.is_dir():
        _configured_agent_path = str(_default_agent)
    else:
        try:
            _candidate = Path(sys.executable).resolve().parents[2]
            if (_candidate / "cli.py").is_file():
                _configured_agent_path = str(_candidate)
        except Exception:
            pass

if _configured_agent_path and Path(_configured_agent_path).is_dir():
    if str(_configured_agent_path) not in sys.path:
        sys.path.insert(0, str(_configured_agent_path))

# Activate official installed bootstrap BEFORE importing project third-party dependencies (e.g. pydantic)
try:
    import hermes_bootstrap  # noqa: F401
except ModuleNotFoundError as exc:
    if exc.name != "hermes_bootstrap":
        raise

# NOTE: Staged source under artifacts/hermes_source_readonly is reference-only.
# It is NEVER appended to sys.path or imported. Missing installed CLI must fail explicitly.

from cio_market_lab.integrations.runtime_evidence import (
    RuntimeEvidenceAdapter,
    RuntimeEvidenceRecord,
    RuntimeEvidenceStreamJsonEmitter,
    EVIDENCE_STRENGTH_LOCAL_RUNTIME,
    TRANSPORT_IDENTIFIER,
    UNSAFE_HERMES_DEFAULT_MODEL,
)


class FakeAgentFixture:
    """Fake Hermes agent fixture explicitly labeled for integration testing only."""
    is_fixture: bool = True

    def __init__(self, provider: str = "openai-codex", model: str = "gpt-6-astra"):
        self.provider = provider
        self.model = model
        self.requested_provider = provider
        self.base_url = "https://fixture.internal/v1"
        self._primary_runtime = {
            "provider": provider,
            "model": model,
            "requested_provider": provider,
            "base_url": "https://fixture.internal/v1",
            "api_mode": "responses",
        }
        self.stream_delta_callback = None
        self.tool_progress_callback = None


def install_runtime_evidence_emitter() -> None:
    """Install RuntimeEvidenceStreamJsonEmitter into stream_json modules if available."""
    try:
        import stream_json
        stream_json.StreamJsonEmitter = RuntimeEvidenceStreamJsonEmitter
    except Exception:
        pass
    try:
        import hermes_cli.stream_json as h_stream_json
        h_stream_json.StreamJsonEmitter = RuntimeEvidenceStreamJsonEmitter
    except Exception:
        pass


def run_chat_command(argv: List[str]) -> int:
    """Execute Hermes chat command using installed Hermes CLI."""
    parser = argparse.ArgumentParser(description="Hermes CIO Bootstrap Chat")
    parser.add_argument("--query-file", default=None, help="Query file path or '-' for stdin")
    parser.add_argument("-q", "--query", default=None, help="Inline query string")
    parser.add_argument("--format", default="text", choices=["text", "stream-json"], help="Output format")
    parser.add_argument("--provider", default=None, help="Provider ID")
    parser.add_argument("--model", default=None, help="Model ID")
    parser.add_argument("--continue", dest="continue_session", default=None, help="Resume session ID")
    parser.add_argument("--session-id", default=None, help="Session identifier for tracking/emitter")
    parser.add_argument("--create-if-missing", action="store_true", default=False, help="Create session if missing when continuing")
    parser.add_argument("--oneshot", action="store_true", default=False, help="Isolated oneshot session without resuming history")
    parser.add_argument("--source", default="tool")
    parser.add_argument("--reasoning", default="medium")
    parser.add_argument("--max-turns", type=int, default=80)
    parser.add_argument("--run-budget", type=float, default=180.0)
    parser.add_argument("--in", dest="workspace_in", default=None)
    parser.add_argument("--toolsets", default=None, help="Comma-separated toolsets or empty/none for zero tools")
    parser.add_argument("--ignore-rules", action="store_true", default=False, help="Disable context files and memory")
    parser.add_argument("--test-fixture-transport", action="store_true", default=False, help="Explicitly labeled offline fixture transport for integration testing only")

    args, unknown = parser.parse_known_args(argv)

    # Honor max turns/run budget/session/source flags or explicitly reject unsupported flags rather than silently ignore
    if unknown:
        print(f"Error: Unsupported CLI flags rejected: {unknown}", file=sys.stderr)
        return 1

    if args.source not in ("tool", "cli", "desktop", "api"):
        print(f"Error: Unsupported source flag '{args.source}'", file=sys.stderr)
        return 1

    if args.max_turns is not None and args.max_turns <= 0:
        print(f"Error: max_turns must be positive, got {args.max_turns}", file=sys.stderr)
        return 1

    if args.run_budget is not None and args.run_budget <= 0:
        print(f"Error: run_budget must be positive, got {args.run_budget}", file=sys.stderr)
        return 1

    # Explicitly reject unsupported flag combinations
    if args.oneshot and args.continue_session:
        print("Error: Unsupported combination: --oneshot cannot be combined with --continue", file=sys.stderr)
        return 1

    if args.oneshot and args.create_if_missing:
        print("Error: Unsupported combination: --oneshot cannot be combined with --create-if-missing", file=sys.stderr)
        return 1

    if args.create_if_missing and not args.continue_session:
        print("Error: Unsupported combination: --create-if-missing requires --continue", file=sys.stderr)
        return 1

    # Read query
    query_text = ""
    if args.query_file == "-":
        query_text = sys.stdin.read().strip()
    elif args.query_file and Path(args.query_file).is_file():
        query_text = Path(args.query_file).read_text(encoding="utf-8").strip()
    elif args.query:
        query_text = args.query.strip()

    is_stream_json = (args.format == "stream-json")
    provider = args.provider or os.getenv("CIO_PROVIDER_ID", "openai-codex")
    model = args.model or os.getenv("CIO_MODEL_ID", "gpt-6-astra")

    # Determine genuine session isolation semantics
    if args.oneshot:
        resume = None
        session_id = args.session_id or f"cio-oneshot-{int(time.time() * 1000)}"
    elif args.continue_session:
        resume = args.continue_session
        session_id = args.session_id or args.continue_session
    else:
        resume = None
        session_id = args.session_id or "cio-market-lab"

    # Install the emitter into stream_json namespaces
    install_runtime_evidence_emitter()

    if args.workspace_in:
        ws_path = Path(args.workspace_in).resolve()
        ws_path.mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_WORKSPACE"] = str(ws_path)
        os.environ["TERMINAL_CWD"] = str(ws_path)
        try:
            os.chdir(str(ws_path))
        except Exception:
            pass

    toolsets_param = (
        []
        if (args.toolsets == "" or args.toolsets == "none" or args.toolsets == "[]")
        else (args.toolsets.split(",") if args.toolsets else None)
    )

    # Fixture transport check
    if args.test_fixture_transport:
        if os.getenv("CIO_PRODUCTION_EXECUTOR") == "1":
            err_msg = "Error: Fixture transport is strictly prohibited and unavailable in production (CIO_PRODUCTION_EXECUTOR=1)"
            if is_stream_json:
                emitter = RuntimeEvidenceStreamJsonEmitter(model=model, session_id=session_id)
                emitter.emit_result({"failed": True, "error": err_msg}, session_id=session_id, exit_code=1)
            else:
                print(err_msg, file=sys.stderr)
            return 1

        # Offline fixture transport execution
        fake_agent = FakeAgentFixture(provider=provider, model=model)
        fixture_packet = {
            "case_id": "case-subprocess-fixture-001",
            "symbol": "2330.TW",
            "action": "BUY",
            "is_fixture": True,
            "query_echo": query_text,
            "provenance": {
                "authority": "MAIN_CIO",
                "signer_id": "hermes-bridge-cio",
                "mode": "fixture_transport",
            },
            "thesis": f"Subprocess fixture decision for {query_text}",
        }
        if is_stream_json:
            emitter = RuntimeEvidenceStreamJsonEmitter(model=model, session_id=session_id)
            emitter.attach(fake_agent)
            return emitter.emit_result(
                {"final_response": json.dumps(fixture_packet)},
                session_id=session_id,
                exit_code=0,
            )
        else:
            print(json.dumps(fixture_packet))
            return 0

    if is_stream_json:
        emitter = RuntimeEvidenceStreamJsonEmitter(model=model, session_id=session_id)
        try:
            from cli import HermesCLI
        except ImportError:
            if _configured_agent_path and str(_configured_agent_path) not in sys.path:
                sys.path.insert(0, str(_configured_agent_path))
            try:
                from cli import HermesCLI
            except ImportError as e:
                err_msg = f"Installed Hermes CLI dependency not found: missing installed Hermes CLI ({e})"
                emitter.emit_result(
                    {"failed": True, "error": err_msg},
                    session_id=session_id,
                    exit_code=1,
                )
                return 1

        try:
            cli = HermesCLI(
                model=model,
                provider=provider,
                toolsets=toolsets_param,
                reasoning=args.reasoning,
                max_turns=args.max_turns,
                run_budget=args.run_budget,
                resume=resume,
                ignore_rules=args.ignore_rules,
            )
            if hasattr(cli, "source"):
                cli.source = args.source

            if not cli._ensure_runtime_credentials():
                emitter.emit_result(
                    {"failed": True, "error": "credentials or agent init failed: no authenticated provider credentials"},
                    session_id=session_id,
                    exit_code=1,
                )
                return 1

            if not cli._init_agent():
                emitter.emit_result(
                    {"failed": True, "error": "credentials or agent init failed: agent initialization returned False"},
                    session_id=session_id,
                    exit_code=1,
                )
                return 1

            emitter.attach(cli.agent)

            # Execute genuine conversation method
            result = cli.agent.run_conversation(
                user_message=query_text,
                conversation_history=getattr(cli, "conversation_history", []),
            )

            exit_code = 0
            if isinstance(result, dict) and (result.get("failed") or result.get("error") or result.get("is_error")):
                exit_code = result.get("exit_code", 1) or 1

            return emitter.emit_result(result, session_id=session_id, exit_code=exit_code)

        except Exception as e:
            emitter.emit_result(
                {"failed": True, "error": f"credentials or agent init failed: {e}"},
                session_id=session_id,
                exit_code=1,
            )
            return 1
    else:
        # General chat text mode
        try:
            from cli import HermesCLI
        except ImportError:
            if _configured_agent_path and str(_configured_agent_path) not in sys.path:
                sys.path.insert(0, str(_configured_agent_path))
            try:
                from cli import HermesCLI
            except ImportError as e:
                print(f"Error: Installed Hermes CLI dependency not found: missing installed Hermes CLI ({e})", file=sys.stderr)
                return 1

        try:
            cli = HermesCLI(
                model=model,
                provider=provider,
                toolsets=toolsets_param,
                reasoning=args.reasoning,
                max_turns=args.max_turns,
                run_budget=args.run_budget,
                resume=resume,
                ignore_rules=args.ignore_rules,
            )
            if hasattr(cli, "source"):
                cli.source = args.source

            if not cli._ensure_runtime_credentials():
                print("Error: credentials or agent init failed: no authenticated provider credentials", file=sys.stderr)
                return 1

            if not cli._init_agent():
                print("Error: credentials or agent init failed: agent initialization returned False", file=sys.stderr)
                return 1

            result = cli.agent.run_conversation(
                user_message=query_text,
                conversation_history=getattr(cli, "conversation_history", []),
            )

            if isinstance(result, dict) and (result.get("failed") or result.get("error")):
                err_msg = result.get("error") or "Execution failed"
                print(f"Error: {err_msg}", file=sys.stderr)
                return result.get("exit_code", 1) or 1

            resp = result.get("final_response", "") if isinstance(result, dict) else str(result)
            if resp:
                print(resp)
            return 0
        except Exception as e:
            print(f"Error during chat execution: {e}", file=sys.stderr)
            return 1


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        print("Hermes CIO Bootstrap. Usage: cio_hermes_bootstrap.py chat [options]")
        sys.exit(0)

    subcmd = argv[0]
    if subcmd == "chat":
        sys.exit(run_chat_command(argv[1:]))
    elif subcmd in ("--version", "-v"):
        print("Hermes CIO Bootstrap 1.0 (with RuntimeEvidenceStreamJsonEmitter)")
        sys.exit(0)
    elif subcmd in ("--help", "-h"):
        print("Hermes CIO Bootstrap. Available commands: chat, --version, --help")
        sys.exit(0)
    else:
        # Pass unknown command to chat by default to preserve general chat
        sys.exit(run_chat_command(argv))


if __name__ == "__main__":
    main()
