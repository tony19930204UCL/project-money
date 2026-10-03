"""A timed-out Hermes bootstrap must not leave its subprocess descendants alive."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from cio_market_lab.integrations import hermes_chat
from cio_market_lab.integrations.hermes_chat import run_hermes_cli_chat as subprocess_chat_under_test


def test_timeout_terminates_bootstrap_descendants(monkeypatch, tmp_path):
    pid_file = tmp_path / "child.pid"
    script = (
        "import os, pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {child.pid}')\n"
        "time.sleep(30)\n"
    )
    monkeypatch.setattr(
        hermes_chat, "build_production_chat_command",
        lambda **kwargs: [sys.executable, "-c", script, str(pid_file)],
    )
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess_chat_under_test("probe", timeout_seconds=1)
        parent_pid, descendant_pid = map(int, pid_file.read_text().split())
        for pid in (parent_pid, descendant_pid):
            proc_stat = Path(f"/proc/{pid}/stat")
            # An unreaped orphan may briefly be a zombie; it must not be running.
            try:
                state = proc_stat.read_text().split()[2]
            except FileNotFoundError:
                continue  # Exited between lookup and read; nonexistence is safe.
            assert state == "Z"
    finally:
        if pid_file.exists():
            parent_pid = int(pid_file.read_text().split()[0])
            try:
                os.killpg(parent_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
