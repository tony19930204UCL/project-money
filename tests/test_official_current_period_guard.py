from datetime import datetime, timezone

from cio_market_lab.research.official import OfficialResearchProducer


def test_sec_selector_prefers_latest_current_period_not_newly_filed_comparative():
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    rows = [
        {"form": "10-Q", "filed": "2026-08-01", "start": "2025-04-01", "end": "2025-06-30", "val": 55},
        {"form": "10-Q", "filed": "2026-08-01", "start": "2026-04-01", "end": "2026-06-30", "val": 99},
        {"form": "10-Q", "filed": "2026-10-01", "start": "2026-07-01", "end": "2026-09-30", "val": 120},
    ]
    assert OfficialResearchProducer._current_fact(rows, now)["val"] == 99


def test_sec_selector_rejects_stale_and_period_mismatched_facts():
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    rows = [
        {"form": "10-Q", "filed": "2026-09-01", "start": "2024-01-01", "end": "2024-03-31", "val": 1},
        {"form": "10-K", "filed": "2026-03-01", "start": "2025-01-01", "end": "2025-12-31", "val": 2},
    ]
    assert OfficialResearchProducer._current_fact(rows, now)["val"] == 2
    assert OfficialResearchProducer._current_fact(rows[:1], now) is None


def test_twse_parser_skips_bad_duplicate_and_keeps_source_unit():
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    from cio_market_lab.research.browser import PublicResearchInboxReader

    def fetch(url):
        if "twse" in url:
            return [None,
                    {"公司代號": "2330", "出表日期": "1150932", "資料年月": "11508", "營業收入-當月營收": "bad"},
                    {"公司代號": "2330", "出表日期": "1150910", "資料年月": "11508", "營業收入-當月營收": "100,200"}]
        return {}

    reader = PublicResearchInboxReader(inbox_dir="/tmp/official-current-period-fixture")
    result = OfficialResearchProducer(fetch_json=fetch).acquire(["2330.TW"], reader, now)
    assert len(result["accepted"]) == 1, result
    items, gaps = reader.get_verified_research_for_symbols(["2330.TW"], now=now)
    assert not gaps
    assert "100,200 (source-reported unit; not converted)" in items[0]["verified_facts"][0]
    assert items[0]["raw_metadata"]["report_date"] == "1150910"


def test_companyfacts_cik_mismatch_fails_closed(tmp_path):
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    from cio_market_lab.research.browser import PublicResearchInboxReader
    from cio_market_lab.research.official import SEC_TICKERS_URL

    def fetch(url):
        if url == SEC_TICKERS_URL:
            return {"0": {"ticker": "NVDA", "cik_str": 1045810}}
        if "companyfacts" in url:
            return {"cik": 999999, "entityName": "Wrong entity", "facts": {"us-gaap": {}}}
        return {"tickers": [], "filings": {"recent": {"form": [], "filingDate": [], "accessionNumber": []}}}

    reader = PublicResearchInboxReader(inbox_dir=tmp_path)
    result = OfficialResearchProducer(fetch_json=fetch).acquire(["NVDA"], reader, now)
    assert not result["accepted"]
    assert any("SEC_COMPANYFACTS_CIK_MISMATCH" in g["reason"] for g in result["gaps"])
