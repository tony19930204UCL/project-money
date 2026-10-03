from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Literal
import uuid

from pydantic import BaseModel, Field, model_validator, field_validator

from cio_market_lab.domain.events import EventEnvelope, EventType
from cio_market_lab.domain.models import (
    DecisionScope,
    Market,
    Order,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
)
from cio_market_lab.engine.execution import ExecutionCostConfig
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.events.store import EventStore


class PaperDataContext(BaseModel):
    source: str = "local_replay"
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    age_seconds: float = 0.0
    last_price: Optional[float] = None
    is_stale: bool = False
    is_fallback: bool = False


class PaperOrderRequest(BaseModel):
    currency: Optional[str] = Field(default=None, pattern="^(USD|TWD)$")
    symbol: str
    market: Market
    bucket: DecisionScope
    side: OrderSide
    order_type: OrderType
    quantity: float = Field(gt=0)
    limit_price: Optional[float] = Field(default=None, gt=0)
    stop_price: Optional[float] = Field(default=None, gt=0)
    origin: OrderOrigin
    reason: str = Field(min_length=1)
    audit_metadata: Dict[str, Any] = Field(default_factory=dict)
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    explicit_user_instruction: bool = False
    data: PaperDataContext = Field(default_factory=PaperDataContext)
    instrument_type: str = "EQUITY"
    underlying_symbol: Optional[str] = None
    option_right: Optional[str] = None
    strike: Optional[float] = None
    expiry: Optional[datetime] = None
    contract_multiplier: float = 1.0
    premium: Optional[float] = None
    provenance: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


class RiskLimits(BaseModel):
    # Cross-market ceiling; per-experiment caps remain the binding limit.
    # 300k accommodates TWD strategies while USD strategies are still bounded
    # by their much smaller experiment-level budgets.
    max_order_notional: float = Field(default=300_000.0, gt=0)
    max_position_notional: float = Field(default=500_000.0, gt=0)
    max_daily_loss: float = Field(default=25_000.0, gt=0)
    max_strategy_loss: float = Field(default=10_000.0, gt=0)
    max_open_positions: int = Field(default=20, ge=1)
    stale_data_threshold_seconds: float = Field(default=300.0, gt=0)


class PaperExperimentSettings(BaseModel):
    strategy_id: str
    strategy_name: str = "Unnamed US Paper Strategy"
    style: str = "balanced_growth"
    market: Market = Market.US
    initial_cash: float = Field(default=100_000.0, gt=0)
    base_currency: str = Field(default="USD", pattern="^(USD|TWD)$")
    reporting_currency: str = Field(default="TWD", pattern="^(USD|TWD)$")
    # Kept optional: native-currency accounting does not imply a conversion.
    fx_to_reporting: Optional[float] = Field(default=None, gt=0)
    fx_rates: Dict[str, float] = Field(default_factory=dict)
    enabled: bool = False
    universe: List[str] = Field(default_factory=list)
    mode: DecisionScope = DecisionScope.SWING
    cadence_seconds: float = Field(default=60.0, ge=0.05, le=86400.0)
    max_position_notional: float = Field(default=50_000.0, gt=0)
    max_daily_loss: float = Field(default=5_000.0, gt=0)
    max_open_positions: int = Field(default=5, ge=1)
    # Daily Yahoo bars are timestamped at the session date rather than at the
    # API observation time. Swing strategies therefore need a wider bound
    # than intraday strategies while still rejecting old/fallback data.
    # Manual orders retain the stricter global freshness threshold.
    max_data_age_seconds: float = Field(default=1_800.0, gt=0, le=172_800.0)
    # Both settings and the authenticated packet must opt into bar simulation.
    paper_execution_model: Literal["QUOTE_BOOK", "NEXT_BAR_OPEN"] = "QUOTE_BOOK"
    allowed_buckets: List[DecisionScope] = Field(
        default_factory=lambda: [DecisionScope.SWING, DecisionScope.INTRADAY]
    )
    bucket_capital_allocations: Dict[str, float] = Field(default_factory=dict)
    expires_at: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def migrate_unambiguous_native_currency(cls, value):
        """Migrate legacy Taiwan-only settings, never override an explicit currency."""
        if not isinstance(value, dict):
            return value
        data = dict(value)
        market = str(getattr(data.get("market"), "value", data.get("market", "US"))).upper()
        universe = data.get("universe") or []
        if universe:
            has_tw = any(str(s).upper().endswith((".TW", ".TWO")) for s in universe)
            has_non_tw = any(not str(s).upper().endswith((".TW", ".TWO")) for s in universe)
            if has_tw and has_non_tw:
                raise ValueError("UNSUPPORTED_MIXED_NATIVE_CURRENCY: mixed-market universe")
        tw_symbols = bool(universe) and all(str(s).upper().endswith((".TW", ".TWO")) for s in universe)
        if "market" not in data and (tw_symbols or str(data.get("base_currency", "")).upper() == "TWD"):
            data["market"] = "TW"
        if "base_currency" not in data and (market == "TW" or tw_symbols):
            data["base_currency"] = "TWD"
        currency = str(data.get("base_currency", "USD")).upper()
        if (market == "TW" or tw_symbols) and currency != "TWD":
            raise ValueError("UNSUPPORTED_MIXED_NATIVE_CURRENCY: Taiwan settings require TWD")
        return data


