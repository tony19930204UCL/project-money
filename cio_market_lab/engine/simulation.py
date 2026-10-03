from __future__ import annotations

import math
from typing import Any, Dict, Iterator, List, Optional, Union
import uuid
from pydantic import BaseModel, Field

from cio_market_lab.data.replay import ReplayAdapter
from cio_market_lab.domain.events import EventEnvelope, EventType
from cio_market_lab.domain.models import (
    Bar,
    DecisionScope,
    Market,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PaperPortfolio,
    Signal,
)
from cio_market_lab.engine.execution import (
    ExecutionCostConfig,
    ExecutionEngine,
    OHLCAmbiguityPolicy,
)
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.events.store import EventStore
from cio_market_lab.strategies.base import BaseStrategy, StrategyContext


class SimulationResult(BaseModel):
    total_bars: int
    total_signals: int
    total_orders: int
    total_fills: int
    total_rejections: int
    swing_portfolio: PaperPortfolio
    intraday_portfolio: PaperPortfolio
    reconstructed_swing: PaperPortfolio
    reconstructed_intraday: PaperPortfolio
    is_deterministic: bool
    summary: Dict[str, Any] = Field(default_factory=dict)


class SimulationEngine:
    """Deterministic simulation engine driving replay bars through strategies,

    persisting all events, executing orders with next-bar and ambiguity semantics,
    and segregating SWING and INTRADAY ledgers.
    """

    def __init__(
        self,
        event_store: Optional[EventStore] = None,
        cost_config: Optional[ExecutionCostConfig] = None,
        ambiguity_policy: OHLCAmbiguityPolicy = OHLCAmbiguityPolicy.PESSIMISTIC,
        initial_cash_swing: float = 1_000_000.0,
        initial_cash_intraday: float = 1_000_000.0,
        reject_stale_bars: bool = True,
        default_risk_pct: float = 0.02,
        default_order_shares: float = 100.0,
    ):
        self.event_store = event_store or EventStore(":memory:")
        self.cost_config = cost_config or ExecutionCostConfig()
        self.ambiguity_policy = ambiguity_policy
        self.initial_cash_swing = initial_cash_swing
        self.initial_cash_intraday = initial_cash_intraday
        self.reject_stale_bars = reject_stale_bars
        self.default_risk_pct = default_risk_pct
        self.default_order_shares = default_order_shares

        self.execution_engine = ExecutionEngine(
            cost_config=self.cost_config,
            ambiguity_policy=self.ambiguity_policy,
            reject_stale=self.reject_stale_bars,
        )
        self.portfolio_manager = PortfolioManager(
            initial_cash_swing=initial_cash_swing,
            initial_cash_intraday=initial_cash_intraday,
        )

        self.bars_history: Dict[str, List[Bar]] = {}
        self.pending_orders: List[Order] = []
        self._order_signals: Dict[str, Signal] = {}  # Map order_id -> Signal
        self._strategy_states: Dict[str, Dict[str, Any]] = {}

        self.total_bars = 0
        self.total_signals = 0
        self.total_orders = 0
        self.total_fills = 0
        self.total_rejections = 0

    def run(
        self,
        data: Union[ReplayAdapter, List[Bar], Iterator[Bar]],
        strategies: Optional[List[BaseStrategy]] = None,
        symbols: Optional[List[str]] = None,
    ) -> SimulationResult:
        """Executes chronological simulation over the provided bar stream or adapter."""
        strategies = strategies or []

        # Obtain chronological bar stream
        if isinstance(data, ReplayAdapter):
            sym_list = symbols or list(data._bars.keys())
            bar_stream = data.stream_bars(sym_list)
        elif isinstance(data, list):
            bar_stream = iter(sorted(data, key=lambda b: (b.timestamp, b.symbol)))
        else:
            bar_stream = data

        for bar in bar_stream:
            self._process_bar_step(bar, strategies)

        # Flush any remaining mark-to-market calculations
        swing_port = self.portfolio_manager.get_portfolio(DecisionScope.SWING)
        intraday_port = self.portfolio_manager.get_portfolio(DecisionScope.INTRADAY)

        # Reconstruct both portfolios deterministically from immutable EventStore
        rec_swing = self.event_store.reconstruct_portfolio(
            DecisionScope.SWING, self.initial_cash_swing
        )
        rec_intraday = self.event_store.reconstruct_portfolio(
            DecisionScope.INTRADAY, self.initial_cash_intraday
        )

        # Check deterministic equality
        is_det = (
            round(swing_port.equity, 2) == round(rec_swing.equity, 2)
            and round(intraday_port.equity, 2) == round(rec_intraday.equity, 2)
            and len(swing_port.fills) == len(rec_swing.fills)
            and len(intraday_port.fills) == len(rec_intraday.fills)
        )

        return SimulationResult(
            total_bars=self.total_bars,
            total_signals=self.total_signals,
            total_orders=self.total_orders,
            total_fills=self.total_fills,
            total_rejections=self.total_rejections,
            swing_portfolio=swing_port,
            intraday_portfolio=intraday_port,
            reconstructed_swing=rec_swing,
            reconstructed_intraday=rec_intraday,
            is_deterministic=is_det,
            summary={
                "swing_equity": swing_port.equity,
                "swing_realized_pnl": swing_port.realized_pnl,
                "swing_unrealized_pnl": swing_port.unrealized_pnl,
                "intraday_equity": intraday_port.equity,
                "intraday_realized_pnl": intraday_port.realized_pnl,
                "intraday_unrealized_pnl": intraday_port.unrealized_pnl,
                "total_events": self.event_store.count(),
            },
        )

    def _process_bar_step(self, bar: Bar, strategies: List[BaseStrategy]) -> None:
        self.total_bars += 1

        # 1. Immutably persist observed bar
        self.event_store.append(
            EventEnvelope(
                event_type=EventType.BAR_OBSERVED,
                timestamp=bar.timestamp,
                aggregate_id=bar.symbol,
                payload=bar.model_dump(mode="json"),
            )
        )

        # 2. Maintain history per symbol
        if bar.symbol not in self.bars_history:
            self.bars_history[bar.symbol] = []
        self.bars_history[bar.symbol].append(bar)

        # 3. Next-Bar execution: execute pending orders against this incoming bar
        exec_result = self.execution_engine.process_bar(bar, self.pending_orders)
        self.pending_orders = exec_result.unfilled_orders

        # Apply and persist fills
        for fill in exec_result.fills:
            self.total_fills += 1
            self.portfolio_manager.apply_fill(fill)
            self.event_store.append(
                EventEnvelope(
                    event_type=EventType.ORDER_FILLED,
                    timestamp=bar.timestamp,
                    aggregate_id=f"order:{fill.order_id}",
                    payload=fill.model_dump(mode="json"),
                )
            )

            # If this was a filled BUY order that had a stop loss, place attached STOP order
            parent_sig = self._order_signals.get(fill.order_id)
            if parent_sig and fill.side == OrderSide.BUY:
                stop_price = parent_sig.invalidation.get("stop_loss")
                if stop_price is not None:
                    stop_order = Order(
                        order_id=str(uuid.uuid4()),
                        signal_id=parent_sig.signal_id,
                        strategy_id=parent_sig.strategy_id,
                        symbol=fill.symbol,
                        market=parent_sig.market,
                        bucket=fill.bucket,
                        side=OrderSide.SELL,
                        order_type=OrderType.STOP,
                        quantity=fill.quantity,
                        stop_price=float(stop_price),
                        status=OrderStatus.PENDING,
                        created_at=bar.timestamp,
                    )
                    self.total_orders += 1
                    self.portfolio_manager.add_order(stop_order)
                    self.pending_orders.append(stop_order)
                    self.event_store.append(
                        EventEnvelope(
                            event_type=EventType.ORDER_CREATED,
                            timestamp=bar.timestamp,
                            aggregate_id=f"order:{stop_order.order_id}",
                            payload=stop_order.model_dump(mode="json"),
                        )
                    )

        # Apply and persist rejections
        for order, reason in exec_result.rejections:
            self.total_rejections += 1
            self.portfolio_manager.record_rejection(order.bucket, order.order_id, reason)
            self.event_store.append(
                EventEnvelope(
                    event_type=EventType.ORDER_REJECTED,
                    timestamp=bar.timestamp,
                    aggregate_id=f"order:{order.order_id}",
                    payload={
                        "order_id": order.order_id,
                        "reason": reason,
                        "bucket": order.bucket.value,
                        "symbol": order.symbol,
                    },
                )
            )

        # 4. Update mark-to-market prices for current positions
        self.portfolio_manager.update_mark_to_market(bar)

        # 5. Check if bar is stale for signal/order generation
        bar_is_stale = (
            self.reject_stale_bars
            and (bar.is_stale or bar.quality == "stale" or bar.delay_seconds > self.cost_config.max_delay_seconds)
        )

        # 6. Evaluate active strategies on this bar
        for strat in strategies:
            strat_desc = strat.describe()
            strat_id = strat_desc.get("name", "unknown")
            context = StrategyContext(
                strategy_id=strat_id,
                config=strat.config,
                bars_history=self.bars_history,
                current_positions=self.portfolio_manager.get_positions(
                    DecisionScope.SWING  # Context provides current positions
                ),
                custom_state=self._strategy_states.setdefault(strat_id, {}),
            )

            signals = strat.on_bar(context, bar)
            for sig in signals:
                self.total_signals += 1
                self.event_store.append(
                    EventEnvelope(
                        event_type=EventType.SIGNAL_GENERATED,
                        timestamp=bar.timestamp,
                        aggregate_id=f"strategy:{sig.strategy_id}:{sig.symbol}",
                        payload=sig.model_dump(mode="json"),
                    )
                )

                # Reject new entries if bar is stale
                if bar_is_stale and sig.side == OrderSide.BUY:
                    rej_order_id = str(uuid.uuid4())
                    self.total_rejections += 1
                    self.portfolio_manager.record_rejection(
                        sig.decision_scope,
                        rej_order_id,
                        "STALE_BAR_REJECTION: Entry signal rejected due to stale bar",
                    )
                    self.event_store.append(
                        EventEnvelope(
                            event_type=EventType.ORDER_REJECTED,
                            timestamp=bar.timestamp,
                            aggregate_id=f"order:{rej_order_id}",
                            payload={
                                "order_id": rej_order_id,
                                "reason": "STALE_BAR_REJECTION: Entry signal rejected due to stale bar",
                                "bucket": sig.decision_scope.value,
                                "symbol": sig.symbol,
                            },
                        )
                    )
                    continue

                # Convert signal to order
                order = self._convert_signal_to_order(sig, bar)
                if order is not None:
                    self.total_orders += 1
                    self._order_signals[order.order_id] = sig
                    self.portfolio_manager.add_order(order)
                    self.pending_orders.append(order)
                    self.event_store.append(
                        EventEnvelope(
                            event_type=EventType.ORDER_CREATED,
                            timestamp=bar.timestamp,
                            aggregate_id=f"order:{order.order_id}",
                            payload=order.model_dump(mode="json"),
                        )
                    )

    def _convert_signal_to_order(self, sig: Signal, bar: Bar) -> Optional[Order]:
        ledger = self.portfolio_manager.get_ledger(sig.decision_scope)

        # Quantity calculation
        quantity = self.default_order_shares
        strat_shares = float(sig.evidence.get("shares", 0.0))
        if strat_shares > 0:
            quantity = strat_shares
        elif sig.max_simulated_risk > 0:
            risk_budget = ledger.equity * self.default_risk_pct
            quantity = max(1.0, math.floor(risk_budget / sig.max_simulated_risk))

        if sig.side == OrderSide.BUY:
            est_price = bar.close
            cost_estimate = (est_price * quantity) * (1.0 + self.cost_config.fee_rate_tw)
            if not ledger.can_afford(sig.symbol, est_price, quantity, cost_estimate):
                # Adjust quantity to affordable amount if possible
                max_affordable = math.floor(ledger.cash / (est_price * (1.0 + self.cost_config.fee_rate_tw)))
                if max_affordable < 1.0:
                    rej_id = str(uuid.uuid4())
                    self.total_rejections += 1
                    self.portfolio_manager.record_rejection(
                        sig.decision_scope, rej_id, "INSUFFICIENT_CASH: Cannot afford 1 share"
                    )
                    self.event_store.append(
                        EventEnvelope(
                            event_type=EventType.ORDER_REJECTED,
                            timestamp=bar.timestamp,
                            aggregate_id=f"order:{rej_id}",
                            payload={
                                "order_id": rej_id,
                                "reason": "INSUFFICIENT_CASH: Cannot afford 1 share",
                                "bucket": sig.decision_scope.value,
                                "symbol": sig.symbol,
                            },
                        )
                    )
                    return None
                quantity = float(max_affordable)

        elif sig.side == OrderSide.SELL:
            pos = ledger.positions.get(sig.symbol)
            if pos is None or pos.quantity <= 0:
                rej_id = str(uuid.uuid4())
                self.total_rejections += 1
                self.portfolio_manager.record_rejection(
                    sig.decision_scope, rej_id, "NO_POSITION_TO_SELL: Position size is 0"
                )
                self.event_store.append(
                    EventEnvelope(
                        event_type=EventType.ORDER_REJECTED,
                        timestamp=bar.timestamp,
                        aggregate_id=f"order:{rej_id}",
                        payload={
                            "order_id": rej_id,
                            "reason": "NO_POSITION_TO_SELL: Position size is 0",
                            "bucket": sig.decision_scope.value,
                            "symbol": sig.symbol,
                        },
                    )
                )
                return None
            quantity = pos.quantity  # Close full position on sell signal

        # Determine order type
        if sig.entry_model in ("NEXT_OPEN", "MARKET"):
            order_type = OrderType.MARKET
        elif sig.entry_model == "LIMIT":
            order_type = OrderType.LIMIT
        else:
            order_type = OrderType.MARKET

        limit_px = sig.evidence.get("limit_price")
        stop_px = sig.invalidation.get("stop_loss")

        return Order(
            order_id=str(uuid.uuid4()),
            signal_id=sig.signal_id,
            strategy_id=sig.strategy_id,
            symbol=sig.symbol,
            market=sig.market,
            bucket=sig.decision_scope,
            side=sig.side,
            order_type=order_type,
            quantity=quantity,
            limit_price=float(limit_px) if limit_px is not None else None,
            stop_price=float(stop_px) if stop_px is not None else None,
            status=OrderStatus.PENDING,
            created_at=bar.timestamp,
        )
