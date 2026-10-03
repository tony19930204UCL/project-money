from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx
import pytest
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HELPER = Path(__file__).with_name("server_helper.py")


class BrowserServer:
    def __init__(self, proc: subprocess.Popen[str], port: int, stop_file: Path):
        self.proc = proc
        self.port = port
        self.stop_file = stop_file
        self.base_url = f"http://127.0.0.1:{port}"


@contextmanager
def _server(runtime_dir: Path, control_dir: Path, *, read_only: bool):
    control_dir.mkdir(parents=True, exist_ok=True)
    token = str(time.monotonic_ns())
    ready_file = control_dir / f"ready-{token}.json"
    stop_file = control_dir / f"stop-{token}"
    args = [
        sys.executable,
        str(HELPER),
        "serve",
        "--workspace-root",
        str(PROJECT_ROOT),
        "--runtime-dir",
        str(runtime_dir),
        "--ready-file",
        str(ready_file),
        "--stop-file",
        str(stop_file),
    ]
    if read_only:
        args.append("--read-only")
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    server = None
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate(timeout=1)
                raise AssertionError(f"browser test server exited: stdout={out!r} stderr={err!r}")
            if ready_file.exists():
                ready = json.loads(ready_file.read_text(encoding="utf-8"))
                server = BrowserServer(proc, int(ready["port"]), stop_file)
                break
            time.sleep(0.05)
        assert server is not None, "browser test server readiness timeout"
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{server.base_url}/api/health", timeout=0.5)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("browser test server health timeout")
        yield server
    finally:
        if proc.poll() is None:
            stop_file.write_text("stop", encoding="utf-8")
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


@pytest.fixture(scope="session")
def browser_runtime(tmp_path_factory):
    runtime_dir = tmp_path_factory.mktemp("browser-runtime")
    subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "prepare",
            "--workspace-root",
            str(PROJECT_ROOT),
            "--runtime-dir",
            str(runtime_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return runtime_dir


@pytest.fixture(scope="session")
def read_only_server(browser_runtime, tmp_path_factory):
    with _server(browser_runtime, tmp_path_factory.mktemp("browser-control-ro"), read_only=True) as server:
        yield server


@pytest.fixture(scope="session")
def writable_server(browser_runtime, tmp_path_factory):
    with _server(browser_runtime, tmp_path_factory.mktemp("browser-control-rw"), read_only=False) as server:
        yield server


@pytest.fixture(scope="session")
def chromium_browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture(scope="session")
def browser_evidence_dir(tmp_path_factory):
    configured = os.environ.get("BROWSER_EVIDENCE_DIR")
    target = Path(configured) if configured else tmp_path_factory.mktemp("browser-evidence")
    target.mkdir(parents=True, exist_ok=True)
    return target

@pytest.fixture
def fresh_runtime(tmp_path: Path):
    runtime_dir = tmp_path / "runtime"
    subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "prepare",
            "--workspace-root",
            str(PROJECT_ROOT),
            "--runtime-dir",
            str(runtime_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return runtime_dir


@pytest.fixture
def server_factory():
    return _server


def run_helper_command(command: str, runtime_dir: Path) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            command,
            "--workspace-root",
            str(PROJECT_ROOT),
            "--runtime-dir",
            str(runtime_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=40,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else {}
