"""Timeout diagnostics expose stream event kinds, not model text or credentials."""
import json
import subprocess
import sys

import pytest

from cio_market_lab.integrations import hermes_chat
from cio_market_lab.integrations.hermes_chat import run_hermes_cli_chat as subprocess_chat_under_test


def test_timeout_diagnostic_redacts_partial_stream(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    script = (
        "import json, sys, time\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'runtime_metadata','resolved_model':'SECRET','is_fixture':True}), flush=True)\n"
        "print(json.dumps({'type':'text','text':'SECRET','is_fixture':True}), flush=True)\n"
        "print('unstructured SECRET', flush=True)\n"
        "print('SECRET', file=sys.stderr, flush=True)\n"
        "time.sleep(30)\n"
    )
    monkeypatch.setattr(hermes_chat, "build_production_chat_command",
                        lambda **kwargs: [sys.executable, "-c", script])
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        subprocess_chat_under_test("probe", timeout_seconds=1)
    diagnostic = caught.value.bridge_diagnostic
    assert diagnostic["event_types"] == ["runtime_metadata", "text"]
    assert diagnostic["stderr_present"] is True
    assert diagnostic["stdout_bytes_seen"] > 0
    assert "SECRET" not in json.dumps(diagnostic)
