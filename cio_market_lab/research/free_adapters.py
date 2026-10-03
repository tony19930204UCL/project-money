"""Bounded Project Money research-data adapters for free and public sources.

Integrates:
- SEC EDGAR Company Facts as official primary evidence (regulatory_filing / official_filing).
- Finviz as secondary discovery / cross-check source only (secondary_cross_check).
- StockAnalysis as secondary discovery / cross-check source only (secondary_cross_check).
- CompaniesMarketCap for slow-changing peer / market-cap reference only (peer_reference, 24h TTL).
- Macrotrends and Koyfin remain manual/browser-only (no lawful stable documented API interface).

Strictly read-only discovery / cross-check data only:
- NEVER executable quotes, broker simulation, fills, or portfolio decisions.
- Preserves provenance, observed_at, source tier, cache/freshness, rate limits, deterministic error states, and fixture rejection.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field

from cio_market_lab.research.browser import PublicResearchEvidence, PublicResearchInboxReader


USER_AGENT = "CIO Market Lab paper-research/1.0 (public research; research@example.org)"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANY_FACTS_URL_TEMPLATE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
FINVIZ_QUOTE_URL_TEMPLATE = "https://finviz.com/quote.ashx?t={symbol}"
STOCK_ANALYSIS_URL_TEMPLATE = "https://stockanalysis.com/stocks/{symbol}/"
COMPANIES_MARKET_CAP_URL_TEMPLATE = "https://companiesmarketcap.com/{symbol}/marketcap/"

# Source tier constants
TIER_OFFICIAL_FILING = "official_filing"
TIER_REGULATORY_FILING = "regulatory_filing"
TIER_OFFICIAL_EXCHANGE = "official_exchange"
TIER_SECONDARY_CROSS_CHECK = "secondary_cross_check"
TIER_PEER_REFERENCE = "peer_reference"
TIER_MANUAL_BROWSER_ONLY = "manual_browser_only"


class ManualBrowserOnlyError(RuntimeError):
    """Raised when an automated request is made to a manual/browser-only source."""
    pass


class RateLimiter:
    """Thread-safe rate limiter with minimum interval between calls."""

    def __init__(self, min_interval_seconds: float = 0.1):
        self.min_interval = min_interval_seconds
        self._last_call: float = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_call = time.monotonic()


def _default_fetch_json(url: str, timeout: float = 8.0, headers: Optional[Dict[str, str]] = None) -> Any:
    req_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    req = Request(url, headers=req_headers)
    with urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise ValueError(f"HTTP_{resp.status}")
        raw = resp.read(8_000_001)
        return json.loads(raw)


def _default_fetch_text(url: str, timeout: float = 8.0, headers: Optional[Dict[str, str]] = None) -> str:
    req_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if headers:
        req_headers.update(headers)
    req = Request(url, headers=req_headers)
    with urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise ValueError(f"HTTP_{resp.status}")
        raw = resp.read(2_000_001)
        return raw.decode("utf-8", errors="replace")


def is_fixture_symbol(symbol: str) -> bool:
    """Strictly identify and reject test fixtures and mock tickers."""
    sym = symbol.strip().upper()
    if sym.startswith(("FIXTURE", "MOCK", "FAKE", "SYNTHETIC", "TEST")):
        return True
    if any(p in sym.lower() for p in ("mock", "fake", "fixture", "synthetic", "test")):
        return True
    return False


class BaseFreeSourceAdapter:
    """Base class for free/public research source adapters with rate limits and caching."""

    def __init__(
        self,
        ttl_seconds: int = 1800,
        min_interval_seconds: float = 0.1,
    ):
        self.ttl_seconds = ttl_seconds
        self.rate_limiter = RateLimiter(min_interval_seconds)
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def _get_cached(self, key: str) -> Optional[Any]:
        with self._lock:
            item = self._cache.get(key)
            if item is None:
                return None
            cached_time, val = item
            if time.monotonic() - cached_time < self.ttl_seconds:
                if isinstance(val, Exception):
                    raise val
                return val
            return None

    def _set_cache(self, key: str, value: Any, is_error: bool = False) -> None:
        with self._lock:
            # Negative error cache is shorter (60 seconds)
            expiry_delta = (self.ttl_seconds - 60) if is_error else 0
            self._cache[key] = (time.monotonic() - expiry_delta, value)

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()


class SecCompanyFactsAdapter(BaseFreeSourceAdapter):
    """Official primary evidence adapter: SEC EDGAR Company Facts.
    
    Produces verified PublicResearchEvidence adhering strictly to public_research_schema.json
    and browser.py verification rules (source_tier='official_filing', research_scope='historical_company_facts_not_catalyst').
    """

    SUPPORTED_GAAP_CONCEPTS = (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
        "NetIncomeLoss",
        "OperatingIncomeLoss",
        "GrossProfit",
        "StockholdersEquity",
        "Assets",
        "EarningsPerShareBasic",
    )

    def __init__(
        self,
        fetch_json: Callable[[str], Any] = _default_fetch_json,
        ttl_seconds: int = 1800,
        min_interval_seconds: float = 0.1,
    ):
        super().__init__(ttl_seconds=ttl_seconds, min_interval_seconds=min_interval_seconds)
        self.fetch_json = fetch_json
        self._ticker_map: Optional[Dict[str, int]] = None

    def _load_ticker_map(self) -> Dict[str, int]:
        cached = self._get_cached("sec_ticker_map")
        if cached is not None:
            return cached
        self.rate_limiter.wait()
        try:
            raw = self.fetch_json(SEC_TICKERS_URL)
            mapping: Dict[str, int] = {}
            for v in raw.values():
                mapping[str(v["ticker"]).upper()] = int(v["cik_str"])
            self._set_cache("sec_ticker_map", mapping)
            return mapping
        except Exception as exc:
            self._set_cache("sec_ticker_map", exc, is_error=True)
            raise

    def acquire_facts(
        self,
        symbol: str,
        now: datetime,
        reader: Optional[PublicResearchInboxReader] = None,
    ) -> Dict[str, Any]:
        """Fetch and extract genuine primary XBRL company facts for an authoritative US symbol."""
        sym = symbol.strip().upper()
        if is_fixture_symbol(sym):
            return {
                "symbol": sym,
                "status": "REJECTED_FIXTURE",
                "evidence_ids": [],
                "gaps": [{"symbol": sym, "reason": "REJECTED_FIXTURE: Test fixture symbol rejected"}],
            }

        now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        evidence_ids: List[str] = []
        gaps: List[Dict[str, str]] = []

        try:
            ticker_map = self._load_ticker_map()
        except Exception as exc:
            return {
                "symbol": sym,
                "status": "SEC_TICKERS_FETCH_FAILED",
                "evidence_ids": [],
                "gaps": [{"symbol": sym, "reason": f"SEC_TICKERS_FETCH_FAILED:{type(exc).__name__}"}],
            }

        cik = ticker_map.get(sym)
        if not cik:
            return {
                "symbol": sym,
                "status": "SEC_CIK_NOT_FOUND",
                "evidence_ids": [],
                "gaps": [{"symbol": sym, "reason": f"SEC_CIK_NOT_FOUND: Symbol {sym} not found in official SEC directory"}],
            }

        facts_url = SEC_COMPANY_FACTS_URL_TEMPLATE.format(cik=cik)
        cached_facts = self._get_cached(facts_url)
        if cached_facts is None:
            self.rate_limiter.wait()
            try:
                data = self.fetch_json(facts_url)
                self._set_cache(facts_url, data)
            except Exception as exc:
                self._set_cache(facts_url, exc, is_error=True)
                return {
                    "symbol": sym,
                    "status": "SEC_FACTS_FETCH_FAILED",
                    "evidence_ids": [],
                    "gaps": [{"symbol": sym, "reason": f"SEC_FACTS_FETCH_FAILED:{type(exc).__name__}"}],
                }
        else:
            data = cached_facts

        # Support both full XBRL Company Facts or fallback Submissions format
        extracted_facts: List[Dict[str, Any]] = []

        if isinstance(data, dict) and "facts" in data:
            # Standard XBRL Company Facts JSON
            us_gaap = data.get("facts", {}).get("us-gaap", {})
            entity_name = str(data.get("entityName", sym))
            for concept in self.SUPPORTED_GAAP_CONCEPTS:
                if concept not in us_gaap:
                    continue
                concept_data = us_gaap[concept]
                label = concept_data.get("label", concept)
                units = concept_data.get("units", {})
                for unit_key, unit_facts in units.items():
                    if not isinstance(unit_facts, list):
                        continue
                    # Filter for 10-K and 10-Q forms, sort by filed date descending
                    candidates = [
                        f for f in unit_facts
                        if f.get("form") in {"10-K", "10-Q", "20-F", "8-K"} and f.get("filed")
                    ]
                    if not candidates:
                        continue
                    latest_fact = max(candidates, key=lambda f: str(f.get("filed", "")))
                    filed_str = str(latest_fact.get("filed"))
                    try:
                        filed_dt = datetime.fromisoformat(filed_str).replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    age = (now_utc - filed_dt).total_seconds()
                    # Up to 730 days allowed for historical company facts
                    if 0 <= age <= 730 * 86400:
                        val = latest_fact.get("val")
                        form = latest_fact.get("form", "10-Q")
                        fy = latest_fact.get("fy", "")
                        fp = latest_fact.get("fp", "")
                        accn = latest_fact.get("accn", "0000000000-00-000000")
                        extracted_facts.append({
                            "concept": concept,
                            "label": label,
                            "val": val,
                            "unit": unit_key,
                            "form": form,
                            "fy": fy,
                            "fp": fp,
                            "filed": filed_str,
                            "accn": accn,
                            "entity_name": entity_name,
                        })
                        break
        elif isinstance(data, dict) and "filings" in data:
            # Fallback SEC Submissions structure
            recent = data.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            dates = recent.get("filingDate", [])
            accessions = recent.get("accessionNumber", [])
            for idx, form in enumerate(forms[:40]):
                if form in {"10-K", "10-Q", "8-K", "6-K", "20-F"}:
                    f_date = dates[idx] if idx < len(dates) else ""
                    acc = accessions[idx] if idx < len(accessions) else ""
                    try:
                        f_dt = datetime.fromisoformat(f_date).replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    age = (now_utc - f_dt).total_seconds()
                    if 0 <= age <= 730 * 86400:
                        extracted_facts.append({
                            "concept": f"Form_{form}",
                            "label": f"SEC Form {form}",
                            "val": f"Filing {acc}",
                            "unit": "filing",
                            "form": form,
                            "fy": "",
                            "fp": "",
                            "filed": f_date,
                            "accn": acc,
                            "entity_name": sym,
                        })
                        break

        if not extracted_facts:
            gaps.append({"symbol": sym, "reason": "NO_FRESH_SUPPORTED_OFFICIAL_ITEM"})
            return {
                "symbol": sym,
                "status": "NO_FRESH_SUPPORTED_OFFICIAL_ITEM",
                "evidence_ids": [],
                "gaps": gaps,
            }

        # Build PublicResearchEvidence items
        for fact in extracted_facts:
            clean_accn = re.sub(r"[^A-Za-z0-9]", "", fact["accn"])
            concept_slug = re.sub(r"[^A-Za-z0-9]", "", fact["concept"].lower())
            research_id = f"sec-facts-{cik:010d}-{concept_slug}-{clean_accn[:10]}"
            verified_fact_text = (
                f"SEC EDGAR Company Facts for {sym} (CIK {cik:010d}, {fact['entity_name']}): "
                f"{fact['label']} = {fact['val']} {fact['unit']} "
                f"(Form {fact['form']}, FY{fact['fy']} {fact['fp']}, filed {fact['filed']}, accession {fact['accn']})."
            )
            item = {
                "research_id": research_id,
                "symbol": sym,
                "source_url": facts_url,
                "source_tier": TIER_OFFICIAL_FILING,
                "observed_at": now_utc.isoformat(),
                "published_at": fact["filed"],
                "is_fixture": False,
                "verification_status": "verified",
                "verified_facts": [verified_fact_text],
                "research_scope": "historical_company_facts_not_catalyst",
                "limitations": [
                    "Official SEC EDGAR XBRL company facts; historical filing record only.",
                    "Not an immediate order catalyst or valuation direction inference.",
                    "Filing date precision is one day.",
                ],
                "raw_metadata": {
                    "source": "official SEC EDGAR Company Facts",
                    "cik": cik,
                    "entity_name": fact["entity_name"],
                    "concept": fact["concept"],
                    "val": fact["val"],
                    "unit": fact["unit"],
                    "form": fact["form"],
                    "fy": fact["fy"],
                    "fp": fact["fp"],
                    "accession": fact["accn"],
                },
            }

            if reader is not None:
                ok, reason = reader.add_evidence(item, now=now)
                if ok:
                    evidence_ids.append(research_id)
                else:
                    gaps.append({"symbol": sym, "reason": reason})
            else:
                evidence_ids.append(research_id)

        return {
            "symbol": sym,
            "status": "SUCCESS" if evidence_ids else "NO_VERIFIED_EVIDENCE",
            "evidence_ids": evidence_ids,
            "gaps": gaps,
            "extracted_count": len(extracted_facts),
        }


class FinvizAdapter(BaseFreeSourceAdapter):
    """Secondary discovery & cross-check source: Finviz.
    
    Strictly secondary discovery / screening / cross-check input.
    NEVER treated as an official regulatory filing, and NEVER an executable quote.
    """

    def __init__(
        self,
        fetch_text: Callable[[str], str] = _default_fetch_text,
        ttl_seconds: int = 1800,
        min_interval_seconds: float = 1.0,
    ):
        super().__init__(ttl_seconds=ttl_seconds, min_interval_seconds=min_interval_seconds)
        self.fetch_text = fetch_text

    def parse_metrics(self, html: str, symbol: str) -> Dict[str, Any]:
        """Extract key fundamental and screening metrics from Finviz HTML snapshot."""
        metrics: Dict[str, Any] = {}
        # Regex extraction of standard Finviz table cells (label and value pairs)
        # e.g. <td class="snapshot-td2-cp">P/E</td><td class="snapshot-td2"><b>28.45</b></td>
        pattern = re.compile(
            r'<td[^>]*class="[^"]*snapshot-td2[^"]*"[^>]*>([^<]+)</td>\s*<td[^>]*class="[^"]*snapshot-td2[^"]*"[^>]*>(?:<b[^>]*>)?([^<]+)(?:</b>)?</td>',
            re.IGNORECASE,
        )
        for match in pattern.finditer(html):
            key = match.group(1).strip()
            val = match.group(2).strip()
            if key and val and val != "-":
                metrics[key] = val

        # Sector and Industry extraction
        # e.g. <a href="screener.ashx?v=111&f=sec_technology" class="tab-link">Technology</a>
        sector_match = re.search(r'f=sec_[^"]*"[^>]*class="tab-link"[^>]*>([^<]+)</a>', html)
        if sector_match:
            metrics["Sector"] = sector_match.group(1).strip()

        industry_match = re.search(r'f=ind_[^"]*"[^>]*class="tab-link"[^>]*>([^<]+)</a>', html)
        if industry_match:
            metrics["Industry"] = industry_match.group(1).strip()

        return metrics

    def acquire(
        self,
        symbol: str,
        now: datetime,
        reader: Optional[PublicResearchInboxReader] = None,
    ) -> Dict[str, Any]:
        sym = symbol.strip().upper()
        if is_fixture_symbol(sym):
            return {
                "symbol": sym,
                "status": "REJECTED_FIXTURE",
                "source_tier": TIER_SECONDARY_CROSS_CHECK,
                "record": None,
                "gaps": [{"symbol": sym, "reason": "REJECTED_FIXTURE: Fixture symbol rejected"}],
            }

        now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        url = FINVIZ_QUOTE_URL_TEMPLATE.format(symbol=sym)

        cached = self._get_cached(url)
        if cached is None:
            self.rate_limiter.wait()
            try:
                html = self.fetch_text(url)
                self._set_cache(url, html)
            except Exception as exc:
                self._set_cache(url, exc, is_error=True)
                return {
                    "symbol": sym,
                    "status": f"FINVIZ_FETCH_FAILED:{type(exc).__name__}",
                    "source_tier": TIER_SECONDARY_CROSS_CHECK,
                    "record": None,
                    "gaps": [{"symbol": sym, "reason": f"FINVIZ_FETCH_FAILED:{exc}"}],
                }
        else:
            html = cached

        metrics = self.parse_metrics(html, sym)
        if not metrics:
            return {
                "symbol": sym,
                "status": "FINVIZ_NO_METRICS",
                "source_tier": TIER_SECONDARY_CROSS_CHECK,
                "record": None,
                "gaps": [{"symbol": sym, "reason": "FINVIZ_NO_METRICS: Unable to parse screening metrics"}],
            }

        verified_facts = [
            f"Finviz secondary cross-check for {sym}: "
            f"Sector='{metrics.get('Sector', 'Unknown')}', "
            f"Market Cap='{metrics.get('Market Cap', 'Unknown')}', "
            f"P/E='{metrics.get('P/E', 'N/A')}', "
            f"Fwd P/E='{metrics.get('Forward P/E', 'N/A')}'."
        ]

        record = {
            "research_id": f"finviz-{sym.lower()}-{now_utc.strftime('%Y%m%d')}",
            "symbol": sym,
            "source_url": url,
            "source_name": "finviz",
            "source_tier": TIER_SECONDARY_CROSS_CHECK,
            "observed_at": now_utc.isoformat(),
            "is_fixture": False,
            "metrics": metrics,
            "verified_facts": verified_facts,
            "research_scope": "secondary_discovery_cross_check",
            "limitations": [
                "Secondary discovery and cross-check input only.",
                "Non-authoritative secondary source; may not override SEC filings or executable market data.",
                "Unverified financial media aggregator; not an official regulatory filing.",
                "NEVER an executable market quote, fill reference, or valuation benchmark.",
            ],
            "provenance": {
                "source": "Finviz public snapshot",
                "url": url,
                "tier": TIER_SECONDARY_CROSS_CHECK,
                "authoritative": False,
                "overrides_official": False,
                "overrides_executable_market_data": False,
            },
        }

        if reader is not None:
            stage_item = {
                "research_id": record["research_id"],
                "symbol": sym,
                "source_url": url,
                "source_tier": TIER_SECONDARY_CROSS_CHECK,
                "observed_at": now_utc.isoformat(),
                "published_at": now_utc.date().isoformat(),
                "is_fixture": False,
                "verification_status": "verified",
                "verified_facts": verified_facts,
                "research_scope": "secondary_discovery_cross_check",
                "limitations": record["limitations"],
                "raw_metadata": record["provenance"],
            }
            reader.add_evidence(stage_item, now=now)

        return {
            "symbol": sym,
            "status": "SUCCESS",
            "source_tier": TIER_SECONDARY_CROSS_CHECK,
            "record": record,
            "gaps": [],
        }


class StockAnalysisAdapter(BaseFreeSourceAdapter):
    """Secondary discovery & cross-check source: StockAnalysis.
    
    Secondary discovery / financial metrics cross-check source only.
    NEVER treated as an official regulatory filing or executable quote.
    """

    def __init__(
        self,
        fetch_text: Callable[[str], str] = _default_fetch_text,
        ttl_seconds: int = 1800,
        min_interval_seconds: float = 1.0,
    ):
        super().__init__(ttl_seconds=ttl_seconds, min_interval_seconds=min_interval_seconds)
        self.fetch_text = fetch_text

    def parse_overview(self, html: str, symbol: str) -> Dict[str, Any]:
        """Extract overview and key statistics from StockAnalysis HTML."""
        metrics: Dict[str, Any] = {}
        # StockAnalysis key metric tables typically have data-test attributes or standard td/span pairs
        # e.g. <tr><td>Market Cap</td><td>$3.25T</td></tr> or similar
        row_pattern = re.compile(
            r'<tr[^>]*>\s*<td[^>]*>([^<]+)</td>\s*<td[^>]*>([^<]+)</td>\s*</tr>',
            re.IGNORECASE,
        )
        for match in row_pattern.finditer(html):
            k = match.group(1).strip()
            v = match.group(2).strip()
            if k and v and v != "-":
                metrics[k] = v

        # Fallback regex for label / value spans
        span_pattern = re.compile(
            r'<div[^>]*class="[^"]*font-semibold[^"]*"[^>]*>([^<]+)</div>\s*<div[^>]*>([^<]+)</div>',
            re.IGNORECASE,
        )
        for match in span_pattern.finditer(html):
            k = match.group(1).strip()
            v = match.group(2).strip()
            if k and v and k not in metrics:
                metrics[k] = v

        return metrics

    def acquire(
        self,
        symbol: str,
        now: datetime,
        reader: Optional[PublicResearchInboxReader] = None,
    ) -> Dict[str, Any]:
        sym = symbol.strip().upper()
        if is_fixture_symbol(sym):
            return {
                "symbol": sym,
                "status": "REJECTED_FIXTURE",
                "source_tier": TIER_SECONDARY_CROSS_CHECK,
                "record": None,
                "gaps": [{"symbol": sym, "reason": "REJECTED_FIXTURE: Fixture symbol rejected"}],
            }

        now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        url = STOCK_ANALYSIS_URL_TEMPLATE.format(symbol=sym.lower())

        cached = self._get_cached(url)
        if cached is None:
            self.rate_limiter.wait()
            try:
                html = self.fetch_text(url)
                self._set_cache(url, html)
            except Exception as exc:
                self._set_cache(url, exc, is_error=True)
                return {
                    "symbol": sym,
                    "status": f"STOCK_ANALYSIS_FETCH_FAILED:{type(exc).__name__}",
                    "source_tier": TIER_SECONDARY_CROSS_CHECK,
                    "record": None,
                    "gaps": [{"symbol": sym, "reason": f"STOCK_ANALYSIS_FETCH_FAILED:{exc}"}],
                }
        else:
            html = cached

        metrics = self.parse_overview(html, sym)
        if not metrics:
            return {
                "symbol": sym,
                "status": "STOCK_ANALYSIS_NO_METRICS",
                "source_tier": TIER_SECONDARY_CROSS_CHECK,
                "record": None,
                "gaps": [{"symbol": sym, "reason": "STOCK_ANALYSIS_NO_METRICS: Unable to parse metrics table"}],
            }

        verified_facts = [
            f"StockAnalysis secondary cross-check for {sym}: "
            f"Market Cap='{metrics.get('Market Cap', metrics.get('Market Cap (intraday)', 'N/A'))}', "
            f"PE Ratio='{metrics.get('PE Ratio', metrics.get('P/E', 'N/A'))}', "
            f"Revenue='{metrics.get('Revenue', metrics.get('Revenue (ttm)', 'N/A'))}'."
        ]

        record = {
            "research_id": f"stockanalysis-{sym.lower()}-{now_utc.strftime('%Y%m%d')}",
            "symbol": sym,
            "source_url": url,
            "source_name": "stock_analysis",
            "source_tier": TIER_SECONDARY_CROSS_CHECK,
            "observed_at": now_utc.isoformat(),
            "is_fixture": False,
            "metrics": metrics,
            "verified_facts": verified_facts,
            "research_scope": "secondary_discovery_cross_check",
            "limitations": [
                "Secondary discovery and cross-check input only.",
                "Non-authoritative secondary source; may not override SEC filings or executable market data.",
                "Third-party aggregator source; not an official regulatory filing.",
                "NEVER an executable market quote or execution benchmark.",
            ],
            "provenance": {
                "source": "StockAnalysis public overview",
                "url": url,
                "tier": TIER_SECONDARY_CROSS_CHECK,
                "authoritative": False,
                "overrides_official": False,
                "overrides_executable_market_data": False,
            },
        }

        if reader is not None:
            stage_item = {
                "research_id": record["research_id"],
                "symbol": sym,
                "source_url": url,
                "source_tier": TIER_SECONDARY_CROSS_CHECK,
                "observed_at": now_utc.isoformat(),
                "published_at": now_utc.date().isoformat(),
                "is_fixture": False,
                "verification_status": "verified",
                "verified_facts": verified_facts,
                "research_scope": "secondary_discovery_cross_check",
                "limitations": record["limitations"],
                "raw_metadata": record["provenance"],
            }
            reader.add_evidence(stage_item, now=now)

        return {
            "symbol": sym,
            "status": "SUCCESS",
            "source_tier": TIER_SECONDARY_CROSS_CHECK,
            "record": record,
            "gaps": [],
        }


class CompaniesMarketCapAdapter(BaseFreeSourceAdapter):
    """Slow-changing peer / market-cap reference adapter: CompaniesMarketCap.
    
    Slow TTL (24h / 86400s) for peer rank and global market-cap reference only.
    NEVER treated as an intraday quote or executable price.
    """

    def __init__(
        self,
        fetch_text: Callable[[str], str] = _default_fetch_text,
        ttl_seconds: int = 86400,  # 24 hours
        min_interval_seconds: float = 1.0,
    ):
        super().__init__(ttl_seconds=ttl_seconds, min_interval_seconds=min_interval_seconds)
        self.fetch_text = fetch_text

    def parse_market_cap(self, html: str, symbol: str) -> Dict[str, Any]:
        """Extract global market cap rank and valuation from CompaniesMarketCap HTML."""
        data: Dict[str, Any] = {}
        # Rank extraction e.g. <div class="ranking-number">#1</div>
        rank_match = re.search(r'class="ranking-number"[^>]*>#?(\d+)</div>', html)
        if rank_match:
            data["rank"] = int(rank_match.group(1))

        # Market cap value e.g. <div class="marketcap-value">$3.250 T</div>
        cap_match = re.search(r'class="marketcap-value"[^>]*>([^<]+)</div>', html)
        if cap_match:
            data["market_cap_formatted"] = cap_match.group(1).strip()

        # Company name
        name_match = re.search(r'class="company-name"[^>]*>([^<]+)</div>', html)
        if name_match:
            data["company_name"] = name_match.group(1).strip()

        return data

    def acquire(
        self,
        symbol: str,
        now: datetime,
        reader: Optional[PublicResearchInboxReader] = None,
    ) -> Dict[str, Any]:
        sym = symbol.strip().upper()
        if is_fixture_symbol(sym):
            return {
                "symbol": sym,
                "status": "REJECTED_FIXTURE",
                "source_tier": TIER_PEER_REFERENCE,
                "record": None,
                "gaps": [{"symbol": sym, "reason": "REJECTED_FIXTURE: Fixture symbol rejected"}],
            }

        now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        url = COMPANIES_MARKET_CAP_URL_TEMPLATE.format(symbol=sym.lower())

        cached = self._get_cached(url)
        if cached is None:
            self.rate_limiter.wait()
            try:
                html = self.fetch_text(url)
                self._set_cache(url, html)
            except Exception as exc:
                self._set_cache(url, exc, is_error=True)
                return {
                    "symbol": sym,
                    "status": f"COMPANIES_MARKET_CAP_FETCH_FAILED:{type(exc).__name__}",
                    "source_tier": TIER_PEER_REFERENCE,
                    "record": None,
                    "gaps": [{"symbol": sym, "reason": f"COMPANIES_MARKET_CAP_FETCH_FAILED:{exc}"}],
                }
        else:
            html = cached

        data = self.parse_market_cap(html, sym)
        if not data:
            return {
                "symbol": sym,
                "status": "COMPANIES_MARKET_CAP_NO_DATA",
                "source_tier": TIER_PEER_REFERENCE,
                "record": None,
                "gaps": [{"symbol": sym, "reason": "COMPANIES_MARKET_CAP_NO_DATA: Unable to extract rank or market cap"}],
            }

        verified_facts = [
            f"CompaniesMarketCap peer reference for {sym}: "
            f"Global Rank #{data.get('rank', 'N/A')}, "
            f"Market Cap Reference={data.get('market_cap_formatted', 'N/A')}."
        ]

        record = {
            "research_id": f"companiesmarketcap-{sym.lower()}-{now_utc.strftime('%Y%m%d')}",
            "symbol": sym,
            "source_url": url,
            "source_name": "companies_market_cap",
            "source_tier": TIER_PEER_REFERENCE,
            "observed_at": now_utc.isoformat(),
            "is_fixture": False,
            "metrics": data,
            "verified_facts": verified_facts,
            "research_scope": "slow_peer_reference_only",
            "limitations": [
                "Slow-changing peer reference only; 24h caching window.",
                "Non-authoritative secondary reference; may not override SEC filings or executable market data.",
                "Not an intraday market quote, not an official regulatory filing.",
                "Never used for execution or mark-to-market pricing.",
            ],
            "provenance": {
                "source": "CompaniesMarketCap global rankings",
                "url": url,
                "tier": TIER_PEER_REFERENCE,
                "authoritative": False,
                "overrides_official": False,
                "overrides_executable_market_data": False,
            },
        }

        if reader is not None:
            stage_item = {
                "research_id": record["research_id"],
                "symbol": sym,
                "source_url": url,
                "source_tier": TIER_PEER_REFERENCE,
                "observed_at": now_utc.isoformat(),
                "published_at": now_utc.date().isoformat(),
                "is_fixture": False,
                "verification_status": "verified",
                "verified_facts": verified_facts,
                "research_scope": "slow_peer_reference_only",
                "limitations": record["limitations"],
                "raw_metadata": record["provenance"],
            }
            reader.add_evidence(stage_item, now=now)

        return {
            "symbol": sym,
            "status": "SUCCESS",
            "source_tier": TIER_PEER_REFERENCE,
            "record": record,
            "gaps": [],
        }


class MacrotrendsAdapter:
    """Explicit manual/browser-only gateway for Macrotrends.
    
    Macrotrends employs anti-bot protection and lacks a lawful stable public API.
    Automated calls are explicitly blocked to prevent ToS / scraping violations.
    Only audited manual/browser intake is supported.
    """

    is_automated_allowed: bool = False

    def fetch_page(self, *args: Any, **kwargs: Any) -> Any:
        raise ManualBrowserOnlyError(
            "MANUAL_BROWSER_ONLY: Macrotrends has no lawful stable documented public API interface. "
            "Automated scraping is disabled to prevent ToS / Cloudflare violations. "
            "Browser-only manual research intake is required."
        )

    @classmethod
    def intake_manual_snapshot(
        cls,
        symbol: str,
        source_url: str,
        verified_facts: List[str],
        observed_at: datetime,
        limitations: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Ingest an audited manual browser snapshot with strict provenance."""
        sym = symbol.strip().upper()
        if is_fixture_symbol(sym):
            raise ValueError("REJECTED_FIXTURE: Fixture symbol not allowed in manual intake")
        now_utc = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=timezone.utc)
        return {
            "research_id": f"macrotrends-manual-{sym.lower()}-{now_utc.strftime('%Y%m%d%H%M')}",
            "symbol": sym,
            "source_url": source_url,
            "source_name": "macrotrends",
            "source_tier": TIER_MANUAL_BROWSER_ONLY,
            "observed_at": now_utc.isoformat(),
            "is_fixture": False,
            "verified_facts": list(verified_facts),
            "limitations": limitations or ["Manual browser export; not an automated or real-time feed."],
            "provenance": {"mode": "manual_browser_intake", "source": "macrotrends.net"},
        }