class RiskDecision(BaseModel):
    allowed: bool
    reasons: List[str] = Field(default_factory=list)
    notional: float = 0.0
    estimated_fee: float = 0.0
    estimated_tax: float = 0.0
    estimated_slippage: float = 0.0
    estimated_total_cost: float = 0.0
    data_status: str


class PaperOrderService:
    """Local-only paper order service; it has no broker or network path."""

    def __init__(
        self,
        portfolio_manager: PortfolioManager,
        event_store: EventStore,
        cost_config: Optional[ExecutionCostConfig] = None,
        now_fn=None,
    ) -> None:
        self.portfolio_manager = portfolio_manager
        self.event_store = event_store
        self.cost_config = cost_config or ExecutionCostConfig()
        self.risk_limits = RiskLimits()
        self.kill_switch = False
        self.experiments: Dict[str, PaperExperimentSettings] = {}
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        value = self._now_fn()
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def set_kill_switch(self, enabled: bool, reason: str = "") -> Dict[str, Any]:
        self.kill_switch = enabled
        self.event_store.append(EventEnvelope(
            event_type=EventType.KILL_SWITCH_TRIGGERED,
            aggregate_id="risk:kill_switch",
            payload={"enabled": enabled, "reason": reason.strip(), "paper_only": True},
        ))
        return self.kill_switch_state()

    def kill_switch_state(self) -> Dict[str, Any]:
        return {"enabled": self.kill_switch, "paper_only": True, "broker_connected": False}

    def configure_risk_limits(self, limits: RiskLimits) -> RiskLimits:
        self.risk_limits = limits
        return self.risk_limits.model_copy()

    def configure_experiment(self, settings: PaperExperimentSettings) -> PaperExperimentSettings:
        # Funding is established by explicit configuration, never a risk preview.
        self.portfolio_manager.register_strategy(
            settings.strategy_id, settings.initial_cash, settings.bucket_capital_allocations,
            currency=settings.base_currency,
        )
        self.experiments[settings.strategy_id] = settings
        self.event_store.append(EventEnvelope(
            event_type=EventType.EXPERIMENT_CONFIGURED,
            aggregate_id=f"strategy:{settings.strategy_id}",
            payload=settings.model_dump(mode="json"),
        ))
        return settings.model_copy()

    def list_experiments(self) -> List[PaperExperimentSettings]:
        return [value.model_copy() for value in self.experiments.values()]

    def experiment_for(self, strategy_id: str) -> PaperExperimentSettings:
        return self.experiments.get(
            strategy_id,
            PaperExperimentSettings(strategy_id=strategy_id),
        ).model_copy()

    def _data_status(self, req: PaperOrderRequest) -> str:
        if req.data.is_stale or req.data.is_fallback:
            return "REJECTED_STALE_OR_FALLBACK"
        age_limit = self.risk_limits.stale_data_threshold_seconds
        if req.origin in (OrderOrigin.STRATEGY, OrderOrigin.MAIN_CIO) and req.strategy_id:
            experiment = self.experiments.get(req.strategy_id)
            if experiment is not None:
                age_limit = experiment.max_data_age_seconds
        effective_age = req.data.age_seconds
        if req.data.observed_at:
            now = self._now()
            obs = req.data.observed_at if req.data.observed_at.tzinfo else req.data.observed_at.replace(tzinfo=timezone.utc)
            if obs > now:
                return "REJECTED_FUTURE_OBSERVATION"
            effective_age = max(effective_age, (now - obs).total_seconds())
        if effective_age > age_limit:
            return "REJECTED_STALE_OR_FALLBACK"
        if req.data.last_price is None and req.order_type == OrderType.MARKET:
            return "MISSING_PRICE"
        return "FRESH_NON_FALLBACK"

    def _risk_decision(self, req: PaperOrderRequest) -> RiskDecision:
        price = req.data.last_price or req.limit_price or req.stop_price
        if price is None:
            return RiskDecision(allowed=False, reasons=["MISSING_REFERENCE_PRICE"], data_status=self._data_status(req))

        is_option = (
            req.instrument_type.upper() in {"OPTION", "LONG_OPTION", "LONG_CALL", "LONG_PUT"}
            or req.option_right is not None
        )
        multiplier = 1.0
        if is_option:
            multiplier = req.contract_multiplier if req.contract_multiplier and req.contract_multiplier > 0 else (50.0 if req.market == Market.TW else 100.0)

        per_share_slippage, effective_price = self.cost_config.calculate_slippage(price, req.side)
        notional = round(req.quantity * price * multiplier, 4)
        trade_value = req.quantity * effective_price * multiplier
        fee = self.cost_config.calculate_fee(req.market, trade_value)
        tax = self.cost_config.calculate_tax(req.market, req.side, trade_value)
        slippage = round(req.quantity * per_share_slippage * multiplier, 4)
        reasons: List[str] = []
        data_status = self._data_status(req)
        if self.kill_switch:
            reasons.append("KILL_SWITCH_ENABLED")
        if data_status != "FRESH_NON_FALLBACK":
            reasons.append(data_status)

        # Forbidden capabilities rejection
        if (req.instrument_type and req.instrument_type.upper() in {"FUTURE", "FUTURES"}) or req.audit_metadata.get("instrument_type") == "FUTURES":
            reasons.append("FUTURES_NOT_PERMITTED")
        if is_option:
            # The ordinary paper-order path has only a reference price, not an
            # accepted per-contract executable quote or expiry lifecycle.
            reasons.append("OPTIONS_UNAVAILABLE_PENDING_ADAPTER_ACCEPTANCE")
        if req.audit_metadata.get("is_spread") or "SPREAD" in req.order_type.value or req.audit_metadata.get("legs"):
            reasons.append("SPREADS_NOT_PERMITTED")
        if req.audit_metadata.get("exercise") or req.audit_metadata.get("assignment"):
            reasons.append("EXERCISE_AND_ASSIGNMENT_NOT_PERMITTED")

        if is_option:
            if not req.option_right or req.option_right.upper() not in {"CALL", "PUT"}:
                reasons.append("INVALID_OPTION_RIGHT")
            if req.strike is None or req.strike <= 0:
                reasons.append("STRIKE_REQUIRED")
            if req.expiry is None:
                reasons.append("EXPIRY_REQUIRED")
            else:
                exp_dt = req.expiry if req.expiry.tzinfo else req.expiry.replace(tzinfo=timezone.utc)
                if exp_dt <= self._now():
                    reasons.append("OPTION_EXPIRED")

        if req.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and req.limit_price is None:
            reasons.append("LIMIT_PRICE_REQUIRED")
        if req.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and req.stop_price is None:
            reasons.append("STOP_PRICE_REQUIRED")
        # CIO packets assigned to a desk share its declared bounds. Authority
        # changes who chooses the trade, not the desk's risk/freshness policy.
        if req.origin == OrderOrigin.STRATEGY or (req.origin == OrderOrigin.MAIN_CIO and req.strategy_id):
            if not req.strategy_id:
                reasons.append("STRATEGY_ID_REQUIRED")
            else:
                exp = self.experiments.get(req.strategy_id)
                if exp is None or not exp.enabled:
                    reasons.append("STRATEGY_AUTONOMOUS_PAPER_DISABLED")
                elif exp.expires_at and exp.expires_at <= self._now():
                    reasons.append("STRATEGY_EXPERIMENT_EXPIRED")
                else:
                    check_sym = req.underlying_symbol or req.symbol
                    if exp.universe and check_sym not in exp.universe and req.symbol not in exp.universe:
                        reasons.append("SYMBOL_OUTSIDE_EXPERIMENT_UNIVERSE")
                    if req.bucket not in exp.allowed_buckets:
                        reasons.append("BUCKET_NOT_ALLOWED_BY_EXPERIMENT")
                    if not exp.max_position_notional >= notional:
                        reasons.append("EXPERIMENT_POSITION_CAP_EXCEEDED")
        if req.origin == OrderOrigin.MAIN_CIO and not req.explicit_user_instruction:
            reasons.append("MAIN_CIO_EXPLICIT_INSTRUCTION_REQUIRED")
        if not req.reason.strip():
            reasons.append("REASON_REQUIRED")
        if notional > self.risk_limits.max_order_notional:
            reasons.append("MAX_ORDER_NOTIONAL_EXCEEDED")

        if req.strategy_id and req.strategy_id not in self.portfolio_manager.strategy_ids():
            # A rejected preview must not silently create and fund a TWD desk.
            # Configuration is the only place that establishes native funding.
            if "STRATEGY_AUTONOMOUS_PAPER_DISABLED" not in reasons:
                reasons.append("STRATEGY_LEDGER_NOT_CONFIGURED")
            return RiskDecision(
                allowed=False, reasons=reasons, notional=notional,
                estimated_fee=fee, estimated_tax=tax, estimated_slippage=slippage,
                estimated_total_cost=round(fee + tax + slippage, 4), data_status=data_status,
            )
        ledger = self.portfolio_manager.get_ledger(
            req.bucket,
            req.strategy_id if req.strategy_id else None,
        )
        native_currency = "USD" if req.market == Market.US else "TWD"
        requested_currency = req.currency or native_currency
        if requested_currency != ledger.currency:
            reasons.append("CURRENCY_MISMATCH")
        if requested_currency != native_currency:
            reasons.append("MARKET_CURRENCY_MISMATCH")
        position = ledger.positions.get(req.symbol)
        current_qty = position.quantity if position else 0.0
        projected_qty = current_qty + req.quantity if req.side == OrderSide.BUY else current_qty - req.quantity

        if req.side == OrderSide.SELL:
            if is_option and current_qty < req.quantity:
                reasons.append("UNCOVERED_SHORT_OPTION_FORBIDDEN")
            if projected_qty < 0:
                reasons.append("INSUFFICIENT_PAPER_POSITION")

        if max(projected_qty, 0.0) * price * multiplier > self.risk_limits.max_position_notional:
            reasons.append("MAX_POSITION_NOTIONAL_EXCEEDED")
        if req.side == OrderSide.BUY and trade_value + fee + tax > ledger.cash:
            reasons.append("INSUFFICIENT_PAPER_CASH")
            reasons.append("MARGIN_BORROWING_NOT_PERMITTED")
        open_symbols = {symbol for symbol, pos in ledger.positions.items() if pos.quantity > 0}
        if req.side == OrderSide.BUY and req.symbol not in open_symbols and len(open_symbols) >= self.risk_limits.max_open_positions:
            reasons.append("MAX_OPEN_POSITIONS_EXCEEDED")

        return RiskDecision(
            allowed=not reasons,
            reasons=reasons,
            notional=notional,
            estimated_fee=fee,
            estimated_tax=tax,
            estimated_slippage=slippage,
            estimated_total_cost=round(fee + tax + slippage, 4),
            data_status=data_status,
        )

    def preview(self, req: PaperOrderRequest) -> Dict[str, Any]:
        decision = self._risk_decision(req)
        return {
            "preview_id": f"preview-{uuid.uuid4()}",
            "status": "APPROVED" if decision.allowed else "REJECTED",
            "paper_only": True,
            "broker_connected": False,
            "origin": req.origin.value,
            "reason": req.reason,
            "audit_metadata": req.audit_metadata,
            "request": req.model_dump(mode="json"),
            "risk_decision": decision.model_dump(mode="json"),
        }

    def submit(self, req: PaperOrderRequest) -> Order:
        decision = self._risk_decision(req)
        if not decision.allowed:
            raise ValueError("; ".join(decision.reasons))
        is_option = (
            req.instrument_type.upper() in {"OPTION", "LONG_OPTION", "LONG_CALL", "LONG_PUT"}
            or req.option_right is not None
        )
        multiplier = req.contract_multiplier if req.contract_multiplier and req.contract_multiplier > 0 else (50.0 if req.market == Market.TW else 100.0) if is_option else 1.0
        price = req.data.last_price or req.limit_price or req.stop_price or 0.0
        max_premium_loss = round(req.quantity * price * multiplier + decision.estimated_total_cost, 4) if is_option else None
        order = Order(
            currency=req.currency or self.portfolio_manager.get_ledger(req.bucket, req.strategy_id).currency,
            order_id=f"paper-{uuid.uuid4()}",
            created_at=self._now(),
            strategy_id=req.strategy_id,
            symbol=req.symbol,
            market=req.market,
            bucket=req.bucket,
            side=req.side,
            order_type=req.order_type,
            quantity=req.quantity,
            limit_price=req.limit_price,
            stop_price=req.stop_price,
            origin=req.origin,
            reason=req.reason,
            audit_metadata={**req.audit_metadata, "data_source": req.data.source, "data_age_seconds": req.data.age_seconds},
            strategy_version=req.strategy_version,
            instrument_type=req.instrument_type if req.instrument_type != "EQUITY" or not is_option else "OPTION",
            underlying_symbol=req.underlying_symbol,
            option_right=req.option_right.upper() if req.option_right else None,
            strike=req.strike,
            expiry=req.expiry,
            multiplier=multiplier,
            premium=req.premium or price,
            max_loss=max_premium_loss,
            provenance=req.provenance,
        )
        self.portfolio_manager.add_order(order)
        self.event_store.append(EventEnvelope(
            event_type=EventType.ORDER_CREATED,
            aggregate_id=order.order_id,
            payload=order.model_dump(mode="json"),
        ))
        return order.model_copy()

    def cancel_replace(self, order_id: str, req: PaperOrderRequest) -> Order:
        existing = self.find_order(order_id)
        if existing is None:
            raise KeyError(order_id)
        if existing.status not in {OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED}:
            raise ValueError("ONLY_PENDING_ORDERS_CAN_BE_REPLACED")
        if existing.status == OrderStatus.PARTIALLY_FILLED:
            identity = ("symbol", "market", "bucket", "side", "strategy_id")
            if any(getattr(existing, key) != getattr(req, key) for key in identity):
                raise ValueError("PARTIAL_REPLACEMENT_IDENTITY_MISMATCH")
            if req.quantity > existing.remaining_quantity + 1e-9:
                raise ValueError("PARTIAL_REPLACEMENT_EXCEEDS_REMAINDER")
        metadata = {**req.audit_metadata, "replaced_order_id": order_id,
                    "replaced_filled_quantity": existing.filled_quantity,
                    "replaced_remaining_quantity": existing.remaining_quantity}
        replacement_req = req.model_copy(update={"audit_metadata": metadata})
        # Submission checks must succeed before cancelling the executable remainder.
        replaced = self.submit(replacement_req)
        existing.status = OrderStatus.CANCELLED
        existing.rejection_reason = "CANCEL_REPLACE"
        self.event_store.append(EventEnvelope(
            event_type=EventType.ORDER_CANCELLED,
            aggregate_id=order_id,
            payload={"order_id": order_id, "reason": "CANCEL_REPLACE"},
        ))
        self.event_store.append(EventEnvelope(
            event_type=EventType.ORDER_REPLACED,
            aggregate_id=replaced.order_id,
            payload={"old_order_id": order_id, "new_order_id": replaced.order_id,
                     "already_filled_quantity": existing.filled_quantity,
                     "replacement_quantity": req.quantity},
        ))
        return replaced

    def find_order(self, order_id: str) -> Optional[Order]:
        for bucket in (DecisionScope.SWING, DecisionScope.INTRADAY):
            ledgers = [self.portfolio_manager.get_ledger(bucket)] + [
                self.portfolio_manager.get_strategy_ledger(strategy_id, bucket)
                for strategy_id in self.portfolio_manager.strategy_ids()
            ]
            for ledger in ledgers:
                for order in ledger.orders:
                    if order.order_id == order_id:
                        return order
        return None

    def cancel(self, order_id: str) -> Order:
        order = self.find_order(order_id)
        if order is None:
            raise KeyError(order_id)
        if order.status not in {OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED}:
            raise ValueError("ONLY_PENDING_ORDERS_CAN_BE_CANCELLED")
        order.status = OrderStatus.CANCELLED
        order.rejection_reason = "USER_CANCELLED"
        self.event_store.append(EventEnvelope(
            event_type=EventType.ORDER_CANCELLED,
            aggregate_id=order_id,
            payload={"order_id": order_id, "reason": "USER_CANCELLED"},
        ))
        return order.model_copy()

    def all_orders(self) -> List[Order]:
        orders = {}
        for bucket in (DecisionScope.SWING, DecisionScope.INTRADAY):
            ledgers = [self.portfolio_manager.get_ledger(bucket)] + [
                self.portfolio_manager.get_strategy_ledger(strategy_id, bucket)
                for strategy_id in self.portfolio_manager.strategy_ids()
            ]
            for ledger in ledgers:
                for order in ledger.orders:
                    orders[order.order_id] = order.model_copy()
        return list(orders.values())
