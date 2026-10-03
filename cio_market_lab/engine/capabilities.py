"""Derivative capabilities and exact capability gaps reporting.

Per client policy:
Options, futures, and leverage are permitted by client for paper research, not forbidden as policy.
However, each is advertised as UNAVAILABLE until it has actual quotes, contract specs,
multipliers, fees, margin/funding, liquidation/expiry settlement, and tested NAV accounting.
This module reports exact capability gaps rather than fabricating fake support.
"""
from __future__ import annotations

from typing import Any, Dict, List
from pydantic import BaseModel, Field


class CapabilityGapDetail(BaseModel):
    capability: str
    status: str  # AVAILABLE, UNAVAILABLE, PARTIALLY_IMPLEMENTED_BLOCKED
    client_permitted: bool = True
    exact_gaps: List[str] = Field(default_factory=list)
    requirements_for_activation: List[str] = Field(default_factory=list)
    description: str = ""
    implemented_components: Dict[str, str] = Field(default_factory=dict)
    implementation_scope: str = "NO_COMPONENT_ACTIVATION_CLAIM"


class DerivativeCapabilitiesReport(BaseModel):
    policy: str = "PERMITTED_FOR_PAPER_RESEARCH"
    policy_statement: str = (
        "Options, futures, and leverage are permitted by client for paper research, "
        "not forbidden as policy. They remain UNAVAILABLE until actual quotes, "
        "specs, multipliers, margin, settlement, and NAV accounting are verified."
    )
    capabilities: Dict[str, CapabilityGapDetail] = Field(default_factory=dict)


