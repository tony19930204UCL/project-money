from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, model_validator


class Market(str, Enum):
    TW = "TW"
    US = "US"


class DecisionScope(str, Enum):
    SWING = "swing"
    INTRADAY = "intraday"
    LONG_TERM = "long_term"
    CASH = "cash"


class DecisionHorizon(str, Enum):
    INTRADAY = "intraday"
    SWING = "swing"
    LONG_TERM = "long_term"
    CASH = "cash"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class OrderOrigin(str, Enum):
    MANUAL = "MANUAL"
    STRATEGY = "STRATEGY"
    MAIN_CIO = "MAIN_CIO"


class StrategyStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    PAPER_ACTIVE = "PAPER_ACTIVE"
    PAUSED = "PAUSED"
    REJECTED = "REJECTED"


class Bar(BaseModel):
    symbol: str
    timestamp: datetime  # exchange_ts
    observed_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str = "synthetic_replay"
    delay_seconds: float = 0.0
    quality: str = "good"
    is_stale: bool = False
    is_fixture: bool = False
    is_synthetic: bool = False


class Quote(BaseModel):
    symbol: str
    timestamp: datetime
    observed_at: datetime
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: float = 0.0
    ask_size: float = 0.0
    last_price: float
    last_size: float = 0.0
    source: str = "synthetic_replay"
    delay_seconds: float = 0.0
    is_stale: bool = False
    session: Optional[str] = "REGULAR"
    regular_price: Optional[float] = None
    extended_price: Optional[float] = None
    quality: Optional[str] = None
    is_synthetic: bool = False
    source_capabilities: Dict[str, Any] = Field(default_factory=dict)
    quote_id: Optional[str] = None


class ConsumedQuoteEvidence(BaseModel):
    """Frozen snapshot of the exact book observation consumed by a paper fill."""
    model_config = {"frozen": True}

    consumed_id: str
    source_quote_id: Optional[str] = None
    symbol: str
    source: str
    exchange_at: datetime
    observed_at: datetime
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    source_capabilities: Dict[str, Any]
    is_synthetic: bool
    is_fixture: bool
    signal_bar_at: datetime
    session: str = "REGULAR"



class Signal(BaseModel):
    signal_id: str
    strategy_id: str
    version: str
    config_hash: str
    symbol: str
    market: Market
    decision_scope: DecisionScope
    side: OrderSide
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    exchange_ts: datetime
    valid_until: Optional[datetime] = None
    reason_codes: List[str] = Field(default_factory=list)
    evidence: Dict[str, Any] = Field(default_factory=dict)
    entry_model: str = "NEXT_OPEN"
    invalidation: Dict[str, Any] = Field(default_factory=dict)
    max_simulated_risk: float = 0.0


