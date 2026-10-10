"""Durable, PAPER-only intraday session runner."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import math
import os
import tempfile

from cio_market_lab.data.intraday_feed import FeedUnavailable, market_open
from cio_market_lab.engine.paper_trading_loop import PaperAccount
from cio_market_lab.engine.pnl_attribution import attribute_pnl


class PaperSession:
    def __init__(self, feed, loop, strategies, account_path, journal_path, symbols):
        self.feed, self.loop = feed, loop
        self.strategies = list(strategies)
        self.symbols = list(dict.fromkeys(symbols))
        self.account_path, self.journal_path = Path(account_path), Path(journal_path)
        if self.account_path.exists():
            try:
                state = json.loads(self.account_path.read_text(encoding="utf-8"))
                if not isinstance(state, dict) or set(state) != {"cash", "positions", "realized_pnl", "marks", "daily_start_equity"}:
                    raise ValueError("invalid state schema")
                if not isinstance(state["positions"], dict) or not isinstance(state["marks"], dict):
                    raise ValueError("invalid position/mark maps")
                for value in [state["cash"], state["realized_pnl"], *state["positions"].values(), *state["marks"].values()]:
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                        raise ValueError("nonfinite state value")
                start = state["daily_start_equity"]
                if start is not None and (isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start)):
                    raise ValueError("invalid starting equity")
                self.account = PaperAccount(**state)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise ValueError("CORRUPT_PAPER_ACCOUNT_STATE") from exc
        else:
            self.account = PaperAccount(cash=0.0)

    def _append(self, record):
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "paper_only": True, **record}
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, allow_nan=False, sort_keys=True, default=str) + "\n")

    def _persist(self):
        self.account_path.parent.mkdir(parents=True, exist_ok=True)
        state = {key: getattr(self.account, key) for key in
                 ("cash", "positions", "realized_pnl", "marks", "daily_start_equity")}
        fd, name = tempfile.mkstemp(prefix=".paper-account-", dir=self.account_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, allow_nan=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.account_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def run_session(self, max_ticks, sleep=lambda seconds: None):
        if not isinstance(max_ticks, int) or isinstance(max_ticks, bool) or max_ticks < 0:
            raise ValueError("max_ticks must be a nonnegative integer")
        events = []
        for tick in range(max_ticks):
            now = self.feed.clock() if callable(getattr(self.feed, "clock", None)) else datetime.now(timezone.utc)
            opened = [symbol for symbol in self.symbols if market_open(
                "TW" if symbol.upper().endswith((".TW", ".TWO")) else "US", now)]
            closed = set(self.symbols) - set(opened)
            for symbol in sorted(closed):
                event = {"status": "SKIPPED", "reason": "MARKET_CLOSED", "ticker": symbol, "tick": tick}
                self._append(event)
                events.append(event)
            active = [s for s in self.strategies if getattr(s, "ticker", None) in opened]
            if opened:
                try:
                    # The existing loop fetches all held positions as well; never
                    # fetch closed-market holdings as part of an open-market tick.
                    if any(symbol not in opened for symbol in self.account.positions):
                        event = {"status": "SKIPPED", "reason": "CLOSED_POSITION_MARKET", "tick": tick}
                        self._append(event)
                        events.append(event)
                    else:
                        result = self.loop.run_once(self.feed, self.account, active)
                        self.account.marks.update(result.get("marks", {}))
                        for item in result.get("events", []):
                            event = {**item, "tick": tick}
                            self._append(event)
                            events.append(event)
                except (FeedUnavailable, ValueError) as exc:
                    event = {"status": "ERROR", "reason": type(exc).__name__, "detail": str(exc), "tick": tick}
                    self._append(event)
                    events.append(event)
            self._persist()
            if tick + 1 < max_ticks:
                sleep(1)
        equity = self.account.cash + sum(
            qty * self.account.marks.get(symbol, 0.0)
            for symbol, qty in self.account.positions.items())
        fills = [e for e in events if e.get("status") == "SIMULATED"]
        attribution = attribute_pnl(fills)
        summary = {"equity": equity, "pnl": self.account.realized_pnl,
                   "trades": len(fills), "per_strategy_pnl": {
                       k: v["realized_pnl"] for k, v in attribution["by_strategy"].items()},
                   "ticks": max_ticks, "paper_only": True}
        self._append({"status": "SESSION_SUMMARY", **summary})
        return summary
