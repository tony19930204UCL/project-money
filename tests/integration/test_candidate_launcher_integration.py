from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from cio_market_lab.api.app import create_app
from cio_market_lab.engine.candidate_launcher import (
    AUTHENTICATED_PAPER,
    FX_NAME,
    RESULT_NAME,
    VERIFY_ONLY,
    read_candidate_result,
    run_candidate,
)
from cio_market_lab.engine.paper_orders import PaperExperimentSettings
from tests.integration.research_learning_chain_helper import ChainMarketAdapter, TestOnlyDailyExecutor

ROOT=Path(__file__).resolve().parents[2]
HELPER=Path(__file__).with_name("candidate_launcher_process_helper.py")
T0=datetime(2026,10,5,14,0,tzinfo=timezone.utc)


def settings():
    return PaperExperimentSettings(
        strategy_id="TEST_ONLY_CANDIDATE",
        enabled=True,
        market="US",
        base_currency="USD",
        reporting_currency="TWD",
        initial_cash=100000.0,
        universe=["MSFT"],
    )


def write_test_only_packet(root: Path):
    root.mkdir(parents=True,exist_ok=True)
    packet={
        "research_id":"TEST_ONLY_CANDIDATE_RESEARCH",
        "symbol":"MSFT",
        "market":"US",
        "source_url":"https://www.sec.gov/Archives/edgar/data/789019/test-only",
        "source_tier":"official_filing",
        "observed_at":T0.isoformat(),
        "published_at":"2026-10-05",
        "is_fixture":False,
        "test_only":True,
        "source_mode":"offline_test",
        "verified":True,
        "verification_status":"verified",
        "verified_facts":["TEST_ONLY persisted official-shaped fact"],
        "research_scope":"event_input_only_not_order",
        "thesis":"TEST_ONLY research-only plan remains unarmed",
        "valuation_scenarios":{},
        "catalysts":[],
        "buy_zone":None,
        "invalidation":"TEST_ONLY wait for refreshed evidence",
        "invalidation_condition":None,
        "exposure_ceiling":0,
        "stance":"WAIT",
        "missing_evidence":["TEST_ONLY missing valuation evidence"],
        "research_only":True,
        "plan_session_date":"2026-10-05",
        "source_excerpts":["TEST_ONLY persisted official-shaped fact"],
    }
    (root/"MSFT.json").write_text(json.dumps(packet),encoding="utf-8")
    return packet


class NeverCalledExecutor:
    def is_available(self):
        raise AssertionError("VERIFY_ONLY_EXECUTOR_MUST_NOT_BE_QUERIED")


def test_verification_only_missing_inputs_is_truthful_and_side_effect_bounded(tmp_path):
    runtime=tmp_path/"runtime"
    result=run_candidate(
        workspace_root=ROOT,
        runtime_dir=runtime,
        mode=VERIFY_ONLY,
        settings=settings(),
        cio_executor=NeverCalledExecutor(),
        now=T0,
    )
    assert result["status"]=="WAITING_FOR_APPROVED_INPUT"
    assert result["provider_invoked"] is False
    assert result["runner_invoked"] is False
    assert result["fx"]["status"]=="COMBINED_NAV_GAP"
    assert result["fx"]["reporting_nav"] is None
    assert not (runtime/FX_NAME).exists()
    persisted=json.loads((runtime/RESULT_NAME).read_text())
    assert persisted==result
    assert persisted["paper_only"] is True and persisted["broker_connected"] is False


def test_supplied_fx_provenance_is_preserved_and_test_only_never_promoted(tmp_path):
    runtime=tmp_path/"runtime"
    supplied=tmp_path/"fx.json"
    supplied.write_text(json.dumps({"receipts":[{
        "pair":"USD/TWD","rate":"31.25","observed_at":T0.isoformat(),
        "source":"Federal Reserve Bank of St. Louis FRED DEXTAUS",
        "source_url":"https://fred.stlouisfed.org/series/DEXTAUS",
        "source_date":"2026-10-05","provenance":"OFFICIAL_SOURCE"
    }]}),encoding="utf-8")
    result=run_candidate(
        workspace_root=ROOT,runtime_dir=runtime,mode=VERIFY_ONLY,settings=settings(),
        fx_receipts_path=supplied,now=T0,
    )
    assert result["fx"]["status"]=="AVAILABLE"
    exact=json.loads((runtime/FX_NAME).read_text())["receipts"][0]
    assert exact["source_date"]=="2026-10-05"
    assert exact["source_url"]=="https://fred.stlouisfed.org/series/DEXTAUS"
    assert exact["provenance"]=="OFFICIAL_SOURCE"

    test_fx=tmp_path/"test-fx.json"
    test_fx.write_text(json.dumps([{
        "pair":"USD/TWD","rate":"31.5","observed_at":T0.isoformat(),
        "source":"TEST_ONLY_SYNTHETIC_FX","source_url":"https://example.com/test-only-fx",
        "source_date":"2026-10-05","provenance":"TEST_ONLY"
    }]),encoding="utf-8")
    with pytest.raises(ValueError,match="TEST_ONLY_FX_NOT_ALLOWED"):
        run_candidate(
            workspace_root=ROOT,runtime_dir=tmp_path/"prod-like",mode=VERIFY_ONLY,
            settings=settings(),fx_receipts_path=test_fx,now=T0,allow_test_only=False)
    tagged=run_candidate(
        workspace_root=ROOT,runtime_dir=tmp_path/"test-runtime",mode=VERIFY_ONLY,
        settings=settings(),fx_receipts_path=test_fx,now=T0,allow_test_only=True)
    assert tagged["fx"]["receipts"][0]["provenance"]=="TEST_ONLY"
    assert json.loads((tmp_path/"test-runtime"/FX_NAME).read_text())["receipts"][0]["provenance"]=="TEST_ONLY"


