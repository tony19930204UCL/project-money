import pytest
from cio_market_lab.research.official_documents import parse_official_document
from cio_market_lab.research.official import OfficialResearchProducer

def test_table_headers_period_units_and_discovery_link_retained():
    raw=b'<script>fake 123</script><table><tr><th>Three months June 30, 2026 USD millions</th></tr><tr><td>Segment revenue</td><td>100</td></tr></table><a href="/FS.pdf">Financial Statements</a>'
    rows=parse_official_document('https://investor.tsmc.com/english/quarterly-results/2026/q2',raw,'text/html')
    assert 'USD millions' in rows[0]['text'] and '100' in rows[0]['text']
    assert 'fake' not in rows[0]['text']
    assert rows[1]['href']=='https://investor.tsmc.com/FS.pdf'

def test_unapproved_host_rejected():
    with pytest.raises(ValueError,match='UNAPPROVED'):
        parse_official_document('https://evil.example/f.pdf',b'hi','text/html')

def test_disclosure_supplement_exact_row_and_no_annualization():
    row={'document_part':'HTML table 1','text':'FY2026 USD millions segment revenue 100; operating income 30'}
    obj=OfficialResearchProducer(fetch_json=lambda u:[row])
    item={'symbol':'MSFT','raw_metadata':{'fy':2026,'fp':'FY'},'verified_facts':[],'limitations':[]};gaps=[]
    obj._attach_company_disclosures(item,gaps)
    assert not gaps
    assert item['raw_metadata']['supplemental_source_rows'][0]['raw_row']==row
    assert 'FY-2026-Q4' in item['raw_metadata']['supplemental_source_rows'][0]['source_url']
    assert len(item['verified_facts'])==1
