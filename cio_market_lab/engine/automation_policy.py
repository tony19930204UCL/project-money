from __future__ import annotations

from datetime import datetime, time
from enum import Enum
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from cio_market_lab.domain.models import DecisionScope


class ExitAction(str, Enum):
    HOLD = "HOLD"
    EXIT = "EXIT"
    FORCE_FLATTEN = "FORCE_FLATTEN"


class ExitDecision(BaseModel):
    action: ExitAction
    reason: str
    quantity: float = 0.0
    reference_price: float = 0.0


class ExitRules(BaseModel):
    stop_loss_pct: float = Field(default=0.04, gt=0, le=1)
    take_profit_pct: float = Field(default=0.08, gt=0)
    trailing_stop_pct: float = Field(default=0.03, gt=0, le=1)
    intraday_flatten_minutes_before_close: int = Field(default=5, ge=0, le=60)


class AutomationRiskPolicy:
    """Deterministic exit and forced-flatten policy for paper positions."""

    def __init__(self, rules: Optional[ExitRules] = None):
        self.rules = rules or ExitRules()
        self._high_water: Dict[str, float] = {}

    def evaluate(
        self,
        *,
        symbol: str,
        bucket: DecisionScope,
        quantity: float,
        average_entry_price: float,
        current_price: float,
        now: datetime,
        thesis_broken: bool = False,
        portfolio_risk_breach: bool = False,
        explicit_main_exit: bool = False,
    ) -> ExitDecision:
        if quantity <= 0 or average_entry_price <= 0 or current_price <= 0:
            return ExitDecision(action=ExitAction.HOLD, reason="NO_OPEN_POSITION")

        if thesis_broken or portfolio_risk_breach or explicit_main_exit:
            reason = "THESIS_BROKEN" if thesis_broken else "PORTFOLIO_RISK_BREACH" if portfolio_risk_breach else "EXPLICIT_MAIN_EXIT"
            return ExitDecision(action=ExitAction.EXIT, reason=reason, quantity=quantity, reference_price=current_price)
        if bucket == DecisionScope.LONG_TERM:
            # Preserve downside protection, not short-horizon profit churn.
            if current_price <= average_entry_price * (1.0 - self.rules.stop_loss_pct):
                return ExitDecision(action=ExitAction.EXIT, reason="LONG_TERM_CONFIGURED_RISK_STOP", quantity=quantity, reference_price=current_price)
            return ExitDecision(action=ExitAction.HOLD, reason="LONG_TERM_MAIN_THESIS_AND_RISK_REVIEW_REQUIRED")
        key = f"{bucket.value}:{symbol}"
        high = max(self._high_water.get(key, average_entry_price), current_price)
        self._high_water[key] = high
        pnl_pct = (current_price / average_entry_price) - 1.0
        drawdown_from_high = (current_price / high) - 1.0

        if pnl_pct <= -self.rules.stop_loss_pct:
            return ExitDecision(action=ExitAction.EXIT, reason="STOP_LOSS", quantity=quantity, reference_price=current_price)
        if pnl_pct >= self.rules.take_profit_pct:
            return ExitDecision(action=ExitAction.EXIT, reason="TAKE_PROFIT", quantity=quantity, reference_price=current_price)
        if high > average_entry_price and drawdown_from_high <= -self.rules.trailing_stop_pct:
            return ExitDecision(action=ExitAction.EXIT, reason="TRAILING_STOP", quantity=quantity, reference_price=current_price)
        if bucket == DecisionScope.INTRADAY and self._is_flatten_window(symbol, now):
            return ExitDecision(action=ExitAction.FORCE_FLATTEN, reason="INTRADAY_SESSION_FLATTEN", quantity=quantity, reference_price=current_price)
        return ExitDecision(action=ExitAction.HOLD, reason="EXIT_RULES_NOT_TRIGGERED")

    def _is_flatten_window(self, symbol: str, now: datetime) -> bool:
        is_tw = symbol.upper().endswith((".TW", ".TWO"))
        zone = ZoneInfo("Asia/Taipei" if is_tw else "America/New_York")
        local = now.astimezone(zone)
        close = time(13, 30) if is_tw else time(16, 0)
        minutes_now = local.hour * 60 + local.minute
        minutes_close = close.hour * 60 + close.minute
        return 0 <= minutes_close - minutes_now <= self.rules.intraday_flatten_minutes_before_close


class LLMVerdict(str, Enum):
    ALLOW = "ALLOW"
    VETO = "VETO"
    REVIEW_ONLY = "REVIEW_ONLY"
    UNAVAILABLE = "UNAVAILABLE"


class LLMReview(BaseModel):
    verdict: LLMVerdict
    reason: str
    scope: DecisionScope
    may_change_quantity: bool = False
    may_place_order: bool = False
    model_output: Dict[str, Any] = Field(default_factory=dict)


class LLMPolicyBoundary:
    """LLM may veto intraday entries or review swing context; it never sizes or places orders."""

    def __init__(self, evaluator: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None):
        self.evaluator = evaluator

    def review(self, scope: DecisionScope, context: Dict[str, Any]) -> LLMReview:
        if self.evaluator is None:
            verdict = LLMVerdict.VETO if scope == DecisionScope.INTRADAY else LLMVerdict.UNAVAILABLE
            return LLMReview(verdict=verdict, reason="LLM_EVALUATOR_UNAVAILABLE_FAIL_CLOSED", scope=scope)
        try:
            raw = self.evaluator(dict(context))
        except Exception as exc:
            verdict = LLMVerdict.VETO if scope == DecisionScope.INTRADAY else LLMVerdict.UNAVAILABLE
            return LLMReview(verdict=verdict, reason=f"LLM_EVALUATION_FAILED:{type(exc).__name__}", scope=scope)
        if scope == DecisionScope.INTRADAY:
            verdict = LLMVerdict.ALLOW if str(raw.get("verdict", "")).upper() == "ALLOW" else LLMVerdict.VETO
        else:
            verdict = LLMVerdict.REVIEW_ONLY
        return LLMReview(
            verdict=verdict,
            reason=str(raw.get("reason") or "LLM_REVIEW_COMPLETED"),
            scope=scope,
            may_change_quantity=False,
            may_place_order=False,
            model_output={k: v for k, v in raw.items() if k not in {"quantity", "order", "price"}},
        )
