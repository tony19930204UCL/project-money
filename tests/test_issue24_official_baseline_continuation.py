from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from cio_market_lab.engine.daily_research_plan import DailyResearchPlanProducer
from cio_market_lab.research.official import (
    OfficialResearchProducer,
    SEC_TICKERS_URL,
    TW_FINANCIAL_URL,
)
from cio_market_lab.research.browser import validate_and_sanitize_evidence

NOW=datetime(2026,10,7,1,0,tzinfo=timezone.utc)
CURRENT_PAGE="https://investor.tsmc.com/english/quarterly-results/2026/q2"
CURRENT_PDF="https://investor.tsmc.com/english/encrypt/files/encrypt_file/reports/2026-q2/FS.pdf"
ANNUAL_PAGE="https://investor.tsmc.com/english/quarterly-results/2025/q4"
ANNUAL_PDF="https://investor.tsmc.com/english/encrypt/files/encrypt_file/reports/2025-q4/FS.pdf"


def _tsmc_item():
    return {
        "symbol":"2330.TW",
        "verified_facts":["TWSE current quarterly fact"],
        "limitations":[],
        "raw_metadata":{
            "raw_row":{
                "公司代號":"2330","出表日期":"1151007","年度":"115","季別":"2",
                "營業收入":"1000","單位":"仟元",
            },
        },
    }


def test_tsmc_current_plus_prior_annual_statement_are_retained_with_roles():
    current_html=[
        {"document_part":"HTML table 1","text":"2Q 2026 NT$ millions current facts"},
        {"document_part":"link","text":"Financial Statements","href":CURRENT_PDF},
    ]
    current_pdf=[{
        "document_part":"PDF page 1",
        "text":"NT$ millions; Six Months 2026 CFO 1482341; capex 846765",
        "source_url":CURRENT_PDF,"observed_at":NOW.isoformat(),
        "document_sha256":"CURRENT_SHA","body_provenance":"EXTRACTED_FROM_CAPTURED_WIRE_BYTES",
    }]
    annual_html=[
        {"document_part":"HTML table 1","text":"FY2025 annual comparison table"},
        {"document_part":"link","text":"Financial Statements","href":ANNUAL_PDF},
    ]
    annual_pdf=[{
        "document_part":"PDF page 1",
        "text":"FY2025 NT$ millions CFO 2274976; capex cash outflow 1272411; Q4/Q3/FY columns preserved",
        "source_url":ANNUAL_PDF,"observed_at":NOW.isoformat(),
        "document_sha256":"ANNUAL_SHA","body_provenance":"EXTRACTED_FROM_CAPTURED_WIRE_BYTES",
    }]
    data={
        CURRENT_PAGE:current_html,CURRENT_PDF:current_pdf,
        ANNUAL_PAGE:annual_html,ANNUAL_PDF:annual_pdf,
    }
    item=_tsmc_item(); gaps=[]
    OfficialResearchProducer(fetch_json=lambda url:data[url])._attach_company_disclosures(item,gaps)

    assert gaps==[]
    supplements=item["raw_metadata"]["supplemental_source_rows"]
    annual=[x for x in supplements if x.get("disclosure_role")=="historical_annual"]
    current=[x for x in supplements if x.get("disclosure_role")=="current"]
    assert current and annual
    assert {x.get("period") for x in annual}=={"2025-FY"}
    assert any(x["source_url"]==ANNUAL_PDF and "CFO 2274976" in x["raw_row"]["text"] for x in annual)
    assert any("Official historical company disclosure" in fact and "1272411" in fact for fact in item["verified_facts"])
    assert not item["raw_metadata"]["blocked_official_documents"]


