"""Research package for CIO Market Lab."""
from cio_market_lab.research.official import OfficialResearchProducer, TW_REVENUE_URL, SEC_TICKERS_URL
from cio_market_lab.research.free_adapters import (
    SecCompanyFactsAdapter,
    FinvizAdapter,
    StockAnalysisAdapter,
    CompaniesMarketCapAdapter,
    MacrotrendsAdapter,
    KoyfinAdapter,
    FreeSourceCoordinator,
    TIER_OFFICIAL_FILING,
    TIER_REGULATORY_FILING,
    TIER_OFFICIAL_EXCHANGE,
    TIER_SECONDARY_CROSS_CHECK,
    TIER_PEER_REFERENCE,
    TIER_MANUAL_BROWSER_ONLY,
)

__all__ = [
    "OfficialResearchProducer",
    "TW_REVENUE_URL",
    "SEC_TICKERS_URL",
    "SecCompanyFactsAdapter",
    "FinvizAdapter",
    "StockAnalysisAdapter",
    "CompaniesMarketCapAdapter",
    "MacrotrendsAdapter",
    "KoyfinAdapter",
    "FreeSourceCoordinator",
    "TIER_OFFICIAL_FILING",
    "TIER_REGULATORY_FILING",
    "TIER_OFFICIAL_EXCHANGE",
    "TIER_SECONDARY_CROSS_CHECK",
    "TIER_PEER_REFERENCE",
    "TIER_MANUAL_BROWSER_ONLY",
]
