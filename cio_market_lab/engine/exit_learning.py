"""Leakage-safe exit-horizon prediction and retrospective scan for PAPER use.

The predictor consumes closed-trade observations only.  It never changes a live
policy by itself: callers must inspect the out-of-sample gate before promotion.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class HorizonScore:
    horizon: int
    train_samples: int
    train_mean_pnl: float
    oos_samples: int = 0
    oos_mean_pnl: float | None = None


@dataclass(frozen=True)
class ExitScan:
    selected_horizon: int | None
    baseline_oos_mean_pnl: float | None
    selected_oos_mean_pnl: float | None
    scores: tuple[HorizonScore, ...]
    evidence_scope: str = "RETROSPECTIVE_ONLY"
    promotion_ready: bool = False


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _horizon_pnl(row: Mapping[str, Any], horizon: int) -> float:
    values = row.get("pnl_by_horizon")
    if not isinstance(values, Mapping) or str(horizon) not in values:
        raise ValueError(f"missing pnl_by_horizon[{horizon}]")
    return _number(values[str(horizon)], "horizon pnl")


def _ordered(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    result = list(rows)
    for row in result:
        if not isinstance(row, Mapping):
            raise ValueError("observations must be mappings")
        if "sequence" not in row:
            raise ValueError("each observation needs sequence")
        if not isinstance(row["sequence"], int) or isinstance(row["sequence"], bool):
            raise ValueError("sequence must be an integer")
        _number(row.get("hold_ticks"), "hold_ticks")
    return sorted(result, key=lambda row: row["sequence"])


def predict_exit_horizon(
    observations: Sequence[Mapping[str, Any]],
    candidates: Sequence[int] = (10, 20, 30, 45, 60),
    *,
    min_samples: int = 5,
) -> dict[str, Any]:
    """Select the best horizon using only observations available at prediction time."""
    if not isinstance(min_samples, int) or min_samples < 1:
        raise ValueError("min_samples must be positive")
    options = sorted({int(value) for value in candidates if isinstance(value, int) and value > 0})
    if not options:
        raise ValueError("candidates must contain positive integers")
    rows = _ordered(observations)
    scored = []
    for horizon in options:
        sample = [
            _horizon_pnl(row, horizon)
            for row in rows
            if float(row["hold_ticks"]) <= horizon
        ]
        if len(sample) >= min_samples:
            scored.append({
                "horizon": horizon,
                "samples": len(sample),
                "mean_pnl": round(sum(sample) / len(sample), 10),
            })
    selected = max(scored, key=lambda item: (item["mean_pnl"], -item["horizon"])) if scored else None
    return {
        "selected_horizon": selected["horizon"] if selected else None,
        "candidates": scored,
        "training_samples": len(rows),
        "evidence_scope": "RETROSPECTIVE_ONLY",
        "promotion_ready": False,
    }


def scan_exit_horizons(
    observations: Sequence[Mapping[str, Any]],
    candidates: Sequence[int] = (10, 20, 30, 45, 60),
    *,
    min_train_samples: int = 5,
    min_oos_samples: int = 3,
    min_improvement: float = 0.0,
) -> ExitScan:
    """Walk forward: predict from prior rows, then score the next row only."""
    if min_train_samples < 1 or min_oos_samples < 1:
        raise ValueError("sample gates must be positive")
    rows = _ordered(observations)
    options = sorted({int(value) for value in candidates if isinstance(value, int) and value > 0})
    if not options:
        raise ValueError("candidates must contain positive integers")
    selected_oos: list[float] = []
    baseline_oos: list[float] = []
    selected_by_fold: list[int] = []
    for index in range(min_train_samples, len(rows)):
        train = rows[:index]
        test = rows[index]
        prediction = predict_exit_horizon(train, options, min_samples=min_train_samples)
        horizon = prediction["selected_horizon"]
        if horizon is None:
            continue
        selected_by_fold.append(horizon)
        selected_oos.append(_horizon_pnl(test, horizon))
        baseline_oos.append(_horizon_pnl(test, max(options)))
    scores = []
    for horizon in options:
        train_values = [_horizon_pnl(row, horizon) for row in rows if float(row["hold_ticks"]) <= horizon]
        oos_values = [_horizon_pnl(row, horizon) for row in rows[-len(selected_oos):]] if selected_oos else []
        scores.append(HorizonScore(horizon, len(train_values), sum(train_values) / len(train_values) if train_values else 0.0, len(oos_values), sum(oos_values) / len(oos_values) if oos_values else None))
    chosen = selected_by_fold[-1] if selected_by_fold else None
    selected_mean = sum(selected_oos) / len(selected_oos) if selected_oos else None
    baseline_mean = sum(baseline_oos) / len(baseline_oos) if baseline_oos else None
    ready = (
        chosen is not None
        and len(selected_oos) >= min_oos_samples
        and selected_mean is not None
        and baseline_mean is not None
        and selected_mean > baseline_mean + min_improvement
    )
    return ExitScan(chosen, baseline_mean, selected_mean, tuple(scores), promotion_ready=ready)
