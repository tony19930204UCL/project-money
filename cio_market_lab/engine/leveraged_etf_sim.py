"""PAPER ONLY. Daily-compounding simulator for leveraged / inverse ETFs.

Never approximate a leveraged ETF as multiple x period return: path dependence
(volatility decay) is the point. NAV_t = NAV_{t-1} * (1 + L*r_t - fee_daily - fin_daily).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class LeveredEtfSpec:
    ticker: str
    leverage: float            # e.g. 3.0, 2.0, -1.0, -3.0
    annual_expense: float = 0.0095
    annual_financing: float = 0.0   # extra drag on borrowed notional (rate-based)
    trading_days: int = 252


def simulate_nav(spec: LeveredEtfSpec, daily_returns: Sequence[float], nav0: float = 1.0) -> list[float]:
    if nav0 <= 0:
        raise ValueError("nav0 must be positive")
    fee = spec.annual_expense / spec.trading_days
    borrowed = max(abs(spec.leverage) - 1.0, 0.0)
    fin = spec.annual_financing * borrowed / spec.trading_days
    nav = [nav0]
    for r in daily_returns:
        if r <= -1.0:
            raise ValueError("daily return <= -100%")
        step = 1.0 + spec.leverage * r - fee - fin
        nav.append(max(nav[-1] * step, 0.0))  # NAV floors at zero (wipe-out)
        if nav[-1] == 0.0:
            nav.extend([0.0] * (len(daily_returns) + 1 - len(nav)))
            break
    return nav


def decay_vs_naive(spec: LeveredEtfSpec, daily_returns: Sequence[float]) -> dict:
    """Compare true compounded result with naive leverage x underlying period return."""
    nav = simulate_nav(spec, daily_returns)
    under = 1.0
    for r in daily_returns:
        under *= 1.0 + r
    true_ret = nav[-1] / nav[0] - 1.0
    naive = spec.leverage * (under - 1.0)
    return {"true_return": true_ret, "naive_return": naive,
            "path_decay": true_ret - naive, "underlying_return": under - 1.0}


# Presets (expense ratios approximate; verify against issuer prospectus before relying on cost).
PRESETS = {
    "00631L.TW": LeveredEtfSpec("00631L.TW", 2.0, annual_expense=0.0100),
    "00632R.TW": LeveredEtfSpec("00632R.TW", -1.0, annual_expense=0.0100),
    "TQQQ": LeveredEtfSpec("TQQQ", 3.0, annual_expense=0.0084),
    "SQQQ": LeveredEtfSpec("SQQQ", -3.0, annual_expense=0.0095),
    "NVDL": LeveredEtfSpec("NVDL", 2.0, annual_expense=0.0115),
}
