"""Explicit PAPER bar-open simulation. Never asserts executable BBO evidence.

A later OHLC bar's published open is a model assumption, not a broker fill.
Only callers that opted into NEXT_BAR_OPEN may use this resolver. The default
order-book execution model remains fail closed when a book is unavailable.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, Optional
from cio_market_lab.domain.models import Bar, ConsumedQuoteEvidence, Order, OrderSide, OrderStatus, OrderType
from cio_market_lab.engine.execution import ExecutionCostConfig


@dataclass(frozen=True)
class ResolvedPaperExecution:
    timestamp: datetime
    base_price: float
    effective_price: float
    per_share_slippage: float
    quote_evidence: Optional[ConsumedQuoteEvidence]
    verification: str
    assumptions: dict[str, Any]


def resolve_next_bar_open(bar: Bar, order: Order, costs: ExecutionCostConfig, *,
                          now: datetime, decision_at: datetime,
                          allow_fixture: bool = False, max_age_seconds: float = 300,
                          used_volume: float = 0) -> Optional[ResolvedPaperExecution]:
    """Resolve one full PAPER order from a source bar; unsupported inputs return None.

    Volume is an explicitly declared model capacity, not order-book depth. The
    caller subtracts earlier model fills sharing this bar across all strategies.
    The decision/order must both precede the selected bar, preventing lookahead.
    """
    dates = (now, decision_at, order.created_at, bar.timestamp, bar.observed_at)
    if any(d is None or d.tzinfo is None or d.utcoffset() is None for d in dates):
        return None
    if not (max(decision_at, order.created_at) < bar.timestamp <= bar.observed_at <= now):
        return None
    if (not math.isfinite(max_age_seconds) or max_age_seconds < 0
            or not math.isfinite(bar.delay_seconds) or bar.delay_seconds < 0
            or bar.delay_seconds > max_age_seconds
            # Retrieval freshness cannot refresh an old source event.
            or (now - bar.timestamp).total_seconds() > max_age_seconds
            or (now - bar.observed_at).total_seconds() > max_age_seconds):
        return None
    source = (bar.source or '').strip()
    quality = (bar.quality or '').lower()
    combined = (source.lower() + ' ' + quality)
    fixture = bar.is_fixture or any(x in combined for x in ('fixture', 'test_only', 'test-only'))
    if (bar.is_stale or bar.is_synthetic or not source or source.lower() == 'missing'
            or any(x in combined for x in ('synthetic', 'fallback', 'proxy', 'missing', 'unavailable'))
            or (fixture and not allow_fixture)):
        return None
    if (order.status != OrderStatus.PENDING or order.order_type != OrderType.MARKET
            or order.instrument_type != 'EQUITY' or order.symbol != bar.symbol
            or order.side not in (OrderSide.BUY, OrderSide.SELL)):
        return None
    values = (bar.open, bar.high, bar.low, bar.close, bar.volume, order.quantity, used_volume)
    if any(isinstance(x, bool) or not math.isfinite(x) for x in values):
        return None
    if (min(bar.open, bar.high, bar.low, bar.close) <= 0
            or bar.high < max(bar.open, bar.close, bar.low)
            or bar.low > min(bar.open, bar.close, bar.high)
            or order.quantity <= 0 or used_volume < 0
            or order.quantity + used_volume > bar.volume):
        return None
    slip, effective = costs.calculate_slippage(bar.open, order.side)
    if not math.isfinite(effective) or effective <= 0:
        return None
    return ResolvedPaperExecution(
        timestamp=bar.timestamp, base_price=bar.open, effective_price=effective,
        per_share_slippage=slip, quote_evidence=None,
        verification='BAR_NEXT_OPEN_TEST_ONLY' if fixture else 'BAR_NEXT_OPEN_SIMULATION',
        assumptions={
            'execution': 'local_paper_only', 'execution_model': 'NEXT_BAR_OPEN',
            'timing_assumption': 'next_bar_open_simulation_not_bbo',
            'historical_evidence_only': True,
            'liquidity_assumption': 'full_quantity_within_reported_bar_volume_not_orderbook_depth',
            'source_bar': bar.model_dump(mode='json'),
            'decision_at': decision_at.isoformat(), 'order_created_at': order.created_at.isoformat(),
            'capacity_previously_consumed': used_volume, 'base_price': bar.open,
            'is_fixture': fixture, 'slippage_embedded': True,
            'broker_connected': False, 'live_execution_acceptance': False,
        },
    )
