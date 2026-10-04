"""Standalone Derivatives Simulation Engine Candidate for Project Money.

Engineering-only candidate module implementing bounded simulation for options,
futures, and equity-margin financing.

Core Invariants:
1. Long premium-paid options with quote freshness, bid-ask spreads, multipliers,
   bounded premium loss, and expiry guard (pre-expiry close required; holding
   across expiry disallowed due to absent delivery/assignment handling).
2. Strict prohibition of naked option shorting (only sell-to-close existing longs).
3. Futures with multiplier, tick size, initial/maintenance margin, daily MTM variation,
   and daily settlement resetting baseline to prevent double counting.
4. Equity-margin financing cost accrual over elapsed time and deterministic
   liquidation-required events (without claiming brokerage execution).
5. No fake live fills, no fake profitability, no external broker dependencies.
6. Authoritative provenance, quote freshness, and fixture verification (production mode).
7. Normalized accounting deltas and immutable events for seamless downstream ledger integration.
8. Runtime eligibility remains UNAVAILABLE pending adapter/integration acceptance.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from enum import Enum
import math
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import uuid

from pydantic import BaseModel, Field, field_validator

from cio_market_lab.domain.events import EventEnvelope, EventType
from cio_market_lab.domain.models import (
    DecisionScope,
    InstrumentType,
    Market,
    OptionRight,
    OrderSide,
    OrderStatus,
    OrderType,
)

# Runtime eligibility constant: module alone is not activation
RUNTIME_ELIGIBILITY: str = "UNAVAILABLE_PENDING_ADAPTER_ACCEPTANCE"
RUNTIME_ELIGIBILITY_DETAILS: str = (
    "Derivatives simulation engine candidate core is verified for unit calculations. "
    "Production execution remains UNAVAILABLE until adapter and integration acceptance is completed."
)

SIMULATION_ASSUMPTION_TAG: str = "SIMULATION_ASSUMPTION: Parameter configured for simulation testing, not exchange rule"


class DerivativeInstrumentType(str, Enum):
    OPTION = "OPTION"
    FUTURE = "FUTURE"
    EQUITY_MARGIN = "EQUITY_MARGIN"


class RejectionReason(str, Enum):
    NAKED_OPTION_SHORT_FORBIDDEN = "NAKED_OPTION_SHORT_FORBIDDEN"
    MARGIN_DEFICIENCY = "MARGIN_DEFICIENCY"
    QUOTE_STALE = "QUOTE_STALE"
    QUOTE_FIXTURE_REJECTED = "QUOTE_FIXTURE_REJECTED"
    PROVENANCE_MISSING = "PROVENANCE_MISSING"
    PRICING_FAILURE_ENTRY_BLOCKED = "PRICING_FAILURE_ENTRY_BLOCKED"
    INVALID_TICK_SIZE = "INVALID_TICK_SIZE"
    CONTRACT_EXPIRED = "CONTRACT_EXPIRED"
    PRE_EXPIRY_CLOSE_WINDOW_ACTIVE = "PRE_EXPIRY_CLOSE_WINDOW_ACTIVE"
    INSUFFICIENT_POSITION_FOR_CLOSE = "INSUFFICIENT_POSITION_FOR_CLOSE"
    INVALID_CONTRACT_SPEC = "INVALID_CONTRACT_SPEC"
    CONTRACT_TARGET_MISMATCH = "CONTRACT_TARGET_MISMATCH"
    EXECUTION_SESSION_UNAUTHORIZED = "EXECUTION_SESSION_UNAUTHORIZED"
    DISPLAYED_CAPACITY_INSUFFICIENT = "DISPLAYED_CAPACITY_INSUFFICIENT"


class ContractSpec(BaseModel):
    """Explicit contract specification supplied by trusted caller."""
    symbol: str
    underlying_symbol: str
    instrument_type: DerivativeInstrumentType
    option_right: Optional[OptionRight] = None
    strike: Optional[float] = None
    expiry: Optional[datetime] = None
    multiplier: float = Field(default=1.0, gt=0)
    tick_size: float = Field(default=0.01, gt=0)
    
    # Margin requirements (explicit simulation assumptions)
    initial_margin_per_contract: float = Field(default=0.0, ge=0)
    maintenance_margin_per_contract: float = Field(default=0.0, ge=0)
    initial_margin_rate: float = Field(default=0.0, ge=0)
    maintenance_margin_rate: float = Field(default=0.0, ge=0)
    margin_rules_label: str = SIMULATION_ASSUMPTION_TAG

    # Expiry guard: seconds before expiry where new entries are blocked and close is required
    pre_expiry_close_lead_seconds: float = Field(default=3600.0, ge=0)
    currency: str = "TWD"

    @field_validator("tick_size", "multiplier")
    @classmethod
    def validate_positive_nonzero(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Multiplier and tick_size must be strictly positive")
        return v


class DerivativeQuote(BaseModel):
    """Authoritative quote for derivatives evaluation."""
    symbol: str
    timestamp: datetime
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    bid: Optional[float] = None
    ask: Optional[float] = None
    last_price: Optional[float] = None
    is_stale: bool = False
    is_fixture: bool = False
    source: str = "authoritative_caller"
    provenance: Dict[str, Any] = Field(default_factory=dict)


class FinancingConfig(BaseModel):
    """Financing rate assumptions for margin borrowing."""
    annual_financing_rate: float = Field(default=0.065, ge=0.0)  # 6.5% annual rate
    day_count_convention: int = Field(default=365, ge=360, le=366)
    assumption_label: str = "SIMULATION_ASSUMPTION: Margin loan interest rate simulation assumption"


class CostConfig(BaseModel):
    """Transaction fees and slippage assumptions."""
    fee_per_contract: float = Field(default=0.0, ge=0.0)
    fee_rate: float = Field(default=0.0005, ge=0.0)
    tax_rate: float = Field(default=0.001, ge=0.0)  # e.g. 0.1% futures transaction tax
    min_fee: float = Field(default=0.0, ge=0.0)
    slippage_ticks: float = Field(default=0.0, ge=0.0)
    assumption_label: str = "SIMULATION_ASSUMPTION: Commission, tax, and execution slippage model"

    def calculate_costs(
        self, price: float, quantity: float, multiplier: float, side: OrderSide
    ) -> Tuple[float, float, float]:
        notional = price * quantity * multiplier
        fee = max(self.min_fee, self.fee_per_contract * quantity + notional * self.fee_rate)
        tax = notional * self.tax_rate if side == OrderSide.SELL else 0.0
        slippage_amt = (self.slippage_ticks * multiplier * quantity) if self.slippage_ticks > 0 else 0.0
        return round(fee, 4), round(tax, 4), round(slippage_amt, 4)


class AccountingDelta(BaseModel):
    """Normalized ledger delta for downstream accounting integration."""
    delta_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    idempotency_key: str
    aggregate_id: str
    event_type: str
    cash_delta: float = 0.0
    margin_locked_delta: float = 0.0
    realized_pnl_delta: float = 0.0
    fee_delta: float = 0.0
    tax_delta: float = 0.0
    financing_cost_delta: float = 0.0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Dict[str, Any] = Field(default_factory=dict)


class NormalizedAccountingEvent(BaseModel):
    """Immutable accounting event adhering to the system event protocol."""
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str
    aggregate_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    delta: AccountingDelta
    payload: Dict[str, Any]
    idempotency_key: str

    def to_event_envelope(self) -> EventEnvelope:
        return EventEnvelope(
            event_id=self.event_id,
            event_type=EventType.POSITION_UPDATED,
            timestamp=self.timestamp,
            aggregate_id=self.aggregate_id,
            payload={
                "normalized_event_type": self.event_type,
                "delta": self.delta.model_dump(mode="json"),
                "idempotency_key": self.idempotency_key,
                **self.payload,
            },
        )


class DerivativePosition(BaseModel):
    """In-memory tracking structure for an active derivative position."""
    position_id: str
    symbol: str
    underlying_symbol: str
    instrument_type: DerivativeInstrumentType
    bucket: DecisionScope = DecisionScope.SWING
    quantity: float = 0.0  # Positive for long, negative for short (futures only)
    multiplier: float = 1.0
    average_entry_price: float = 0.0
    last_settlement_price: float = 0.0  # Daily MTM reset benchmark to avoid double counting
    current_price: Optional[float] = None
    market_value: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    accumulated_settled_pnl: float = 0.0
    margin_locked: float = 0.0
    total_premium_paid: float = 0.0  # For options: bounds maximum loss
    option_right: Optional[OptionRight] = None
    strike: Optional[float] = None
    expiry: Optional[datetime] = None
    is_closed: bool = False
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ExecutionAttemptResult(BaseModel):
    success: bool
    rejection_reason: Optional[str] = None
    fill_price: Optional[float] = None
    executed_quantity: float = 0.0
    fee: float = 0.0
    tax: float = 0.0
    slippage: float = 0.0
    cash_flow: float = 0.0
    max_loss_bound: Optional[float] = None
    delta: Optional[AccountingDelta] = None
    event: Optional[NormalizedAccountingEvent] = None
    position: Optional[DerivativePosition] = None


class SettlementResult(BaseModel):
    position_id: str
    symbol: str
    settlement_date: str
    settlement_price: float
    prior_settlement_price: float
    settled_quantity: float
    variation_pnl: float
    accumulated_settled_pnl: float
    delta: AccountingDelta
    event: NormalizedAccountingEvent


class FinancingAccrualResult(BaseModel):
    position_id: str
    elapsed_seconds: float
    borrowed_principal: float
    annual_rate: float
    financing_cost: float
    delta: AccountingDelta
    event: NormalizedAccountingEvent


class RiskReviewResult(BaseModel):
    symbol: str
    healthy: bool
    margin_deficient: bool = False
    liquidation_required: bool = False
    pre_expiry_close_required: bool = False
    pricing_available: bool = True
    messages: List[str] = Field(default_factory=list)
    events: List[NormalizedAccountingEvent] = Field(default_factory=list)


class PaperDerivativesEngine:
    """Standalone derivatives simulation engine candidate.

    Never persists to disk or instantiates separate database ledgers;
    yields pure normalized accounting deltas and events for host integration.
    """

    def __init__(
        self,
        cost_config: Optional[CostConfig] = None,
        financing_config: Optional[FinancingConfig] = None,
        max_staleness_seconds: float = 300.0,
        production_mode: bool = False,
    ) -> None:
        self.cost_config = cost_config or CostConfig()
        self.financing_config = financing_config or FinancingConfig()
        self.max_staleness_seconds = max_staleness_seconds
        self.production_mode = production_mode
        self._processed_idempotency_keys: Set[str] = set()

    @property
    def runtime_eligibility(self) -> str:
        return RUNTIME_ELIGIBILITY

    def validate_quote(
        self, quote: DerivativeQuote, as_of: Optional[datetime] = None
    ) -> Tuple[bool, Optional[str]]:
        now = as_of or datetime.now(timezone.utc)
        if quote.timestamp.tzinfo is None:
            quote_ts = quote.timestamp.replace(tzinfo=timezone.utc)
        else:
            quote_ts = quote.timestamp

        # Check production provenance and fixture guards
        if self.production_mode:
            if quote.is_fixture:
                return False, RejectionReason.QUOTE_FIXTURE_REJECTED.value
            if not quote.provenance or "authority" not in quote.provenance:
                return False, RejectionReason.PROVENANCE_MISSING.value

        # Check staleness
        if quote.is_stale:
            return False, RejectionReason.QUOTE_STALE.value
        age = (now - quote_ts).total_seconds()
        if age < -5 or age > self.max_staleness_seconds:
            return False, RejectionReason.QUOTE_STALE.value

        # Validate pricing presence
        if quote.bid is None and quote.ask is None and quote.last_price is None:
            return False, RejectionReason.PRICING_FAILURE_ENTRY_BLOCKED.value

        if quote.bid is not None and quote.ask is not None:
            if not all(math.isfinite(p) for p in (quote.bid, quote.ask)) or quote.bid < 0 or quote.ask < 0 or quote.bid > quote.ask:
                return False, RejectionReason.PRICING_FAILURE_ENTRY_BLOCKED.value

        return True, None

    def option_chain_snapshot(
        self,
        spec: ContractSpec,
        quote: Optional[DerivativeQuote],
        *,
        underlying_price: Optional[float],
        implied_volatility: Optional[float],
        risk_free_rate: float = 0.0,
        as_of: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Validate one exact option-chain target and calculate bounded Black-Scholes Greeks.

        This is a valuation/constraint helper on the existing paper derivatives engine.
        It never creates an executable quote, order, broker authority, or live readiness.
        """
        now = as_of or datetime.now(timezone.utc)
        unavailable = lambda reason: {
            "status": "UNAVAILABLE",
            "reason": reason,
            "symbol": spec.symbol,
            "expiry": spec.expiry.isoformat() if spec.expiry else None,
            "paper_only": True,
            "execution_enabled": False,
            "live_approved": False,
        }
        if spec.instrument_type != DerivativeInstrumentType.OPTION:
            return unavailable("OPTION_CONTRACT_REQUIRED")
        if spec.expiry is None or spec.expiry.tzinfo is None or spec.strike is None or spec.option_right is None:
            return unavailable("EXPIRY_AWARE_OPTION_SPEC_REQUIRED")
        if now >= spec.expiry:
            return unavailable("CONTRACT_EXPIRED")
        if quote is None:
            return unavailable("OPTION_CHAIN_MISSING")
        valid, reason = self.validate_quote(quote, as_of=now)
        if not valid:
            return unavailable(reason or "OPTION_CHAIN_UNAVAILABLE")
        if quote.symbol != spec.symbol:
            return unavailable("CONTRACT_TARGET_MISMATCH")
        meta = quote.provenance or {}
        if meta.get("strike") is not None and float(meta["strike"]) != float(spec.strike):
            return unavailable("CONTRACT_TARGET_MISMATCH")
        if meta.get("contract_right") is not None and str(meta["contract_right"]).upper() not in {
            str(spec.option_right.value).upper(), str(spec.option_right).split(".")[-1].upper()
        }:
            return unavailable("CONTRACT_TARGET_MISMATCH")
        if meta.get("expiry") is not None:
            try:
                observed_expiry = datetime.fromisoformat(str(meta["expiry"]).replace("Z", "+00:00"))
            except ValueError:
                return unavailable("CONTRACT_TARGET_MISMATCH")
            if observed_expiry != spec.expiry:
                return unavailable("CONTRACT_TARGET_MISMATCH")
        if underlying_price is None or not math.isfinite(underlying_price) or underlying_price <= 0:
            return unavailable("UNDERLYING_MARK_MISSING")
        if implied_volatility is None or not math.isfinite(implied_volatility) or implied_volatility <= 0:
            return unavailable("IMPLIED_VOLATILITY_MISSING")
        if not math.isfinite(risk_free_rate):
            return unavailable("RISK_FREE_RATE_INVALID")
        seconds = (spec.expiry - now).total_seconds()
        years = seconds / (365.0 * 24.0 * 3600.0)
        if years <= 0:
            return unavailable("CONTRACT_EXPIRED")
        sigma = float(implied_volatility)
        spot = float(underlying_price)
        strike = float(spec.strike)
        sqrt_t = math.sqrt(years)
        d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * sigma * sigma) * years) / (sigma * sqrt_t)
        d2 = d1 - sigma * sqrt_t
        cdf = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
        pdf = lambda x: math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)
        is_call = str(spec.option_right.value).upper() == "CALL"
        if is_call:
            delta = cdf(d1)
            theta = (-(spot * pdf(d1) * sigma) / (2 * sqrt_t)
                     - risk_free_rate * strike * math.exp(-risk_free_rate * years) * cdf(d2)) / 365.0
        else:
            delta = cdf(d1) - 1.0
            theta = (-(spot * pdf(d1) * sigma) / (2 * sqrt_t)
                     + risk_free_rate * strike * math.exp(-risk_free_rate * years) * cdf(-d2)) / 365.0
        gamma = pdf(d1) / (spot * sigma * sqrt_t)
        vega = spot * pdf(d1) * sqrt_t / 100.0
        return {
            "status": "AVAILABLE_TEST_ONLY" if quote.is_fixture else "AVAILABLE_SOURCE_VALUATION_ONLY",
            "symbol": spec.symbol,
            "underlying_symbol": spec.underlying_symbol,
            "expiry": spec.expiry.isoformat(),
            "seconds_to_expiry": seconds,
            "strike": strike,
            "option_right": spec.option_right.value,
            "multiplier": spec.multiplier,
            "tick_size": spec.tick_size,
            "currency": spec.currency,
            "quote_timestamp": quote.timestamp.isoformat(),
            "source": quote.source,
            "implied_volatility": sigma,
            "underlying_price": spot,
            "greeks": {"delta": delta, "gamma": gamma, "vega": vega, "theta_per_day": theta},
            "paper_only": True,
            "execution_enabled": False,
            "live_approved": False,
        }

    def validate_tick_size(self, price: float, tick_size: float) -> bool:
        if tick_size <= 0:
            return False
        # Account for IEEE 754 precision issues
        ratio = price / tick_size
        nearest = round(ratio)
        return abs(ratio - nearest) < 1e-5

    def attempt_execution(
        self,
        order_id: str,
        spec: ContractSpec,
        quote: DerivativeQuote,
        side: OrderSide,
        quantity: float,
        available_cash: float,
        existing_position: Optional[DerivativePosition] = None,
        as_of: Optional[datetime] = None,
        allow_deficit_close: bool = False,
    ) -> ExecutionAttemptResult:
        """Alias for execute_order with (spec, quote, side) parameter ordering."""
        return self.execute_order(
            order_id=order_id,
            side=side,
            quantity=quantity,
            spec=spec,
            quote=quote,
            available_cash=available_cash,
            existing_position=existing_position,
            as_of=as_of,
            allow_deficit_close=allow_deficit_close,
        )

    def execute_order(
        self,
        order_id: str,
        side: OrderSide,
        quantity: float,
        spec: ContractSpec,
        quote: DerivativeQuote,
        available_cash: float,
        existing_position: Optional[DerivativePosition] = None,
        as_of: Optional[datetime] = None,
        allow_deficit_close: bool = False,
    ) -> ExecutionAttemptResult:
        """Executes a simulated derivative order under strict risk constraints."""
        now = as_of or datetime.now(timezone.utc)
        idempotency_key = f"exec:{order_id}:{spec.symbol}:{side.value}:{quantity}"
        if idempotency_key in self._processed_idempotency_keys:
            return ExecutionAttemptResult(
                success=False,
                rejection_reason="DUPLICATE_IDEMPOTENT_REQUEST",
            )

        if quantity <= 0:
            return ExecutionAttemptResult(
                success=False, rejection_reason="QUANTITY_MUST_BE_POSITIVE"
            )

        if quote.symbol != spec.symbol:
            return ExecutionAttemptResult(success=False, rejection_reason=RejectionReason.INVALID_CONTRACT_SPEC.value)

        # 1. Quote validation
        valid_quote, reason = self.validate_quote(quote, as_of=now)
        if not valid_quote:
            return ExecutionAttemptResult(success=False, rejection_reason=reason)

        # 2. Expiry verification
        if spec.expiry is not None:
            exp = spec.expiry if spec.expiry.tzinfo else spec.expiry.replace(tzinfo=timezone.utc)
            if now >= exp:
                return ExecutionAttemptResult(
                    success=False, rejection_reason=RejectionReason.CONTRACT_EXPIRED.value
                )
            # If entering new long option inside pre-expiry close lead window, block entry
            is_closing = existing_position is not None and (
                (existing_position.quantity > 0 and side == OrderSide.SELL)
                or (existing_position.quantity < 0 and side == OrderSide.BUY)
            )
            time_to_expiry = (exp - now).total_seconds()
            if not is_closing and time_to_expiry <= spec.pre_expiry_close_lead_seconds:
                return ExecutionAttemptResult(
                    success=False,
                    rejection_reason=RejectionReason.PRE_EXPIRY_CLOSE_WINDOW_ACTIVE.value,
                )

        # 3. Execution pricing via bid/ask spread
        if side == OrderSide.BUY:
            raw_price = quote.ask
        else:
            raw_price = quote.bid

        if raw_price is None or not math.isfinite(raw_price) or raw_price <= 0:
            return ExecutionAttemptResult(
                success=False,
                rejection_reason=RejectionReason.PRICING_FAILURE_ENTRY_BLOCKED.value,
            )

        # Explicit contract-book authority constraints, when supplied by the source.
        # Missing metadata preserves legacy fixture compatibility; explicit refusal never upgrades.
        meta = quote.provenance or {}
        if meta.get("contract_authorized") is False:
            return ExecutionAttemptResult(success=False, rejection_reason=RejectionReason.CONTRACT_TARGET_MISMATCH.value)
        if meta.get("session_open") is False:
            return ExecutionAttemptResult(success=False, rejection_reason=RejectionReason.EXECUTION_SESSION_UNAUTHORIZED.value)
        displayed = meta.get("ask_size") if side == OrderSide.BUY else meta.get("bid_size")
        if displayed is not None:
            try:
                displayed_qty = float(displayed)
            except (TypeError, ValueError):
                displayed_qty = 0.0
            if not math.isfinite(displayed_qty) or displayed_qty < quantity:
                return ExecutionAttemptResult(success=False, rejection_reason=RejectionReason.DISPLAYED_CAPACITY_INSUFFICIENT.value)
        if meta.get("expiry") is not None and spec.expiry is not None:
            try:
                quoted_expiry = datetime.fromisoformat(str(meta["expiry"]).replace("Z", "+00:00"))
            except ValueError:
                return ExecutionAttemptResult(success=False, rejection_reason=RejectionReason.CONTRACT_TARGET_MISMATCH.value)
            if quoted_expiry != spec.expiry:
                return ExecutionAttemptResult(success=False, rejection_reason=RejectionReason.CONTRACT_TARGET_MISMATCH.value)
        if meta.get("strike") is not None and spec.strike is not None and float(meta["strike"]) != float(spec.strike):
            return ExecutionAttemptResult(success=False, rejection_reason=RejectionReason.CONTRACT_TARGET_MISMATCH.value)
        if meta.get("contract_right") is not None and spec.option_right is not None:
            right = str(meta["contract_right"]).upper()
            if right not in {str(spec.option_right.value).upper(), str(spec.option_right).split(".")[-1].upper()}:
                return ExecutionAttemptResult(success=False, rejection_reason=RejectionReason.CONTRACT_TARGET_MISMATCH.value)

        # 4. Tick size check
        if not self.validate_tick_size(raw_price, spec.tick_size):
            return ExecutionAttemptResult(
                success=False, rejection_reason=RejectionReason.INVALID_TICK_SIZE.value
            )

        fee, tax, slippage = self.cost_config.calculate_costs(
            raw_price, quantity, spec.multiplier, side
        )
        effective_price = raw_price + (slippage / (quantity * spec.multiplier) if side == OrderSide.BUY else -slippage / (quantity * spec.multiplier))

        # 5. Instrument-specific validation
        if spec.instrument_type == DerivativeInstrumentType.OPTION:
            # Long premium-paid options ONLY: No naked short options
            curr_qty = existing_position.quantity if existing_position else 0.0
            if side == OrderSide.SELL:
                if curr_qty <= 0 or curr_qty < quantity:
                    return ExecutionAttemptResult(
                        success=False,
                        rejection_reason=RejectionReason.NAKED_OPTION_SHORT_FORBIDDEN.value,
                    )
                # Selling to close existing long
                gross_proceeds = round(quantity * effective_price * spec.multiplier, 4)
                net_cash_flow = round(gross_proceeds - fee - tax, 4)
                
                # Realized PnL calculation
                cost_basis_portion = round(quantity * existing_position.average_entry_price * spec.multiplier, 4)
                realized_pnl = round(net_cash_flow - cost_basis_portion, 4)
                
                new_qty = round(curr_qty - quantity, 6)
                updated_pos = existing_position.model_copy(
                    update={
                        "quantity": new_qty,
                        "is_closed": (new_qty == 0),
                        "last_updated_at": now,
                    }
                )
                if new_qty == 0:
                    updated_pos.market_value = 0.0
                    updated_pos.unrealized_pnl = 0.0

                delta = AccountingDelta(
                    idempotency_key=idempotency_key,
                    aggregate_id=spec.symbol,
                    event_type="DERIVATIVE_OPTION_CLOSED",
                    cash_delta=net_cash_flow,
                    realized_pnl_delta=realized_pnl,
                    fee_delta=fee,
                    tax_delta=tax,
                    timestamp=now,
                    metadata={"side": side.value, "quantity": quantity, "price": effective_price},
                )
                event = NormalizedAccountingEvent(
                    event_type="DERIVATIVE_OPTION_CLOSED",
                    aggregate_id=spec.symbol,
                    timestamp=now,
                    delta=delta,
                    payload={"order_id": order_id, "symbol": spec.symbol, "position": updated_pos.model_dump(mode="json")},
                    idempotency_key=idempotency_key,
                )
                self._processed_idempotency_keys.add(idempotency_key)
                return ExecutionAttemptResult(
                    success=True,
                    fill_price=effective_price,
                    executed_quantity=quantity,
                    fee=fee,
                    tax=tax,
                    slippage=slippage,
                    cash_flow=net_cash_flow,
                    delta=delta,
                    event=event,
                    position=updated_pos,
                )

            elif side == OrderSide.BUY:
                # Buying long premium-paid option
                total_cost = round(quantity * effective_price * spec.multiplier + fee + tax, 4)
                if available_cash < total_cost:
                    return ExecutionAttemptResult(
                        success=False, rejection_reason=RejectionReason.MARGIN_DEFICIENCY.value
                    )
                # Bounded premium loss: max loss cannot exceed premium paid + fees
                max_loss_bound = total_cost

                prev_qty = existing_position.quantity if existing_position else 0.0
                prev_cost = (prev_qty * existing_position.average_entry_price * spec.multiplier) if existing_position else 0.0
                new_qty = round(prev_qty + quantity, 6)
                new_avg_px = (prev_cost + (quantity * effective_price * spec.multiplier)) / (new_qty * spec.multiplier)

                pos_id = existing_position.position_id if existing_position else f"pos-{uuid.uuid4().hex[:8]}"
                premium_paid_amount = round(quantity * effective_price * spec.multiplier, 4)
                updated_pos = DerivativePosition(
                    position_id=pos_id,
                    symbol=spec.symbol,
                    underlying_symbol=spec.underlying_symbol,
                    instrument_type=DerivativeInstrumentType.OPTION,
                    quantity=new_qty,
                    multiplier=spec.multiplier,
                    average_entry_price=round(new_avg_px, 6),
                    last_settlement_price=round(new_avg_px, 6),
                    current_price=effective_price,
                    market_value=round(new_qty * effective_price * spec.multiplier, 4),
                    unrealized_pnl=0.0,
                    total_premium_paid=round((existing_position.total_premium_paid if existing_position else 0.0) + premium_paid_amount, 4),
                    option_right=spec.option_right,
                    strike=spec.strike,
                    expiry=spec.expiry,
                    opened_at=existing_position.opened_at if existing_position else now,
                    last_updated_at=now,
                )

                delta = AccountingDelta(
                    idempotency_key=idempotency_key,
                    aggregate_id=spec.symbol,
                    event_type="DERIVATIVE_OPTION_OPENED",
                    cash_delta=-total_cost,
                    fee_delta=fee,
                    tax_delta=tax,
                    timestamp=now,
                    metadata={"side": side.value, "quantity": quantity, "price": effective_price, "max_loss_bound": max_loss_bound},
                )
                event = NormalizedAccountingEvent(
                    event_type="DERIVATIVE_OPTION_OPENED",
                    aggregate_id=spec.symbol,
                    timestamp=now,
                    delta=delta,
                    payload={"order_id": order_id, "symbol": spec.symbol, "position": updated_pos.model_dump(mode="json")},
                    idempotency_key=idempotency_key,
                )
                self._processed_idempotency_keys.add(idempotency_key)
                return ExecutionAttemptResult(
                    success=True,
                    fill_price=effective_price,
                    executed_quantity=quantity,
                    fee=fee,
                    tax=tax,
                    slippage=slippage,
                    cash_flow=-total_cost,
                    max_loss_bound=max_loss_bound,
                    delta=delta,
                    event=event,
                    position=updated_pos,
                )

        elif spec.instrument_type == DerivativeInstrumentType.FUTURE:
            curr_qty = existing_position.quantity if existing_position else 0.0
            signed_qty_change = quantity if side == OrderSide.BUY else -quantity
            # Reversal is not an atomic close; prevent hidden new exposure.
            if curr_qty and curr_qty * signed_qty_change < 0 and quantity > abs(curr_qty):
                return ExecutionAttemptResult(success=False, rejection_reason="FUTURES_REVERSAL_NOT_SUPPORTED")
            # Futures: require initial margin per contract (or rate)
            required_margin = (
                spec.initial_margin_per_contract * max(0.0, abs(curr_qty + signed_qty_change) - abs(curr_qty))
                if spec.initial_margin_per_contract > 0
                else (effective_price * max(0.0, abs(curr_qty + signed_qty_change) - abs(curr_qty)) * spec.multiplier * spec.initial_margin_rate)
            )
            total_cash_needed = required_margin + fee + tax
            # An explicitly requested PAPER reduction must reflect insolvency,
            # not leave an existing liability open because cash is negative.
            # This bypass is never available for entries, adds, or reversals.
            strict_reduction = (curr_qty != 0 and curr_qty * signed_qty_change < 0
                                and quantity <= abs(curr_qty))
            deficit_close = allow_deficit_close and strict_reduction
            if available_cash < total_cash_needed and not deficit_close:
                return ExecutionAttemptResult(
                    success=False, rejection_reason=RejectionReason.MARGIN_DEFICIENCY.value
                )

            pos_id = existing_position.position_id if existing_position else f"pos-{uuid.uuid4().hex[:8]}"

            # Determine whether this trade increases, reverses, or closes position
            new_qty = round(curr_qty + signed_qty_change, 6)

            realized_pnl = 0.0
            closing_qty = 0.0
            if (curr_qty > 0 and signed_qty_change < 0) or (curr_qty < 0 and signed_qty_change > 0):
                # Partial or complete close
                closing_qty = min(abs(curr_qty), abs(signed_qty_change))
                prior_ref_px = existing_position.last_settlement_price
                if curr_qty > 0:
                    realized_pnl = round((effective_price - prior_ref_px) * closing_qty * spec.multiplier, 4)
                else:
                    realized_pnl = round((prior_ref_px - effective_price) * closing_qty * spec.multiplier, 4)

            # Locked margin calculation
            new_margin_locked = (
                spec.initial_margin_per_contract * abs(new_qty)
                if spec.initial_margin_per_contract > 0
                else (effective_price * abs(new_qty) * spec.multiplier * spec.initial_margin_rate)
            )
            prior_margin_locked = existing_position.margin_locked if existing_position else 0.0
            margin_locked_delta = round(new_margin_locked - prior_margin_locked, 4)

            # The settlement benchmark of surviving contracts must not be reset
            # by a partial close or an add; otherwise unsettled variation vanishes.
            new_avg_entry = effective_price
            new_settlement_ref = effective_price
            if (
                existing_position is not None
                and curr_qty != 0
                and new_qty != 0
                and math.copysign(1, new_qty) == math.copysign(1, curr_qty)
            ):
                if abs(new_qty) > abs(curr_qty):
                    new_avg_entry = (
                        existing_position.average_entry_price * abs(curr_qty) + effective_price * quantity
                    ) / abs(new_qty)
                    new_settlement_ref = (
                        existing_position.last_settlement_price * abs(curr_qty) + effective_price * quantity
                    ) / abs(new_qty)
                else:
                    new_avg_entry = existing_position.average_entry_price
                    new_settlement_ref = existing_position.last_settlement_price

            updated_pos = DerivativePosition(
                position_id=pos_id,
                symbol=spec.symbol,
                underlying_symbol=spec.underlying_symbol,
                instrument_type=DerivativeInstrumentType.FUTURE,
                quantity=new_qty,
                multiplier=spec.multiplier,
                average_entry_price=round(new_avg_entry, 6),
                last_settlement_price=round(new_settlement_ref, 6),
                current_price=effective_price,
                market_value=round((effective_price - new_settlement_ref) * new_qty * spec.multiplier, 4),
                unrealized_pnl=round((effective_price - new_settlement_ref) * new_qty * spec.multiplier, 4),
                margin_locked=round(new_margin_locked, 4),
                accumulated_settled_pnl=(existing_position.accumulated_settled_pnl if existing_position else 0.0) + realized_pnl,
                is_closed=(new_qty == 0),
                opened_at=existing_position.opened_at if existing_position else now,
                last_updated_at=now,
            )

            # Cash flow consists of realized pnl from closing portion minus fees/taxes
            net_cash_flow = round(realized_pnl - fee - tax, 4)

            delta = AccountingDelta(
                idempotency_key=idempotency_key,
                aggregate_id=spec.symbol,
                event_type="DERIVATIVE_FUTURE_EXECUTED",
                cash_delta=net_cash_flow,
                margin_locked_delta=margin_locked_delta,
                realized_pnl_delta=realized_pnl,
                fee_delta=fee,
                tax_delta=tax,
                timestamp=now,
                metadata={
                    "side": side.value,
                    "quantity": quantity,
                    "price": effective_price,
                    "margin_assumption": spec.margin_rules_label,
                },
            )
            event = NormalizedAccountingEvent(
                event_type="DERIVATIVE_FUTURE_EXECUTED",
                aggregate_id=spec.symbol,
                timestamp=now,
                delta=delta,
                payload={"order_id": order_id, "symbol": spec.symbol, "position": updated_pos.model_dump(mode="json")},
                idempotency_key=idempotency_key,
            )
            self._processed_idempotency_keys.add(idempotency_key)
            return ExecutionAttemptResult(
                success=True,
                fill_price=effective_price,
                executed_quantity=quantity,
                fee=fee,
                tax=tax,
                slippage=slippage,
                cash_flow=net_cash_flow,
                delta=delta,
                event=event,
                position=updated_pos,
            )

        return ExecutionAttemptResult(
            success=False, rejection_reason="UNSUPPORTED_DERIVATIVE_TYPE"
        )

    def mark_to_market(
        self,
        position: DerivativePosition,
        spec: ContractSpec,
        quote: DerivativeQuote,
        as_of: Optional[datetime] = None,
    ) -> DerivativePosition:
        """Computes current mark-to-market and unrealized PnL without altering settled cash."""
        now = as_of or datetime.now(timezone.utc)
        if position.quantity == 0 or position.is_closed:
            pos = position.model_copy()
            pos.market_value = 0.0
            pos.unrealized_pnl = 0.0
            return pos

        is_valid, _ = self.validate_quote(quote, as_of=now)
        mark_price = quote.bid if position.quantity > 0 else quote.ask
        if mark_price is None:
            mark_price = quote.last_price

        if not is_valid or mark_price is None:
            # Missing mark: retain previous values with stale warning
            pos = position.model_copy(update={"last_updated_at": now})
            return pos

        updated = position.model_copy()
        updated.current_price = mark_price
        updated.last_updated_at = now

        if position.instrument_type == DerivativeInstrumentType.OPTION:
            # Long option: market value = quantity * mark_price * multiplier
            # Bounded loss: unrealized PnL cannot be worse than -total_premium_paid
            mv = round(position.quantity * mark_price * spec.multiplier, 4)
            cost_basis = round(position.quantity * position.average_entry_price * spec.multiplier, 4)
            unrealized = round(mv - cost_basis, 4)
            # Bound loss
            if position.total_premium_paid > 0:
                unrealized = max(-position.total_premium_paid, unrealized)
            updated.market_value = mv
            updated.unrealized_pnl = unrealized

        elif position.instrument_type == DerivativeInstrumentType.FUTURE:
            # Future MTM: measured against last_settlement_price (avoiding double counting)
            ref_px = position.last_settlement_price
            if position.quantity > 0:
                unrealized = round((mark_price - ref_px) * position.quantity * spec.multiplier, 4)
            else:
                unrealized = round((ref_px - mark_price) * abs(position.quantity) * spec.multiplier, 4)
            updated.market_value = 0.0  # Futures have no asset market value, only variation margin
            updated.unrealized_pnl = unrealized

        return updated

    def settle_daily_variation(
        self,
        position: DerivativePosition,
        spec: ContractSpec,
        settlement_price: float,
        settlement_date: str,
        as_of: Optional[datetime] = None,
    ) -> SettlementResult:
        """Daily marked-to-market variation settlement for futures.

        CRITICAL INVARIANT: The position's `last_settlement_price` is updated to
        `settlement_price`. This resets the variation baseline and completely prevents
        double-counting upon subsequent settlement or position close.
        """
        now = as_of or datetime.now(timezone.utc)
        idempotency_key = f"settle:{position.position_id}:{settlement_date}:{settlement_price}"

        ref_px = position.last_settlement_price
        qty = position.quantity
        mult = spec.multiplier

        if qty > 0:
            variation_pnl = round((settlement_price - ref_px) * qty * mult, 4)
        elif qty < 0:
            variation_pnl = round((ref_px - settlement_price) * abs(qty) * mult, 4)
        else:
            variation_pnl = 0.0

        # Create accounting delta for cash credit/debit
        delta = AccountingDelta(
            idempotency_key=idempotency_key,
            aggregate_id=position.symbol,
            event_type="MTM_DAILY_VARIATION_SETTLED",
            cash_delta=variation_pnl,
            realized_pnl_delta=variation_pnl,
            timestamp=now,
            metadata={
                "settlement_date": settlement_date,
                "settlement_price": settlement_price,
                "prior_settlement_price": ref_px,
                "variation_pnl": variation_pnl,
            },
        )
        event = NormalizedAccountingEvent(
            event_type="MTM_DAILY_VARIATION_SETTLED",
            aggregate_id=position.symbol,
            timestamp=now,
            delta=delta,
            payload={
                "position_id": position.position_id,
                "symbol": position.symbol,
                "settlement_price": settlement_price,
                "variation_pnl": variation_pnl,
            },
            idempotency_key=idempotency_key,
        )

        new_accumulated = round(position.accumulated_settled_pnl + variation_pnl, 4)
        position.last_settlement_price = settlement_price
        position.accumulated_settled_pnl = new_accumulated
        position.unrealized_pnl = 0.0
        position.last_updated_at = now

        return SettlementResult(
            position_id=position.position_id,
            symbol=position.symbol,
            settlement_date=settlement_date,
            settlement_price=settlement_price,
            prior_settlement_price=ref_px,
            settled_quantity=qty,
            variation_pnl=variation_pnl,
            accumulated_settled_pnl=new_accumulated,
            delta=delta,
            event=event,
        )

    def accrue_financing_cost(
        self,
        position_id: str,
        symbol: str,
        borrowed_principal: float,
        elapsed_seconds: float,
        as_of: Optional[datetime] = None,
        config: Optional[FinancingConfig] = None,
    ) -> FinancingAccrualResult:
        """Accrues equity-margin borrowing financing cost over elapsed time."""
        now = as_of or datetime.now(timezone.utc)
        cfg = config or self.financing_config
        if borrowed_principal <= 0 or elapsed_seconds <= 0:
            zero_delta = AccountingDelta(
                idempotency_key=f"finance:{position_id}:{now.isoformat()}",
                aggregate_id=symbol,
                event_type="FINANCING_COST_ACCRUED",
                cash_delta=0.0,
                financing_cost_delta=0.0,
                timestamp=now,
            )
            return FinancingAccrualResult(
                position_id=position_id,
                elapsed_seconds=elapsed_seconds,
                borrowed_principal=borrowed_principal,
                annual_rate=cfg.annual_financing_rate,
                financing_cost=0.0,
                delta=zero_delta,
                event=NormalizedAccountingEvent(
                    event_type="FINANCING_COST_ACCRUED",
                    aggregate_id=symbol,
                    timestamp=now,
                    delta=zero_delta,
                    payload={"cost": 0.0},
                    idempotency_key=zero_delta.idempotency_key,
                ),
            )

        # Annualized rate divided by day count convention
        cost = (
            borrowed_principal
            * cfg.annual_financing_rate
            * (elapsed_seconds / (cfg.day_count_convention * 86400.0))
        )
        cost = round(cost, 4)
        idempotency_key = f"finance:{position_id}:{now.strftime('%Y%m%d%H%M%S')}:{cost}"

        delta = AccountingDelta(
            idempotency_key=idempotency_key,
            aggregate_id=symbol,
            event_type="FINANCING_COST_ACCRUED",
            cash_delta=-cost,
            financing_cost_delta=cost,
            timestamp=now,
            metadata={
                "borrowed_principal": borrowed_principal,
                "annual_rate": cfg.annual_financing_rate,
                "assumption": cfg.assumption_label,
            },
        )
        event = NormalizedAccountingEvent(
            event_type="FINANCING_COST_ACCRUED",
            aggregate_id=symbol,
            timestamp=now,
            delta=delta,
            payload={
                "position_id": position_id,
                "symbol": symbol,
                "borrowed_principal": borrowed_principal,
                "financing_cost": cost,
                "elapsed_seconds": elapsed_seconds,
            },
            idempotency_key=idempotency_key,
        )

        return FinancingAccrualResult(
            position_id=position_id,
            elapsed_seconds=elapsed_seconds,
            borrowed_principal=borrowed_principal,
            annual_rate=cfg.annual_financing_rate,
            financing_cost=cost,
            delta=delta,
            event=event,
        )

    def check_position_risk(
        self,
        position: DerivativePosition,
        spec: ContractSpec,
        quote: Optional[DerivativeQuote],
        total_account_equity: float,
        total_maintenance_required: float,
        as_of: Optional[datetime] = None,
    ) -> RiskReviewResult:
        """Evaluates margin maintenance and expiry constraints.

        Pricing failure during risk review does NOT block close/risk review.
        Breaches emit deterministic LIQUIDATION_REQUIRED or PRE_EXPIRY_CLOSE_REQUIRED.
        """
        now = as_of or datetime.now(timezone.utc)
        result = RiskReviewResult(symbol=position.symbol, healthy=True)

        # 1. Pricing inspection
        quote_valid = False
        if quote is not None:
            quote_valid, _ = self.validate_quote(quote, as_of=now)
        result.pricing_available = quote_valid
        if not quote_valid:
            result.messages.append("PRICING_UNAVAILABLE_INSPECTION_ONLY")

        # 2. Expiry Guard check (missing delivery/assignment handling requires pre-expiry close)
        if spec.expiry is not None and position.quantity != 0 and not position.is_closed:
            exp = spec.expiry if spec.expiry.tzinfo else spec.expiry.replace(tzinfo=timezone.utc)
            time_to_exp = (exp - now).total_seconds()
            if time_to_exp <= spec.pre_expiry_close_lead_seconds:
                result.healthy = False
                result.pre_expiry_close_required = True
                result.messages.append(
                    f"PRE_EXPIRY_CLOSE_REQUIRED: {time_to_exp:.0f}s to expiry; holding across expiry disallowed"
                )
                idempotency_key = f"pre_expiry_close:{position.position_id}:{now.strftime('%Y%m%d%H%M')}"
                delta = AccountingDelta(
                    idempotency_key=idempotency_key,
                    aggregate_id=position.symbol,
                    event_type="PRE_EXPIRY_CLOSE_REQUIRED",
                    timestamp=now,
                    metadata={"time_to_expiry_seconds": time_to_exp, "expiry": exp.isoformat()},
                )
                close_event = NormalizedAccountingEvent(
                    event_type="PRE_EXPIRY_CLOSE_REQUIRED",
                    aggregate_id=position.symbol,
                    timestamp=now,
                    delta=delta,
                    payload={
                        "position_id": position.position_id,
                        "symbol": position.symbol,
                        "time_to_expiry_seconds": time_to_exp,
                        "action_required": "CLOSE_BEFORE_EXPIRY",
                    },
                    idempotency_key=idempotency_key,
                )
                result.events.append(close_event)

        # 3. Maintenance Margin deficiency check
        if total_maintenance_required > 0 and total_account_equity < total_maintenance_required:
            result.healthy = False
            result.margin_deficient = True
            result.liquidation_required = True
            result.messages.append(
                f"LIQUIDATION_REQUIRED: Equity {total_account_equity:.2f} below maintenance margin {total_maintenance_required:.2f}"
            )
            idempotency_key = f"liquidation:{position.position_id}:{now.strftime('%Y%m%d%H%M')}"
            delta = AccountingDelta(
                idempotency_key=idempotency_key,
                aggregate_id=position.symbol,
                event_type="LIQUIDATION_REQUIRED",
                timestamp=now,
                metadata={
                    "total_account_equity": total_account_equity,
                    "maintenance_margin_required": total_maintenance_required,
                    "disclaimer": "DO_NOT_CLAIM_BROKERAGE_EXECUTION: Engine signals required liquidation event, not broker fill",
                },
            )
            liq_event = NormalizedAccountingEvent(
                event_type="LIQUIDATION_REQUIRED",
                aggregate_id=position.symbol,
                timestamp=now,
                delta=delta,
                payload={
                    "position_id": position.position_id,
                    "symbol": position.symbol,
                    "equity": total_account_equity,
                    "maintenance_margin_required": total_maintenance_required,
                    "execution_claim": "NONE_SIMULATION_FLAG_ONLY",
                },
                idempotency_key=idempotency_key,
            )
            result.events.append(liq_event)

        return result

    def simulate_synthetic_stress_scenario(
        self,
        position: DerivativePosition,
        spec: ContractSpec,
        scenario_name: str,
        price_shock_pct: float,
        base_underlying_price: float,
    ) -> SyntheticStressResult:
        """Simulates synthetic stress scenarios without mutating market PnL or cash balances.
        
        Stress testing is an analytical projection and remains strictly separate from live/market PnL.
        """
        sim_px = max(0.01, round(base_underlying_price * (1.0 + price_shock_pct), 4))
        mult = spec.multiplier
        if spec.instrument_type == DerivativeInstrumentType.OPTION:
            strike = spec.strike or 0.0
            if spec.option_right == OptionRight.CALL:
                intrinsic = max(0.0, sim_px - strike)
            else:
                intrinsic = max(0.0, strike - sim_px)
            sim_value = round(intrinsic * mult * position.quantity, 4)
            cost_basis = round(position.average_entry_price * mult * position.quantity, 4)
            sim_pnl = round(sim_value - cost_basis, 4)
        elif spec.instrument_type == DerivativeInstrumentType.FUTURE:
            diff = sim_px - position.average_entry_price
            sim_pnl = round(diff * mult * position.quantity, 4)
            sim_value = round(sim_px * mult * position.quantity, 4)
        else:
            sim_pnl = 0.0
            sim_value = 0.0

        return SyntheticStressResult(
            scenario_name=scenario_name,
            underlying_price_shock_pct=price_shock_pct,
            simulated_underlying_price=sim_px,
            simulated_contract_value=sim_value,
            simulated_pnl=sim_pnl,
            is_synthetic=True,
            separate_from_market_pnl=True,
        )


