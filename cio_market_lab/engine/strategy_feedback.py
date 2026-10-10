"""Bounded, deterministic PAPER-only strategy weight feedback."""
from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(name + " must be a finite number")
    return float(value)


def validate_table(table):
    if not isinstance(table, dict) or table.get("schema_version") != 1:
        raise ValueError("invalid weight table schema")
    weights = table.get("weights")
    if not isinstance(weights, dict):
        raise ValueError("weights must be a mapping")
    for name, row in weights.items():
        if not isinstance(name, str) or not name or not isinstance(row, dict):
            raise ValueError("invalid strategy entry")
        weight = _finite(row.get("weight"), "weight")
        if weight < 0:
            raise ValueError("negative weight")
        if row.get("last_direction", 0) not in (-1, 0, 1):
            raise ValueError("invalid direction")
        if not isinstance(row.get("pending", 0), int) or isinstance(row.get("pending", 0), bool) or row["pending"] < 0:
            raise ValueError("invalid pending")
    return table


def load_table(path):
    try:
        with Path(path).open(encoding="utf-8") as handle:
            return validate_table(json.load(handle))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError("CORRUPT_WEIGHT_TABLE") from exc


def save_table(path, table):
    """Atomically persist a validated JSON weight table."""
    import os
    import tempfile
    validate_table(table)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            tmp = handle.name
            json.dump(table, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)


def update_weights(attribution, table, *, min_trades=10, step=0.1,
                   floor=0.0, ceiling=1.5, hysteresis=2, evidence_id=None):
    """Return (new_table, human_readable_log).

    Evidence IDs prevent repeated application of the same attribution snapshot.
    A sign reversal requires hysteresis consecutive *distinct* observations.
    """
    validate_table(table)
    if not isinstance(min_trades, int) or isinstance(min_trades, bool) or min_trades < 1:
        raise ValueError("min_trades must be positive integer")
    if not isinstance(hysteresis, int) or isinstance(hysteresis, bool) or hysteresis < 1:
        raise ValueError("hysteresis must be positive integer")
    step, floor, ceiling = (_finite(v, k) for v, k in ((step, "step"), (floor, "floor"), (ceiling, "ceiling")))
    if step <= 0 or floor < 0 or ceiling < floor:
        raise ValueError("invalid bounds")
    if not isinstance(attribution, dict) or not isinstance(attribution.get("by_strategy"), dict):
        raise ValueError("invalid attribution")
    result = json.loads(json.dumps(table, allow_nan=False))
    logs = []
    for name, metrics in sorted(attribution["by_strategy"].items()):
        if name not in result["weights"]:
            continue
        row = result["weights"][name]
        if evidence_id is not None and row.get("last_evidence_id") == evidence_id:
            logs.append(f"{name}: unchanged; evidence already processed")
            continue
        if not isinstance(metrics, dict):
            raise ValueError("invalid metrics")
        count = metrics.get("closed_trades")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("invalid closed_trades")
        expectancy = metrics.get("expectancy")
        if count < min_trades or expectancy is None:
            logs.append(f"{name}: unchanged; {count} closed trades below minimum {min_trades}")
            continue
        expectancy = _finite(expectancy, "expectancy")
        direction = (expectancy > 0) - (expectancy < 0)
        if direction == 0:
            logs.append(f"{name}: unchanged; zero expectancy")
            continue
        previous = row.get("last_direction", 0)
        pending = row.get("pending", 0) + 1 if previous == direction else 1
        row["last_direction"], row["pending"] = direction, pending
        if evidence_id is not None:
            row["last_evidence_id"] = evidence_id
        if pending < hysteresis:
            logs.append(f"{name}: unchanged; hysteresis {pending}/{hysteresis} for {'positive' if direction > 0 else 'negative'} expectancy")
            continue
        old = row["weight"]
        new = min(ceiling, max(floor, round(old + direction * step, 12)))
        row["weight"] = new
        row["pending"] = 0
        logs.append(f"{name}: {old:g} -> {new:g}; {count} closed trades, expectancy {expectancy:g}; {'cap/floor reached' if new == old else 'positive' if direction > 0 else 'negative'}")
    return result, "\n".join(logs)


class WeightedStrategy:
    """Non-mutating strategy adapter; retains strategy_id and ticker."""
    def __init__(self, strategy, multiplier):
        self._strategy = strategy
        self.multiplier = multiplier
        self.ticker = strategy.ticker
        self.strategy_id = getattr(strategy, "strategy_id", type(strategy).__name__)

    def signals(self, quotes, account):
        if self.multiplier == 0:
            return []
        return [replace(signal, size=signal.size * self.multiplier)
                for signal in self._strategy.signals(quotes, account)]


def apply_weights(strategies, table):
    """Wrap pluggable strategies so Signal.size is scaled exactly once."""
    validate_table(table)
    result = []
    for strategy in strategies:
        if isinstance(strategy, WeightedStrategy):
            result.append(strategy)
            continue
        name = getattr(strategy, "strategy_id", type(strategy).__name__)
        multiplier = table["weights"].get(name, {"weight": 1.0})["weight"]
        result.append(WeightedStrategy(strategy, multiplier))
    return result
