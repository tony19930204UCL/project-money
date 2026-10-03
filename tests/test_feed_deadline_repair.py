"""A nominal provider timeout is not a deadline for the paper runner."""
from datetime import datetime, timezone
import os
import subprocess
import sys
import time

from cio_market_lab.engine.autonomous_runner import DYNAMIC_DESK_ID
from cio_market_lab.engine.paper_orders import PaperExperimentSettings
from tests.test_cio_owned_desk import build_test_runner


def test_scheduled_feed_ignores_nominal_timeout_and_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "isolated_hermes_home"))
    clock = [datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)]  # XNYS open, fixture only
    runner, orders, _, adapter = build_test_runner(tmp_path, clock)
    adapter.is_fixture = True
    runner.configure(PaperExperimentSettings(
        strategy_id="feed-us-test", enabled=True, universe=["AAPL"],
        mode="intraday", cadence_seconds=900, base_currency="USD",
    ))
    started = tmp_path / "worker.started"
    late_write = tmp_path / "late_write"

    def ignoring_timeout(symbol, *args, **kwargs):
        assert symbol == "AAPL"
        started.write_text(str(os.getpid()))
        time.sleep(1.5)  # Simulates a network library ignoring timeout=0.1.
        late_write.write_text("unacceptable")
        return []

    monkeypatch.setattr(adapter, "get_bars", ignoring_timeout)
    monkeypatch.setattr(runner, "FEED_STAGE_SECONDS", 0.2, raising=False)
    before = time.monotonic()
    result = runner.run_one_cycle("feed-us-test", ["AAPL"])
    elapsed = time.monotonic() - before
    assert elapsed < 1.0, f"actual production caller took {elapsed:.3f}s"
    assert started.exists()
    assert not late_write.exists()
    assert result["run"]["status"] == "BLOCKED"
    assert "FEED_UNAVAILABLE" in result["decisions"][0]["reason"]
    assert orders.all_orders() == []
    worker_pid = int(started.read_text())
    assert worker_pid != os.getpid()
    try:
        os.kill(worker_pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("timed-out network worker survived the deadline")
    time.sleep(1.6)
    assert not late_write.exists(), "late writes must remain impossible after return"


def test_context_feed_timeout_kills_descendants_before_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "isolated_hermes_home"))
    clock = [datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)]
    runner, orders, _, adapter = build_test_runner(tmp_path, clock)
    adapter.is_fixture = True
    runner.FEED_STAGE_SECONDS = 0.5
    runner.configure(PaperExperimentSettings(
        strategy_id="feed-us-test", enabled=True, universe=["AAPL"],
        mode="intraday", cadence_seconds=900, base_currency="USD",
    ))

    class UnusedProvider:
        def is_available(self):
            return True

        def request_decision(self, context):
            raise AssertionError("provider must not see an incomplete context")

    runner.cio_executor = UnusedProvider()
    child_pid_file = tmp_path / "descendant.pid"
    late_write = tmp_path / "descendant.late"

    def ignoring_timeout(symbol):
        child = subprocess.Popen([
            sys.executable, "-c",
            "import pathlib,sys,time; time.sleep(1.5); pathlib.Path(sys.argv[1]).write_text('late')",
            str(late_write),
        ])
        child_pid_file.write_text(str(child.pid))
        time.sleep(2)
        return adapter.get_bars(symbol)[-1]

    monkeypatch.setattr(adapter, "get_latest_bar", ignoring_timeout)
    start = time.monotonic()
    result = runner.run_one_cycle("feed-us-test", ["AAPL"])
    assert time.monotonic() - start < 1.4
    assert child_pid_file.exists(), "context feed stage was not reached"
    assert result["run"]["status"] == "BLOCKED"
    assert "FEED_UNAVAILABLE" in result["decisions"][0]["reason"]
    assert orders.all_orders() == []
    child_pid = int(child_pid_file.read_text())
    try:
        state = open(f"/proc/{child_pid}/stat", encoding="utf-8").read().split()[2]
    except FileNotFoundError:
        state = "gone"
    assert state in {"Z", "gone"}, "adapter descendant remains running"
    time.sleep(1.6)
    assert not late_write.exists()
