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

ROOT = Path(__file__).resolve().parents[2]
HELPER = Path(__file__).with_name("server_helper.py")


@contextmanager
def _serve(tmp_path: Path, *, writable: bool = False):
    runtime = tmp_path / "TEST_ONLY_runtime"
    ready = tmp_path / "ready.json"
    stop = tmp_path / "stop"
    args = [
        sys.executable, str(HELPER),
        "--root", str(ROOT),
        "--runtime", str(runtime),
        "--ready-file", str(ready),
        "--stop-file", str(stop),
    ]
    if writable:
        args.append("--writable")
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 15
    port = None
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate(timeout=1)
                raise AssertionError(f"browser server exited before ready: {out}\n{err}")
            if ready.exists():
                port = json.loads(ready.read_text(encoding="utf-8"))["port"]
                break
            time.sleep(0.05)
        assert port is not None, "browser server readiness timeout"
        base = f"http://127.0.0.1:{port}"
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{base}/api/health", timeout=0.5)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("browser server health readiness timeout")
        yield base, runtime, proc.pid
    finally:
        if proc.poll() is None:
            stop.write_text("stop", encoding="utf-8")
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


@pytest.fixture
def browser_server(tmp_path):
    with _serve(tmp_path, writable=False) as server:
        yield server


@pytest.fixture
def writable_browser_server(tmp_path):
    with _serve(tmp_path, writable=True) as server:
        yield server


@pytest.fixture(scope="session")
def chromium():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def evidence_dir(tmp_path):
    configured = os.environ.get("BROWSER_EVIDENCE_DIR")
    path = Path(configured) if configured else tmp_path / "browser-evidence"
    path.mkdir(parents=True, exist_ok=True)
    return path