def get_derivative_capabilities_report() -> DerivativeCapabilitiesReport:
    """Return explicit capability status and exact gaps for all instrument classes."""
    caps = {
        "CASH_EQUITY": CapabilityGapDetail(
            capability="CASH_EQUITY",
            status="AVAILABLE",
            client_permitted=True,
            exact_gaps=[],
            requirements_for_activation=[],
            description="TWSE/TPEx and US spot cash equities with authoritative quotes, slippage, fees, tax, and canonical NAV accounting.",
        ),
        "SPOT_ETF": CapabilityGapDetail(
            capability="SPOT_ETF",
            status="AVAILABLE",
            client_permitted=True,
            exact_gaps=[],
            requirements_for_activation=[],
            description="TW and US spot ETFs with authoritative quotes, slippage, fees, tax, and canonical NAV accounting.",
        ),
        "LONG_PREMIUM_OPTIONS": CapabilityGapDetail(
            capability="LONG_PREMIUM_OPTIONS",
            status="UNAVAILABLE",
            implemented_components={"monthly_TXO_European_expiry_cash": "IMPLEMENTED_NOT_ACTIVATED"},
            implementation_scope="OFFLINE_AND_RETAINED_SOURCE_ACCOUNTING_ONLY_NOT_LIVE_EXECUTION",
            client_permitted=True,
            exact_gaps=[
                "MISSING_LIVE_QUOTES: Market adapter lacks real-time and historical option chain quote stream (bid/ask/IV) from TAIFEX/OPRA.",
                "STATIC_SPECS_ONLY: Dynamic contract specs and strike grids from exchange series not connected; static mock multiplier only.",
                "NO_EXERCISE_ASSIGNMENT: Early exercise and assignment workflow not implemented.",
                "NO_IV_SURFACE: Implied volatility surface and Greeks mark-to-market accounting not implemented.",
            ],
            requirements_for_activation=[
                "Authoritative option chain quote stream with non-synthetic bid/ask/IV",
                "Exchange contract specification and expiry series registry",
                "Full lifecycle exercise/assignment simulation",
                "Tested options NAV mark-to-market accounting under market close and expiration",
            ],
            description="Long-call and long-put options permitted for paper research, but blocked until live quotes and exchange specs are connected.",
        ),
        "UNCOVERED_SHORT_OPTIONS": CapabilityGapDetail(
            capability="UNCOVERED_SHORT_OPTIONS",
            status="UNAVAILABLE",
            client_permitted=True,
            exact_gaps=[
                "NO_MARGIN_MODEL: Dynamic portfolio margin calculation for naked option short legs not implemented.",
                "NO_ASSIGNMENT_SIMULATION: Physical delivery and cash assignment handling not implemented.",
                "NO_DEFICIT_LIQUIDATION: Liquidation engine for margin deficit not implemented.",
            ],
            requirements_for_activation=[
                "SPAN/TIMS risk margin calculation engine",
                "Assignment simulation and cash settlement",
                "Intraday margin call and forced liquidation simulation",
            ],
            description="Short option writing without underlying cover.",
        ),
        "FUTURES": CapabilityGapDetail(
            capability="FUTURES",
            status="UNAVAILABLE",
            implemented_components={
                "daily_variation_margin": "IMPLEMENTED_NOT_ACTIVATED",
                "monthly_TX_exchange_expiry_cash": "IMPLEMENTED_NOT_ACTIVATED",
                "two_leg_futures_roll": "IMPLEMENTED_NOT_ACTIVATED",
                "shared_cash_margin_reduction": "IMPLEMENTED_NOT_ACTIVATED",
            },
            implementation_scope="OFFLINE_AND_RETAINED_SOURCE_ACCOUNTING_ONLY_NOT_LIVE_EXECUTION",
            client_permitted=True,
            exact_gaps=[
                "NO_FUTURES_FEED: TAIFEX (TX/MTX) and CME (ES/NQ) futures tick/bar feed not connected.",
                "SERIES_REGISTRY_NOT_ACTIVATED: Static specs and official monthly TX rules are implemented; complete live exchange series and CME specifications remain unverified.",
                "VARIATION_MARGIN_NOT_LIVE_ACTIVATED: Canonical daily accounting and retained TAIFEX settlement intake are implemented; no actual funded live derivative position acceptance is claimed.",
                "CERTIFIED_BOOK_TIMESTAMP_MISSING: Official MIS CDate/CTime are last-trade times, not certified bid/ask book update times.",
                "ROLL_NOT_LIVE_ACTIVATED: Two-leg PAPER roll accounting and the official monthly TX expiry calendar are implemented; complete live series, funded positions, and source-executable acceptance remain unverified.",
                "MARGIN_REDUCTION_NOT_LIVE_ACTIVATED: Shared-cash maintenance review, quoted deficit reductions, and replay are implemented for fixtures; no live derivative activation is claimed.",
            ],
            requirements_for_activation=[
                "Live futures market data adapter",
                "Futures contract multiplier and tick spec registry",
                "Daily variation margin accounting debited/credited to canonical cash pool",
                "Futures settlement and roll calendar",
            ],
            description="Index and single-stock futures contracts permitted for paper research once infrastructure is ready.",
        ),
        "MARGIN_LEVERAGE": CapabilityGapDetail(
            capability="MARGIN_LEVERAGE",
            status="UNAVAILABLE",
            client_permitted=True,
            exact_gaps=[
                "NO_FUNDING_RATES: Financing interest rate schedule (TWD/USD borrow rates) not connected.",
                "NO_COLLATERAL_HAIRCUTS: Maintenance margin haircuts and loan-to-value tracking not implemented.",
                "NO_LIQUIDATION_ENGINE: Automated forced liquidation upon margin call breach not implemented.",
                "NO_BORROW_LOCATES: Short-sale stock borrow inventory and borrow fee tracking not connected.",
            ],
            requirements_for_activation=[
                "Borrow fee and funding rate calculator integrated into daily ledger",
                "Maintenance margin and collateral haircut monitoring",
                "Forced liquidation trigger upon equity breach",
            ],
            description="Leveraged paper margin and short borrowing accounts.",
        ),
    }
    return DerivativeCapabilitiesReport(capabilities=caps)
