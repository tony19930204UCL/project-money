"""Comprehensive unit and integration tests for free and public research source adapters.

Covers:
- SecCompanyFactsAdapter (SEC EDGAR XBRL Company Facts as official primary evidence)
- FinvizAdapter (secondary discovery and cross-check)
- StockAnalysisAdapter (secondary cross-check and discrepancy detection)
- CompaniesMarketCapAdapter (slow-changing peer/rank reference, 24h TTL)
- MacrotrendsAdapter & KoyfinAdapter (manual/browser-only enforcement)
- FreeSourceCoordinator (multi-source refresh, discrepancy detection, fixture rejection)
- PublicResearchInboxReader integration
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from cio_market_lab.research.free_adapters import (
    SecCompanyFactsAdapter,
    FinvizAdapter,
    StockAnalysisAdapter,
    CompaniesMarketCapAdapter,
    MacrotrendsAdapter,
    KoyfinAdapter,
    FreeSourceCoordinator,
    ManualBrowserOnlyError,
    TIER_OFFICIAL_FILING,
    TIER_SECONDARY_CROSS_CHECK,
    TIER_PEER_REFERENCE,
    TIER_MANUAL_BROWSER_ONLY,
    is_fixture_symbol,
)
from cio_market_lab.research.browser import PublicResearchInboxReader


# Sample mock data for deterministic offline testing
SAMPLE_SEC_TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
}

SAMPLE_SEC_COMPANY_FACTS_NVDA = {
    "cik": 1045810,
    "entityName": "NVIDIA CORP",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "label": "Revenues",
                "description": "Total revenue",
                "units": {
                    "USD": [
                        {
                            "end": "2026-07-28",
                            "val": 30040000000,
                            "fy": 2027,
                            "fp": "Q2",
                            "form": "10-Q",
                            "filed": "2026-08-28",
                            "accn": "0001045810-26-000088",
                        }
                    ]
                }
            },
            "NetIncomeLoss": {
                "label": "Net Income (Loss)",
                "description": "Net income",
                "units": {
                    "USD": [
                        {
                            "end": "2026-07-28",
                            "val": 16599000000,
                            "fy": 2027,
                            "fp": "Q2",
                            "form": "10-Q",
                            "filed": "2026-08-28",
                            "accn": "0001045810-26-000088",
                        }
                    ]
                }
            }
        }
    }
}

SAMPLE_FINVIZ_HTML = """
<html><body>
<a href="screener.ashx?v=111&f=sec_technology" class="tab-link">Technology</a>
<a href="screener.ashx?v=111&f=ind_semiconductors" class="tab-link">Semiconductors</a>
<table class="snapshot-table2">
<tr>
<td class="snapshot-td2-cp">Market Cap</td><td class="snapshot-td2"><b>3.05T</b></td>
<td class="snapshot-td2-cp">P/E</td><td class="snapshot-td2"><b>45.20</b></td>
<td class="snapshot-td2-cp">Forward P/E</td><td class="snapshot-td2"><b>32.10</b></td>
</tr>
</table>
</body></html>
"""

SAMPLE_STOCK_ANALYSIS_HTML = """
<html><body>
<div class="font-semibold">Market Cap</div><div>$3.12T</div>
<div class="font-semibold">PE Ratio</div><div>46.50</div>
<div class="font-semibold">Revenue</div><div>$120.5B</div>
</body></html>
"""

SAMPLE_COMPANIES_MARKET_CAP_HTML = """
<html><body>
<div class="ranking-number">#2</div>
<div class="company-name">NVIDIA</div>
<div class="marketcap-value">$3.050 T</div>
</body></html>
"""


# ---------------------------------------------------------------------------
# SEC EDGAR Company Facts Tests
# ---------------------------------------------------------------------------

def test_sec_company_facts_positive_acquisition(tmp_path: Path):
    """Verify SEC XBRL company facts are extracted with full provenance and verified tier."""
    now = datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)

    def mock_fetch(url: str):
        if "company_tickers.json" in url:
            return SAMPLE_SEC_TICKERS
        if "CIK0001045810.json" in url:
            return SAMPLE_SEC_COMPANY_FACTS_NVDA
        raise ValueError(f"Unexpected url: {url}")

    reader = PublicResearchInboxReader(inbox_dir=tmp_path / "inbox")
    adapter = SecCompanyFactsAdapter(fetch_json=mock_fetch, ttl_seconds=300)

    res = adapter.acquire_facts("NVDA", now=now, reader=reader)
    assert res["status"] == "SUCCESS"
    assert len(res["evidence_ids"]) == 2
    assert not res["gaps"]

    # Verify staging into PublicResearchInboxReader
    items, gaps = reader.get_verified_research_for_symbols(["NVDA"], now=now)
    assert len(items) == 2
    assert not gaps

    for item in items:
        assert item["symbol"] == "NVDA"
        assert item["source_tier"] == TIER_OFFICIAL_FILING
        assert item["is_fixture"] is False
        assert item["verification_status"] == "verified"
        assert item["research_scope"] == "historical_company_facts_not_catalyst"
        assert "official" in item["raw_metadata"]["source"].lower()
        assert any("NVIDIA" in f and ("Revenues" in f or "Net Income" in f) for f in item["verified_facts"])


def test_sec_company_facts_fixture_rejection():
    """Negative test: Test fixture symbols are strictly rejected."""
    now = datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)
    adapter = SecCompanyFactsAdapter(fetch_json=lambda url: {})

    for sym in ["FIXTURE_NVDA", "MOCK_AAPL", "TEST_MSFT", "SYNTHETIC_STOCK"]:
        res = adapter.acquire_facts(sym, now=now)
        assert res["status"] == "REJECTED_FIXTURE"
        assert len(res["evidence_ids"]) == 0
        assert any("REJECTED_FIXTURE" in g["reason"] for g in res["gaps"])


def test_sec_company_facts_cik_not_found():
    """Negative test: Symbols not in SEC directory result in deterministic gap."""
    now = datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)
    adapter = SecCompanyFactsAdapter(fetch_json=lambda url: SAMPLE_SEC_TICKERS)

    res = adapter.acquire_facts("NONEXISTENT_TICKER", now=now)
    assert res["status"] == "SEC_CIK_NOT_FOUND"
    assert any("SEC_CIK_NOT_FOUND" in g["reason"] for g in res["gaps"])


def test_sec_company_facts_stale_filing_rejected(tmp_path: Path):
    """Negative test: Historical facts older than 730 days are rejected."""
    now = datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)
    very_old_date = (now - timedelta(days=800)).strftime("%Y-%m-%d")

    stale_facts = {
        "cik": 1045810,
        "entityName": "NVIDIA CORP",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "end": very_old_date,
                                "val": 1000000000,
                                "fy": 2024,
                                "fp": "Q1",
                                "form": "10-Q",
                                "filed": very_old_date,
                                "accn": "0001045810-24-000001",
                            }
                        ]
                    }
                }
            }
        }
    }

    def mock_fetch(url: str):
        if "company_tickers.json" in url:
            return SAMPLE_SEC_TICKERS
        return stale_facts

    adapter = SecCompanyFactsAdapter(fetch_json=mock_fetch)
    res = adapter.acquire_facts("NVDA", now=now)
    assert res["status"] == "NO_FRESH_SUPPORTED_OFFICIAL_ITEM"
    assert any("NO_FRESH_SUPPORTED_OFFICIAL_ITEM" in g["reason"] for g in res["gaps"])


# ---------------------------------------------------------------------------
# Finviz Adapter Tests
# ---------------------------------------------------------------------------

def test_finviz_adapter_parsing_and_isolation():
    """Verify Finviz parsing produces non-authoritative secondary cross-checks isolated from execution."""
    now = datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)
    adapter = FinvizAdapter(fetch_text=lambda url: SAMPLE_FINVIZ_HTML)

    res = adapter.acquire("NVDA", now=now)
    assert res["status"] == "SUCCESS"
    assert res["source_tier"] == TIER_SECONDARY_CROSS_CHECK

    record = res["record"]
    assert record["symbol"] == "NVDA"
    assert record["source_name"] == "finviz"
    assert record["source_tier"] == TIER_SECONDARY_CROSS_CHECK
    assert record["provenance"]["authoritative"] is False
    assert record["provenance"]["overrides_official"] is False
    assert record["provenance"]["overrides_executable_market_data"] is False
    assert record["metrics"]["Sector"] == "Technology"
    assert record["metrics"]["Industry"] == "Semiconductors"
    assert record["metrics"]["Market Cap"] == "3.05T"
    assert record["metrics"]["P/E"] == "45.20"
    assert any("Non-authoritative" in lim for lim in record["limitations"])


def test_finviz_adapter_fixture_rejection():
    """Negative test: Finviz rejects mock/fixture symbols."""
    now = datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)
    adapter = FinvizAdapter(fetch_text=lambda url: SAMPLE_FINVIZ_HTML)

    res = adapter.acquire("FIXTURE_AAPL", now=now)
    assert res["status"] == "REJECTED_FIXTURE"
    assert res["record"] is None


# ---------------------------------------------------------------------------
# StockAnalysis Adapter Tests
# ---------------------------------------------------------------------------

def test_stock_analysis_adapter_parsing():
    """Verify StockAnalysis parsing produces secondary discovery metrics."""
    now = datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)
    adapter = StockAnalysisAdapter(fetch_text=lambda url: SAMPLE_STOCK_ANALYSIS_HTML)

    res = adapter.acquire("NVDA", now=now)
    assert res["status"] == "SUCCESS"
    assert res["source_tier"] == TIER_SECONDARY_CROSS_CHECK

    record = res["record"]
    assert record["symbol"] == "NVDA"
    assert record["source_name"] == "stock_analysis"
    assert record["provenance"]["authoritative"] is False
    assert record["provenance"]["overrides_official"] is False
    assert record["metrics"]["Market Cap"] == "$3.12T"
    assert record["metrics"]["PE Ratio"] == "46.50"


# ---------------------------------------------------------------------------
# CompaniesMarketCap Adapter Tests
# ---------------------------------------------------------------------------

def test_companies_market_cap_adapter():
    """Verify CompaniesMarketCap produces slow peer reference with 24h TTL."""
    now = datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)
    adapter = CompaniesMarketCapAdapter(fetch_text=lambda url: SAMPLE_COMPANIES_MARKET_CAP_HTML)
    assert adapter.ttl_seconds == 86400

    res = adapter.acquire("NVDA", now=now)
    assert res["status"] == "SUCCESS"
    assert res["source_tier"] == TIER_PEER_REFERENCE

    record = res["record"]
    assert record["symbol"] == "NVDA"
    assert record["provenance"]["authoritative"] is False
    assert record["metrics"]["rank"] == 2
    assert record["metrics"]["market_cap_formatted"] == "$3.050 T"


# ---------------------------------------------------------------------------
# Macrotrends & Koyfin Manual-Only Enforcement Tests
# ---------------------------------------------------------------------------

def test_macrotrends_and_koyfin_automated_calls_blocked():
    """Verify automated calls to Macrotrends and Koyfin raise ManualBrowserOnlyError."""
    mt = MacrotrendsAdapter()
    assert mt.is_automated_allowed is False
    with pytest.raises(ManualBrowserOnlyError, match="MANUAL_BROWSER_ONLY: Macrotrends"):
        mt.fetch_page("https://www.macrotrends.net/stocks/charts/NVDA/nvidia/pe-ratio")

    kf = KoyfinAdapter()
    assert kf.is_automated_allowed is False
    with pytest.raises(ManualBrowserOnlyError, match="MANUAL_BROWSER_ONLY: Koyfin"):
        kf.fetch_page("https://app.koyfin.com/chart/NVDA")


def test_macrotrends_manual_intake_valid():
    """Verify audited manual snapshot intake works for Macrotrends."""
    now = datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)
    snapshot = MacrotrendsAdapter.intake_manual_snapshot(
        symbol="NVDA",
        source_url="https://www.macrotrends.net/stocks/charts/NVDA/nvidia/pe-ratio",
        verified_facts=["Historical 10-year median P/E was 38.5"],
        observed_at=now,
    )
    assert snapshot["symbol"] == "NVDA"
    assert snapshot["source_tier"] == TIER_MANUAL_BROWSER_ONLY
    assert snapshot["provenance"]["mode"] == "manual_browser_intake"


# ---------------------------------------------------------------------------
# FreeSourceCoordinator & Discrepancy Detection Tests
# ---------------------------------------------------------------------------

def test_coordinator_refreshes_and_detects_discrepancies(tmp_path: Path):
    """Verify coordinator handles multi-source refresh and detects market cap discrepancies."""
    now = datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc)

    def sec_fetch(url: str):
        if "company_tickers.json" in url:
            return SAMPLE_SEC_TICKERS
        return SAMPLE_SEC_COMPANY_FACTS_NVDA

    sec_ad = SecCompanyFactsAdapter(fetch_json=sec_fetch)
    fv_ad = FinvizAdapter(fetch_text=lambda url: SAMPLE_FINVIZ_HTML)
    sa_ad = StockAnalysisAdapter(fetch_text=lambda url: SAMPLE_STOCK_ANALYSIS_HTML)
    cmc_ad = CompaniesMarketCapAdapter(fetch_text=lambda url: SAMPLE_COMPANIES_MARKET_CAP_HTML)

    coord = FreeSourceCoordinator(
        sec_adapter=sec_ad,
        finviz_adapter=fv_ad,
        stock_analysis_adapter=sa_ad,
        market_cap_adapter=cmc_ad,
    )

    reader = PublicResearchInboxReader(inbox_dir=tmp_path / "inbox")
    res = coord.refresh_symbol("NVDA", now=now, reader=reader)

    assert res["symbol"] == "NVDA"
    assert res["official_facts"]["status"] == "SUCCESS"
    assert res["secondary_finviz"]["status"] == "SUCCESS"
    assert res["secondary_stock_analysis"]["status"] == "SUCCESS"
    assert res["peer_market_cap"]["status"] == "SUCCESS"

    # Discrepancy check: Finviz reports 3.05T, StockAnalysis reports $3.12T
    discrepancies = res["cross_checks"]["discrepancies"]
    assert len(discrepancies) == 1
    assert discrepancies[0]["field"] == "Market Cap"
    assert discrepancies[0]["finviz"] == "3.05T"
    assert discrepancies[0]["stock_analysis"] == "$3.12T"
