from datetime import datetime, timezone
from types import SimpleNamespace
import json

from cio_market_lab.engine.paper_runner import build_config
from cio_market_lab.engine.paper_scheduler import run_daily

OPEN = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)   # Mon 11:00 NY
CLOSE = datetime(2026, 10, 12, 21, 0, tzinfo=timezone.utc)


class Feed:
    def __init__(self):
        self.n = 0
        self.clock = lambda: OPEN

    def latest(self, symbol, **kw):
        self.n += 1
        # rises, then drops: forces buy then exit
        price = 100 + [0, 1, 2, 3, 0, -3, -6, -9, -12, -15][min(self.n // 5, 9)]
        return SimpleNamespace(symbol=symbol, c=float(price), staleness_seconds=0.0, source="FIX", ts=OPEN)


def test_runner_end_to_end(tmp_path):
    from cio_market_lab.engine.paper_runner import _default_orders
    orders = _default_orders(tmp_path)
    orders._now_fn = lambda: OPEN
    cfg = build_config(tmp_path, feed=Feed(), order_service=orders, ticks=40, sleep_free=True)
    cfg["markets"].pop("TW")
    assert run_daily(OPEN, cfg)["markets"]["US"] == "SESSION_RAN"
    assert run_daily(OPEN, cfg)["markets"]["US"] == "ALREADY_RAN"
    assert run_daily(CLOSE, cfg)["markets"]["US"] == "FEEDBACK_DONE"
    assert (tmp_path / "reports" / "2026-10-12.md").exists()
    rows = [json.loads(x) for x in (tmp_path / "US_journal.jsonl").read_text().splitlines()]
    assert any(r.get("status") == "SIMULATED" for r in rows), rows[:5]


def test_http_fetch_sends_ua_and_retries_429(monkeypatch):
    import io
    from urllib.error import HTTPError
    from cio_market_lab.data import intraday_feed as m
    seen = []

    def fake(req, timeout):
        seen.append(req.get_header("User-agent"))
        if len(seen) < 3:
            raise HTTPError(req.full_url, 429, "x", {}, None)
        return io.BytesIO(b'{"ok": 1}')

    monkeypatch.setattr(m, "urlopen", fake)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert m.IntradayFeed._http_fetch("https://x") == {"ok": 1}
    assert len(seen) == 3 and all("Mozilla" in u for u in seen)


def test_real_session_sleeps_one_tick_interval(monkeypatch):
    from cio_market_lab.engine import paper_runner as pr
    slept = []
    monkeypatch.setattr(pr.time, "sleep", lambda s: slept.append(s))
    captured = {}
    from cio_market_lab.engine.paper_session import PaperSession
    monkeypatch.setattr(PaperSession, "run_session", lambda self, n, sleep=None: captured.setdefault("sleep", sleep))
    pr._RealSleepSession.__new__(pr._RealSleepSession).run_session(2)
    captured["sleep"](1)
    assert slept == [pr.TICK_SECONDS] and pr.TICK_SECONDS == 60


def test_stale_lock_is_cleared_fresh_lock_blocks(tmp_path):
    import os, time
    from datetime import datetime, timezone
    from cio_market_lab.engine.paper_scheduler import run_daily
    lock = tmp_path / ".l"
    lock.write_text("x")
    cfg = {"report_dir": str(tmp_path), "lock_path": str(lock), "markets": {}}
    now = datetime.now(timezone.utc)
    assert run_daily(now, cfg)["status"] == "LOCKED"
    old = time.time() - 10 * 3600
    os.utime(lock, (old, old))
    assert run_daily(now, cfg)["status"] == "OK"
