"""Bounded read-only discovery of official public filing metadata and TWSE revenue.

This produces evidence for the existing PublicResearchInboxReader, never decisions.
Remote text is data only; no instructions or market inferences are accepted.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
import time
from typing import Any, Callable
from urllib.request import Request, urlopen


TW_REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TW_BALANCE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap07_L_ci"
TW_FINANCIAL_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
USER_AGENT = "CIO Market Lab paper-research/1.0 (public research; research@example.org)"


def _public_json(url: str) -> Any:
    from urllib.parse import urlparse
    from cio_market_lab.research.official_documents import ALLOWED_HOSTS, MAX_BYTES, parse_official_document
    disclosure=urlparse(url).hostname in ALLOWED_HOSTS
    headers={'User-Agent':'Googlebot' if urlparse(url).hostname=='investor.tsmc.com' else USER_AGENT,
             'Accept':'application/json,text/html,application/pdf'}
    with urlopen(Request(url,headers=headers),timeout=25) as response:
        body=response.read(MAX_BYTES+1)
        if response.status!=200 or len(body)>MAX_BYTES:
            raise ValueError('PUBLIC_SOURCE_UNAVAILABLE_OR_OVERSIZE')
        return parse_official_document(url,body,response.headers.get('Content-Type','')) if disclosure else json.loads(body)


from cio_market_lab.research.financial_periods import classify_flow_period, aligned_cash_flow_derivations


class OfficialResearchProducer:
    """Refresh bounded official datasets with a TTL; feed the canonical inlet only."""

    def __init__(self, fetch_json: Callable[[str], Any] = _public_json, ttl_seconds: int = 1800):
        self.fetch_json = fetch_json
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[float, Any]] = {}
        self.last_gaps: list[dict[str, str]] = []

    def _get(self, url: str) -> Any:
        cached = self._cache.get(url)
        if cached and time.monotonic() - cached[0] < self.ttl_seconds:
            if isinstance(cached[1], Exception):
                raise cached[1]
            return cached[1]
        try:
            value = self.fetch_json(url)
        except Exception as exc:
            self._cache[url] = (time.monotonic() - self.ttl_seconds + 60, exc)
            raise
        self._cache[url] = (time.monotonic(), value)
        return value

    @staticmethod
    def _current_fact(rows: list[dict[str, Any]], observed: datetime) -> dict[str, Any] | None:
        """Select a recent, period-comparable filed fact; reject stale/comparative rows."""
        today = observed.date().isoformat()
        eligible = []
        for fact in rows:
            form, filed, end = fact.get("form"), fact.get("filed"), fact.get("end")
            if form not in {"10-K", "10-Q", "20-F"} or not filed or not end or end > today or filed > today:
                continue
            try:
                age_days = (observed.date() - datetime.fromisoformat(end).date()).days
                filed_age = (observed.date() - datetime.fromisoformat(filed).date()).days
            except (TypeError, ValueError):
                continue
            # Annual periods are bounded at 18 months; interim periods at 9 months.
            max_age = 548 if form in {"10-K", "20-F"} else 274
            if age_days < 0 or age_days > max_age or filed_age < 0 or filed_age > 730:
                continue
            start = fact.get("start")
            if start:
                try:
                    duration = (datetime.fromisoformat(end).date() - datetime.fromisoformat(start).date()).days
                except (TypeError, ValueError):
                    continue
                if duration < 60 or duration > 380:
                    continue
                kind = classify_flow_period(start, end)
                allowed = {'annual'} if form in {'10-K', '20-F'} else {'quarterly', 'half_year', 'nine_month'}
                if kind not in allowed:
                    continue
            eligible.append(fact)
        return max(eligible, key=lambda f: (f["end"], f.get("filed", ""), f.get("start", "")), default=None)

    @staticmethod
    def _date(raw: str, roc: bool = False) -> datetime:
        text = str(raw)
        if roc:
            if not re.fullmatch(r"\d{7}", text):
                raise ValueError("INVALID_TWSE_REPORT_DATE")
            text = f"{int(text[:3]) + 1911}-{text[3:5]}-{text[5:]}"
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)

    def _attach_company_disclosures(self, item, gaps):
        """Attach independent official rows without letting one blocked document erase others."""
        symbol=item['symbol']; urls=[]
        metadata=item.setdefault('raw_metadata',{})
        supplements=metadata.setdefault('supplemental_source_rows',[])
        blocked=metadata.setdefault('blocked_official_documents',[])
        if symbol == 'MSFT':
            fy=str(metadata.get('fy',''))
            fp=str(metadata.get('fp',''))
            quarter='4' if fp=='FY' else fp.removeprefix('Q')
            if not re.fullmatch(r'20\d{2}',fy) or quarter not in {'1','2','3','4'}:
                return
            urls=[f'https://www.microsoft.com/en-us/Investor/earnings/FY-{fy}-Q{quarter}/press-release-webcast']
        elif symbol == '2330.TW':
            row=metadata.get('raw_row',{})
            year=str(row.get('年度',''));quarter=str(row.get('季別',''))
            if not re.fullmatch(r'\d{3}',year) or quarter not in {'1','2','3','4'}:
                return
            urls=[f'https://investor.tsmc.com/english/quarterly-results/{int(year)+1911}/q{quarter}']
        for landing_url in urls:
            try:
                landing_rows=self._get(landing_url)
                if not isinstance(landing_rows,list): raise ValueError('INVALID_DISCLOSURE_ROWS')
            except Exception as exc:
                gaps.append({'symbol':symbol,'reason':f'COMPANY_DISCLOSURE_SUPPLEMENT_UNAVAILABLE:{type(exc).__name__}'})
                continue

            # Preserve every independently verified HTML fact before following a
            # linked document. A blocked PDF must not erase already verified HTML.
            for row in landing_rows:
                if row.get('document_part')=='link' or not row.get('text'):
                    continue
                supplements.append({'source_url':landing_url,'raw_row':row})
                item['verified_facts'].append(
                    f"Official company disclosure [{landing_url}, {row['document_part']}]: {row['text']}"
                )

            statement_url=None
            statement_rows=[]
            if symbol=='2330.TW':
                link=next((r for r in landing_rows if r.get('document_part')=='link' and r.get('text')=='Financial Statements'),None)
                if not link:
                    blocked.append({'source_url':landing_url,'reason':'OFFICIAL_STATEMENT_LINK_MISSING'})
                    gaps.append({'symbol':symbol,'reason':'COMPANY_DISCLOSURE_DOCUMENT_BLOCKED:OFFICIAL_STATEMENT_LINK_MISSING'})
                    continue
                supplements.append({'source_url':landing_url,'raw_row':link})
                statement_url=link['href']
                try:
                    statement_rows=self._get(statement_url)
                    if not isinstance(statement_rows,list):
                        raise ValueError('INVALID_DISCLOSURE_ROWS')
                except Exception as exc:
                    reason=str(exc) if str(exc) in {'PDF_PARSER_UNAVAILABLE','OFFICIAL_DOCUMENT_OVERSIZE'} else type(exc).__name__
                    blocked.append({'source_url':statement_url,'discovered_from':landing_url,'reason':reason})
                    gaps.append({'symbol':symbol,'reason':f'COMPANY_DISCLOSURE_DOCUMENT_BLOCKED:{reason}'})
                    continue
            else:
                statement_url=landing_url
                statement_rows=landing_rows

            for row in statement_rows:
                if row.get('document_part')=='link' or not row.get('text'):
                    continue
                supplements.append({'source_url':statement_url,'raw_row':row})
                item['verified_facts'].append(
                    f"Official company disclosure [{statement_url}, {row['document_part']}]: {row['text']}"
                )

            item['limitations'].append(
                'Company report excerpts retain original headers, reporting units, comparisons and annual/cumulative/quarterly flow labels. '
                'No annualization or valuation inferred. Extraction is limited to first ten PDF pages and supported HTML tables/paragraphs; '
                'unextracted notes remain unknown.'
            )

    def acquire(self, symbols: list[str], reader: Any, now: datetime) -> dict[str, Any]:
        """Return verified counts and explicit source gaps; never claim no-news success."""
        accepted: list[str] = []
        gaps: list[dict[str, str]] = []
        tw_symbols = [s for s in symbols if re.fullmatch(r"\d{4,6}\.TW", s.upper())]
        us_symbols = [s for s in symbols if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", s.upper())]
        tw_rows: list[Any] = []
        tw_financial_rows: list[Any] = []
        ticker_map: dict[str, int] = {}
        if tw_symbols:
            try:
                rows = self._get(TW_REVENUE_URL)
                if not isinstance(rows, list) or len(rows) > 100_000:
                    raise ValueError("INVALID_TWSE_RESPONSE")
                tw_rows = rows
            except Exception as exc:
                gaps.extend({"symbol": s, "reason": f"TWSE_FETCH_FAILED:{type(exc).__name__}"} for s in tw_symbols)
        if tw_symbols:
            try:
                rows = self._get(TW_FINANCIAL_URL)
                if not isinstance(rows, list) or len(rows) > 100_000:
                    raise ValueError("INVALID_TWSE_FINANCIAL_RESPONSE")
                tw_financial_rows = rows
            except Exception as exc:
                gaps.extend({"symbol": s, "reason": f"TWSE_FINANCIAL_FETCH_FAILED:{type(exc).__name__}"} for s in tw_symbols)
        if us_symbols:
            try:
                tickers = self._get(SEC_TICKERS_URL)
                ticker_map = {str(v["ticker"]).upper(): int(v["cik_str"]) for v in tickers.values()}
            except Exception as exc:
                gaps.extend({"symbol": s, "reason": f"SEC_TICKERS_FETCH_FAILED:{type(exc).__name__}"} for s in us_symbols)

        observed = now.astimezone(timezone.utc)
        for sym in symbols:
            symbol = sym.upper()
            try:
                item = None
                if symbol in tw_symbols:
                    code = symbol.split(".")[0]
                    fs = [r for r in tw_financial_rows if isinstance(r, dict) and str(r.get("公司代號", "")) == code]
                    if fs:
                        fields = [k for k in fs[0] if k not in {"公司代號", "公司名稱", "資料年月", "出表日期", "營業收入-當月營收", "單位"}]
                        chosen = max(fs, key=lambda r: str(r.get("資料年月", "")))
                        vals = [(field, str(chosen.get(field, ""))) for field in fields
                                if re.fullmatch(r"-?[\d,]+(?:\.\d+)?", str(chosen.get(field, "")))]
                        if vals:
                            pub = self._date(chosen.get("出表日期", ""), roc=True)
                            month = f"{chosen.get('年度', '')}-Q{chosen.get('季別', '')}"
                            if not re.fullmatch(r"\d{3}-Q[1-4]", month):
                                raise ValueError("INVALID_TWSE_FINANCIAL_PERIOD")
                            if 0 <= (observed - pub).total_seconds() <= 730 * 86400:
                                item = {"symbol": symbol, "source_url": TW_FINANCIAL_URL, "source_tier": "official_exchange",
                                    "published_at": pub.isoformat(), "verified_facts": [f"TWSE {field} = {value} (ROC period {month}; source-reported value; unit not converted)." for field, value in vals],
                                    "limitations": ["Official TWSE open data; reported fields/units preserved without conversion or inferred ratios."],
                                    "raw_metadata": {"source": "official TWSE financial statements", "data_month": month, "raw_row": dict(chosen), "reported_fields": [k for k,v in vals]},
                                    "research_scope": "historical_company_facts_not_catalyst", "research_id": f"twse-financial-{code}-{month}"}
                    if item is not None and item['source_url'] == TW_FINANCIAL_URL:
                        supplements = []
                        try:
                            balance_rows = self._get(TW_BALANCE_URL)
                            balance = next((row for row in balance_rows if isinstance(row, dict)
                                and row.get('公司代號') == code
                                and str(row.get('年度')) == str(chosen.get('年度'))
                                and str(row.get('季別')) == str(chosen.get('季別'))), None)
                            if balance:
                                supplements.append({'source_url':TW_BALANCE_URL,'raw_row':balance})
                                for field, value in balance.items():
                                    if field not in {'出表日期','年度','季別','公司代號'} and re.fullmatch(r'-?[\d,]+(?:\.\d+)?', str(value)):
                                        item['verified_facts'].append(f'TWSE balance sheet {field} = {value} (ROC period {month}; source-reported unit; not converted).')
                        except Exception as exc:
                            gaps.append({'symbol':symbol,'reason':f'TW_BALANCE_SOURCE_UNAVAILABLE:{type(exc).__name__}'})
                        revenue = next((row for row in tw_rows if isinstance(row,dict) and row.get('公司代號') == code), None)
                        if revenue:
                            supplements.append({'source_url':TW_REVENUE_URL,'raw_row':revenue})
                            value = str(revenue.get('營業收入-當月營收',''))
                            if re.fullmatch(r'[\d,]+(?:\.\d+)?', value):
                                item['verified_facts'].append(f"TWSE monthly revenue = {value} (ROC month {revenue.get('資料年月')}; source-reported unit; not converted).")
                        item['raw_metadata']['supplemental_source_rows'] = supplements
                        item['limitations'].append('Income-statement ROC quarter labels must not be confused with standalone-quarter flow; EPS is not annualized. No cash-flow statement acquired from these TWSE endpoints.')
                    matching = [r for r in tw_rows if isinstance(r, dict) and r.get("公司代號") == code]
                    if matching and item is None:
                        valid_rows = []
                        for candidate in matching:
                            try:
                                publish_date = self._date(candidate.get("出表日期", ""), roc=True)
                                report_month = str(candidate.get("資料年月", ""))
                                revenue_text = str(candidate.get("營業收入-當月營收", ""))
                                if not re.fullmatch(r"\d{5}", report_month) or not re.fullmatch(r"-?[\d,]+(?:\.\d+)?", revenue_text):
                                    continue
                                report_year = int(report_month[:3]) + 1911
                                report_month_num = int(report_month[3:])
                                if not 1 <= report_month_num <= 12 or datetime(report_year, report_month_num, 1, tzinfo=timezone.utc) > publish_date:
                                    continue
                                valid_rows.append((publish_date, candidate))
                            except (TypeError, ValueError):
                                continue
                        if not valid_rows:
                            raise ValueError("INVALID_TWSE_REVENUE_ROWS")
                        published, row = max(valid_rows, key=lambda pair: pair[0])
                        month = str(row["資料年月"])
                        revenue = str(row["營業收入-當月營收"])
                        if 0 <= (observed - published).total_seconds() <= 730 * 86400:
                            item = {
                                "symbol": symbol, "source_url": TW_REVENUE_URL,
                                "source_tier": "official_exchange", "published_at": published.isoformat(),
                                "verified_facts": [f"TWSE code {code} monthly revenue for ROC {month}: {revenue} (source-reported unit; not converted)."],
                                "limitations": ["TWSE publication date has day precision; source units are not converted; no forecast or direction inferred."],
                                "raw_metadata": {"source": "official TWSE open data", "report_date": row["出表日期"], "data_month": month, "raw_row": dict(row), "revenue_unit": row.get("單位", "SOURCE_REPORTED_UNIT_NOT_CONVERTED")},
                                "research_scope": "historical_company_facts_not_catalyst",
                                "research_id": f"twse-revenue-{code}-{month}",
                            }
                elif symbol in us_symbols and symbol in ticker_map:
                    cik = ticker_map[symbol]
                    facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
                    sub_url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
                    data = None
                    url = sub_url
                    # Operating facts take priority over a filing-index headline.
                    # A Form 4 index alone cannot support an ownership decision.
                    try:
                        data = self._get(facts_url)
                        url = facts_url
                    except ValueError as exc:
                        if str(exc) == "SEC_COMPANYFACTS_CIK_MISMATCH":
                            raise
                        gaps.append({"symbol": symbol, "reason":
                            "COMPANYFACTS_ACQUISITION_FAILED:" + type(exc).__name__})
                        data = None
                    except Exception as exc:
                        gaps.append({"symbol": symbol, "reason":
                            "COMPANYFACTS_ACQUISITION_FAILED:" + type(exc).__name__})
                        data = None
                    if data is None:
                        try:
                            data = self._get(sub_url)
                            url = sub_url
                        except Exception:
                            data = self._get(facts_url)
                            url = facts_url

                    # Case A: SEC EDGAR XBRL Company Facts
                    if isinstance(data, dict) and "facts" in data:
                        if str(data.get("cik", "")).lstrip("0") != str(cik):
                            raise ValueError("SEC_COMPANYFACTS_CIK_MISMATCH")
                        us_gaap = data.get("facts", {}).get("us-gaap", {})
                        entity_name = str(data.get("entityName", symbol))
                        for concept in ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "NetIncomeLoss", "OperatingIncomeLoss", "GrossProfit", "StockholdersEquity", "Assets"):
                            if concept not in us_gaap:
                                continue
                            c_units = us_gaap[concept].get("units", {})
                            for u_name, u_facts in c_units.items():
                                latest_f = self._current_fact(u_facts, observed)
                                if latest_f is None:
                                    continue
                                published = self._date(latest_f["filed"])
                                if not 0 <= (observed - published).total_seconds() <= 730 * 86400:
                                    continue
                                form = str(latest_f.get("form", "10-Q"))
                                val = latest_f.get("val")
                                fy = latest_f.get("fy", "")
                                fp = latest_f.get("fp", "")
                                accn = str(latest_f.get("accn", "0000000000-00-000000"))
                                item = {
                                    "symbol": symbol, "source_url": url, "source_tier": "official_filing",
                                    "published_at": published.isoformat(),
                                    "verified_facts": [f"SEC EDGAR Company Facts for {symbol}: {concept} = {val} {u_name} (Form {form}, FY{fy} {fp}, filed {latest_f['filed']}, accession {accn})."],
                                    "limitations": ["Official SEC EDGAR XBRL company facts; historical filing record only; not an immediate catalyst or order signal."],
                                    "raw_metadata": {"source": "official SEC EDGAR Company Facts", "cik": cik, "entity_name": entity_name, "concept": concept, "val": val, "unit": u_name, "form": form, "fy": fy, "fp": fp, "accession": accn},
                                    "research_scope": "historical_company_facts_not_catalyst",
                                    "research_id": f"sec-facts-{cik}-{concept.lower()[:12]}-{accn.replace('-', '')[:10]}",
                                }
                                break
                            if item is not None:
                                break

                    # Preserve dated, unit-qualified operating baselines, not just
                    # one headline value or the filing year of a comparative row.
                    if item is not None and isinstance(data, dict) and 'facts' in data:
                        baseline = []
                        baseline_gaps = []
                        concepts = ('RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues', 'NetIncomeLoss', 'OperatingIncomeLoss', 'GrossProfit', 'NetCashProvidedByUsedInOperatingActivities', 'NetCashProvidedByUsedInInvestingActivities', 'NetCashProvidedByUsedInFinancingActivities', 'PaymentsToAcquirePropertyPlantAndEquipment', 'ShareBasedCompensation', 'EarningsPerShareDiluted', 'WeightedAverageNumberOfDilutedSharesOutstanding', 'CashAndCashEquivalentsAtCarryingValue', 'ShortTermInvestments', 'LongTermDebtCurrent', 'LongTermDebtNoncurrent', 'OperatingLeaseLiabilityCurrent', 'OperatingLeaseLiabilityNoncurrent', 'FinanceLeaseLiabilityCurrent', 'FinanceLeaseLiabilityNoncurrent')
                        for name in concepts:
                            units = data.get('facts', {}).get('us-gaap', {}).get(name, {}).get('units', {})
                            for unit, rows in units.items():
                                usable = [f for f in rows if f.get('val') is not None and self._current_fact([f], observed) is not None]
                                usable.sort(key=lambda f: (f['end'], f['filed'], f.get('start', '')), reverse=True)
                                # Detect conflicting values BEFORE period deduplication.
                                # Equal decimal values are harmless duplicate disclosures.
                                same_filing = {}
                                for f in usable:
                                    identity = tuple(f.get(k) for k in ('start', 'end', 'filed', 'form', 'accn'))
                                    same_filing.setdefault(identity, []).append(f)
                                rejected = set()
                                for identity, same_rows in same_filing.items():
                                    values = set()
                                    reason = None
                                    for fact in same_rows:
                                        try:
                                            value = Decimal(str(fact['val']))
                                            if not value.is_finite():
                                                raise InvalidOperation
                                            values.add(value)
                                        except (InvalidOperation, ValueError):
                                            reason = 'INVALID_FINANCIAL_FACT_VALUE'
                                    if len(values) > 1:
                                        reason = 'AMBIGUOUS_SAME_FILING_FACT'
                                    if reason:
                                        rejected.add(identity)
                                        baseline_gaps.append({'concept': name, 'unit': unit,
                                            **dict(zip(('start', 'end', 'filed', 'form', 'accn'), identity)),
                                            'reason': reason, 'source_rows': [dict(r) for r in same_rows]})
                                seen_periods = set()
                                period_counts = {}
                                for f in usable:
                                    period = (f.get('start'), f['end'])
                                    kind = classify_flow_period(f.get('start'), f['end'])
                                    if kind == 'invalid':
                                        continue
                                    if period in seen_periods or period_counts.get(kind, 0) >= 2:
                                        continue
                                    seen_periods.add(period)
                                    identity = tuple(f.get(k) for k in ('start', 'end', 'filed', 'form', 'accn'))
                                    if identity in rejected:
                                        continue
                                    period_counts[kind] = period_counts.get(kind, 0) + 1
                                    baseline.append({'concept': name, 'unit': unit, 'period_kind': kind, **{k: f.get(k) for k in ('val', 'start', 'end', 'filed', 'form', 'accn')}})
                        item['verified_facts'] = [f"SEC {f['concept']} = {f['val']} {f['unit']}; period {f['start'] or 'instant'}/{f['end']}; form {f['form']}; filed {f['filed']}; accession {f['accn']}." for f in baseline] or item['verified_facts']
                        if not baseline:
                            raise ValueError('NO_UNAMBIGUOUS_SEC_FINANCIAL_BASELINE')
                        item['raw_metadata']['financial_baseline'] = baseline
                        item['raw_metadata']['financial_baseline_gaps'] = baseline_gaps
                        item['raw_metadata']['financial_derivations'] = aligned_cash_flow_derivations(baseline)
                        item['limitations'].append('Cash-flow derivations are explicitly derived, not company-reported FCF. Only identical period/unit/filing/accession inputs are joined; unavailable or ambiguous inputs produce no derived metric.')
                        item['limitations'].append('Each fact retains its own start/end/unit. Quarterly and annual facts are not mixed into inferred growth or margins.')

                    # Case B: SEC EDGAR Submissions index
                    elif isinstance(data, dict) and "filings" in data:
                        if symbol not in [str(t).upper() for t in data.get("tickers", [])]:
                            raise ValueError("SEC_TICKER_CIK_MISMATCH")
                        recent = data["filings"]["recent"]
                        for idx, form in enumerate(recent["form"][:80]):
                            if form not in {"10-K", "10-Q", "8-K", "6-K", "20-F", "4"}:
                                continue
                            published = self._date(recent["filingDate"][idx])
                            if not 0 <= (observed - published).total_seconds() <= 730 * 86400:
                                continue
                            accession = recent["accessionNumber"][idx]
                            if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
                                continue
                            item = {
                                "symbol": symbol, "source_url": url, "source_tier": "official_filing",
                                "published_at": published.isoformat(),
                                "verified_facts": [f"SEC submissions index lists {symbol} Form {form}, filing date {recent['filingDate'][idx]}, accession {accession}."],
                                "limitations": ["Filing metadata only; underlying filing content not read or interpreted; date precision is one day."],
                                "raw_metadata": {"source": "official SEC EDGAR submissions", "cik": cik, "form": form, "accession": accession},
                                "research_scope": "historical_company_facts_not_catalyst",
                                "research_id": f"sec-{cik}-{accession.replace('-', '')}",
                            }
                            break
                if item is None:
                    gaps.append({"symbol": symbol, "reason": "NO_FRESH_SUPPORTED_OFFICIAL_ITEM"})
                    continue
                published = self._date(item["published_at"])
                days = 730 if item.get("research_scope") == "historical_company_facts_not_catalyst" else 7
                if not 0 <= (observed - published).total_seconds() <= days * 86400:
                    gaps.append({"symbol": symbol, "reason": "OFFICIAL_ITEM_OUTSIDE_FRESHNESS_WINDOW"})
                    continue
                self._attach_company_disclosures(item, gaps)
                item.update({"observed_at": observed.isoformat(), "verification_status": "verified", "is_fixture": False})
                ok, reason = reader.add_evidence(item, now=now)
                if ok:
                    accepted.append(item["research_id"])
                else:
                    gaps.append({"symbol": symbol, "reason": reason})
            except Exception as exc:
                detail = str(exc)
                if not re.fullmatch(r"[A-Z0-9_:-]{1,80}", detail):
                    detail = type(exc).__name__
                gaps.append({"symbol": symbol, "reason": f"OFFICIAL_ITEM_INVALID:{detail}"})
        self.last_gaps = gaps
        return {"accepted": accepted, "gaps": gaps}
