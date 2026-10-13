"""PAPER-only sizing and exit rules used by the daily strategy runner.

The policy is deliberately deterministic and parameterized so forward results can
be compared across sizing, exit, and holding-period variants without changing the
execution model. It never submits a broker order.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class SizingPolicy:
    """Allocate a fixed fraction of current marked equity per ticker."""

    target_pct: float = 0.10
    max_pct: float = 0.20
    min_qty: int = 1

    def __post_init__(self):
        if not (0 < self.target_pct <= self.max_pct <= 1):
            raise ValueError("target_pct/max_pct must satisfy 0 < target <= max <= 1")
        if not isinstance(self.min_qty, int) or self.min_qty < 1:
            raise ValueError("min_qty must be a positive integer")

    def quantity(self, equity: float, price: float, current_qty: float = 0) -> int:
        if not all(math.isfinite(v) for v in (equity, price, current_qty)) or equity <= 0 or price <= 0:
            return 0
        target_notional = equity * self.target_pct
        max_notional = equity * self.max_pct
        remaining = min(target_notional, max_notional) - current_qty * price
        if remaining < price * self.min_qty:
            return 0
        return max(self.min_qty, math.floor(remaining / price))


@dataclass(frozen=True)
class ExitPolicy:
    """Deterministic, learnable exit ladder.

    The four exit limbs are separately journaled by the strategy: hard stop,
    fixed reward target, profitable trailing stop, and time stop. A TW day-trade
    policy adds a session-end flat rule so positions cannot be carried overnight.
    """

    stop_pct: float = 0.008
    take_pct: float = 0.012
    trail_pct: float = 0.006
    max_hold_ticks: int = 30
    force_flat_at_session_end: bool = False

    def __post_init__(self):
        if not (0 < self.stop_pct < 1 and 0 < self.take_pct < 10 and 0 < self.trail_pct < 1):
            raise ValueError("invalid exit percentages")
        if not isinstance(self.max_hold_ticks, int) or self.max_hold_ticks < 1:
            raise ValueError("max_hold_ticks must be a positive integer")

    def reason(self, entry: float, price: float, peak: float, held_ticks: int, session_end: bool = False) -> str | None:
        if not all(math.isfinite(v) for v in (entry, price, peak)) or min(entry, price, peak) <= 0:
            return "invalid_exit_state"
        if self.force_flat_at_session_end and session_end:
            return "tw_daytrade_session_end"
        if price <= entry * (1 - self.stop_pct):
            return "hard_stop"
        if price >= entry * (1 + self.take_pct):
            return "take_profit"
        if peak > entry * (1 + self.trail_pct) and price <= peak * (1 - self.trail_pct):
            return "trailing_stop"
        if held_ticks >= self.max_hold_ticks:
            return "time_stop"
        return None

    def learn_hold_ticks(self, observations, candidates=(10, 20, 30, 45, 60),
                         min_samples=5):
        """Return a bounded policy tuned from closed-trade observations.

        ``observations`` contains mappings with ``hold_ticks`` and ``pnl``.
        The selected horizon is the candidate with the best mean PnL among
        trades that would have exited by that horizon.  No mutation occurs;
        callers persist the returned policy only after a forward-evaluation
        gate passes.
        """
        rows = [row for row in observations
                if isinstance(row, dict)
                and isinstance(row.get("hold_ticks"), (int, float))
                and isinstance(row.get("pnl"), (int, float))
                and math.isfinite(float(row["hold_ticks"]))
                and math.isfinite(float(row["pnl"]))
                and row["hold_ticks"] > 0]
        options = sorted({int(v) for v in candidates if isinstance(v, int) and v > 0})
        if not options:
            return self
        scored = []
        for horizon in options:
            sample = [float(row["pnl"]) for row in rows if row["hold_ticks"] <= horizon]
            if len(sample) >= min_samples:
                scored.append((sum(sample) / len(sample), -horizon, horizon))
        if not scored:
            return self
        chosen = max(scored)[2]
        return ExitPolicy(stop_pct=self.stop_pct, take_pct=self.take_pct,
                          trail_pct=self.trail_pct, max_hold_ticks=chosen,
                          force_flat_at_session_end=self.force_flat_at_session_end)