def test_missing_historical_annual_does_not_discard_current_statement():
    current_html=[
        {"document_part":"HTML table 1","text":"2Q 2026 current facts"},
        {"document_part":"link","text":"Financial Statements","href":CURRENT_PDF},
    ]
    current_pdf=[{"document_part":"PDF page 1","text":"current statement body"}]
    def fetch(url):
        if url==CURRENT_PAGE: return current_html
        if url==CURRENT_PDF: return current_pdf
        if url==ANNUAL_PAGE: raise RuntimeError("HISTORY_NOT_AVAILABLE")
        raise AssertionError(url)

    item=_tsmc_item(); gaps=[]
    OfficialResearchProducer(fetch_json=fetch)._attach_company_disclosures(item,gaps)
    assert any("current statement body" in fact for fact in item["verified_facts"])
    assert item["raw_metadata"]["blocked_official_documents"]==[]
    assert item["raw_metadata"]["historical_disclosure_gaps"]==[{
        "source_url":ANNUAL_PAGE,"period":"2025-FY",
        "reason":"COMPANY_DISCLOSURE_SUPPLEMENT_UNAVAILABLE:RuntimeError",
    }]


def _sec_fact(val, *, start="2025-07-01", end="2026-06-30", filed="2026-07-30",
              form="10-K", accn="0000789019-26-000001"):
    row={"val":val,"end":end,"filed":filed,"form":form,"fy":2026,"fp":"FY","accn":accn}
    if start is not None: row["start"]=start
    return row


def _sec_data(extra):
    revenue=_sec_fact(281724000000)
    return {
        "cik":789019,"entityName":"Microsoft",
        "facts":{"us-gaap":{
            "RevenueFromContractWithCustomerExcludingAssessedTax":{"units":{"USD":[revenue]}},
            **extra,
        }},
    }


class Collector:
    def __init__(self): self.rows=[]
    def add_evidence(self,row,now=None): self.rows.append(row); return True,"OK"


def test_sec_finance_lease_and_capital_return_concepts_remain_separate():
    extra={
        "FinanceLeaseLiability":{"units":{"USD":[_sec_fact(15000,start=None)]}},
        "FinanceLeasePrincipalPayments":{"units":{"USD":[_sec_fact(4000)]}},
        "RightOfUseAssetObtainedInExchangeForFinanceLeaseLiability":{"units":{"USD":[_sec_fact(7000)]}},
        "PaymentsOfDividendsCommonStock":{"units":{"USD":[_sec_fact(24000)]}},
        "DividendsCommonStockCash":{"units":{"USD/shares":[_sec_fact(3.32)]}},
        "PaymentsToAcquirePropertyPlantAndEquipment":{"units":{"USD":[_sec_fact(64000)]}},
        "NetCashProvidedByUsedInOperatingActivities":{"units":{"USD":[_sec_fact(136000)]}},
    }
    data=_sec_data(extra)
    def fetch(url):
        if url==SEC_TICKERS_URL: return {"0":{"ticker":"MSFT","cik_str":789019}}
        if "companyfacts/CIK0000789019.json" in url: return data
        raise RuntimeError("OPTIONAL_DISCLOSURE_UNAVAILABLE")

    c=Collector()
    result=OfficialResearchProducer(fetch_json=fetch).acquire(["MSFT"],c,NOW)
    assert result["accepted"]
    baseline=c.rows[0]["raw_metadata"]["financial_baseline"]
    by_concept={x["concept"]:x for x in baseline}
    assert by_concept["FinanceLeaseLiability"]["economic_class"]=="finance_lease_liability_balance"
    assert by_concept["FinanceLeasePrincipalPayments"]["economic_class"]=="finance_lease_principal_cash_payment"
    assert by_concept["RightOfUseAssetObtainedInExchangeForFinanceLeaseLiability"]["economic_class"]=="finance_lease_noncash_rou_addition"
    assert by_concept["PaymentsOfDividendsCommonStock"]["economic_class"]=="capital_return_cash_dividend"
    assert by_concept["DividendsCommonStockCash"]["economic_class"]=="capital_return_dividend_declared_or_paid"
    derivations=c.rows[0]["raw_metadata"]["financial_derivations"]
    assert len(derivations)==1
    assert derivations[0]["value_decimal"]=="72000"
    assert "FinanceLeasePrincipalPayments" not in derivations[0]["formula"]
    assert "RightOfUseAsset" not in derivations[0]["formula"]


