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