class NormalizedStateReducer:
    """Deterministic, pure in-memory state reducer for replay and idempotency verification."""

    def __init__(self, initial_cash: float = 1_000_000.0) -> None:
        self.cash = float(initial_cash)
        self.locked_margin = 0.0
        self.realized_pnl = 0.0
        self.total_fees = 0.0
        self.total_tax = 0.0
        self.total_financing = 0.0
        self.applied_keys: Set[str] = set()

    def apply(self, event: NormalizedAccountingEvent) -> bool:
        """Applies event idempotently. Returns True if applied, False if duplicate."""
        if event.idempotency_key in self.applied_keys:
            return False

        d = event.delta
        self.cash += d.cash_delta
        self.locked_margin += d.margin_locked_delta
        self.realized_pnl += d.realized_pnl_delta
        self.total_fees += d.fee_delta
        self.total_tax += d.tax_delta
        self.total_financing += d.financing_cost_delta

        self.applied_keys.add(event.idempotency_key)
        return True


class SyntheticStressResult(BaseModel):
    """Synthetic scenario stress test result kept strictly separate from market PnL."""
    scenario_name: str
    underlying_price_shock_pct: float
    simulated_underlying_price: float
    simulated_contract_value: float
    simulated_pnl: float
    is_synthetic: bool = True
    separate_from_market_pnl: bool = True
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def apply_derivative_event_to_portfolio(
    event: NormalizedAccountingEvent,
    portfolio_manager: Any,
    strategy_id: str,
    bucket: DecisionScope,
    event_store: Optional[Any] = None,
    replay: bool = False,
) -> bool:
    """Integrates normalized derivative accounting events through the single canonical portfolio path.
    
    No second ledger. Applies cash deltas through CanonicalCashAccount, updates positions,
    tracks realized PnL, recalculates equity, and records to EventStore.
    """
    applied_keys = getattr(portfolio_manager, "_applied_derivative_keys", None)
    if applied_keys is None:
        applied_keys = set()
        portfolio_manager._applied_derivative_keys = applied_keys
    scoped_key = f"{strategy_id}:{bucket.value}:{event.idempotency_key}"
    if scoped_key in applied_keys:
        return False

    if event_store is not None and not replay:
        from uuid import NAMESPACE_URL, uuid5
        persisted = event.model_copy(deep=True)
        persisted.event_id = str(uuid5(NAMESPACE_URL, f"paper-derivative:{scoped_key}"))
        persisted.payload.update({"strategy_id": strategy_id, "bucket": bucket.value,
                                  "paper_derivative": True})
        # An existing event may be the last committed write of a prior attempt.
        # In that case apply its ORIGINAL delta, never recompute a new fill.
        existing = event_store.get_by_event_id(persisted.event_id)
        if existing is not None:
            payload = existing.payload
            if payload.get("strategy_id") != strategy_id or payload.get("bucket") != bucket.value:
                raise ValueError("CONFLICTING_DERIVATIVE_EVENT_SCOPE")
            event = NormalizedAccountingEvent(
                event_id=existing.event_id, event_type=payload["normalized_event_type"],
                aggregate_id=existing.aggregate_id, timestamp=existing.timestamp,
                delta=AccountingDelta.model_validate(payload["delta"]),
                idempotency_key=payload["idempotency_key"], payload=payload,
            )
        else:
            event_store.append_once(persisted.to_event_envelope())
            event = persisted

    ledger = portfolio_manager.get_strategy_ledger(strategy_id, bucket)
    delta = event.delta

    # 1. Cash flow application (single canonical cash pool)
    if delta.cash_delta != 0.0:
        if getattr(ledger, "_cash_account", None) is not None:
            if delta.cash_delta > 0:
                ledger._cash_account.credit(delta.cash_delta)
            else:
                ledger._cash_account.debit(-delta.cash_delta)
        else:
            ledger.cash += delta.cash_delta

    # 2. Realized PnL application
    if delta.realized_pnl_delta != 0.0:
        ledger.realized_pnl += delta.realized_pnl_delta

    # 3. Position update
    sym = event.aggregate_id
    pos = ledger.get_position(sym)
    pos_data = event.payload.get("position")
    if pos_data:
        pos.quantity = float(pos_data.get("quantity", 0.0))
        pos.multiplier = float(pos_data.get("multiplier", 1.0))
        pos.average_entry_price = float(pos_data.get("average_entry_price", 0.0))
        pos.current_price = float(pos_data.get("current_price", 0.0)) if pos_data.get("current_price") is not None else None
        pos.market_value = float(pos_data.get("market_value", 0.0)) if pos_data.get("market_value") is not None else None
        pos.unrealized_pnl = float(pos_data.get("unrealized_pnl", 0.0)) if pos_data.get("unrealized_pnl") is not None else None
        if "instrument_type" in pos_data:
            pos.instrument_type = pos_data["instrument_type"]
        if "option_right" in pos_data:
            pos.option_right = pos_data["option_right"]
        if "strike" in pos_data:
            pos.strike = pos_data["strike"]
        if pos_data.get("expiry"):
            exp_val = pos_data["expiry"]
            pos.expiry = datetime.fromisoformat(str(exp_val)) if isinstance(exp_val, str) else exp_val
        if pos_data.get("total_premium_paid") is not None:
            if hasattr(pos, "assumptions") and isinstance(pos.assumptions, dict):
                pos.assumptions["total_premium_paid"] = pos_data["total_premium_paid"]
            elif hasattr(pos, "provenance") and isinstance(pos.provenance, dict):
                pos.provenance["total_premium_paid"] = pos_data["total_premium_paid"]
        pos.assumptions["derivative_position"] = pos_data
        if event.payload.get("contract_spec"):
            pos.assumptions["contract_spec"] = event.payload["contract_spec"]
        if pos.instrument_type == "FUTURE" and pos.quantity:
            pos.market_value = pos.unrealized_pnl
        if pos.quantity == 0.0:
            pos.market_value = 0.0
            pos.unrealized_pnl = 0.0

    ledger._recalculate_equity()

    applied_keys.add(scoped_key)
    return True
