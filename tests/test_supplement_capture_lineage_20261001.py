import json
from types import SimpleNamespace
import pytest
from cio_market_lab.engine.daily_research_plan import DailyResearchPlanProducer

def test_supplement_capture_is_hashed_and_rejects_mismatch(tmp_path):
    primary=tmp_path/'primary.json'; supplement=tmp_path/'supplement.json'
    primary.write_text(json.dumps({'source_url':'https://official/income','content':[{'a':1}],'tls_verified':True,'is_fixture':False}))
    supplement.write_text(json.dumps({'source_url':'https://official/balance','content':[{'b':2}],'tls_verified':True,'is_fixture':False}))
    obj=SimpleNamespace(root=tmp_path,captures_by_url={'https://official/income':primary,'https://official/balance':supplement})
    evidence={'source_url':'https://official/income','raw_metadata':{'supplemental_source_rows':[{'source_url':'https://official/balance','raw_row':{'b':2}}]}}
    path=DailyResearchPlanProducer._evidence_capture(obj,evidence)
    assert json.loads(path.read_text())['supplemental_official_captures'][0]['content']==[{'b':2}]
    evidence['raw_metadata']['supplemental_source_rows'][0]['raw_row']={'b':3}
    with pytest.raises(RuntimeError,match='row/capture mismatch'):
        DailyResearchPlanProducer._evidence_capture(obj,evidence)
