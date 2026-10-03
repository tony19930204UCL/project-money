"""Offline TEST_ONLY capture bootstrap; no network or model acceptance."""
import hashlib, json
from cio_market_lab.engine.daily_research_plan import DailyResearchPlanProducer

class Reader:
    def add_evidence(self, row, now=None):
        return True, 'TEST_ONLY'

class Response:
    status_code=200
    headers={'Content-Type':'text/html'}
    content=b'<html><body><p>TEST_ONLY annual financial statement. USD millions. Operating cash flow: 100.</p></body></html>'
    def raise_for_status(self): pass


def test_first_disclosure_capture_persists_exact_bytes_before_json(tmp_path, monkeypatch):
    p=DailyResearchPlanProducer(root=tmp_path/'new-root',packet_root=tmp_path/'packets',
        session_id='TEST_ONLY',workspace_root=str(tmp_path),learning_store=None,reader=Reader())
    monkeypatch.setattr(p.network,'get',lambda *a,**kw:Response())
    url='https://www.microsoft.com/en-us/Investor/earnings/FY-2026-Q4/press-release-webcast'
    captured=p._capture_official(url)
    digest=hashlib.sha256(Response.content).hexdigest()
    assert (p.root/'raw_official'/(digest+'.wire')).read_bytes()==Response.content
    stored=json.loads((p.root/'raw_official'/(digest+'.json')).read_text())
    assert stored['sha256_of_wire_bytes']==digest
    assert stored['source_url']==url
    assert stored['content']==captured
    assert p.captures_by_url[url].is_file()
