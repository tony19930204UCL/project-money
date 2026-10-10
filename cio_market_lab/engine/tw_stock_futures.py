"""Taiwan single-stock futures PAPER simulation contract specifications.

Tick sizes follow the requested underlying stock reference-price ladder.
Margin percentages are assumptions, NOT exchange-published requirements.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
import math

from cio_market_lab.engine.paper_derivatives import ContractSpec, DerivativeInstrumentType

SIMULATION_ASSUMPTION = "SIMULATION_ASSUMPTION: Taiwan stock futures margin rates are illustrative, not exchange rules"


def stock_tick_size(reference_price: float) -> float:
    """Return the requested stock-price tick ladder at a caller-provided reference."""
    if isinstance(reference_price, bool) or not isinstance(reference_price, (int, float)):
        raise ValueError("reference_price must be a finite positive number")
    if not math.isfinite(reference_price) or reference_price <= 0:
        raise ValueError("reference_price must be a finite positive number")
    if reference_price < 10:
        return 0.01
    if reference_price < 50:
        return 0.05
    if reference_price < 100:
        return 0.1
    if reference_price < 500:
        return 0.5
    if reference_price < 1000:
        return 1.0
    return 5.0


def make_stock_future_spec(
    underlying: str,
    expiry: date | datetime,
    mini: bool = False,
    *,
    reference_price: float,
    initial_margin_rate: float = 0.135,
    maintenance_margin_rate: float = 0.1035,
) -> ContractSpec:
    """Build a FUTURE ContractSpec for the existing paper derivatives engine.

    reference_price is mandatory because tick size depends on the price band.
    The engine computes initial margin from entry notional and initial_margin_rate.
    """
    if not isinstance(underlying, str) or not underlying.strip():
        raise ValueError("underlying must be nonempty")
    if not isinstance(mini, bool):
        raise ValueError("mini must be bool")
    for name, rate in (("initial_margin_rate", initial_margin_rate), ("maintenance_margin_rate", maintenance_margin_rate)):
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if maintenance_margin_rate > initial_margin_rate:
        raise ValueError("maintenance margin cannot exceed initial margin")
    if isinstance(expiry, datetime):
        expiry_dt = expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
    elif isinstance(expiry, date):
        expiry_dt = datetime.combine(expiry, time(23, 59, 59), tzinfo=timezone.utc)
    else:
        raise ValueError("expiry must be a date or datetime")
    root = underlying.strip().upper()
    multiplier = 100 if mini else 2000
    return ContractSpec(
        symbol=f"{root}-{'MINI-' if mini else ''}FUT-{expiry_dt:%Y%m%d}",
        underlying_symbol=root,
        instrument_type=DerivativeInstrumentType.FUTURE,
        expiry=expiry_dt,
        multiplier=multiplier,
        tick_size=stock_tick_size(reference_price),
        initial_margin_rate=initial_margin_rate,
        maintenance_margin_rate=maintenance_margin_rate,
        margin_rules_label=SIMULATION_ASSUMPTION,
        currency="TWD",
    )