def test_authenticated_test_only_consumer_calls_real_runner_and_persists_lineage_restart(tmp_path):
    runtime=tmp_path/"runtime"
    packets=tmp_path/"packets"
    write_test_only_packet(packets)
    executor=TestOnlyDailyExecutor(T0,"TEST_ONLY_CANDIDATE_CASE_1")
    result=run_candidate(
        workspace_root=ROOT,runtime_dir=runtime,mode=AUTHENTICATED_PAPER,
        settings=settings(),packet_root=packets,trusted_manifest=None,
        session_id="TEST_ONLY_CHAIN_SESSION",
        market_adapter=ChainMarketAdapter(T0),cio_executor=executor,
        now=T0,allow_test_only=True,
    )
    assert result["status"]=="COMPLETED_TEST_ONLY_PAPER_CANDIDATE"
    assert result["runner_invoked"] is True
    assert result["provider_invoked"] is True
    assert executor.call_count==1
    assert len(result["research_lineage"])==1
    context_id=result["research_lineage"][0]["context_id"]
    assert context_id and result["research_lineage"][0]["daily_plan_review"] is True
    assert result["cycle"]["decisions"][0]["action"]=="NO_TRADE"
    assert result["cycle"]["fills"] if "fills" in result["cycle"] else True
    assert result["fx"]["status"]=="COMBINED_NAV_GAP"
    assert not (runtime/FX_NAME).exists()

    proc=subprocess.run([
        sys.executable,str(HELPER),
        "--workspace-root",str(ROOT),
        "--runtime-dir",str(runtime),
        "--packet-root",str(packets),
        "--now",T0.isoformat(),
    ],cwd=ROOT,text=True,capture_output=True,check=True,timeout=60)
    restarted=json.loads(proc.stdout.strip().splitlines()[-1])
    assert restarted["status"]=="COMPLETED_TEST_ONLY_PAPER_CANDIDATE"
    assert restarted["research_lineage"][0]["context_id"]==context_id
    assert restarted["persisted_config"]["mode"]=="AUTHENTICATED_PAPER"
    assert restarted["persisted_config"]["session_id"]=="TEST_ONLY_CHAIN_SESSION"

    app=create_app(workspace_root=ROOT,runtime_dir=runtime,fixture_mode=True,is_read_only=True)
    with TestClient(app) as client:
        api=client.get("/api/paper/candidate-result").json()
    assert api==read_candidate_result(runtime)
    assert api["research_lineage"][0]["context_id"]==context_id


def test_authenticated_mode_without_approved_context_never_falls_back_to_ungated(tmp_path):
    runtime=tmp_path/"runtime"
    result=run_candidate(
        workspace_root=ROOT,runtime_dir=runtime,mode=AUTHENTICATED_PAPER,
        settings=settings(),packet_root=tmp_path/"missing-packets",trusted_manifest=tmp_path/"missing-manifest.json",
        session_id="project-money-main-cio",market_adapter=ChainMarketAdapter(T0),
        cio_executor=TestOnlyDailyExecutor(T0,"SHOULD_NOT_AUTHENTICATE"),
        now=T0,allow_test_only=False,
    )
    assert result["runner_invoked"] is True
    assert result["status"]=="WAITING_FOR_APPROVED_INPUT"
    assert result["material_gate_enabled"] is True
    assert result["research_lineage"]==[]
    assert result["cycle"]["decisions"][0]["reason"]=="BLOCKED_DAILY_PLAN_SOURCE_UNAVAILABLE"
