"""End-to-end TW paper session with the real feed/loop/runner and fake TWSE MIS
payloads that reproduce the 2026-10-12 failure (last trade '-', one ticker dead)."""
import json
from datetime import datetime, timezone

from cio_market_lab.data.intraday_feed import IntradayFeed
from cio_market_lab.engine.paper_runner import build_config, _default_orders
from cio_market_lab.engine.paper_scheduler import run_daily

OPEN = datetime(2026, 10, 13, 2, 0, tzinfo=timezone.utc)  # Tue 10:00 Taipei
DEAD = "2308"


def make_fetch(counter):
    def fetch(url):
        if "yahoo" in url:
            raise ValueError("yahoo down")
        sym = url.split("tse_")[1].split(".tw")[0]
        counter[sym] = counter.get(sym, 0) + 1
        n = counter[sym]
        if sym == DEAD:  # no last trade and empty book
            return {"msgArray": [{"z": "-", "pz": "-", "b": "-", "a": "-",
                                  "o": "100", "h": "101", "l": "99", "v": "5",
                                  "tlong": str(int(OPEN.timestamp() * 1000))}]}
        base = 100 + (n // 4)  # rising: triggers momentum buys
        row = {"o": str(base), "h": str(base + 1), "l": str(base - 1), "v": "50",
               "tlong": str(int(OPEN.timestamp() * 1000))}
        if n % 2:  # odd ticks: last trade is '-', fall back to book midpoint
            row.update(z="-", pz="-", b=f"{base - 0.5}_1_", a=f"{base + 0.5}_1_")
        else:
            row.update(z=str(base), pz=str(base))
        return {"msgArray": [row]}
    return fetch


def test_tw_session_trades_despite_dash_quotes(tmp_path):
    counter = {}
    feed = IntradayFeed(make_fetch(counter), clock=lambda: OPEN)
    feed.latency_log = tmp_path / "feed_latency.jsonl"
    orders = _default_orders(tmp_path)
    orders._now_fn = lambda: OPEN
    # Use a complete production swing warmup rather than a retired 40-tick scalp.
    cfg = build_config(tmp_path, feed=feed, order_service=orders, ticks=160, sleep_free=True)
    cfg["markets"].pop("US")
    assert run_daily(OPEN, cfg)["markets"]["TW"] == "SESSION_RAN"
    rows = [json.loads(x) for x in (tmp_path / "TW_journal.jsonl").read_text().splitlines()]
    sim = [r for r in rows if r.get("status") == "SIMULATED"]
    errors = [r for r in rows if r.get("status") == "ERROR"]
    assert sim, rows[:5]
    assert len(errors) < len(rows) * 0.2, errors[:3]
    assert counter["2330"] >= 160