def test_sec_ambiguous_and_future_lease_facts_fail_closed_without_erasing_other_facts():
    shared=dict(start="2025-07-01",end="2026-06-30",filed="2026-07-30",form="10-K",accn="0000789019-26-000001")
    ambiguous=[
        _sec_fact(4000,**shared),
        _sec_fact(5000,**shared),
    ]
    future=_sec_fact(9999,start="2026-07-01",end="2027-06-30",filed="2027-07-30",form="10-K",accn="0000789019-27-000001")
    data=_sec_data({
        "FinanceLeasePrincipalPayments":{"units":{"USD":ambiguous+[future]}},
        "PaymentsOfDividendsCommonStock":{"units":{"USD":[_sec_fact(24000)]}},
    })
    def fetch(url):
        if url==SEC_TICKERS_URL: return {"0":{"ticker":"MSFT","cik_str":789019}}
        if "companyfacts/CIK0000789019.json" in url: return data
        raise RuntimeError("OPTIONAL_DISCLOSURE_UNAVAILABLE")

    c=Collector()
    OfficialResearchProducer(fetch_json=fetch).acquire(["MSFT"],c,NOW)
    baseline=c.rows[0]["raw_metadata"]["financial_baseline"]
    assert any(x["concept"]=="PaymentsOfDividendsCommonStock" for x in baseline)
    assert not any(x["concept"]=="FinanceLeasePrincipalPayments" for x in baseline)
    gaps=c.rows[0]["raw_metadata"]["financial_baseline_gaps"]
    assert any(x["concept"]=="FinanceLeasePrincipalPayments" and x["reason"]=="AMBIGUOUS_SAME_FILING_FACT" for x in gaps)


