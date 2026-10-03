"""Explicit cash-expiry accounting; not exchange delivery or market execution.

Evidence supplied to this calculator does not grant production adapter acceptance.
The lifecycle still gates production derivatives. No BBO, FX, broker, inferred
expiry price, or option exercise stock position is manufactured here.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cio_market_lab.engine.paper_derivatives import (
    AccountingDelta, ContractSpec, DerivativeInstrumentType, DerivativePosition,
    NormalizedAccountingEvent, OptionRight,
)


class CashSettlementEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    contract_symbol: str
    underlying_symbol: str
    expiry: datetime
    settlement_type: str
    settlement_price: float = Field(ge=0)
    timestamp: datetime
    observed_at: datetime
    is_fixture: bool
    source: str
    provenance: dict[str, Any]


def build_cash_expiry_event(position: DerivativePosition, spec: ContractSpec,
                            evidence: CashSettlementEvidence, now: datetime,
                            *, fixture_mode: bool) -> NormalizedAccountingEvent:
    """Return one immutable final settlement, without mutating the input position.

    Futures release reserved margin and settle only the variation after the last
    daily MTM. Long options receive intrinsic cash, including a legitimate zero
    payoff; the premium was already debited at entry. Settlement charges are zero
    explicit paper assumptions, not an inferred exchange fee schedule.
    """
    dates = (now, spec.expiry, evidence.expiry, evidence.timestamp, evidence.observed_at)
    if any(d is None or d.tzinfo is None for d in dates):
        raise ValueError('SETTLEMENT_CLOCK_OR_EXPIRY_UNAVAILABLE')
    expiry = spec.expiry.astimezone(timezone.utc)
    if (evidence.contract_symbol != spec.symbol or
        evidence.underlying_symbol != spec.underlying_symbol or
        evidence.expiry != expiry or evidence.settlement_type != 'CASH' or
        evidence.is_fixture != fixture_mode or not evidence.source.strip() or
        not evidence.provenance.get('authority') or
        evidence.provenance.get('kind') != 'FINAL_SETTLEMENT' or
        now < expiry or evidence.timestamp < expiry or
        evidence.observed_at < evidence.timestamp or evidence.observed_at > now):
        raise ValueError('SETTLEMENT_SOURCE_UNAVAILABLE')
    px = evidence.settlement_price
    if not math.isfinite(px) or px < 0:
        raise ValueError('SETTLEMENT_PRICE_INVALID')
    if not position.quantity or position.symbol != spec.symbol:
        raise ValueError('NO_OPEN_DERIVATIVE')
    key = f'expiry_cash:{position.position_id}:{spec.expiry.isoformat()}'
    closed = position.model_copy(deep=True)
    if position.instrument_type == DerivativeInstrumentType.FUTURE:
        if px <= 0:
            raise ValueError('SETTLEMENT_PRICE_INVALID')
        cash = round((px - position.last_settlement_price) * position.quantity * spec.multiplier, 4)
        realized = cash
        closed.last_settlement_price = px
        closed.accumulated_settled_pnl = round(position.accumulated_settled_pnl + cash, 4)
        payoff_price = px
    elif position.instrument_type == DerivativeInstrumentType.OPTION:
        if position.quantity < 0 or spec.strike is None or spec.option_right is None:
            raise ValueError('SHORT_OPTION_OR_EXERCISE_SPEC_UNSUPPORTED')
        payoff_price = max(px - spec.strike, 0.0) if spec.option_right == OptionRight.CALL else max(spec.strike - px, 0.0)
        cash = round(payoff_price * position.quantity * spec.multiplier, 4)
        realized = round(cash - position.total_premium_paid, 4)
    else:
        raise ValueError('UNSUPPORTED_DERIVATIVE_CONTRACT')
    closed.quantity = 0.0
    closed.margin_locked = 0.0
    closed.current_price = payoff_price
    closed.market_value = 0.0
    closed.unrealized_pnl = 0.0
    closed.total_premium_paid = 0.0
    closed.last_updated_at = now
    delta = AccountingDelta(
        idempotency_key=key, aggregate_id=spec.symbol,
        event_type='DERIVATIVE_EXPIRY_CASH_SETTLED', timestamp=now,
        cash_delta=cash, realized_pnl_delta=realized,
        margin_locked_delta=-position.margin_locked,
        metadata={'cash_settlement': True, 'prior_settlement_price': position.last_settlement_price,
                  'settlement_price': px, 'settled_quantity': position.quantity,
                  'payoff_price': payoff_price, 'settlement_charges_assumption': 'PAPER_ZERO_SETTLEMENT_CHARGES'},
    )
    return NormalizedAccountingEvent(
        event_type='DERIVATIVE_EXPIRY_CASH_SETTLED', aggregate_id=spec.symbol,
        timestamp=now, delta=delta, idempotency_key=key,
        payload={'position': closed.model_dump(mode='json'),
                 'settlement_evidence': evidence.model_dump(mode='json'),
                 'settlement_source': evidence.source,
                 'settlement_provenance': evidence.provenance,
                 'settled_quantity': position.quantity, 'cash_settlement': True,
                 'execution_status': 'PAPER_CASH_SETTLED',
                 'is_fixture': evidence.is_fixture, 'paper_only': True, 'broker_connected': False},
    )
