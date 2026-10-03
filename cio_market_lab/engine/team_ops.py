"""Canonical Team Ops engine for CIO Market Lab.

Provides:
1. Unified Team NAV calculation with explicit fail-closed NAV_UNAVAILABLE on stale/missing/synthetic marks.
2. Consolidated canonical positions per (market, symbol, currency) with strategy attribution metadata.
3. Durable quote snapshot contract (never fabricates bid/ask).
4. Deterministic team posture metadata driven by TW/US regime, data quality, and gross exposure (with monotonicity).
5. Canonical Team Ops snapshot contract and disk persistence for cross-process convergence.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from cio_market_lab.domain.models import (
    Bar,
    DecisionScope,
    Fill,
    Market,
    Order,
    OrderSide,
    PaperPortfolio,
    Position,
    Quote,
)
from cio_market_lab.engine.portfolio import is_slippage_embedded
from cio_market_lab.engine.market_schedule import intraday_market_open


class DurableQuoteSnapshot(BaseModel):
    """Durable quote snapshot contract without fabricated bid/ask."""
    symbol: str
    market: Market
    source: str
    observed_at: datetime
    bar_time: Optional[datetime] = None
    session: str = "REGULAR"  # REGULAR, EXTENDED_PRE, EXTENDED_POST, CLOSED
    regular_price: Optional[float] = None
    extended_price: Optional[float] = None
    last_price: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    age_seconds: float = 0.0
    quality: str = "good"
    is_stale: bool = False
    is_synthetic: bool = False
    fabrication_guard: bool = True  # True indicates bid/ask was never fabricated from daily close

    @property
    def observed_time(self) -> datetime:
        return self.observed_at


class StrategyAttribution(BaseModel):
    strategy_id: str
    strategy_name: str
    quantity: float
    average_entry_price: float
    cost_basis: float
    market_value: float
    unrealized_pnl: float
    realized_pnl: float


class CanonicalPositionRow(BaseModel):
    """Consolidated position row per (market, symbol, currency)."""
    market: Market
    symbol: str
    currency: str
    quantity: float
    average_entry_price: float
    current_price: Optional[float] = None
    cost_basis: float
    market_value: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    realized_pnl: float
    unrealized_pnl_pct: Optional[float] = None
    quote_snapshot: Optional[Dict[str, Any]] = None
    strategy_attribution: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class FunctionalDeskRole(BaseModel):
    """Functional role on the autonomous paper-execution desk."""
    role_id: str
    role_name: str
    title: str
    scope: str
    status: str = "MONITORING"
    current_task: str = ""
    next_review_time: Optional[str] = None


class ActivePlaybookInfo(BaseModel):
    """Currently active playbook metadata selected by the desk."""
    playbook_id: str
    playbook_name: str
    description: str = ""
    selection_rationale: str = ""
    regime: str = ""
    data_quality: str = "FRESH"
    session_state: str = ""
    gross_exposure: float = 0.0
    target_cash_pct: float = 30.0
    risk_multiplier: float = 1.0
    selected_at: str = ""
    next_review_time: str = ""


class DeskStatus(BaseModel):
    """Unified autonomous paper-execution desk state."""
    desk_id: str = "dynamic-desk"
    desk_name: str = "Autonomous Paper Execution Desk"
    active_playbook: ActivePlaybookInfo
    roles: List[FunctionalDeskRole] = Field(default_factory=list)
    next_review_time: str = ""


class TeamPostureMetadata(BaseModel):
    """Deterministic team posture driven by TW/US regime, data quality, and gross exposure."""
    posture: str  # ACTIVE_EXPANSION, BALANCED_MODERATE, DEFENSIVE_PRESERVATION, DEFENSIVE_HALT
    risk_budget_multiplier: float
    allow_new_entries: bool
    fail_closed: bool
    tw_regime: str
    us_regime: str
    data_quality: str  # FRESH, DELAYED_VALID, STALE, SYNTHETIC, MISSING
    gross_exposure: float
    net_exposure: float
    rationale: str


def compute_team_posture(
    tw_regime: str,
    us_regime: str,
    data_quality: str,
    gross_exposure: float,
    net_exposure: float,
) -> TeamPostureMetadata:
    """Deterministically compute team posture.

    Monotonicity invariant:
    1. If data_quality is STALE, SYNTHETIC, or MISSING, fail closed to 0.0 multiplier.
    2. Under identical market regime, risk_budget_multiplier is monotonically non-increasing
       as gross_exposure increases.
    3. As regime risk increases (calm -> volatile/bear), multiplier is monotonically non-increasing.
    """
    quality_upper = data_quality.upper()
    if quality_upper in {"STALE", "SYNTHETIC", "MISSING", "FAILED_CLOSED"}:
        return TeamPostureMetadata(
            posture="DEFENSIVE_HALT",
            risk_budget_multiplier=0.0,
            allow_new_entries=False,
            fail_closed=True,
            tw_regime=tw_regime,
            us_regime=us_regime,
            data_quality=data_quality,
            gross_exposure=round(gross_exposure, 4),
            net_exposure=round(net_exposure, 4),
            rationale=f"FAIL_CLOSED: data quality '{data_quality}' is unacceptable for active risk allocation.",
        )

    # Assess market regime tension
    volatile_or_bear = any(
        r in {"HIGH_VOLATILITY", "BEAR_TREND", "CORRECTION", "CLOSED_RISK_OFF"}
        for r in (tw_regime.upper(), us_regime.upper())
    )

    if volatile_or_bear:
        if gross_exposure < 0.30:
            posture = "DEFENSIVE_PRESERVATION"
            multiplier = 0.30
            allow = False
            rationale = "Regime indicates high volatility/bear risk; exposure capped to preservation budget."
        elif gross_exposure < 0.60:
            posture = "DEFENSIVE_PRESERVATION"
            multiplier = 0.15
            allow = False
            rationale = "Regime volatility high with moderate exposure; risk budget curtailed."
        else:
            posture = "DEFENSIVE_HALT"
            multiplier = 0.0
            allow = False
            rationale = "Regime volatility high and gross exposure elevated; new risk halted."
    else:
        # Normal or Bull market regimes
        if gross_exposure < 0.35:
            posture = "ACTIVE_EXPANSION"
            multiplier = 1.0
            allow = True
            rationale = "Healthy regime and low exposure; full risk allocation allowed."
        elif gross_exposure < 0.70:
            posture = "BALANCED_MODERATE"
            multiplier = 0.60
            allow = True
            rationale = "Healthy regime with moderate exposure; balanced risk budget."
        elif gross_exposure < 0.90:
            posture = "BALANCED_MODERATE"
            multiplier = 0.25
            allow = False
            rationale = "Approaching portfolio exposure ceiling; entries throttled."
        else:
            posture = "DEFENSIVE_HALT"
            multiplier = 0.0
            allow = False
            rationale = "Portfolio gross exposure capped (>90%); new entries blocked."

    return TeamPostureMetadata(
        posture=posture,
        risk_budget_multiplier=round(multiplier, 4),
        allow_new_entries=allow,
        fail_closed=False,
        tw_regime=tw_regime,
        us_regime=us_regime,
        data_quality=data_quality,
        gross_exposure=round(gross_exposure, 4),
        net_exposure=round(net_exposure, 4),
        rationale=rationale,
    )


class TeamOpsSnapshotBuilder:
    """Builds and persists the authoritative Team Ops snapshot."""

    @staticmethod
    def market_for_symbol(symbol: str) -> Market:
        return Market.TW if symbol.upper().endswith((".TW", ".TWO")) else Market.US

    @staticmethod
    def currency_for_symbol(symbol: str) -> str:
        return "TWD" if symbol.upper().endswith((".TW", ".TWO")) else "USD"

    @classmethod
    def consolidate_positions(
        cls,
        fills: List[Fill],
        quotes: Dict[str, DurableQuoteSnapshot],
        strategy_fills: Optional[Dict[str, List[Fill]]] = None,
        strategy_names: Optional[Dict[str, str]] = None,
    ) -> Tuple[List[CanonicalPositionRow], List[str]]:
        """Consolidate all fills into exactly one row per (market, symbol, currency).

        Preserves strategy attribution as metadata, preventing duplicate holdings.
        """
        # Group fills by (market, symbol, currency)
        by_key: Dict[Tuple[str, str, str], List[Fill]] = {}
        for f in fills:
            m = cls.market_for_symbol(f.symbol)
            c = cls.currency_for_symbol(f.symbol)
            key = (m.value, f.symbol, c)
            by_key.setdefault(key, []).append(f)

        warnings: List[str] = []
        rows: List[CanonicalPositionRow] = []

        strategy_fills_map = strategy_fills or {}
        strat_names = strategy_names or {}

        for (m_val, symbol, currency), symbol_fills in by_key.items():
            market = Market(m_val)
            net_qty = 0.0
            total_cost_basis = 0.0
            realized_pnl = 0.0

            # Sort fills chronologically
            sorted_fills = sorted(symbol_fills, key=lambda x: x.timestamp)
            for f in sorted_fills:
                fill_cost = f.quantity * f.fill_price
                cost_deductions = (f.fee + f.tax) if is_slippage_embedded(f) else (f.fee + f.tax + f.slippage)
                if f.side == OrderSide.BUY:
                    net_qty += f.quantity
                    total_cost_basis += fill_cost + cost_deductions
                elif f.side == OrderSide.SELL:
                    avg_cost = (total_cost_basis / net_qty) if net_qty > 0 else 0.0
                    cost_of_sold = avg_cost * f.quantity
                    pnl = (f.fill_price * f.quantity) - cost_of_sold - cost_deductions
                    realized_pnl += pnl
                    net_qty = max(0.0, net_qty - f.quantity)
                    total_cost_basis = max(0.0, total_cost_basis - cost_of_sold)

            if net_qty <= 1e-6 and abs(realized_pnl) < 1e-6:
                continue

            avg_entry = (total_cost_basis / net_qty) if net_qty > 1e-6 else 0.0

            # Mark to market using authoritative quote
            quote = quotes.get(symbol)
            quote_dict = quote.model_dump(mode="json") if quote else None
            is_mark_valid = (
                quote is not None
                and quote.last_price is not None
                and not quote.is_stale
                and not quote.is_synthetic
                and "synthetic" not in (quote.quality or "").lower()
                and "fallback" not in (quote.source or "").lower()
                and quote.source != "missing"
            )

            if is_mark_valid and quote is not None and quote.last_price is not None:
                curr_px: Optional[float] = round(quote.last_price, 4)
                market_val: Optional[float] = round(net_qty * curr_px, 4)
                unrealized_pnl: Optional[float] = round(market_val - total_cost_basis, 4)
                unrealized_pct: Optional[float] = (
                    round(((unrealized_pnl / total_cost_basis) * 100.0), 4) if total_cost_basis > 0 else 0.0
                )
            else:
                curr_px = None
                market_val = None
                unrealized_pnl = None
                unrealized_pct = None
                if quote is None or quote.last_price is None or (quote and quote.source == "missing"):
                    warnings.append(f"MISSING_MARK:{symbol} - authoritative price quote unavailable")
                elif quote.is_stale:
                    warnings.append(f"STALE_MARK:{symbol} - quote is stale")
                elif quote.is_synthetic:
                    warnings.append(f"SYNTHETIC_MARK:{symbol} - synthetic quote cannot mark canonical position")

            # Calculate strategy attribution
            attribution: Dict[str, Dict[str, Any]] = {}
            for strat_id, s_fills in strategy_fills_map.items():
                s_symbol_fills = [f for f in s_fills if f.symbol == symbol]
                if not s_symbol_fills:
                    continue
                s_net_qty = 0.0
                s_cost_basis = 0.0
                s_realized = 0.0
                for f in sorted(s_symbol_fills, key=lambda x: x.timestamp):
                    fc = f.quantity * f.fill_price
                    cd = (f.fee + f.tax) if is_slippage_embedded(f) else (f.fee + f.tax + f.slippage)
                    if f.side == OrderSide.BUY:
                        s_net_qty += f.quantity
                        s_cost_basis += fc + cd
                    elif f.side == OrderSide.SELL:
                        s_avg = (s_cost_basis / s_net_qty) if s_net_qty > 0 else 0.0
                        sold_c = s_avg * f.quantity
                        p = (f.fill_price * f.quantity) - sold_c - cd
                        s_realized += p
                        s_net_qty = max(0.0, s_net_qty - f.quantity)
                        s_cost_basis = max(0.0, s_cost_basis - sold_c)

                if s_net_qty > 1e-6 or abs(s_realized) > 1e-6:
                    s_avg_entry = (s_cost_basis / s_net_qty) if s_net_qty > 1e-6 else 0.0
                    if is_mark_valid and curr_px is not None:
                        s_mv: Optional[float] = round(s_net_qty * curr_px, 4)
                        s_unrealized: Optional[float] = round(s_mv - s_cost_basis, 4)
                    else:
                        s_mv = None
                        s_unrealized = None
                    attribution[strat_id] = {
                        "strategy_id": strat_id,
                        "strategy_name": strat_names.get(strat_id, strat_id),
                        "quantity": round(s_net_qty, 4),
                        "average_entry_price": round(s_avg_entry, 4),
                        "cost_basis": round(s_cost_basis, 4),
                        "market_value": s_mv,
                        "unrealized_pnl": s_unrealized,
                        "realized_pnl": round(s_realized, 4),
                    }

            row = CanonicalPositionRow(
                market=market,
                symbol=symbol,
                currency=currency,
                quantity=round(net_qty, 4),
                average_entry_price=round(avg_entry, 4),
                current_price=curr_px,
                cost_basis=round(total_cost_basis, 4),
                market_value=market_val,
                unrealized_pnl=unrealized_pnl,
                realized_pnl=round(realized_pnl, 4),
                unrealized_pnl_pct=unrealized_pct,
                quote_snapshot=quote_dict,
                strategy_attribution=attribution,
            )
            rows.append(row)

        return rows, warnings

    @classmethod
    def evaluate_nav(
        cls,
        cash_twd: float,
        initial_capital_twd: float,
        positions: List[CanonicalPositionRow],
        quotes: Dict[str, DurableQuoteSnapshot],
        fx_rates: Optional[Dict[str, float]] = None,
        max_data_age_seconds: float = 86400.0,
        cash_fx_missing: bool = False,
    ) -> Tuple[str, Optional[float], Optional[float], Optional[float], Optional[float], List[str]]:
        """Compute authoritative Team NAV.

        Fails closed with NAV_UNAVAILABLE if any open position mark is missing,
        stale, or synthetic, or if cash/positions require an FX rate that is missing.
        """
        rates = dict(fx_rates) if fx_rates is not None else {"TWD": 1.0}
        rates.setdefault("TWD", 1.0)
        warnings: List[str] = []
        nav_unavailable = False

        if cash_fx_missing:
            nav_unavailable = True
            warnings.append("MISSING_VALUATION_FX: cash accounting lacks validated/configured FX rate for fills.")

        open_positions = [p for p in positions if p.quantity > 1e-6]

        for p in open_positions:
            if p.currency != "TWD" and (p.currency not in rates or rates[p.currency] is None or rates[p.currency] <= 0):
                nav_unavailable = True
                warnings.append(f"MISSING_VALUATION_FX:{p.currency} - open position lacks validated/configured FX rate.")

            quote = quotes.get(p.symbol)
            if quote is None or quote.last_price is None or quote.source == "missing":
                nav_unavailable = True
                warnings.append(f"MISSING_MARK:{p.symbol} - open position lacks authoritative price quote.")
                continue

            if quote.is_stale or quote.age_seconds > max_data_age_seconds:
                nav_unavailable = True
                warnings.append(
                    f"STALE_MARK:{p.symbol} - quote age {quote.age_seconds:.1f}s exceeds threshold {max_data_age_seconds}s."
                )

            if quote.is_synthetic or "synthetic" in (quote.quality or "").lower() or "fallback" in (quote.source or "").lower():
                nav_unavailable = True
                warnings.append(f"SYNTHETIC_MARK:{p.symbol} - synthetic/fallback quote cannot mark canonical NAV.")

            if p.market_value is None or p.current_price is None:
                nav_unavailable = True
                if not (quote.is_stale or quote.is_synthetic):
                    warnings.append(f"MISSING_MARK:{p.symbol} - open position lacks authoritative price quote.")

        if nav_unavailable:
            return "NAV_UNAVAILABLE", None, None, None, None, warnings

        # Calculate NAV
        positions_market_value_twd = sum(
            p.market_value * rates.get(p.currency, 1.0) for p in open_positions if p.market_value is not None
        )
        total_equity_twd = cash_twd + positions_market_value_twd
        total_unrealized_twd = sum(
            p.unrealized_pnl * rates.get(p.currency, 1.0) for p in open_positions if p.unrealized_pnl is not None
        )
        total_pnl_twd = total_equity_twd - initial_capital_twd
        return_pct = ((total_pnl_twd / initial_capital_twd) * 100.0) if initial_capital_twd > 0 else 0.0

        return (
            "OK",
            round(total_equity_twd, 4),
            round(total_unrealized_twd, 4),
            round(total_pnl_twd, 4),
            round(return_pct, 6),
            warnings,
        )


class CanonicalTeamOpsSnapshot(BaseModel):
    """Authoritative Team Ops snapshot contract."""
    timestamp: str = Field(description="Server-generated ISO timestamp")
    server_time: Optional[str] = Field(default=None, description="Server-generated ISO timestamp")
    version: int = Field(default=1, description="Monotonically increasing version")
    safety: Dict[str, Any]
    sessions: Dict[str, Any]
    quote_freshness: Optional[Dict[str, Any]] = None
    data_freshness: Optional[Dict[str, Any]] = None
    canonical_positions: List[CanonicalPositionRow] = Field(default_factory=list)
    holdings: List[Dict[str, Any]] = Field(default_factory=list)
    portfolio: Optional[Dict[str, Any]] = None
    cash: float
    equity: Optional[float] = None
    initial_capital: float
    realized_pnl: float
    unrealized_pnl: Optional[float] = None
    total_pnl: Optional[float] = None
    return_pct: Optional[float] = None
    nav_status: str  # "OK" or "NAV_UNAVAILABLE"
    nav: Optional[float] = None  # None when NAV_UNAVAILABLE
    regime_and_posture: Optional[Dict[str, Any]] = None
    regime: Optional[Dict[str, Any]] = None
    posture: Dict[str, Any] = Field(default_factory=dict)
    risk: Dict[str, Any]
    quotes: Dict[str, Any] = Field(default_factory=dict)
    orders: List[Dict[str, Any]] = Field(default_factory=list)
    fills: List[Dict[str, Any]] = Field(default_factory=list)
    activity: List[Dict[str, Any]] = Field(default_factory=list)
    benchmark: Dict[str, Any]
    data_status: str
    desk: Optional[Dict[str, Any]] = None
    integrity_warnings: List[str] = Field(default_factory=list)


TEAM_INITIAL_CAPITAL_TWD: float = 2_378_465.0


def to_public_team_ops_snapshot(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize canonical Team Ops snapshot to the strict public contract.

    Returns only:
    server_time, data_freshness, safety, sessions, portfolio, holdings, posture,
    risk, quotes, orders, fills, activity, benchmark, and optional integrity_warnings/legacy_leaderboard.
    """
    server_time = raw.get("server_time") or raw.get("timestamp") or datetime.now(timezone.utc).isoformat()

    # data_freshness: fresh|stale|unavailable string
    raw_df = raw.get("data_freshness")
    if isinstance(raw_df, str) and raw_df.lower() in ("fresh", "stale", "unavailable"):
        data_freshness = raw_df.lower()
    elif isinstance(raw_df, dict):
        st = str(raw_df.get("status", "")).upper()
        if st in ("UNAVAILABLE", "FAIL_CLOSED"):
            data_freshness = "unavailable"
        elif st == "DEGRADED" or int(raw_df.get("stale_count", 0)) > 0:
            data_freshness = "stale"
        else:
            data_freshness = "fresh"
    else:
        raw_status = str(raw.get("data_status", "")).upper()
        if raw_status in ("UNAVAILABLE", "FAIL_CLOSED"):
            data_freshness = "unavailable"
        elif raw_status == "DEGRADED":
            data_freshness = "stale"
        elif raw.get("nav_status") == "SNAPSHOT_UNAVAILABLE" or raw.get("status") == "unavailable":
            data_freshness = "unavailable"
        else:
            data_freshness = "fresh"

    # sessions.tw / sessions.us: market, status, session_label, timezone, server_time
    sessions_in = raw.get("sessions") or {}
    tw_in = sessions_in.get("tw") or sessions_in.get("TW") or {}
    us_in = sessions_in.get("us") or sessions_in.get("US") or {}

    sessions = {
        "tw": {
            "market": "TW",
            "status": tw_in.get("status", "CLOSED"),
            "session_label": tw_in.get("session_label") or tw_in.get("session", "CLOSED"),
            "timezone": tw_in.get("timezone", "Asia/Taipei"),
            "server_time": tw_in.get("server_time", server_time),
        },
        "us": {
            "market": "US",
            "status": us_in.get("status", "CLOSED"),
            "session_label": us_in.get("session_label") or us_in.get("session", "CLOSED"),
            "timezone": us_in.get("timezone", "America/New_York"),
            "server_time": us_in.get("server_time", server_time),
        },
    }

    # portfolio: reporting_currency, equity, nav_status, cash, realized_pnl, unrealized_pnl, return_pct, initial_cash, as_of
    p_in = raw.get("portfolio") or {}
    nav_status = p_in.get("nav_status") or raw.get("nav_status", "SNAPSHOT_UNAVAILABLE")
    is_authoritative = nav_status == "OK"
    is_missing_snap = nav_status == "SNAPSHOT_UNAVAILABLE"

    cash = None if is_missing_snap else (p_in.get("cash") if "cash" in p_in else raw.get("cash"))
    initial_cash = None if is_missing_snap else (p_in.get("initial_cash") if "initial_cash" in p_in else (p_in.get("initial_capital") if "initial_capital" in p_in else raw.get("initial_capital")))
    realized_pnl = None if is_missing_snap else (p_in.get("realized_pnl") if "realized_pnl" in p_in else raw.get("realized_pnl"))
    unrealized_pnl = (p_in.get("unrealized_pnl") if "unrealized_pnl" in p_in else raw.get("unrealized_pnl")) if is_authoritative else None
    equity = (p_in.get("equity") if "equity" in p_in else raw.get("equity")) if is_authoritative else None
    return_pct = (p_in.get("return_pct") if "return_pct" in p_in else raw.get("return_pct")) if is_authoritative else None
    as_of = p_in.get("as_of") or (server_time if not is_missing_snap else None)

    portfolio = {
        "reporting_currency": p_in.get("reporting_currency", "TWD"),
        "equity": equity,
        "nav_status": nav_status,
        "cash": cash,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "return_pct": return_pct,
        "initial_cash": initial_cash,
        "initial_capital": initial_cash,
        "as_of": as_of,
    }

    # holdings
    raw_holdings = raw.get("holdings") or raw.get("canonical_positions") or []
    holdings = []
    for h in raw_holdings:
        hd = h.copy() if isinstance(h, dict) else h.model_dump(mode="json")
        curr_px = hd.get("current_price")
        mv = hd.get("market_value")
        unrealized = hd.get("unrealized_pnl")
        ret_pct = hd.get("return_pct") or hd.get("unrealized_pnl_pct")
        if curr_px is None or mv is None:
            curr_px = None
            mv = None
            unrealized = None
            ret_pct = None
            price_freshness = "unavailable"
        else:
            price_freshness = hd.get("price_freshness", "fresh")

        cost_basis = float(hd.get("cost_basis", 0.0))
        entry_price = float(hd.get("entry_price") or hd.get("average_entry_price", 0.0))
        qty = float(hd.get("quantity", 0.0))
        mkt = hd.get("market", "")
        if hasattr(mkt, "value"):
            mkt = mkt.value
        mkt = str(mkt)

        holdings.append({
            "symbol": str(hd.get("symbol", "")),
            "market": mkt,
            "currency": str(hd.get("currency", "TWD")),
            "quantity": qty,
            "entry_price": entry_price,
            "average_entry_price": entry_price,
            "cost_basis": cost_basis,
            "current_price": curr_px,
            "market_value": mv,
            "unrealized_pnl": unrealized,
            "realized_pnl": float(hd.get("realized_pnl", 0.0)),
            "return_pct": ret_pct,
            "unrealized_pnl_pct": ret_pct,
            "price_freshness": price_freshness,
            "strategy_attribution": hd.get("strategy_attribution", {}),
        })

    # posture: tw_regime / us_regime are objects with market, regime, trend, volatility, updated_at; include posture_label and updated_at
    post_in = raw.get("posture") or raw.get("regime_and_posture") or {}
    tw_reg_in = post_in.get("tw_regime")
    if not isinstance(tw_reg_in, dict):
        reg_val = (raw.get("regime") or {}).get("TW", "CLOSED") if isinstance(raw.get("regime"), dict) else "CLOSED"
        tw_reg_in = {
            "market": "TW",
            "regime": str(reg_val),
            "trend": "BULLISH" if "BULL" in str(reg_val) else ("BEARISH" if "BEAR" in str(reg_val) else "NEUTRAL"),
            "volatility": "NORMAL",
            "updated_at": server_time,
        }
    us_reg_in = post_in.get("us_regime")
    if not isinstance(us_reg_in, dict):
        reg_val = (raw.get("regime") or {}).get("US", "CLOSED") if isinstance(raw.get("regime"), dict) else "CLOSED"
        us_reg_in = {
            "market": "US",
            "regime": str(reg_val),
            "trend": "BULLISH" if "BULL" in str(reg_val) else ("BEARISH" if "BEAR" in str(reg_val) else "NEUTRAL"),
            "volatility": "NORMAL",
            "updated_at": server_time,
        }

    posture_label = post_in.get("posture_label") or post_in.get("posture") or "BALANCED_MODERATE"
    if is_missing_snap:
        posture_label = "UNKNOWN"

    posture = {
        "posture": posture_label,
        "posture_label": posture_label,
        "updated_at": post_in.get("updated_at", server_time),
        "tw_regime": tw_reg_in,
        "us_regime": us_reg_in,
        "risk_budget_multiplier": post_in.get("risk_budget_multiplier", 1.0),
        "allow_new_entries": post_in.get("allow_new_entries", False if is_missing_snap else True),
        "fail_closed": post_in.get("fail_closed", True if is_missing_snap else False),
        "rationale": post_in.get("rationale", ""),
    }

    # risk: kill_switch_active, kill_switch_armed, budget_usage_pct, max_drawdown_pct, drawdown_limit_pct, daily_loss_limit, current_daily_loss, breaches.
    # Unknown numerics null; breaches may be [] only when authoritative snapshot exists.
    r_in = raw.get("risk") or {}
    ks = r_in.get("kill_switch_active", r_in.get("kill_switch", False))
    if is_missing_snap:
        risk = {
            "kill_switch_active": ks,
            "kill_switch_armed": False,
            "budget_usage_pct": None,
            "max_drawdown_pct": None,
            "drawdown_limit_pct": None,
            "daily_loss_limit": None,
            "current_daily_loss": None,
            "breaches": [],
        }
    else:
        risk = {
            "kill_switch_active": ks,
            "kill_switch_armed": r_in.get("kill_switch_armed", False),
            "budget_usage_pct": r_in.get("budget_usage_pct"),
            "max_drawdown_pct": r_in.get("max_drawdown_pct"),
            "drawdown_limit_pct": r_in.get("drawdown_limit_pct", 10.0),
            "daily_loss_limit": r_in.get("daily_loss_limit"),
            "current_daily_loss": r_in.get("current_daily_loss", 0.0),
            "breaches": r_in.get("breaches", []),
            "gross_exposure": r_in.get("gross_exposure"),
            "net_exposure": r_in.get("net_exposure"),
            "current_drawdown_pct": r_in.get("current_drawdown_pct"),
        }

    # quotes: array, not symbol map
    raw_quotes = raw.get("quotes")
    if isinstance(raw_quotes, dict):
        raw_quote_items = list(raw_quotes.values())
    elif isinstance(raw_quotes, list):
        raw_quote_items = list(raw_quotes)
    else:
        raw_quote_items = []
    quotes = []
    for q in raw_quote_items:
        symbol = str(q.get("symbol", ""))
        quotes.append({
            "symbol": symbol,
            "market": q.get("market") or ("TW" if symbol.upper().endswith((".TW", ".TWO")) else "US"),
            "price": q.get("price", q.get("last_price")),
            "change_pct": q.get("change_pct"),
            "source": q.get("source", "unknown"),
            "timestamp": q.get("timestamp", q.get("observed_at")),
            "freshness": q.get("freshness") or ("stale" if q.get("is_stale") else "fresh"),
        })

    # orders, fills, activity, benchmark
    orders = raw.get("orders") or []
    fills = []
    for f in raw.get("fills") or []:
        item = dict(f)
        symbol = str(item.get("symbol", ""))
        item["price"] = item.get("price", item.get("fill_price"))
        item["market"] = item.get("market") or ("TW" if symbol.upper().endswith((".TW", ".TWO")) else "US")
        fills.append(item)
    activity = []
    for a in raw.get("activity") or []:
        activity.append({
            "id": a.get("id", a.get("decision_id", "")),
            "actor": a.get("actor", a.get("strategy_id", "TEAM")),
            "status": a.get("status", a.get("terminal_status", "MONITORING")),
            "action": a.get("action", "MONITOR"),
            "target": a.get("target", a.get("symbol")),
            "rationale": a.get("rationale", a.get("reason", "")),
            "timestamp": a.get("timestamp", server_time),
        })

    bm_in = raw.get("benchmark") or {}
    benchmark = {
        "benchmark_name": bm_in.get("benchmark_name", "BLENDED_TW_US_BENCHMARK"),
        "period": bm_in.get("period", "30D"),
        "currency": bm_in.get("currency", "TWD"),
        "team_return_pct": return_pct if is_authoritative else None,
        "alpha_pct": bm_in.get("alpha_pct"),
        "updated_at": bm_in.get("updated_at", server_time),
        "name": bm_in.get("name", "BLENDED_TW_US_BENCHMARK"),
        "tw_proxy": bm_in.get("tw_proxy", "0050.TW"),
        "us_proxy": bm_in.get("us_proxy", "SPY"),
        "benchmark_return_pct": bm_in.get("benchmark_return_pct"),
        "relative_return_pct": bm_in.get("relative_return_pct"),
    }

    raw_safety = raw.get("safety") or {}
    fail_safe_defaults_applied = not bool(raw_safety)
    safety = {
        "paper_only": raw_safety.get("paper_only", True),
        "broker_connected": raw_safety.get("broker_connected", False),
        "broker_state": raw_safety.get("broker_state", "DISCONNECTED"),
        "autonomous_capital_decisions": raw_safety.get("autonomous_capital_decisions", False),
        "autonomous_live_capital_decisions": raw_safety.get("autonomous_live_capital_decisions", False),
        "autonomous_paper_execution": raw_safety.get("autonomous_paper_execution", True),
        "derivative_trading_supported": raw_safety.get("derivative_trading_supported", False),
        "kill_switch": ks,
        "kill_switch_active": ks,
        "fail_safe_defaults_applied": fail_safe_defaults_applied,
    }
    if fail_safe_defaults_applied:
        posture["allow_new_entries"] = False
        posture["fail_closed"] = True
        posture["risk_budget_multiplier"] = 0.0
        posture["rationale"] = "SAFETY_METADATA_MISSING_FAIL_CLOSED"
        risk["kill_switch"] = True
        risk["kill_switch_active"] = True
        safety["kill_switch"] = True
        safety["kill_switch_active"] = True

    max_drawdown_pct = risk.get("max_drawdown_pct")
    if max_drawdown_pct is not None:
        drawdown_limit_pct = 10.0
        risk["drawdown_limit_pct"] = drawdown_limit_pct
        risk["drawdown_remaining_pct"] = round(max(0.0, drawdown_limit_pct - float(max_drawdown_pct)), 6)

    out = {
        "server_time": server_time,
        "data_freshness": data_freshness,
        "safety": safety,
        "sessions": sessions,
        "portfolio": portfolio,
        "holdings": holdings,
        "posture": posture,
        "risk": risk,
        "quotes": quotes,
        "orders": orders,
        "fills": fills,
        "activity": activity,
        "benchmark": benchmark,
    }
    if "integrity_warnings" in raw:
        out["integrity_warnings"] = raw["integrity_warnings"]
    if "legacy_leaderboard" in raw:
        out["legacy_leaderboard"] = raw["legacy_leaderboard"]
    if "desk" in raw and raw["desk"]:
        desk_info = raw["desk"]
        posture["desk"] = desk_info
        if "active_playbook" in desk_info and isinstance(desk_info["active_playbook"], dict):
            posture["active_playbook"] = desk_info["active_playbook"]
        if "next_review_time" in desk_info:
            posture["next_review_time"] = desk_info["next_review_time"]
    return out


def save_canonical_snapshot(runtime_dir: Path, snapshot: Dict[str, Any]) -> None:
    """Atomically persist canonical Team Ops snapshot to disk."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target = runtime_dir / "canonical_team_ops.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(target)


def load_canonical_snapshot(runtime_dir: Path) -> Optional[Dict[str, Any]]:
    """Load canonical Team Ops snapshot from disk if present."""
    target = runtime_dir / "canonical_team_ops.json"
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None

