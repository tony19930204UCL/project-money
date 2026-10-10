"""PAPER ONLY. Taiwan warrant Black-Scholes simulation; not executable quotes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
from typing import Sequence

SQRT2 = math.sqrt(2.0)
SQRT2PI = math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class WarrantSpec:
    code: str
    underlying: str
    right: str
    strike: float
    expiry: date
    exercise_ratio: float
    issuer: str
    currency: str = "TWD"
    label: str = "SIMULATION_ASSUMPTION"

    def __post_init__(self):
        if self.right not in ("CALL", "PUT"):
            raise ValueError("right must be CALL or PUT")
        if not math.isfinite(self.strike) or self.strike <= 0:
            raise ValueError("strike must be positive")
        if not math.isfinite(self.exercise_ratio) or self.exercise_ratio <= 0:
            raise ValueError("exercise_ratio must be positive")
        if self.currency != "TWD" or self.label != "SIMULATION_ASSUMPTION":
            raise ValueError("paper Taiwan warrant metadata required")


def _cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / SQRT2)


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT2PI


def _inputs(spec: WarrantSpec, spot: float, as_of: date, rate: float, sigma: float, q: float):
    if as_of > spec.expiry:
        raise ValueError("pricing after expiry")
    if not math.isfinite(spot) or spot <= 0:
        raise ValueError("spot must be positive")
    if any(not math.isfinite(v) for v in (rate, sigma, q)) or sigma < 0:
        raise ValueError("invalid rate, yield or volatility")
    return (spec.expiry - as_of).days / 365.0


def settlement(spec: WarrantSpec, spot: float, as_of: date) -> float:
    if as_of != spec.expiry:
        raise ValueError("cash settlement only on expiry")
    if not math.isfinite(spot) or spot < 0:
        raise ValueError("invalid settlement spot")
    intrinsic = max(spot - spec.strike, 0.0) if spec.right == "CALL" else max(spec.strike - spot, 0.0)
    return intrinsic * spec.exercise_ratio


def bs_price(spec: WarrantSpec, spot: float, as_of: date, rate: float, sigma: float, q: float = 0.0) -> float:
    t = _inputs(spec, spot, as_of, rate, sigma, q)
    if t == 0:
        return settlement(spec, spot, as_of)
    k = spec.strike
    if sigma == 0:
        call = max(spot * math.exp(-q*t) - k * math.exp(-rate*t), 0.0)
        put = max(k * math.exp(-rate*t) - spot * math.exp(-q*t), 0.0)
    else:
        d1 = (math.log(spot/k) + (rate-q+0.5*sigma*sigma)*t)/(sigma*math.sqrt(t))
        d2 = d1-sigma*math.sqrt(t)
        call = spot*math.exp(-q*t)*_cdf(d1)-k*math.exp(-rate*t)*_cdf(d2)
        put = k*math.exp(-rate*t)*_cdf(-d2)-spot*math.exp(-q*t)*_cdf(-d1)
    return max(0.0, call if spec.right == "CALL" else put)*spec.exercise_ratio


def implied_vol(spec: WarrantSpec, price: float, spot: float, as_of: date,
                rate: float, q: float = 0.0, tol: float = 1e-9,
                max_vol: float = 10.0, iterations: int = 200) -> float | None:
    _inputs(spec, spot, as_of, rate, 0.0, q)
    if as_of == spec.expiry or not math.isfinite(price) or price < 0 or tol <= 0 or max_vol <= 0:
        return None
    lo, hi = 0.0, max_vol
    floor = bs_price(spec, spot, as_of, rate, lo, q)
    ceiling = bs_price(spec, spot, as_of, rate, hi, q)
    if price < floor-tol or price > ceiling+tol:
        return None
    if abs(price-floor) <= tol:
        return 0.0
    if abs(price-ceiling) <= tol:
        return hi
    for _ in range(iterations):
        mid = (lo+hi)/2
        val = bs_price(spec, spot, as_of, rate, mid, q)
        if abs(val-price) <= tol:
            return mid
        if val < price:
            lo = mid
        else:
            hi = mid
    return None


def greeks(spec: WarrantSpec, spot: float, as_of: date, rate: float,
           sigma: float, q: float = 0.0) -> dict[str, float]:
    t = _inputs(spec, spot, as_of, rate, sigma, q)
    if t == 0 or sigma == 0:
        raise ValueError("greeks undefined at expiry or zero volatility")
    root = math.sqrt(t)
    d1 = (math.log(spot/spec.strike)+(rate-q+0.5*sigma*sigma)*t)/(sigma*root)
    d2 = d1-sigma*root
    discq, discr = math.exp(-q*t), math.exp(-rate*t)
    phi = _pdf(d1)
    call = spec.right == "CALL"
    delta = discq*(_cdf(d1) if call else _cdf(d1)-1)
    gamma = discq*phi/(spot*sigma*root)
    vega = spot*discq*phi*root
    theta = -spot*discq*phi*sigma/(2*root)
    if call:
        theta += q*spot*discq*_cdf(d1)-rate*spec.strike*discr*_cdf(d2)
    else:
        theta += -q*spot*discq*_cdf(-d1)+rate*spec.strike*discr*_cdf(-d2)
    ratio = spec.exercise_ratio
    return {"delta": delta*ratio, "gamma": gamma*ratio,
            "theta_per_day": theta*ratio/365.0, "vega": vega*ratio}


def warrant_tick(price: float) -> float:
    if not math.isfinite(price) or price < 0:
        raise ValueError("invalid price")
    if price < 5: return 0.01
    if price < 50: return 0.05
    if price < 100: return 0.1
    if price < 500: return 0.5
    if price < 1000: return 1.0
    return 5.0


def _floor_tick(value: float) -> float:
    # Work in integer cents to avoid floating point boundary drift.
    cents = max(0, math.floor(value*100+1e-9))
    boundaries = ((100000,500),(50000,100),(10000,50),(5000,10),(500,5),(0,1))
    for boundary, step in boundaries:
        if cents >= boundary:
            return (cents//step)*step/100
    return 0.0


def _ceil_tick(value: float) -> float:
    if value <= 0: return 0.0
    candidate = _floor_tick(value)
    while candidate+1e-10 < value:
        candidate = round(candidate+warrant_tick(candidate), 2)
    return candidate


def issuer_quote(fair_value: float, spread_ticks: int = 1) -> dict[str, float]:
    if not math.isfinite(fair_value) or fair_value < 0 or not isinstance(spread_ticks, int) or spread_ticks < 0:
        raise ValueError("invalid quote inputs")
    bid = _floor_tick(fair_value)
    ask = _ceil_tick(fair_value)
    for _ in range(spread_ticks):
        ask = round(ask+warrant_tick(ask), 2)
    return {"fair_value": fair_value, "bid": bid, "ask": ask}


def daily_mark_path(spec: WarrantSpec, observations: Sequence[tuple[date, float]],
                    rate: float, sigma: float, q: float = 0.0) -> list[dict]:
    return [{"date": day, "spot": spot,
             "model_price": bs_price(spec, spot, day, rate, sigma, q)}
            for day, spot in observations]
