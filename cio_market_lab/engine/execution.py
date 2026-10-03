from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
import uuid
from pydantic import BaseModel, Field

from cio_market_lab.domain.models import (
    Bar,
    DecisionScope,
    Fill,
    Market,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)


class OHLCAmbiguityPolicy(str, Enum):
    """Policy for resolving execution order when multiple thresholds are breached in one bar."""
    PESSIMISTIC = "PESSIMISTIC"  # Assume adverse path (e.g. stop loss triggered before profit limit)
    OPTIMISTIC = "OPTIMISTIC"    # Assume favorable path (e.g. profit limit triggered before stop loss)
    OPEN_FIRST = "OPEN_FIRST"    # Assume path closest to open price triggered first


class ExecutionCostConfig(BaseModel):
    """Configurable fees, taxes, and slippage."""
    fee_rate_tw: float = 0.001425  # 0.1425% commission for TW
    fee_rate_us: float = 0.0005    # 0.05% commission for US
    min_fee_tw: float = 20.0       # Minimum NTD 20
    min_fee_us: float = 1.0        # Minimum USD 1.0
    tax_rate_tw_sell: float = 0.003  # 0.3% TW securities transaction tax on sell
    tax_rate_us_sell: float = 0.0    # 0% transaction tax on US
    slippage_bps: float = 5.0      # 5 basis points (0.05%)
    allow_stale_execution: bool = False
    max_delay_seconds: float = 300.0

    def calculate_fee(self, market: Market, trade_value: float) -> float:
        if market == Market.TW:
            return round(max(self.min_fee_tw, trade_value * self.fee_rate_tw), 4)
        return round(max(self.min_fee_us, trade_value * self.fee_rate_us), 4)

    def calculate_tax(self, market: Market, side: OrderSide, trade_value: float) -> float:
        if side == OrderSide.SELL and market == Market.TW:
            return round(trade_value * self.tax_rate_tw_sell, 4)
        return 0.0

    def get_tax_rate(self, market: Market, side: OrderSide) -> float:
        """Return jurisdictional tax rate for side; BUY is always 0.0 unless explicit jurisdictional buy tax exists."""
        if side == OrderSide.BUY:
            return 0.0
        if market == Market.TW:
            return self.tax_rate_tw_sell
        return self.tax_rate_us_sell

    def calculate_slippage(self, base_price: float, side: OrderSide) -> Tuple[float, float]:
        slip_pct = self.slippage_bps / 10000.0
        if side == OrderSide.BUY:
            effective_price = base_price * (1.0 + slip_pct)
        else:
            effective_price = max(0.01, base_price * (1.0 - slip_pct))
        per_share_slippage = abs(effective_price - base_price)
        return round(per_share_slippage, 4), round(effective_price, 4)


class ExecutionResult(BaseModel):
    fills: List[Fill] = Field(default_factory=list)
    rejections: List[Tuple[Order, str]] = Field(default_factory=list)
    unfilled_orders: List[Order] = Field(default_factory=list)


