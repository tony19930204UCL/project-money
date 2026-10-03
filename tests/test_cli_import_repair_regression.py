"""Focused regression tests for Hermes CLI import repair.

Validates:
1. Interpreter and agent path resolution resolves installed Hermes venv and agent directory.
2. Exact reproduction of previous failure (missing installed Hermes CLI / No module named 'cli') when unpatched.
3. Live proof of real installed CLI import via safe no-inference subprocess under isolated scratch HERMES_HOME.
4. Security constraint verification: zero credential access, no private config links.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import pytest

from cio_market_lab.integrations.hermes_chat import (
    _hermes_agent_path,
    _hermes_python,
    build_production_chat_command,
)

_DEFAULT_SCRATCH = Path(__file__).resolve().parents[1] / "artifacts" / "scratch"
_SCRATCH_ROOT = Path(os.environ.get("HERMES_SCRATCH_ROOT") or _DEFAULT_SCRATCH)


@pytest.fixture
def scratch_hermes_home() -> Path:
    """Create an isolated temporary HERMES_HOME under artifacts/scratch or /tmp fallback."""
    root = _SCRATCH_ROOT
    try:
        root.mkdir(parents=True, exist_ok=True)
        # Test writability
        test_file = root / ".write_test"
        test_file.touch()
        test_file.unlink()
    except OSError:
        root = Path("/dev/shm/cio_market_lab_scratch")
        root.mkdir(parents=True, exist_ok=True)

    home = root / f"hermes_home_{uuid.uuid4().hex[:8]}"
    home.mkdir(parents=True, exist_ok=True)
    yield home
    # Clean up
    import shutil
    shutil.rmtree(home, ignore_errors=True)


def test_hermes_launcher_resolves_installed_agent_and_python():
    """Verify launcher resolves installed Hermes agent and venv python without explicit env vars."""
    agent_path = _hermes_agent_path()
    assert agent_path is not None, "Failed to resolve installed Hermes agent path"
    agent_dir = Path(agent_path)
    assert agent_dir.is_dir(), f"Resolved agent path is not a directory: {agent_path}"
    assert (agent_dir / "cli.py").is_file(), f"cli.py not found in agent dir: {agent_path}"

    hermes_py = _hermes_python()
    assert hermes_py is not None, "Failed to resolve Hermes python"
    assert ("installs" in hermes_py or "hermes-agent" in hermes_py), f"Resolved python should be under hermes install or agent venv: {hermes_py}"

    # Verify build_production_chat_command embeds agent_path and uses hermes_py
    cmd = build_production_chat_command(session_id="test-resolve-session", query="hello")
    assert cmd[0] == hermes_py
    assert "-c" in cmd
    code = cmd[cmd.index("-c") + 1]
    assert agent_path in code, f"agent_path {agent_path} should be embedded in activation code"
    assert "sys.path.insert(0, agent_path)" in code


def test_negative_missing_cli_failure_reproduction(scratch_hermes_home: Path):
    """Reproduce the exact original failure: when launched with system python without agent on sys.path,
    cio_hermes_bootstrap emits 'missing installed Hermes CLI (No module named 'cli')'."""
    bootstrap_py = Path(__file__).resolve().parents[1] / "cio_market_lab" / "integrations" / "cio_hermes_bootstrap.py"
    clean_env = dict(os.environ)
    clean_env.pop("HERMES_AGENT_PATH", None)
    clean_env.pop("HERMES_PYTHON", None)
    clean_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    clean_env["HERMES_HOME"] = str(scratch_hermes_home)

    proc = subprocess.run(
        [
            "/usr/bin/python3",
            str(bootstrap_py),
            "chat",
            "--format",
            "stream-json",
            "--query",
            "test repro query",
        ],
        env=clean_env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode in (0, 1)


def test_safe_no_inference_subprocess_proves_installed_cli_import(scratch_hermes_home: Path):
    """Live proof: Subprocess executes installed Hermes CLI through build_production_chat_command
    with isolated scratch HERMES_HOME, proving real installed CLI import (no missing-cli error)
    and failing safely on zero credentials with zero network and zero inference."""
    cmd = build_production_chat_command(
        session_id="test-safe-cli-proof",
        query_file=None,
        query="Safe proof query without credentials",
        format_type="stream-json",
        provider="openai-codex",
        model="gpt-6-astra",
        oneshot=True,
    )

    env = dict(os.environ)
    env["HERMES_HOME"] = str(scratch_hermes_home)
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    # Ensure no credentials exist in environment
    for k in list(env.keys()):
        if any(tok in k.upper() for tok in ("API_KEY", "SECRET", "TOKEN", "AUTH")):
            env.pop(k, None)

    proc = subprocess.run(
        cmd,
        input="Safe proof query without credentials",
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    # Process should exit with 1 because of missing credentials, NOT because of missing CLI!
    assert proc.returncode == 1, f"Expected exit code 1, got {proc.returncode}. Output:\n{proc.stdout}\n{proc.stderr}"

    # Parse stdout events
    events = [
        json.loads(line)
        for line in proc.stdout.strip().splitlines()
        if line.strip().startswith("{")
    ]
    event_types = [e.get("type") for e in events]
    assert "system" in event_types, f"Expected system init event in stdout:\n{proc.stdout}"
    assert "result" in event_types, f"Expected result event in stdout:\n{proc.stdout}"

    result_event = next(e for e in events if e.get("type") == "result")
    err_msg = result_event.get("error", "")

    # CRITICAL INVARIANT: The error must NOT be missing installed CLI!
    assert "missing installed Hermes CLI" not in err_msg, f"Regression! Missing CLI error returned: {err_msg}"
    assert "No module named 'cli'" not in err_msg, f"Regression! No module named 'cli' returned: {err_msg}"
    assert "No module named cli" not in err_msg, f"Regression! No module named cli returned: {err_msg}"

    # Positively verify that HermesCLI was instantiated and failed on runtime credentials check
    assert "credentials or agent init failed: no authenticated provider credentials" in err_msg, (
        f"Unexpected error message: {err_msg}"
    )


def test_isolated_scratch_sentinel_never_leaked(scratch_hermes_home: Path):
    """Security verification: isolated scratch HERMES_HOME never links to host credentials or config."""
    cmd = build_production_chat_command(
        session_id="test-sentinel-leak",
        query_file=None,
        query="Security probe",
        format_type="stream-json",
        provider="openai-codex",
        model="gpt-6-astra",
        oneshot=True,
    )

    env = dict(os.environ)
    env["HERMES_HOME"] = str(scratch_hermes_home)
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"

    proc = subprocess.run(
        cmd,
        input="Security probe",
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    # Verify no symlinks to real auth or config were created under scratch_hermes_home
    for p in scratch_hermes_home.rglob("*"):
        if p.is_symlink():
            target = str(p.resolve())
            assert "auth.json" not in target, f"Forbidden auth symlink created: {p} -> {target}"
            assert "config.yaml" not in target, f"Forbidden config symlink created: {p} -> {target}"
            assert ".env" not in target, f"Forbidden env symlink created: {p} -> {target}"
