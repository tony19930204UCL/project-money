"""PAPER-only trade discipline + lesson loop.

Reads the strategy's own realized journal, turns every closed lot into a lesson
(cost-aware), and gates new entries: cooldown after exits, one entry per
ticker/strategy/day, minimum edge vs round-trip cost, and quarantine of
strategy x ticker cells whose net-of-cost expectancy is negative.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

ROUND_TRIP_COST_PCT = {"TW": 0.00585, "US": 0.0005}


def _ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def build_lessons(rows):
    """FIFO-pair SIMULATED BUY/SELL rows into lessons with net-of-cost pnl."""
    inv, out = {}, []
    for r in sorted((x for x in rows if x.get("status") == "SIMULATED"), key=lambda x: x["timestamp"]):
        t, fee = r["ticker"], float(r.get("fee", 0)) + float(r.get("tax", 0))
        if r["side"] == "BUY":
            inv.setdefault(t, []).append([float(r["size"]), float(r["fill_price"]), _ts(r["timestamp"]), r.get("thesis", ""), fee, float(r["size"])])
            continue
        q = float(r["size"])
        while q > 0 and inv.get(t):
            lot = inv[t][0]
            u = min(q, lot[0])
            buy_fee = lot[4] * u / lot[5]
            sell_fee = fee * u / float(r["size"])
            gross = (float(r["fill_price"]) - lot[1]) * u
            out.append({"ticker": t, "qty": u, "entry_thesis": lot[3], "exit_reason": r.get("thesis", ""),
                        "entry_ts": lot[2].isoformat(), "exit_ts": r["timestamp"],
                        "hold_min": round((_ts(r["timestamp"]) - lot[2]).total_seconds() / 60, 1),
                        "gross": round(gross, 4), "cost": round(buy_fee + sell_fee, 4),
                        "net": round(gross - buy_fee - sell_fee, 4)})
            lot[0] -= u
            q -= u
            if lot[0] <= 1e-9:
                inv[t].pop(0)
    return out


class TradeDiscipline:
    def __init__(self, market, journal_path, lessons_path=None, *, cooldown_after_stop_h=24,
                 cooldown_after_exit_h=6, min_edge_multiple=3.0, quarantine_min_lots=8,
                 probe_every_days=5, now=None):
        self.market, self.journal = market, Path(journal_path)
        self.lessons_path = Path(lessons_path) if lessons_path else self.journal.with_name(market + "_lessons.jsonl")
        self.cd_stop = timedelta(hours=cooldown_after_stop_h)
        self.cd_exit = timedelta(hours=cooldown_after_exit_h)
        self.mult, self.qmin, self.probe = min_edge_multiple, quarantine_min_lots, timedelta(days=probe_every_days)
        self._now = now
        self._cache = (None, [], [])

    def now(self):
        return self._now() if self._now else datetime.now(timezone.utc)

    def _load(self):
        try:
            size = self.journal.stat().st_size
        except OSError:
            return []
        if self._cache[0] != size:
            rows = []
            for l in self.journal.read_text().splitlines():
                try:
                    rows.append(json.loads(l))
                except ValueError:
                    pass
            lessons = build_lessons(rows)
            buys = [r for r in rows if r.get("status") == "SIMULATED" and r.get("side") == "BUY"]
            self._cache = (size, lessons, buys)
            try:
                self.lessons_path.write_text("".join(json.dumps(x) + "\n" for x in lessons))
            except OSError:
                pass
        return self._cache[1]

    def allow_entry(self, ticker, thesis, take_pct):
        """Return (allowed, reason). Reason is logged by the caller."""
        cost = ROUND_TRIP_COST_PCT.get(self.market, 0.001)
        if take_pct < self.mult * cost:
            return False, "edge_below_cost: take %.4f < %.1fx cost %.4f" % (take_pct, self.mult, cost)
        lessons = self._load()
        buys = self._cache[2]
        now = self.now()
        mine = [x for x in lessons if x["ticker"] == ticker and x["entry_thesis"] == thesis]
        for b in buys:
            if b["ticker"] == ticker and b.get("thesis") == thesis and _ts(b["timestamp"]).date() == now.date():
                return False, "one_entry_per_day"
        if mine:
            last = max(mine, key=lambda x: x["exit_ts"])
            ago = now - _ts(last["exit_ts"])
            if "stop" in last["exit_reason"] and ago < self.cd_stop:
                return False, "cooldown_after_stop"
            if ago < self.cd_exit:
                return False, "cooldown_after_exit"
            if len(mine) >= self.qmin and sum(x["net"] for x in mine) < 0:
                if ago < self.probe:
                    return False, "quarantined_negative_net_expectancy(n=%d)" % len(mine)
        return True, "ok"
