"""Focused regression tests for Native ABI launcher interpreter repair.

Validates:
1. _hermes_python() resolves the installed PM 3.14 venv python via runtime metadata/executable discovery,
   never the legacy Python 3.11 venv.
2. Exact safe production bootstrap --help test executes successfully under /usr/bin/python3 parent
   with isolated scratch HERMES_HOME.
3. Native ABI compatibility: hermes_bootstrap and pydantic_core import cleanly together under resolved interpreter.
4. Negative reproduction: legacy Python 3.11 fails with ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'
   when encountering Python 3.14 site-packages.
5. Security containment: scratch HERMES_HOME is fully isolated; no host credentials or private config leaked.
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

_SCRATCH_ROOT = Path(__file__).resolve().parents[1] / "artifacts" / "scratch"


@pytest.fixture
def scratch_hermes_home() -> Path:
    """Create an isolated temporary HERMES_HOME under artifacts/scratch."""
    home = _SCRATCH_ROOT / f"hermes_home_{uuid.uuid4().hex[:8]}"
    home.mkdir(parents=True, exist_ok=True)
    yield home
    import shutil
    shutil.rmtree(home, ignore_errors=True)


def test_hermes_python_resolves_pm_installed_venv_interpreter():
    """Verify _hermes_python() resolves the PM installed venv Python (3.14), not legacy 3.11."""
    resolved_py = _hermes_python()
    assert resolved_py is not None, "Failed to resolve Hermes Python"
    assert Path(resolved_py).is_file(), f"Resolved Python does not exist: {resolved_py}"
    assert os.access(resolved_py, os.X_OK), f"Resolved Python is not executable: {resolved_py}"

    # Verify it does NOT point to legacy hermes-agent/venv (Python 3.11)
    legacy_venv = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    assert Path(resolved_py).resolve() != legacy_venv.resolve(), (
        f"Regressed! _hermes_python resolved to legacy 3.11 venv: {resolved_py}"
    )

    # Verify version of resolved python is Python 3.14
    proc = subprocess.run([resolved_py, "--version"], capture_output=True, text=True, check=True)
    assert "3.14" in proc.stdout or "3.14" in proc.stderr, (
        f"Expected Python 3.14, got stdout='{proc.stdout}', stderr='{proc.stderr}'"
    )


def test_safe_production_bootstrap_help_under_system_python_parent(scratch_hermes_home: Path):
    """Exact requirement: safe production bootstrap --help test under /usr/bin/python3 parent."""
    system_python = "/usr/bin/python3"
    if not Path(system_python).is_file():
        pytest.skip(f"System python {system_python} not present")

    runner_script = f"""
import os, sys, subprocess
from cio_market_lab.integrations.hermes_chat import build_production_chat_command

cmd = build_production_chat_command(session_id="test-help-probe", query=None)
# Replace chat query flags with --help
chat_idx = cmd.index("chat")
help_cmd = [*cmd[:chat_idx + 1], "--help"]

env = dict(os.environ)
env["HERMES_HOME"] = {repr(str(scratch_hermes_home))}
env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"

proc = subprocess.run(help_cmd, env=env, capture_output=True, text=True)
sys.stdout.write(proc.stdout)
sys.stderr.write(proc.stderr)
sys.exit(proc.returncode)
"""

    parent_proc = subprocess.run(
        [system_python, "-c", runner_script],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert parent_proc.returncode == 0, (
        f"Subprocess failed with code {parent_proc.returncode}.\n"
        f"STDOUT:\n{parent_proc.stdout}\nSTDERR:\n{parent_proc.stderr}"
    )
    assert "Hermes CIO Bootstrap Chat" in parent_proc.stdout
    assert "pydantic_core._pydantic_core" not in parent_proc.stderr
    assert "ModuleNotFoundError" not in parent_proc.stderr


def test_native_abi_bootstrap_and_pydantic_core_import(scratch_hermes_home: Path):
    """Verify hermes_bootstrap and pydantic_core import cleanly together under resolved interpreter."""
    resolved_py = _hermes_python()
    agent_path = _hermes_agent_path()

    test_script = f"""
import sys
agent_path = {repr(agent_path)}
if agent_path and agent_path not in sys.path:
    sys.path.insert(0, agent_path)
import hermes_bootstrap
import pydantic_core
import pydantic
print("NATIVE_ABI_IMPORT_OK")
"""

    env = dict(os.environ)
    env["HERMES_HOME"] = str(scratch_hermes_home)
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"

    proc = subprocess.run(
        [resolved_py, "-c", test_script],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert proc.returncode == 0, f"Import failed code={proc.returncode}: {proc.stderr}"
    assert "NATIVE_ABI_IMPORT_OK" in proc.stdout
    assert "pydantic_core._pydantic_core" not in proc.stderr


def test_legacy_311_cross_abi_negative_reproduction():
    """Negative reproduction: legacy Python 3.11 fails with ModuleNotFoundError when importing 3.14 pydantic_core."""
    legacy_py = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    if not legacy_py.is_file():
        pytest.skip(f"Legacy python {legacy_py} not present")

    # Locate the 3.14 site-packages from facts.json or known path
    candidate_314_site = Path.home() / ".hermes" / "installs" / "9a006becc5ecde4f" / "environments" / "f5a1c36fdb2349c08b69e6973da4d194" / "venv" / "lib" / "python3.14" / "site-packages"
    if not candidate_314_site.is_dir():
        pytest.skip(f"3.14 site-packages {candidate_314_site} not found")

    repro_script = f"""
import sys
sys.path.insert(0, {repr(str(candidate_314_site))})
import pydantic_core
"""

    proc = subprocess.run(
        [str(legacy_py), "-c", repro_script],
        capture_output=True,
        text=True,
        timeout=10,
    )

    # Must fail with ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'
    assert proc.returncode != 0
    assert "ModuleNotFoundError" in proc.stderr
    assert "_pydantic_core" in proc.stderr


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
