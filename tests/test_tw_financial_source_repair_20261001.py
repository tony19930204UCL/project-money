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