class ExecutionEngine:
    """Simulated paper execution engine with next-bar semantics and ambiguity resolution."""

    def __init__(
        self,
        cost_config: Optional[ExecutionCostConfig] = None,
        ambiguity_policy: OHLCAmbiguityPolicy = OHLCAmbiguityPolicy.PESSIMISTIC,
        reject_stale: bool = True,
    ):
        self.cost_config = cost_config or ExecutionCostConfig()
        self.ambiguity_policy = ambiguity_policy
        self.reject_stale = reject_stale

    def _is_bar_stale(self, bar: Bar) -> bool:
        if not self.reject_stale:
            return False
        if bar.is_stale or bar.quality == "stale":
            return True
        if bar.delay_seconds > self.cost_config.max_delay_seconds:
            return True
        return False

    def process_bar(self, bar: Bar, pending_orders: List[Order]) -> ExecutionResult:
        result = ExecutionResult()
        symbol_orders = [o for o in pending_orders if o.symbol == bar.symbol and o.status == OrderStatus.PENDING]
        other_orders = [o for o in pending_orders if o.symbol != bar.symbol or o.status != OrderStatus.PENDING]
        result.unfilled_orders.extend(other_orders)

        if not symbol_orders:
            return result

        # Check stale bar rejection
        if self._is_bar_stale(bar):
            for order in symbol_orders:
                order.status = OrderStatus.REJECTED
                order.rejection_reason = "STALE_BAR_REJECTION: Market data stale, execution refused"
                result.rejections.append((order, order.rejection_reason))
            return result

        # Check for OHLC ambiguity between Stop and Limit orders for the same symbol
        stops = [o for o in symbol_orders if o.order_type == OrderType.STOP]
        limits = [o for o in symbol_orders if o.order_type == OrderType.LIMIT]

        has_stop_trigger = any(
            (o.side == OrderSide.SELL and bar.low <= (o.stop_price or 0.0)) or
            (o.side == OrderSide.BUY and bar.high >= (o.stop_price or float("inf")))
            for o in stops
        )
        has_limit_trigger = any(
            (o.side == OrderSide.BUY and bar.low <= (o.limit_price or float("-inf"))) or
            (o.side == OrderSide.SELL and bar.high >= (o.limit_price or float("inf")))
            for o in limits
        )

        orders_to_process = list(symbol_orders)
        if stops and limits and has_stop_trigger and has_limit_trigger:
            if self.ambiguity_policy == OHLCAmbiguityPolicy.PESSIMISTIC:
                # Process stops first, cancel conflicting limits
                orders_to_process = stops + limits
            elif self.ambiguity_policy == OHLCAmbiguityPolicy.OPTIMISTIC:
                # Process limits first, cancel conflicting stops
                orders_to_process = limits + stops

        position_closed = False

        for order in orders_to_process:
            if position_closed and order.order_type in (OrderType.STOP, OrderType.LIMIT):
                order.status = OrderStatus.CANCELLED
                order.rejection_reason = f"CANCELLED_DUE_TO_OHLC_AMBIGUITY_{self.ambiguity_policy.value}"
                result.rejections.append((order, order.rejection_reason))
                continue

            fill, rejected, reason = self._attempt_fill(order, bar)
            if fill is not None:
                order.status = OrderStatus.FILLED
                result.fills.append(fill)
                if order.side == OrderSide.SELL:
                    position_closed = True
            elif rejected:
                order.status = OrderStatus.REJECTED
                order.rejection_reason = reason
                result.rejections.append((order, reason))
            else:
                # Remains pending
                result.unfilled_orders.append(order)

        return result

    def _attempt_fill(
        self, order: Order, bar: Bar
    ) -> Tuple[Optional[Fill], bool, Optional[str]]:
        """Attempts to fill an order against incoming bar. Returns (Fill?, is_rejected, reason?)."""
        base_price: Optional[float] = None

        if order.order_type == OrderType.MARKET:
            # Next-bar open execution
            base_price = bar.open

        elif order.order_type == OrderType.LIMIT:
            if order.limit_price is None:
                return None, True, "INVALID_ORDER: Limit order missing limit_price"

            if order.side == OrderSide.BUY:
                if bar.low <= order.limit_price:
                    # Fills at open if opened below limit (price improvement), else at limit_price
                    base_price = min(bar.open, order.limit_price)
            elif order.side == OrderSide.SELL:
                if bar.high >= order.limit_price:
                    base_price = max(bar.open, order.limit_price)

        elif order.order_type == OrderType.STOP:
            if order.stop_price is None:
                return None, True, "INVALID_ORDER: Stop order missing stop_price"

            if order.side == OrderSide.SELL:
                if bar.low <= order.stop_price:
                    # Gapped below stop -> fills at open; else at stop_price
                    base_price = min(bar.open, order.stop_price)
            elif order.side == OrderSide.BUY:
                if bar.high >= order.stop_price:
                    base_price = max(bar.open, order.stop_price)

        elif order.order_type == OrderType.STOP_LIMIT:
            if order.stop_price is None or order.limit_price is None:
                return None, True, "INVALID_ORDER: Stop-limit order missing stop or limit price"
            if order.side == OrderSide.SELL:
                if bar.low <= order.stop_price and bar.high >= order.limit_price:
                    base_price = order.limit_price
            elif order.side == OrderSide.BUY:
                if bar.high >= order.stop_price and bar.low <= order.limit_price:
                    base_price = order.limit_price

        if base_price is None:
            # Order conditions not triggered on this bar
            return None, False, None

        per_share_slip, effective_price = self.cost_config.calculate_slippage(
            base_price, order.side
        )
        trade_value = order.quantity * effective_price
        fee = self.cost_config.calculate_fee(order.market, trade_value)
        tax = self.cost_config.calculate_tax(order.market, order.side, trade_value)
        slippage_total = round(order.quantity * per_share_slip, 4)

        fill = Fill(
            currency=order.currency,
            fill_id=str(uuid.uuid4()),
            order_id=order.order_id,
            symbol=order.symbol,
            bucket=order.bucket,
            side=order.side,
            quantity=order.quantity,
            fill_price=effective_price,
            fee=fee,
            tax=tax,
            slippage=slippage_total,
            timestamp=bar.timestamp,
            provenance={"source_bar": bar.model_dump(mode="json"),
                        "evidence_scope": "HISTORICAL_OHLC_SIMULATION_NOT_LIVE_EXECUTION"},
            consumed_quote=None,
            quote_verification="HISTORICAL_OHLC_SIMULATION_NOT_BBO",
            assumptions={
                "execution": "local_paper_only",
                "source_bar": bar.model_dump(mode="json"),
                "historical_evidence_only": True,
                "live_execution_acceptance": False,
                "broker_connected": False,
                "timing_assumption": "next_bar_ohlc_simulation_not_bbo",
                "liquidity_assumption": "full_quantity_simulation_not_orderbook_depth",
                "partial_fill_supported": False,
                "slippage_embedded": True,
                "base_price": base_price,
                "effective_price": effective_price,
                "slippage_bps": self.cost_config.slippage_bps,
                "fee_rate": self.cost_config.fee_rate_tw if order.market == Market.TW else self.cost_config.fee_rate_us,
                "tax_rate": self.cost_config.get_tax_rate(order.market, order.side),
                "ambiguity_policy": self.ambiguity_policy.value,
                "execution_type": "NEXT_BAR",
                "market": order.market.value,
            },
        )
        return fill, False, None
