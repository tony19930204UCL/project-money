from datetime import datetime, timezone
from cio_market_lab.research.official import OfficialResearchProducer, TW_FINANCIAL_URL, TW_REVENUE_URL

class Collector:
    def __init__(self): self.rows=[]
    def add_evidence(self,row,now=None): self.rows.append(row); return True, 'OK'

def test_real_documented_endpoint_and_financial_not_overwritten():
    assert TW_FINANCIAL_URL.endswith('/t187ap06_L_ci')
    def fetch(url):
        if url == TW_FINANCIAL_URL:
            return [{'公司代號':'2330','公司名稱':'台積電','出表日期':'1151001','年度':'115','季別':'2','營業收入':'1000','基本每股盈餘（元）':'4'}]
        if url == TW_REVENUE_URL:
            return [{'公司代號':'2330','出表日期':'1151001','資料年月':'11508','營業收入-當月營收':'200'}]
        raise ValueError('unexpected source')
    c=Collector()
    result=OfficialResearchProducer(fetch_json=fetch).acquire(['2330.TW'],c,datetime(2026,10,1,15,tzinfo=timezone.utc))
    assert result['accepted'] == ['twse-financial-2330-115-Q2']
    assert c.rows[0]['source_url'] == TW_FINANCIAL_URL
    assert c.rows[0]['raw_metadata']['raw_row']['基本每股盈餘（元）']=='4'

def test_unavailable_financial_falls_back_with_gap_not_fabricated():
    def fetch(url):
        if url == TW_FINANCIAL_URL: raise ValueError('HTML_NOT_JSON')
        return [{'公司代號':'2330','出表日期':'1151001','資料年月':'11508','營業收入-當月營收':'200'}]
    c=Collector()
    result=OfficialResearchProducer(fetch_json=fetch).acquire(['2330.TW'],c,datetime(2026,10,1,15,tzinfo=timezone.utc))
    assert result['gaps']
    assert c.rows[0]['source_url'] == TW_REVENUE_URL


def _crossover_fetch(report_date):
    def fetch(url):
        if url == TW_FINANCIAL_URL:
            return [{
                '公司代號':'2330','公司名稱':'台積電','出表日期':report_date,
                '年度':'115','季別':'2','營業收入':'1000','基本每股盈餘（元）':'4',
            }]
        if url == TW_REVENUE_URL:
            return []
        raise ValueError('optional source unavailable')
    return fetch


def test_twse_day_precision_uses_taipei_calendar_at_utc_crossover():
    before=datetime(2026,10,6,15,59,59,tzinfo=timezone.utc)  # 23:59:59 Taipei Oct 6
    after=datetime(2026,10,6,16,0,1,tzinfo=timezone.utc)     # 00:00:01 Taipei Oct 7

    c_before=Collector()
    r_before=OfficialResearchProducer(fetch_json=_crossover_fetch('1151007')).acquire(
        ['2330.TW'],c_before,before
    )
    assert r_before['accepted'] == []
    assert c_before.rows == []

    c_after=Collector()
    r_after=OfficialResearchProducer(fetch_json=_crossover_fetch('1151007')).acquire(
        ['2330.TW'],c_after,after
    )
    assert r_after['accepted'] == ['twse-financial-2330-115-Q2']
    row=c_after.rows[0]
    assert row['published_at']=='2026-10-07T00:00:00+08:00'
    assert row['raw_metadata']['published_at_original']=='1151007'
    assert row['raw_metadata']['published_at_precision']=='day'
    assert row['raw_metadata']['published_at_timezone']=='Asia/Taipei'


def test_twse_future_local_day_stays_rejected_and_stale_bound_is_preserved():
    observed=datetime(2026,10,6,22,39,33,tzinfo=timezone.utc)  # Oct 7 in Taipei

    future=Collector()
    future_result=OfficialResearchProducer(fetch_json=_crossover_fetch('1151008')).acquire(
        ['2330.TW'],future,observed
    )
    assert future_result['accepted'] == []
    assert future.rows == []

    def stale_fetch(url):
        if url == TW_FINANCIAL_URL:
            return [{
                '公司代號':'2330','公司名稱':'台積電','出表日期':'1131006',
                '年度':'113','季別':'2','營業收入':'1000','基本每股盈餘（元）':'4',
            }]
        if url == TW_REVENUE_URL:
            return []
        raise ValueError('optional source unavailable')

    stale=Collector()
    stale_result=OfficialResearchProducer(fetch_json=stale_fetch).acquire(
        ['2330.TW'],stale,observed
    )
    assert stale_result['accepted'] == []
    assert stale.rows == []
