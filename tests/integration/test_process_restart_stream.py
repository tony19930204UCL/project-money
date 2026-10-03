from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Iterator

import httpx
import pytest
from websockets.sync.client import connect as websocket_connect

HELPER = Path(__file__).with_name("process_server_helper.py")
STRATEGY_ID = "TEST_ONLY_restart_stream"
LEARNING_CASE_ID = "TEST_ONLY_restart_stream_case"
FILL_ID = "TEST_ONLY_restart_stream_fill"


def _hash_directory(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _run_helper(command: str, workspace_root: Path, runtime_dir: Path) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            command,
            "--workspace-root",
            str(workspace_root),
            "--runtime-dir",
            str(runtime_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, proc.stderr
    return json.loads(lines[-1])


class _Server:
    def __init__(self, proc: subprocess.Popen[str], port: int, stop_file: Path):
        self.proc = proc
        self.port = port
        self.stop_file = stop_file
        self.base_url = f"http://127.0.0.1:{port}"
        self.ws_url = f"ws://127.0.0.1:{port}"

    @property
    def pid(self) -> int:
        return self.proc.pid

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)


@contextmanager
def _server(workspace_root: Path, runtime_dir: Path, control_dir: Path, *, read_only: bool = False) -> Iterator[_Server]:
    control_dir.mkdir(parents=True, exist_ok=True)
    token = f"{time.monotonic_ns()}"
    ready_file = control_dir / f"ready-{token}.json"
    stop_file = control_dir / f"stop-{token}"
    args = [
        sys.executable,
        str(HELPER),
        "serve",
        "--workspace-root",
        str(workspace_root),
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
    server: _Server | None = None
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate(timeout=1)
                raise AssertionError(f"server exited before readiness: stdout={out!r} stderr={err!r}")
            if ready_file.exists():
                ready = json.loads(ready_file.read_text(encoding="utf-8"))
                server = _Server(proc, int(ready["port"]), stop_file)
                break
            time.sleep(0.05)
        assert server is not None, "server readiness file was not created before timeout"

        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{server.base_url}/api/health", timeout=0.5)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("server health probe did not become ready before timeout")
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


def _get_json(base_url: str, path: str):
    response = httpx.get(f"{base_url}{path}", timeout=5)
    response.raise_for_status()
    return response.json()


def _state_snapshot(base_url: str, seed: dict) -> dict:
    portfolio = _get_json(base_url, "/api/portfolios")
    orders = _get_json(base_url, "/api/paper/orders")
    experiments = _get_json(base_url, "/api/paper/experiments")
    fills = _get_json(base_url, "/api/fills")
    events = _get_json(base_url, "/api/events?since_id=0&limit=1000")["events"]
    order_by_id = {item["order_id"]: item for item in orders}
    experiment = next(item for item in experiments if item["strategy_id"] == STRATEGY_ID)
    return {
        "cash": portfolio["swing"]["cash"],
        "positions": portfolio["swing"]["positions"],
        "filled_order": order_by_id[seed["filled_order_id"]],
        "pending_order": order_by_id[seed["pending_order_id"]],
        "fill_ids": [item["fill_id"] for item in fills],
        "experiment": experiment,
        "event_sequences": [item["sequence"] for item in events],
        "event_ids": [item["event"]["event_id"] for item in events],
        "event_types": [item["event"]["event_type"] for item in events],
    }


def _read_sse_sequences(base_url: str, cursor: int, expected_count: int) -> list[int]:
    sequences: list[int] = []
    with httpx.Client(timeout=httpx.Timeout(5.0)) as client:
        with client.stream(
            "GET",
            f"{base_url}/api/events/stream",
            headers={"Last-Event-ID": str(cursor)},
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("id: "):
                    sequences.append(int(line[4:]))
                    if len(sequences) == expected_count:
                        break
    return sequences


def _read_ws_sequences(ws_url: str, cursor: int, expected_count: int) -> list[int]:
    sequences: list[int] = []
    with websocket_connect(
        f"{ws_url}/api/events/ws?since_id={cursor}",
        open_timeout=5,
        close_timeout=2,
    ) as websocket:
        for _ in range(expected_count):
            payload = json.loads(websocket.recv(timeout=5))
            assert payload.get("type") != "heartbeat"
            sequences.append(int(payload["sequence"]))
    return sequences


def _assert_sequence_contract(snapshot: dict) -> None:
    sequences = snapshot["event_sequences"]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert sequences == list(range(sequences[0], sequences[-1] + 1))


def test_process_restart_preserves_committed_state_and_resumable_streams(tmp_path: Path):
    workspace_root = tmp_path / "workspace"
    runtime_dir = tmp_path / "runtime"
    control_dir = tmp_path / "control"
    workspace_root.mkdir()
    seed = _run_helper("seed", workspace_root, runtime_dir)
    assert seed["fixture_receipt"] == "TEST_ONLY_PROCESS_RESTART_STREAM"
    assert seed["order_filled_count"] == 1

    with _server(workspace_root, runtime_dir, control_dir) as first:
        first_pid = first.pid
        initial = _state_snapshot(first.base_url, seed)
        _assert_sequence_contract(initial)
        assert initial["cash"] == pytest.approx(seed["cash_after_fill"])
        assert initial["pending_order"]["status"] == "PENDING"
        assert initial["fill_ids"].count(FILL_ID) == 1
        assert initial["event_types"].count("ORDER_FILLED") == 1

        cursor = initial["event_sequences"][1]
        expected_after_cursor = [seq for seq in initial["event_sequences"] if seq > cursor]
        assert expected_after_cursor
        assert _read_sse_sequences(first.base_url, cursor, len(expected_after_cursor)) == expected_after_cursor
        assert _read_ws_sequences(first.ws_url, cursor, len(expected_after_cursor)) == expected_after_cursor

    with _server(workspace_root, runtime_dir, control_dir) as second:
        assert second.pid != first_pid
        graceful = _state_snapshot(second.base_url, seed)
        assert graceful == initial
        assert graceful["event_types"].count("ORDER_FILLED") == 1
        inspect = _run_helper("inspect", workspace_root, runtime_dir)
        assert inspect["learning_case_id"] == LEARNING_CASE_ID
        assert inspect["event_count"] == seed["event_count"]

    with _server(workspace_root, runtime_dir, control_dir) as abrupt:
        abrupt_before_kill = _state_snapshot(abrupt.base_url, seed)
        assert abrupt_before_kill == initial
        abrupt_pid = abrupt.pid
        abrupt.kill()

    with _server(workspace_root, runtime_dir, control_dir) as after_kill:
        assert after_kill.pid != abrupt_pid
        recovered = _state_snapshot(after_kill.base_url, seed)
        assert recovered == initial
        assert recovered["fill_ids"].count(FILL_ID) == 1
        assert recovered["event_types"].count("ORDER_FILLED") == 1


def test_read_only_observer_does_not_mutate_runtime(tmp_path: Path):
    workspace_root = tmp_path / "workspace"
    runtime_dir = tmp_path / "runtime"
    control_dir = tmp_path / "control"
    workspace_root.mkdir()
    seed = _run_helper("seed", workspace_root, runtime_dir)
    before = _hash_directory(runtime_dir)
    assert before

    with _server(workspace_root, runtime_dir, control_dir, read_only=True) as observer:
        snapshot = _state_snapshot(observer.base_url, seed)
        _assert_sequence_contract(snapshot)
        cursor = snapshot["event_sequences"][1]
        expected = [seq for seq in snapshot["event_sequences"] if seq > cursor]
        assert _read_sse_sequences(observer.base_url, cursor, len(expected)) == expected
        assert _read_ws_sequences(observer.ws_url, cursor, len(expected)) == expected

    assert _hash_directory(runtime_dir) == before


def test_server_context_cleans_process_and_socket_on_failure(tmp_path: Path):
    workspace_root = tmp_path / "workspace"
    runtime_dir = tmp_path / "runtime"
    control_dir = tmp_path / "control"
    workspace_root.mkdir()
    holder: dict[str, _Server] = {}

    with pytest.raises(RuntimeError, match="TEST_ONLY forced client failure"):
        with _server(workspace_root, runtime_dir, control_dir) as server:
            holder["server"] = server
            raise RuntimeError("TEST_ONLY forced client failure")

    server = holder["server"]
    assert server.proc.poll() is not None
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", server.port), timeout=0.2)
