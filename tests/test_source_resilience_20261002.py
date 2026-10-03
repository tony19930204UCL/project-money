"""Offline TEST_ONLY acquisition resilience, not live acceptance."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import requests
from cio_market_lab.engine.daily_research_plan import DailyResearchPlanProducer

URL='https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json'
def response(status=200):
 r=requests.Response();r.status_code=status;r.url=URL
 r.headers['Content-Type']='application/json';r._content=b'{"cik":789019,"facts":{}}'
 return r

def producer(tmp_path,side_effect=None):
 p=DailyResearchPlanProducer(root=tmp_path/'formation',packet_root=tmp_path/'packets',session_id='TEST_ONLY',workspace_root=str(tmp_path),learning_store=SimpleNamespace())
 (p.root/'raw_official').mkdir(exist_ok=True)
 p.network=Mock();p.network.get.side_effect=side_effect
 return p

@pytest.mark.parametrize('failure',[requests.Timeout('TEST_ONLY'),requests.ConnectionError('TEST_ONLY'),response(502),response(503),response(504)])
def test_transient_retry_has_identical_url_and_retained_capture(tmp_path,failure):
 p=producer(tmp_path,[failure,response()]); raw=p._capture_official(URL)
 assert p.network.get.call_count==2
 assert all(call.args==(URL,) for call in p.network.get.call_args_list)
 assert raw['cik']==789019
 cap=json.loads(p.captures_by_url[URL].read_text())
 assert cap['source_url']==URL and not cap['is_fixture'] and cap['tls_verified']

@pytest.mark.parametrize('status',[403,429])
def test_access_or_rate_limit_is_not_bypassed(tmp_path,status):
 p=producer(tmp_path,[response(status),response()])
 with pytest.raises(requests.HTTPError):p._capture_official(URL)
 assert p.network.get.call_count==1

def test_transient_failure_is_bounded(tmp_path):
 p=producer(tmp_path,[requests.Timeout('one'),requests.Timeout('two'),response()])
 with pytest.raises(requests.Timeout):p._capture_official(URL)
 assert p.network.get.call_count==2 and not p.captures_by_url

def test_invalid_json_does_not_retry_or_invent_facts(tmp_path):
 bad=response();bad._content=b'not json';p=producer(tmp_path,[bad,response()])
 with pytest.raises(ValueError):p._capture_official(URL)
 assert p.network.get.call_count==1

def test_filing_index_rejected_before_inference_and_publication(tmp_path,monkeypatch):
 from cio_market_lab.integrations import hermes_chat
 p=producer(tmp_path)
 def acquire(symbols,reader,now):
  reader.rows.append({'symbol':'MSFT','source_url':URL,'verified_facts':['TEST_ONLY index'],'raw_metadata':{'form':'4'}})
  return {'accepted':['TEST_ONLY'],'gaps':[]}
 p.official=SimpleNamespace(acquire=acquire)
 called=Mock(side_effect=AssertionError('inference must not run'))
 monkeypatch.setattr(hermes_chat,'run_hermes_cli_chat',called)
 with pytest.raises(ValueError,match='index alone'):
  p.refresh('MSFT',now=datetime.now(timezone.utc))
 called.assert_not_called()
 assert not (p.root/'official_baselines/MSFT.json').exists()
