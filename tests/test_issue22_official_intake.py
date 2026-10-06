from __future__ import annotations

import builtins
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cio_market_lab.engine.daily_research_plan import (
    DailyResearchPlanProducer,
    normalize_reference_quote,
    semantic_reference_quote_digest,
)
from cio_market_lab.research.financial_periods import aligned_cash_flow_derivations
from cio_market_lab.research.official import OfficialResearchProducer
from cio_market_lab.research.official_documents import parse_official_document


NOW=datetime(2026,10,6,12,0,tzinfo=timezone.utc)
TSMC_PAGE="https://investor.tsmc.com/english/quarterly-results/2026/q2"
TSMC_PDF="https://investor.tsmc.com/english/encrypt/files/encrypt_file/reports/2026-07/114aaca0fea2050e96b91fffbab9ed04ba09cd92/FS.pdf"


def _minimal_text_pdf(text: str) -> bytes:
    stream=f"BT /F1 10 Tf 50 750 Td ({text}) Tj ET".encode("latin-1")
    objects=[
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "+str(len(stream)).encode()+b" >>\nstream\n"+stream+b"\nendstream",
    ]
    out=bytearray(b"%PDF-1.4\n")
    offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(out))
        out.extend(f"{i} 0 obj\n".encode())
        out.extend(obj)
        out.extend(b"\nendobj\n")
    xref=len(out)
    out.extend(f"xref\n0 {len(objects)+1}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode())
    out.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(out)


def test_supported_pdf_parser_extracts_statement_body():
    body=_minimal_text_pdf("Six Months 2026 NTD 1482341 and 2Q 2026 NTD 783365")
    rows=parse_official_document(TSMC_PDF,body,"application/pdf")
    assert rows[0]["document_part"]=="PDF page 1"
    assert "Six Months 2026" in rows[0]["text"]
    assert "2Q 2026" in rows[0]["text"]


def test_missing_pdf_parser_fails_closed(monkeypatch):
    real_import=builtins.__import__
    def blocked_import(name,*args,**kwargs):
        if name=="pypdf":
            raise ImportError("TEST_ONLY blocked optional dependency")
        return real_import(name,*args,**kwargs)
    monkeypatch.setattr(builtins,"__import__",blocked_import)
    with pytest.raises(RuntimeError,match="PDF_PARSER_UNAVAILABLE"):
        parse_official_document(TSMC_PDF,b"%PDF-1.4\n%%EOF\n","application/pdf")


def test_linked_pdf_block_preserves_html_and_records_exact_document():
    landing_rows=[
        {"document_part":"HTML table 1","text":"2Q 2026 revenue NTD millions 1,270,381"},
        {"document_part":"link","text":"Financial Statements","href":TSMC_PDF},
    ]
    def fetch(url):
        if url==TSMC_PAGE:
            return landing_rows
        if url==TSMC_PDF:
            raise RuntimeError("PDF_PARSER_UNAVAILABLE")
        raise AssertionError(url)

    item={
        "symbol":"2330.TW",
        "verified_facts":[],
        "limitations":[],
        "raw_metadata":{"raw_row":{"年度":"115","季別":"2"}},
    }
    gaps=[]
    producer=OfficialResearchProducer(fetch_json=fetch)
    producer._attach_company_disclosures(item,gaps)

    assert any("2Q 2026 revenue" in fact for fact in item["verified_facts"])
    assert item["raw_metadata"]["supplemental_source_rows"][0]["source_url"]==TSMC_PAGE
    blocked=item["raw_metadata"]["blocked_official_documents"]
    assert blocked==[{"source_url":TSMC_PDF,"discovered_from":TSMC_PAGE,"reason":"PDF_PARSER_UNAVAILABLE"}]
    assert gaps==[{"symbol":"2330.TW","reason":"COMPANY_DISCLOSURE_DOCUMENT_BLOCKED:PDF_PARSER_UNAVAILABLE"}]


def test_blocked_linked_statement_never_promotes_daily_plan(tmp_path, monkeypatch):
    class Reader:
        def add_evidence(self,row,now=None):
            return True,"accepted"
    p=DailyResearchPlanProducer(
        root=tmp_path/"research",packet_root=tmp_path/"packets",
        session_id="TEST_ONLY",workspace_root=str(tmp_path),
        learning_store=SimpleNamespace(),reader=Reader(),
    )
    evidence={
        "symbol":"2330.TW","source_url":"https://openapi.twse.com.tw/test",
        "source_tier":"official_exchange","published_at":"2026-08-01T00:00:00+00:00",
        "observed_at":NOW.isoformat(),"verification_status":"verified",
        "verified_facts":["independent HTML fact remains available"],
        "limitations":[],"research_scope":"historical_company_facts_not_catalyst",
        "research_id":"TEST_ONLY-2330",
        "raw_metadata":{
            "raw_row":{"年度":"115","季別":"2","公司代號":"2330"},
            "blocked_official_documents":[{"source_url":TSMC_PDF,"reason":"PDF_PARSER_UNAVAILABLE"}],
        },
        "is_fixture":False,
    }
    def acquire(symbols,collector,now):
        collector.add_evidence(evidence,now=now)
        return {"accepted":[evidence["research_id"]],"gaps":[]}
    monkeypatch.setattr(p.official,"acquire",acquire)

    with pytest.raises(RuntimeError,match="OFFICIAL_DISCLOSURE_DOCUMENT_BLOCKED"):
        p.refresh("2330.TW",now=NOW)
    saved=json.loads((p.root/"official_baselines"/"2330.TW.json").read_text())
    assert saved["verified_facts"]==evidence["verified_facts"]
    assert not (p.root/"authenticated_plans").exists()


def test_capture_records_wire_digest_observed_time_and_body_provenance(tmp_path, monkeypatch):
    body=_minimal_text_pdf("2Q 2026 NTD millions operating cash flow 783365")
    class Response:
        status_code=200
        content=body
        headers={"Content-Type":"application/pdf"}
        def raise_for_status(self): pass
    class Reader:
        def add_evidence(self,row,now=None): return True,"accepted"

    p=DailyResearchPlanProducer(
        root=tmp_path/"research",packet_root=tmp_path/"packets",
        session_id="TEST_ONLY",workspace_root=str(tmp_path),
        learning_store=SimpleNamespace(),reader=Reader(),now_fn=lambda:NOW,
    )
    monkeypatch.setattr(p.network,"get",lambda *a,**kw:Response())
    rows=p._capture_official(TSMC_PDF)
    digest=hashlib.sha256(body).hexdigest()

    assert rows[0]["source_url"]==TSMC_PDF
    assert rows[0]["document_sha256"]==digest
    assert rows[0]["observed_at"]==NOW.isoformat()
    assert rows[0]["body_provenance"]=="EXTRACTED_FROM_CAPTURED_WIRE_BYTES"
    assert (p.root/"raw_official"/(digest+".wire")).read_bytes()==body
    stored=json.loads((p.root/"raw_official"/(digest+".json")).read_text())
    assert stored["sha256_of_wire_bytes"]==digest
    assert stored["content"]==rows


def _cash_rows(start,ocf,capex):
    base={
        "unit":"NTD","start":start,"end":"2026-06-30","filed":"2026-07-16",
        "form":"OFFICIAL_FINANCIAL_STATEMENT","accn":"TSMC-2026Q2-FS",
    }
    return [
        {**base,"concept":"NetCashProvidedByUsedInOperatingActivities","val":ocf},
        {**base,"concept":"PaymentsToAcquirePropertyPlantAndEquipment","val":capex},
    ]


def test_tsmc_quarter_and_six_month_fcf_stay_separate_without_annualization():
    rows=_cash_rows("2026-04-01",783365,496002)+_cash_rows("2026-01-01",1482341,846765)
    derived=aligned_cash_flow_derivations(rows)
    by_kind={row["period_kind"]:row for row in derived}
    assert by_kind["quarterly"]["value_decimal"]=="287363"
    assert by_kind["half_year"]["value_decimal"]=="635576"
    assert len(derived)==2
    assert all(row["unit"]=="NTD" for row in derived)
    assert all(row["classification"]=="DERIVED_NOT_COMPANY_REPORTED" for row in derived)


def test_quote_revision_identity_uses_source_observation_not_retrieval_clock():
    q5={"symbol":"2330.TW","price":2570,"source":"TWSE_OPENAPI_DAILY","source_date":"2026-10-05"}
    q6={"symbol":"2330.TW","price":2585,"source":"TWSE_OPENAPI_DAILY","source_date":"2026-10-06"}
    c5=normalize_reference_quote(q5,now=NOW)
    c6=normalize_reference_quote(q6,now=NOW)
    assert semantic_reference_quote_digest(c5)!=semantic_reference_quote_digest(c6)
    assert c6["observed_at"]==NOW.isoformat()
    assert "date-only official EOD" in c6["timestamp_semantics"]["source_timestamp_or_date"]

    later=normalize_reference_quote(q6,now=datetime(2026,10,6,13,tzinfo=timezone.utc))
    assert semantic_reference_quote_digest(c6)==semantic_reference_quote_digest(later)
