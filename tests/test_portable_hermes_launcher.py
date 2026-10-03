"""Portable source-regression coverage for Hermes launcher resolution.

These tests use only pytest-owned fixture paths. They do not prove an installed
Hermes host, Python 3.14 ABI compatibility, credentials, or private runtime state.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from cio_market_lab.integrations import hermes_chat


@pytest.fixture
def isolated_launcher_env(tmp_path: Path, monkeypatch):
    home = tmp_path / "TEST_ONLY_home"
    hermes_home = tmp_path / "TEST_ONLY_hermes_home"
    home.mkdir()
    hermes_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    for key in ("HERMES_AGENT_PATH", "HERMES_PYTHON", "HERMES_CHAT_EXECUTABLE"):
        monkeypatch.delenv(key, raising=False)
    return home, hermes_home


def _fake_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_agent(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "cli.py").write_text("# TEST_ONLY portable launcher fixture\n", encoding="utf-8")
    return path


def test_portable_explicit_agent_and_python_selection(isolated_launcher_env, monkeypatch):
    home, _ = isolated_launcher_env
    agent = _fake_agent(home / "TEST_ONLY_explicit_agent")
    python = _fake_executable(home / "TEST_ONLY_explicit_python")
    monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
    monkeypatch.setenv("HERMES_PYTHON", str(python))

    assert hermes_chat._hermes_agent_path() == str(agent)
    assert hermes_chat._hermes_python() == str(python)


def test_portable_facts_json_interpreter_has_priority(isolated_launcher_env, monkeypatch):
    home, hermes_home = isolated_launcher_env
    agent = _fake_agent(home / "TEST_ONLY_facts_agent")
    monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))

    install_key = hashlib.sha256(str(agent.resolve()).encode("utf-8")).hexdigest()[:16]
    facts_env = hermes_home / "TEST_ONLY_facts_environment" / "venv"
    facts_python = _fake_executable(facts_env / "bin" / "python")
    fallback_python = _fake_executable(
        hermes_home / "installs" / "000_TEST_ONLY_fallback" /
        "environments" / "env" / "venv" / "bin" / "python3.14"
    )
    assert fallback_python != facts_python

    facts_path = hermes_home / "installs" / install_key / "facts.json"
    facts_path.parent.mkdir(parents=True, exist_ok=True)
    facts_path.write_text(json.dumps({
        "packages": {"venv": {"environment": str(facts_env)}}
    }), encoding="utf-8")

    assert hermes_chat._hermes_python() == str(facts_python)


def test_portable_missing_installation_falls_back_to_current_interpreter(isolated_launcher_env):
    assert hermes_chat._hermes_agent_path() is None
    assert hermes_chat._hermes_python() == sys.executable


def test_portable_invalid_configured_paths_fall_back_to_fixture_install(isolated_launcher_env, monkeypatch):
    home, _ = isolated_launcher_env
    monkeypatch.setenv("HERMES_AGENT_PATH", str(home / "TEST_ONLY_missing_agent"))
    monkeypatch.setenv("HERMES_PYTHON", str(home / "TEST_ONLY_missing_python"))

    agent = _fake_agent(home / ".hermes" / "hermes-agent")
    agent_python = _fake_executable(agent / "venv" / "bin" / "python")

    assert hermes_chat._hermes_agent_path() == str(agent)
    assert hermes_chat._hermes_python() == str(agent_python)


def test_portable_command_activates_bootstrap_before_entrypoint(isolated_launcher_env, monkeypatch):
    home, _ = isolated_launcher_env
    agent = _fake_agent(home / "TEST_ONLY_command_agent")
    python = _fake_executable(home / "TEST_ONLY_command_python")
    monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
    monkeypatch.setenv("HERMES_PYTHON", str(python))

    command = hermes_chat.build_production_chat_command(
        session_id="TEST_ONLY_portable_command",
        query="TEST_ONLY no execution",
    )

    assert command[0] == str(python)
    assert command[1] == "-c"
    activation = command[2]
    assert str(agent) in activation
    assert "sys.path.insert(0, agent_path)" in activation
    assert activation.index("import hermes_bootstrap") < activation.index("runpy.run_path")
    assert command[3] == str(hermes_chat.BOOTSTRAP_SCRIPT)


def test_portable_current_interpreter_imports_pydantic_and_core(isolated_launcher_env):
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pydantic, pydantic_core; print('TEST_ONLY_PYDANTIC_IMPORT_OK')",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "TEST_ONLY_PYDANTIC_IMPORT_OK" in proc.stdout