class KoyfinAdapter:
    """Explicit manual/browser-only gateway for Koyfin.
    
    Koyfin requires user authentication / paid subscription without a lawful public REST API.
    Automated calls are explicitly blocked to protect account safety and ToS compliance.
    """

    is_automated_allowed: bool = False

    def fetch_page(self, *args: Any, **kwargs: Any) -> Any:
        raise ManualBrowserOnlyError(
            "MANUAL_BROWSER_ONLY: Koyfin has no lawful stable documented public API interface. "
            "Automated scraping is disabled to protect credentials and comply with ToS. "
            "Browser-only manual research intake is required."
        )

    @classmethod
    def intake_manual_snapshot(
        cls,
        symbol: str,
        source_url: str,
        verified_facts: List[str],
        observed_at: datetime,
        limitations: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Ingest an audited manual browser snapshot with strict provenance."""
        sym = symbol.strip().upper()
        if is_fixture_symbol(sym):
            raise ValueError("REJECTED_FIXTURE: Fixture symbol not allowed in manual intake")
        now_utc = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=timezone.utc)
        return {
            "research_id": f"koyfin-manual-{sym.lower()}-{now_utc.strftime('%Y%m%d%H%M')}",
            "symbol": sym,
            "source_url": source_url,
            "source_name": "koyfin",
            "source_tier": TIER_MANUAL_BROWSER_ONLY,
            "observed_at": now_utc.isoformat(),
            "is_fixture": False,
            "verified_facts": list(verified_facts),
            "limitations": limitations or ["Manual browser export; not an automated feed."],
            "provenance": {"mode": "manual_browser_intake", "source": "koyfin.com"},
        }


class FreeSourceCoordinator:
    """Unified coordinator managing primary official evidence, secondary discovery, and peer references."""

    def __init__(
        self,
        sec_adapter: Optional[SecCompanyFactsAdapter] = None,
        finviz_adapter: Optional[FinvizAdapter] = None,
        stock_analysis_adapter: Optional[StockAnalysisAdapter] = None,
        market_cap_adapter: Optional[CompaniesMarketCapAdapter] = None,
    ):
        self.sec_adapter = sec_adapter or SecCompanyFactsAdapter()
        self.finviz_adapter = finviz_adapter or FinvizAdapter()
        self.stock_analysis_adapter = stock_analysis_adapter or StockAnalysisAdapter()
        self.market_cap_adapter = market_cap_adapter or CompaniesMarketCapAdapter()
        self.macrotrends = MacrotrendsAdapter()
        self.koyfin = KoyfinAdapter()

        self._secondary_inbox: Dict[str, List[Dict[str, Any]]] = {}
        self._gaps: List[Dict[str, str]] = []

    def refresh_symbol(
        self,
        symbol: str,
        now: datetime,
        reader: Optional[PublicResearchInboxReader] = None,
    ) -> Dict[str, Any]:
        """Bounded research refresh for a single symbol across official and secondary sources."""
        sym = symbol.strip().upper()
        results: Dict[str, Any] = {
            "symbol": sym,
            "official_facts": None,
            "secondary_finviz": None,
            "secondary_stock_analysis": None,
            "peer_market_cap": None,
            "gaps": [],
            "cross_checks": {},
        }

        # 1. Primary official evidence: SEC EDGAR Company Facts for US symbols
        if not sym.endswith((".TW", ".TWO")):
            sec_res = self.sec_adapter.acquire_facts(sym, now, reader=reader)
            results["official_facts"] = sec_res
            results["gaps"].extend(sec_res.get("gaps", []))

        # 2. Secondary discovery / cross-check: Finviz
        fv_res = self.finviz_adapter.acquire(sym, now, reader=reader)
        results["secondary_finviz"] = fv_res
        if fv_res.get("record"):
            self._secondary_inbox.setdefault(sym, []).append(fv_res["record"])
        results["gaps"].extend(fv_res.get("gaps", []))

        # 3. Secondary discovery / cross-check: StockAnalysis
        sa_res = self.stock_analysis_adapter.acquire(sym, now, reader=reader)
        results["secondary_stock_analysis"] = sa_res
        if sa_res.get("record"):
            self._secondary_inbox.setdefault(sym, []).append(sa_res["record"])
        results["gaps"].extend(sa_res.get("gaps", []))

        # 4. Slow-changing peer reference: CompaniesMarketCap
        cmc_res = self.market_cap_adapter.acquire(sym, now, reader=reader)
        results["peer_market_cap"] = cmc_res
        if cmc_res.get("record"):
            self._secondary_inbox.setdefault(sym, []).append(cmc_res["record"])
        results["gaps"].extend(cmc_res.get("gaps", []))

        # 5. Cross-check analysis between secondary sources
        cross_checks: Dict[str, Any] = {"discrepancies": []}
        fv_metrics = (fv_res.get("record") or {}).get("metrics", {})
        sa_metrics = (sa_res.get("record") or {}).get("metrics", {})

        fv_mktcap = fv_metrics.get("Market Cap")
        sa_mktcap = sa_metrics.get("Market Cap", sa_metrics.get("Market Cap (intraday)"))
        if fv_mktcap and sa_mktcap and fv_mktcap != sa_mktcap:
            cross_checks["discrepancies"].append({
                "field": "Market Cap",
                "finviz": fv_mktcap,
                "stock_analysis": sa_mktcap,
                "note": "Aggregator market cap discrepancy observed; official filings remain primary authority.",
            })

        results["cross_checks"] = cross_checks
        return results

    def refresh_universe(
        self,
        symbols: List[str],
        now: datetime,
        reader: Optional[PublicResearchInboxReader] = None,
    ) -> Dict[str, Any]:
        """Batch bounded refresh for universe symbols."""
        summary: Dict[str, Any] = {
            "timestamp": (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).isoformat(),
            "symbols_processed": len(symbols),
            "results_by_symbol": {},
            "all_gaps": [],
        }
        for s in symbols:
            res = self.refresh_symbol(s, now, reader=reader)
            summary["results_by_symbol"][s] = res
            summary["all_gaps"].extend(res["gaps"])
        return summary