class OptionRight(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


class InstrumentType(str, Enum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    OPTION = "OPTION"
    FUTURE = "FUTURE"


class Order(BaseModel):
    currency: str = Field(default="TWD", pattern="^(USD|TWD)$")
    order_id: str
    signal_id: Optional[str] = None
    strategy_id: Optional[str] = None
    symbol: str
    market: Market
    bucket: DecisionScope
    side: OrderSide
    order_type: OrderType
    quantity: float
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    rejection_reason: Optional[str] = None
    origin: OrderOrigin = OrderOrigin.MANUAL
    reason: str = ""
    audit_metadata: Dict[str, Any] = Field(default_factory=dict)
    strategy_version: Optional[str] = None
    instrument_type: str = "EQUITY"
    underlying_symbol: Optional[str] = None
    option_right: Optional[str] = None
    strike: Optional[float] = None
    expiry: Optional[datetime] = None
    multiplier: float = 1.0
    premium: Optional[float] = None
    max_loss: Optional[float] = None
    provenance: Dict[str, Any] = Field(default_factory=dict)

    @property
    def filled_quantity(self) -> float:
        return float(self.audit_metadata.get("filled_quantity", self.quantity if self.status == OrderStatus.FILLED else 0.0))

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)


class Fill(BaseModel):
    # Legacy untyped accounting retains TWD; native USD must be explicit.
    currency: str = Field(default="TWD", pattern="^(USD|TWD)$")
    fill_id: str
    order_id: str
    symbol: str
    bucket: DecisionScope
    side: OrderSide
    quantity: float
    fill_price: float
    fee: float = 0.0
    tax: float = 0.0
    slippage: float = 0.0
    timestamp: datetime
    assumptions: Dict[str, Any] = Field(default_factory=dict)
    instrument_type: str = "EQUITY"
    underlying_symbol: Optional[str] = None
    option_right: Optional[str] = None
    strike: Optional[float] = None
    expiry: Optional[datetime] = None
    multiplier: float = 1.0
    premium: Optional[float] = None
    cash_flow: float = 0.0
    provenance: Dict[str, Any] = Field(default_factory=dict)
    consumed_quote: Optional[ConsumedQuoteEvidence] = None
    quote_verification: str = "LEGACY_UNVERIFIED"

    @model_validator(mode="before")
    @classmethod
    def classify_legacy_quote(cls, value: Any) -> Any:
        if isinstance(value, dict) and "quote_verification" not in value:
            value = dict(value)
            if "base_price" in value.get("assumptions", {}):
                value["quote_verification"] = "LEGACY_UNVERIFIED_LAST_SALE"
        return value


class Position(BaseModel):
    symbol: str
    bucket: DecisionScope
    currency: str = "TWD"
    quantity: float = 0.0
    average_entry_price: float = 0.0
    current_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    realized_pnl: float = 0.0
    market_value: Optional[float] = None
    instrument_type: str = "EQUITY"
    underlying_symbol: Optional[str] = None
    option_right: Optional[str] = None
    strike: Optional[float] = None
    expiry: Optional[datetime] = None
    multiplier: float = 1.0
    settled: bool = False
    settlement_value: Optional[float] = None
    assumptions: Dict[str, Any] = Field(default_factory=dict)
    provenance: Dict[str, Any] = Field(default_factory=dict)


class PaperPortfolio(BaseModel):
    # Legacy untyped accounting retains TWD; native USD must be explicit.
    currency: str = Field(default="TWD", pattern="^(USD|TWD)$")
    bucket: DecisionScope
    cash: float
    initial_cash: float
    equity: Optional[float] = None
    realized_pnl: float = 0.0
    unrealized_pnl: Optional[float] = None
    nav_status: Optional[str] = "OK"
    positions: Dict[str, Position] = Field(default_factory=dict)
    orders: List[Order] = Field(default_factory=list)
    fills: List[Fill] = Field(default_factory=list)


class MarketRegime(BaseModel):
    market: Market
    status: str  # OPEN, CLOSED, PRE_MARKET, POST_MARKET
    regime_tag: str  # TRENDING_BULL, RANGE_BOUND, HIGH_VOLATILITY, etc.
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ResearchItem(BaseModel):
    id: str
    url: str
    title: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    extracted_claims: List[str] = Field(default_factory=list)
    provenance: str = "direct_fetch"
    related_symbols: List[str] = Field(default_factory=list)
    verification_status: str = "UNVERIFIED"  # UNVERIFIED, VERIFIED, COMMUNITY_NARRATIVE, CONTRADICTED
    promoted_to_experiment: bool = False


class PublicResearchEvidence(BaseModel):
    research_id: str
    symbol: str
    source_url: str
    source_tier: str = "official_filing"
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    published_at: Optional[str] = None
    is_fixture: bool = False
    verification_status: str = "verified"  # verified, unverified, community_narrative, contradicted
    verified_facts: List[str] = Field(default_factory=list)
    research_scope: str = "event_input_only_not_order"
    limitations: List[str] = Field(default_factory=list)
    raw_metadata: Dict[str, Any] = Field(default_factory=dict)


class CIOProvenance(BaseModel):
    authority: str = "MAIN_CIO"  # Must be MAIN_CIO
    actor_role: str = "CHIEF_INVESTMENT_OFFICER"
    signer_id: str = "main-cio"
    source: str = "external_packet"  # external_packet, hermes_bridge, staged_fixture
    signature: Optional[str] = None
    signature_algorithm: str = "hmac-sha256"
    receipt_id: Optional[str] = None
    verified_by_worker: bool = False


class CIOExecutionReceipt(BaseModel):
    """Readback receipt from an authentic CIO decision execution."""
    receipt_id: str
    case_id: str
    session_id: str
    provider_id: str
    model_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_prompt_hash: str
    raw_response_hash: str
    readback_verified: bool = True
    authority: str = "MAIN_CIO"
    evidence_strength: str = "local-runtime"
    transport: Optional[str] = None
    requested_provider: Optional[str] = None
    requested_model: Optional[str] = None


class CIODecisionPacket(BaseModel):
    """Externally supplied CIO decision packet.
    
    AGY worker is an engineering worker, NOT the investment decision owner.
    All capital and allocation verdicts must originate from the CIO.
    """
    case_id: str
    as_of: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    evidence: List[str] = Field(default_factory=list)
    thesis: str
    selected_instrument: str
    action: str  # BUY, SELL, HOLD, REJECT, NO_TRADE
    holding_horizon: DecisionScope = DecisionScope.SWING  # Metadata only; not a separate cash pool
    quantity: float = 0.0
    conditions: Dict[str, Any] = Field(default_factory=dict)
    risk_assessment: Dict[str, Any] = Field(default_factory=dict)
    alternatives_considered: List[Dict[str, Any]] = Field(default_factory=list)
    expiry: datetime
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    strategy_version: str = "dynamic-desk-cio-20260927"
    provenance: CIOProvenance = Field(default_factory=CIOProvenance)
    is_fixture: bool = False  # Labeled test fixtures are never counted as live trades
    catalyst: Optional[str] = None
    invalidation: Optional[str] = None
    tool_eligibility: Dict[str, Any] = Field(default_factory=dict)
    risk_budget_rationale: Optional[str] = None
    predecision_snapshot: Dict[str, Any] = Field(default_factory=dict)
    predecision_version: str = "v1"


class CIODecisionContextRequest(BaseModel):
    """Context prepared by engineering worker for CIO reasoning.
    
    Worker validates, accounts, and simulates only.
    """
    request_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    universe: List[str] = Field(default_factory=list)
    canonical_portfolio: Dict[str, Any] = Field(default_factory=dict)
    verified_quotes: Dict[str, Any] = Field(default_factory=dict)
    verified_research: List[Dict[str, Any]] = Field(default_factory=list)
    research_gaps: List[Dict[str, Any]] = Field(default_factory=list)
    prior_lessons: List[Dict[str, Any]] = Field(default_factory=list)
    past_outcomes: List[Dict[str, Any]] = Field(default_factory=list)
    rejected_opportunities: List[Dict[str, Any]] = Field(default_factory=list)
    tactical_risk_limits: Dict[str, Any] = Field(default_factory=dict)
    desk_posture: Dict[str, Any] = Field(default_factory=dict)
    holding_horizons_available: List[str] = Field(
        default_factory=lambda: ["intraday", "swing", "long_term", "cash"]
    )
    tool_eligibility: Dict[str, Any] = Field(default_factory=dict)
    predecision_snapshot: Dict[str, Any] = Field(default_factory=dict)
    predecision_version: str = "v1"
    fx_rates: Dict[str, float] = Field(default_factory=dict)
    fx_accounting: Dict[str, Any] = Field(default_factory=dict)
    sizing_gaps: List[Dict[str, Any]] = Field(default_factory=list)


class CIOLessonRecord(BaseModel):
    """Persistent lesson from prior decisions and outcomes."""
    lesson_id: str
    case_id: str
    symbol: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    hypothesis_version: int = 1
    pre_decision_thesis: str
    fill_details: Optional[Dict[str, Any]] = None
    outcome_details: Optional[Dict[str, Any]] = None
    attribution: Dict[str, Any] = Field(default_factory=dict)
    rejected_alternatives: List[Dict[str, Any]] = Field(default_factory=list)
    takeaway: str
    superseded_by: Optional[str] = None
    strategy_version: str = "dynamic-desk-cio-20260927"
    prediction_vs_outcome: Dict[str, Any] = Field(default_factory=dict)
    applied_lesson_ids: List[str] = Field(default_factory=list)
    decision_delta: Dict[str, Any] = Field(default_factory=dict)
    validation_status: str = "RECORDED"  # RECORDED, VALIDATED_IMPROVEMENT
    catalyst: Optional[str] = None
    invalidation: Optional[str] = None