def test_authenticated_consumer_path_carries_current_annual_quote_and_keeps_old_plan_immutable(tmp_path,monkeypatch):
    current_pdf={"document_part":"PDF page 1","text":"Six Months 2026 CFO 1482341 capex 846765",
                 "source_url":CURRENT_PDF,"observed_at":NOW.isoformat(),"document_sha256":"CUR",
                 "body_provenance":"EXTRACTED_FROM_CAPTURED_WIRE_BYTES"}
    annual_pdf={"document_part":"PDF page 1","text":"FY2025 CFO 2274976 capex 1272411 NT$ millions",
                "source_url":ANNUAL_PDF,"observed_at":NOW.isoformat(),"document_sha256":"ANN",
                "body_provenance":"EXTRACTED_FROM_CAPTURED_WIRE_BYTES"}
    evidence={
        "symbol":"2330.TW","source_url":TW_FINANCIAL_URL,"source_tier":"official_exchange",
        "published_at":"2026-10-07T00:00:00+08:00","verified_facts":[
            "TWSE current quarterly fact",
            f"Official company disclosure [{CURRENT_PDF}, PDF page 1]: {current_pdf['text']}",
            f"Official historical company disclosure [{ANNUAL_PDF}, PDF page 1]: {annual_pdf['text']}",
        ],
        "limitations":[],"research_scope":"historical_company_facts_not_catalyst",
        "research_id":"twse-financial-2330-115-Q2",
        "raw_metadata":{
            "source":"official TWSE financial statements",
            "raw_row":{"公司代號":"2330","出表日期":"1151007","年度":"115","季別":"2","營業收入":"1000"},
            "supplemental_source_rows":[
                {"source_url":CURRENT_PDF,"raw_row":current_pdf,"disclosure_role":"current","period":"2026-Q2"},
                {"source_url":ANNUAL_PDF,"raw_row":annual_pdf,"disclosure_role":"historical_annual","period":"2025-FY"},
            ],
            "blocked_official_documents":[],"historical_disclosure_gaps":[],
        },
        "observed_at":NOW.isoformat(),"verification_status":"verified","is_fixture":False,
    }

    class Reader:
        def add_evidence(self,row,now=None): return True,"OK"
    learning=SimpleNamespace(
        retrieve_context_lessons=lambda **kwargs:[],
        retrieve_past_outcomes=lambda **kwargs:[],
    )
    p=DailyResearchPlanProducer(
        root=tmp_path/"research",packet_root=tmp_path/"packets",session_id="TEST_ONLY_ISSUE24",
        workspace_root=str(tmp_path),learning_store=learning,reader=Reader(),now_fn=lambda:NOW,
    )
    def acquire(symbols,collector,now):
        collector.add_evidence(evidence,now=now)
        return {"accepted":[evidence["research_id"]],"gaps":[]}
    monkeypatch.setattr(p.official,"acquire",acquire)

    primary={"source_url":TW_FINANCIAL_URL,"tls_verified":True,"is_fixture":False,"content":[evidence["raw_metadata"]["raw_row"]]}
    sources={TW_FINANCIAL_URL:primary,CURRENT_PDF:{"source_url":CURRENT_PDF,"tls_verified":True,"is_fixture":False,"content":[current_pdf]},
             ANNUAL_PDF:{"source_url":ANNUAL_PDF,"tls_verified":True,"is_fixture":False,"content":[annual_pdf]}}
    for url,obj in sources.items():
        path=p.root/"raw_official"/(hashlib.sha256(url.encode()).hexdigest()+".json")
        path.write_text(json.dumps(obj,ensure_ascii=False))
        p.captures_by_url[url]=path

    captured={}
    plan={
        "thesis":"Historical and current official facts are present; remain WAIT for valuation support.",
        "valuation_scenarios":{},"catalysts":[],"buy_zone":None,
        "invalidation":"Wait for adequate valuation evidence before any paper action.",
        "invalidation_condition":None,"exposure_ceiling":0,"stance":"WAIT",
        "missing_evidence":["valuation support"],"review_trigger":"Review after next official valuation input.",
    }
    def fake_chat(message,*args,**kwargs):
        captured["message"]=message
        return {"response":json.dumps(plan),"runtime_metadata":{"TEST_ONLY":True},
                "session_id":"TEST_ONLY_AUTH","returncode":0,"is_fixture":False,"failed":False,"error":None}
    monkeypatch.setattr("cio_market_lab.integrations.hermes_chat.run_hermes_cli_chat",fake_chat)
    monkeypatch.setattr(
        "cio_market_lab.integrations.runtime_evidence.RuntimeEvidenceAdapter.verify_runtime_evidence",
        lambda self,**kwargs:SimpleNamespace(is_fixture=False,auth_verified=True,is_success_response=True),
    )

    quote={"symbol":"2330.TW","price":2585,"source":"TWSE_OPENAPI_DAILY","source_date":"2026-10-06"}
    first=p.refresh("2330.TW",now=NOW,reference_quote=quote)
    assert first["status"]=="AUTHENTICATED_RESEARCH_ONLY_PLAN"
    assert current_pdf["text"] in captured["message"]
    assert annual_pdf["text"] in captured["message"]
    assert '"price": 2585' in captured["message"]

    old_plan=Path(first["plan_path"])
    old_bytes=old_plan.read_bytes()
    receipt=json.loads(old_plan.read_text())
    public_input=receipt["public_model_input"]
    assert public_input["official_evidence"]["raw_metadata"]["source"]=="official TWSE financial statements"
    facts="\n".join(public_input["official_evidence"]["verified_facts"])
    assert current_pdf["text"] in facts and annual_pdf["text"] in facts
    assert public_input["reference_quote_for_valuation_only"]["price"]==2585

    second=p.refresh("2330.TW",now=NOW,reference_quote=quote)
    assert second["status"]=="CACHED_IMMUTABLE_PLAN"
    assert old_plan.read_bytes()==old_bytes
    reloaded=json.loads(old_plan.read_text())
    assert reloaded["public_model_input"]["official_evidence"]["raw_metadata"]["source"]=="official TWSE financial statements"


def test_historical_scope_missing_official_source_provenance_remains_rejected():
    evidence={
        "symbol":"2330.TW",
        "source_url":TW_FINANCIAL_URL,
        "source_tier":"official_exchange",
        "published_at":"2026-10-07T00:00:00+08:00",
        "observed_at":NOW.isoformat(),
        "verification_status":"verified",
        "verified_facts":["TWSE historical fact"],
        "limitations":[],
        "research_scope":"historical_company_facts_not_catalyst",
        "research_id":"TEST_ONLY_MISSING_PROVENANCE",
        "raw_metadata":{
            "raw_row":{"公司代號":"2330","出表日期":"1151007","年度":"115","季別":"2"},
        },
        "is_fixture":False,
    }
    ok,sanitized,reason=validate_and_sanitize_evidence(evidence,now=NOW)
    assert ok is False
    assert sanitized is None
    assert reason=="REJECTED_HISTORICAL_SCOPE_PROVENANCE"
