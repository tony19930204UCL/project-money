"""Deterministic chronological walk-forward/holdout evaluator for retrospective bars."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Sequence
from cio_market_lab.domain.models import Bar
from cio_market_lab.engine.simulation import SimulationEngine
from cio_market_lab.strategies.base import BaseStrategy

@dataclass(frozen=True)
class EvaluationWindow:
    name: str
    bars: tuple[Bar, ...]
    start_index: int
    end_index: int

@dataclass(frozen=True)
class WalkForwardResult:
    windows: tuple[EvaluationWindow, ...]
    results: tuple[dict, ...]
    evidence_scope: str = "RETROSPECTIVE_ONLY"


def chronological_windows(bars: Sequence[Bar], train_fraction: float = .6, validation_fraction: float = .2) -> tuple[EvaluationWindow, ...]:
    ordered = sorted(bars, key=lambda b: (b.timestamp, b.symbol))
    if not 0 < train_fraction < 1 or not 0 < validation_fraction < 1 or train_fraction + validation_fraction >= 1:
        raise ValueError("fractions must be positive and leave a non-empty holdout")
    # The unit of chronology is a timestamp, not a row: symbols observed at the
    # same instant must never straddle training, validation or untouched OOS.
    timestamps = []
    offsets = []
    seen = set()
    for index, bar in enumerate(ordered):
        key = (bar.timestamp, bar.symbol)
        if key in seen:
            raise ValueError("Duplicate symbol/timestamp observation")
        seen.add(key)
        if not timestamps or bar.timestamp != timestamps[-1]:
            timestamps.append(bar.timestamp)
            offsets.append(index)
    if len(timestamps) < 3:
        raise ValueError("At least three distinct timestamps are required")
    n = len(timestamps)
    a, b = int(n * train_fraction), int(n * (train_fraction + validation_fraction))
    if a < 1 or b <= a or b >= n:
        raise ValueError("fractions produce an empty evaluation window")
    first, second = offsets[a], offsets[b]
    return tuple(EvaluationWindow(name, tuple(ordered[i:j]), i, j)
        for name, i, j in (("TRAIN", 0, first), ("VALIDATION", first, second),
                          ("HOLDOUT_OOS", second, len(ordered))))

def evaluate_walk_forward(bars: Sequence[Bar], strategy_factory: Callable[[], BaseStrategy], engine_factory: Callable[[], SimulationEngine] = SimulationEngine, train_fraction: float=.6, validation_fraction: float=.2) -> WalkForwardResult:
    windows=chronological_windows(bars,train_fraction,validation_fraction); results=[]
    for window in windows:
        # Isolated engine and freshly instantiated strategy prevent state leakage between folds.
        engine=engine_factory(); result=engine.run(list(window.bars), [strategy_factory()])
        if not result.is_deterministic: raise RuntimeError(f"event/portfolio reconciliation failed in {window.name}")
        results.append({"window":window.name,"start_index":window.start_index,"end_index":window.end_index,"bar_count":len(window.bars),"first_timestamp":window.bars[0].timestamp.isoformat(),"last_timestamp":window.bars[-1].timestamp.isoformat(),"total_signals":result.total_signals,"total_fills":result.total_fills,"equity":result.swing_portfolio.equity,"is_deterministic":result.is_deterministic})
    return WalkForwardResult(windows,tuple(results))
