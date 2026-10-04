from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import tempfile
import time
from typing import Any
import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 1. Capture pristine environment at collection time
_ORIG_ENV = dict(os.environ)

# 2. Enforce collection-time isolated test home under artifacts (unique per run)
_PID = os.getpid()
_TS = int(time.time() * 1000)
_UNIQUE_DIR_NAME = f"test_hermes_home_{_PID}_{_TS}"
_UNIQUE_TEST_HOME = _PROJECT_ROOT / "artifacts" / _UNIQUE_DIR_NAME

try:
    _UNIQUE_TEST_HOME.mkdir(parents=True, exist_ok=True)
except OSError:
    # If artifacts directory is on a read-only filesystem (e.g. sandbox mount)
    _UNIQUE_TEST_HOME = Path(tempfile.mkdtemp(prefix="test_hermes_home_"))

os.environ["HERMES_HOME"] = str(_UNIQUE_TEST_HOME)
_TEST_RUNTIME = _UNIQUE_TEST_HOME / "data" / "runtime"
_TEST_RUNTIME.mkdir(parents=True, exist_ok=True)
os.environ["CIO_MARKET_LAB_RUNTIME_DIR"] = str(_TEST_RUNTIME)
os.environ["HERMES_OFFLINE"] = "1"
os.environ["CIO_MARKET_LAB_OFFLINE"] = "1"
os.environ["CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS"] = "1"
if "HERMES_AGENT_PATH" in os.environ and ".hermes" in os.environ["HERMES_AGENT_PATH"]:
    del os.environ["HERMES_AGENT_PATH"]

# 3. Enforce collection-time socket blocker for actual external networks
# Preserves local ASGI tests (127.0.0.1, localhost, ::1, 0.0.0.0, testserver, AF_UNIX)
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "testserver"}
_orig_socket_connect = socket.socket.connect
_orig_create_connection = socket.create_connection


def _is_loopback_or_local(host: Any) -> bool:
    if not host or not isinstance(host, str):
        return True
    host_clean = host.strip().lower()
    if host_clean in _ALLOWED_HOSTS or host_clean.startswith("127.") or host_clean == "::1":
        return True
    return False


def _guarded_connect(self, address, *args, **kwargs):
    if getattr(self, "family", None) == getattr(socket, "AF_UNIX", None):
        return _orig_socket_connect(self, address, *args, **kwargs)
    if isinstance(address, (tuple, list)) and len(address) >= 1:
        host = str(address[0])
        if not _is_loopback_or_local(host):
            raise RuntimeError(f"BLOCKED: External network connection prohibited during test isolation: {address}")
    elif isinstance(address, str) and not _is_loopback_or_local(address):
        raise RuntimeError(f"BLOCKED: External network connection prohibited during test isolation: {address}")
    return _orig_socket_connect(self, address, *args, **kwargs)


def _guarded_create_connection(address, *args, **kwargs):
    if isinstance(address, (tuple, list)) and len(address) >= 1:
        host = str(address[0])
        if not _is_loopback_or_local(host):
            raise RuntimeError(f"BLOCKED: External network connection prohibited during test isolation: {address}")
    return _orig_create_connection(address, *args, **kwargs)


socket.socket.connect = _guarded_connect
socket.create_connection = _guarded_create_connection


def pytest_unconfigure(config):
    """Restore changed environment, socket connections, and clean up isolated test home."""
    # Restore sockets
    socket.socket.connect = _orig_socket_connect
    socket.create_connection = _orig_create_connection

    # Restore environment
    os.environ.clear()
    os.environ.update(_ORIG_ENV)

    # Clean up isolated test home without touching shared paths
    if _UNIQUE_TEST_HOME.exists() and "test_hermes_home_" in str(_UNIQUE_TEST_HOME):
        try:
            shutil.rmtree(_UNIQUE_TEST_HOME, ignore_errors=True)
        except Exception:
            pass


@pytest.fixture(autouse=True, scope="session")
def _isolate_hermes_home():
    """Session fixture yielding the collection-time isolated test home."""
    yield _UNIQUE_TEST_HOME


@pytest.fixture(autouse=True)
def _enforce_socket_isolation_per_test(monkeypatch):
    """Reinstall guards after tests which monkeypatch socket helpers themselves."""
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)


@pytest.fixture(autouse=True)
def _allow_closed_market_orders_in_unit_tests(monkeypatch):
    """Keep unit tests deterministic while production defaults block closed-session orders."""
    monkeypatch.setenv("CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS", "1")
    monkeypatch.setenv("CIO_MARKET_LAB_OFFLINE", "1")


@pytest.fixture(autouse=True)
def _prevent_real_model_subprocesses(monkeypatch):
    """Prevent tests from calling real model subprocesses; use explicit fixture executors scoped to tests."""
    from cio_market_lab.integrations import hermes_chat
    orig_chat = hermes_chat.run_hermes_cli_chat

    def _guarded_hermes_chat(*args, **kwargs):
        if kwargs.get("transport") is not None:
            return orig_chat(*args, **kwargs)
        raise RuntimeError(
            "BLOCKED: Real model subprocess execution is disabled during tests. "
            "Use explicit fixture executors scoped to tests."
        )

    monkeypatch.setattr(
        "cio_market_lab.integrations.hermes_chat.run_hermes_cli_chat",
        _guarded_hermes_chat,
    )


def pytest_ignore_collect(collection_path, config):
    """Keep browser acceptance opt-in while allowing explicit tests/browser collection."""
    browser_root = (_PROJECT_ROOT / "tests" / "browser").resolve()
    try:
        candidate = Path(str(collection_path)).resolve()
    except OSError:
        return False
    explicit_browser = any(
        str(arg).replace("\\", "/").rstrip("/").startswith("tests/browser")
        for arg in config.args
        if isinstance(arg, str)
    )
    if candidate == browser_root or browser_root in candidate.parents:
        return not explicit_browser
    return False
