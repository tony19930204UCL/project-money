"""Regression tests for CLI bootstrap canonical runtime directory resolution.

Validates:
1. Subprocess-isolated regression proving two process invocations use one intended directory,
   persist event and learning state, leave a sentinel decoy runtime unchanged,
   and do not reset existing cash or settings.
2. Default runtime path resolves to canonical root/data/runtime (not root/runtime).
3. Explicit runtime path via --runtime-dir is honored.
4. Environment conflict precedence: explicit --runtime-dir CLI flag overrides
   CIO_MARKET_LAB_RUNTIME_DIR environment variable without polluting the env path.
5. Corrupt / invalid checkpoint fails closed without resetting runtime data.
6. Execution proceeds safely without requiring or accessing external credentials.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.events.store import EventStore


def test_cli_bootstrap_two_process_invocations_use_intended_dir_and_persist_state(tmp_path: Path):
    """Prove two subprocess invocations use intended runtime, persist events and learning,
    leave a sentinel decoy runtime untouched, and do not reset existing cash or settings."""
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    intended_runtime = tmp_path / "intended_runtime"
    intended_runtime.mkdir(parents=True, exist_ok=True)
    decoy_runtime = tmp_path / "decoy_runtime"
    decoy_runtime.mkdir(parents=True, exist_ok=True)

    sentinel_file = decoy_runtime / "sentinel.txt"
    sentinel_content = "SENTINEL_DECOY_DO_NOT_TOUCH"
    sentinel_file.write_text(sentinel_content, encoding="utf-8")

    out_file_1 = tmp_path / "out_1.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    env["CIO_WORKSPACE_ROOT"] = str(workspace_dir)
    env["CIO_MARKET_LAB_OFFLINE"] = "1"
    # Set env var to decoy to ensure --runtime-dir strictly overrides it
    env["CIO_MARKET_LAB_RUNTIME_DIR"] = str(decoy_runtime)

    cmd_1 = [
        sys.executable,
        "-m",
        "cio_market_lab.engine.continuation",
        "--max-cycles",
        "1",
        "--runtime-dir",
        str(intended_runtime),
        "--output",
        str(out_file_1),
    ]

    # Process Invocation 1
    proc_1 = subprocess.run(cmd_1, env=env, capture_output=True, text=True)
    assert proc_1.returncode == 0, f"Process 1 failed:\nSTDOUT: {proc_1.stdout}\nSTDERR: {proc_1.stderr}"
    assert out_file_1.exists()

    data_1 = json.loads(out_file_1.read_text(encoding="utf-8"))
    assert data_1["terminal_state"] in ("COMPLETED", "BLOCKED")
    assert data_1["completed_cycles"] == 1

    # Verify event store in intended directory persisted events
    events_db_path = intended_runtime / "events.db"
    assert events_db_path.exists(), "EventStore db was not created in intended runtime dir"
    store_1 = EventStore(events_db_path)
    count_1 = store_1.count()
    assert count_1 > 0, "No events were persisted in intended runtime events.db"

    # Verify decoy runtime was left completely untouched
    decoy_files = list(decoy_runtime.iterdir())
    assert decoy_files == [sentinel_file], f"Decoy runtime was polluted: {decoy_files}"
    assert sentinel_file.read_text(encoding="utf-8") == sentinel_content

    # Verify checkpoint in intended directory
    chk_file = intended_runtime / "continuation_checkpoint.json"
    assert chk_file.exists()
    chk_1 = json.loads(chk_file.read_text(encoding="utf-8"))
    assert chk_1["cycle_index"] == 1

    # Verify settings file in intended directory
    settings_file = intended_runtime / "experiment_settings.json"
    assert settings_file.exists()

    # Now simulate non-default cash and updated settings before process 2
    # Verify process 2 preserves them instead of resetting to defaults
    portfolio_state_file = intended_runtime / "portfolio_state.json"
    custom_cash = 789_123.45
    port_state = {
        "aggregate": {},
        "strategies": {},
        "cash_accounts": {
            "dynamic-desk": {
                "initial_cash": 1_000_000.0,
                "cash": custom_cash,
                "applied_fill_ids": [],
            }
        },
    }
    portfolio_state_file.write_text(json.dumps(port_state, indent=2), encoding="utf-8")

    # Modify settings custom description
    settings_data = json.loads(settings_file.read_text(encoding="utf-8"))
    settings_data["dynamic-desk"]["strategy_name"] = "Custom Preserved Desk Name"
    settings_file.write_text(json.dumps(settings_data, indent=2), encoding="utf-8")

    # Persist a learning decision record in intended_runtime
    learning_file = intended_runtime / "cio_learning.jsonl"
    learning_record = {
        "case_id": "case-test-persist-001",
        "status": "ACTIVE",
        "packet": {
            "case_id": "case-test-persist-001",
            "as_of": "2026-09-28T09:30:00Z",
            "evidence": ["quote://2330.TW-1000"],
            "thesis": "Preserved test thesis",
            "selected_instrument": "2330.TW",
            "action": "BUY",
            "holding_horizon": "swing",
            "quantity": 10.0,
            "expiry": "2026-09-28T13:30:00Z",
            "confidence": 0.9,
            "strategy_version": "v1",
        },
        "pre_decision_portfolio": {},
        "pre_decision_quotes": {},
        "rejected_opportunities": [],
        "hypothesis_version": 1,
    }
    learning_file.write_text(json.dumps(learning_record) + "\n", encoding="utf-8")

    # Process Invocation 2
    out_file_2 = tmp_path / "out_2.json"
    cmd_2 = [
        sys.executable,
        "-m",
        "cio_market_lab.engine.continuation",
        "--max-cycles",
        "1",
        "--runtime-dir",
        str(intended_runtime),
        "--output",
        str(out_file_2),
    ]

    proc_2 = subprocess.run(cmd_2, env=env, capture_output=True, text=True)
    assert proc_2.returncode == 0, f"Process 2 failed:\nSTDOUT: {proc_2.stdout}\nSTDERR: {proc_2.stderr}"
    assert out_file_2.exists()

    data_2 = json.loads(out_file_2.read_text(encoding="utf-8"))
    assert data_2["terminal_state"] in ("COMPLETED", "BLOCKED")
    assert data_2["completed_cycles"] == 2

    # Verify event store persisted additional events across the process boundary
    store_2 = EventStore(events_db_path)
    count_2 = store_2.count()
    assert count_2 > count_1, f"Event store count did not increase across restarts: {count_2} <= {count_1}"

    # Verify decoy runtime is STILL completely untouched
    decoy_files_after = list(decoy_runtime.iterdir())
    assert decoy_files_after == [sentinel_file], f"Decoy runtime was altered after process 2: {decoy_files_after}"
    assert sentinel_file.read_text(encoding="utf-8") == sentinel_content

    # Verify custom cash was preserved and NOT reset to TEAM_INITIAL_CAPITAL_TWD
    chk_2 = json.loads(chk_file.read_text(encoding="utf-8"))
    assert chk_2["cycle_index"] == 2
    assert abs(chk_2["canonical_cash"] - custom_cash) < 1e-4, (
        f"Cash was reset to default! Expected {custom_cash}, got {chk_2['canonical_cash']}"
    )

    # Verify settings were preserved
    settings_after = json.loads(settings_file.read_text(encoding="utf-8"))
    assert settings_after["dynamic-desk"]["strategy_name"] == "Custom Preserved Desk Name"

    # Verify learning state was preserved in intended runtime and never written to decoy
    assert learning_file.exists()
    assert "case-test-persist-001" in learning_file.read_text(encoding="utf-8")
    assert not (decoy_runtime / "cio_learning.jsonl").exists()


def test_cli_bootstrap_default_runtime_path(tmp_path: Path):
    """Verify default runtime path resolves to canonical workspace_root/data/runtime,
    leaving workspace_root/runtime decoy untouched."""
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    decoy_runtime = workspace_dir / "runtime"
    decoy_runtime.mkdir(parents=True, exist_ok=True)
    sentinel = decoy_runtime / "sentinel.txt"
    sentinel.write_text("DECOY_RUNTIME", encoding="utf-8")

    out_file = tmp_path / "default_out.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    env["CIO_WORKSPACE_ROOT"] = str(workspace_dir)
    env["CIO_MARKET_LAB_OFFLINE"] = "1"
    # Ensure CIO_MARKET_LAB_RUNTIME_DIR is NOT set
    env.pop("CIO_MARKET_LAB_RUNTIME_DIR", None)

    cmd = [
        sys.executable,
        "-m",
        "cio_market_lab.engine.continuation",
        "--max-cycles",
        "1",
        "--output",
        str(out_file),
    ]

    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, f"Default CLI failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"

    canonical_runtime = workspace_dir / "data" / "runtime"
    assert canonical_runtime.exists(), "Canonical workspace_root/data/runtime was not created"
    assert (canonical_runtime / "events.db").exists(), "events.db not in canonical runtime"
    assert (canonical_runtime / "continuation_checkpoint.json").exists()

    # Verify workspace/runtime decoy was NOT used
    assert list(decoy_runtime.iterdir()) == [sentinel]


def test_cli_bootstrap_explicit_runtime_path(tmp_path: Path):
    """Verify explicit --runtime-dir is honored and creates runtime files at the specified path."""
    explicit_runtime = tmp_path / "custom_explicit_runtime"
    out_file = tmp_path / "explicit_out.json"

    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    env["CIO_MARKET_LAB_OFFLINE"] = "1"
    env.pop("CIO_MARKET_LAB_RUNTIME_DIR", None)

    cmd = [
        sys.executable,
        "-m",
        "cio_market_lab.engine.continuation",
        "--max-cycles",
        "1",
        "--runtime-dir",
        str(explicit_runtime),
        "--output",
        str(out_file),
    ]

    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, f"Explicit CLI failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    assert (explicit_runtime / "events.db").exists()
    assert (explicit_runtime / "continuation_checkpoint.json").exists()


def test_cli_bootstrap_env_conflict_precedence(tmp_path: Path):
    """Verify that explicit --runtime-dir takes precedence over CIO_MARKET_LAB_RUNTIME_DIR."""
    env_dir = tmp_path / "env_decoy_dir"
    env_dir.mkdir(parents=True, exist_ok=True)
    sentinel = env_dir / "env_sentinel.txt"
    sentinel.write_text("ENV_DECOY", encoding="utf-8")

    cli_dir = tmp_path / "cli_intended_dir"
    out_file = tmp_path / "conflict_out.json"

    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    env["CIO_MARKET_LAB_OFFLINE"] = "1"
    env["CIO_MARKET_LAB_RUNTIME_DIR"] = str(env_dir)

    cmd = [
        sys.executable,
        "-m",
        "cio_market_lab.engine.continuation",
        "--max-cycles",
        "1",
        "--runtime-dir",
        str(cli_dir),
        "--output",
        str(out_file),
    ]

    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, f"Precedence CLI failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"

    # Intended CLI directory must have all artifacts
    assert (cli_dir / "events.db").exists()
    assert (cli_dir / "continuation_checkpoint.json").exists()

    # Env directory must remain completely untouched
    assert list(env_dir.iterdir()) == [sentinel]


def test_cli_bootstrap_invalid_checkpoint_fail_closed(tmp_path: Path):
    """Verify that an invalid/corrupt checkpoint in runtime dir fails closed with non-zero exit code."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    # Write corrupt checkpoint
    chk_file = runtime_dir / "continuation_checkpoint.json"
    corrupt_content = "NOT_A_VALID_JSON_CORRUPT"
    chk_file.write_text(corrupt_content, encoding="utf-8")

    out_file = tmp_path / "fail_closed_out.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    env["CIO_MARKET_LAB_OFFLINE"] = "1"

    cmd = [
        sys.executable,
        "-m",
        "cio_market_lab.engine.continuation",
        "--max-cycles",
        "1",
        "--runtime-dir",
        str(runtime_dir),
        "--output",
        str(out_file),
    ]

    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert proc.returncode == 1, f"Expected non-zero exit code on corrupt checkpoint, got {proc.returncode}"

    # Corrupt checkpoint must not be overwritten or wiped
    assert chk_file.read_text(encoding="utf-8") == corrupt_content

    # Output file if written must report ERROR status
    if out_file.exists():
        data = json.loads(out_file.read_text(encoding="utf-8"))
        assert data["status"] == "ERROR"
        assert data["terminal_state"] == "ERROR"


def test_cli_bootstrap_no_credential_access(tmp_path: Path):
    """Verify that continuation CLI operates safely in offline/paper mode with no credentials in environment."""
    runtime_dir = tmp_path / "runtime"
    out_file = tmp_path / "clean_env_out.json"

    # Build clean environment with all credentials stripped
    clean_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": ".",
        "CIO_MARKET_LAB_OFFLINE": "1",
        "HOME": str(tmp_path),
    }

    cmd = [
        sys.executable,
        "-m",
        "cio_market_lab.engine.continuation",
        "--max-cycles",
        "1",
        "--runtime-dir",
        str(runtime_dir),
        "--output",
        str(out_file),
    ]

    proc = subprocess.run(cmd, env=clean_env, capture_output=True, text=True)
    assert proc.returncode == 0, f"Failed in clean credential-free environment:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["terminal_state"] in ("COMPLETED", "BLOCKED")
