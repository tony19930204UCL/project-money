from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient

from cio_market_lab.api.app import create_app
from cio_market_lab.data.taifex_derivatives import decode_quote_snapshot
from cio_market_lab.domain.models import DecisionScope
from cio_market_lab.engine.derivative_lifecycle import PaperDerivativeLifecycle
from cio_market_lab.engine.paper_orders import PaperExperimentSettings

ROOT=Path(__file__).resolve().parents[2]
HELPER=Path(__file__).with_name("derivative_restart_process_helper.py")
SID="TEST_ONLY_DERIV_RESTART"

def run(runtime,kind,action):
    cp=subprocess.run([sys.executable,str(HELPER),str(runtime),kind,action],
        cwd=ROOT,text=True,capture_output=True,check=True)
    return json.loads(cp.stdout.strip().splitlines()[-1])

def test_option_restart_missing_quote_then_real_fixture_close_is_idempotent(tmp_path):
    runtime=tmp_path/"option-runtime"
    first=run(runtime,"option","open")
    assert first["changed"]["success"] is True
    assert first["positions"]["TXO-TEST"]["quantity"]==1
    cash_after_open=first["cash"]
    missing=run(runtime,"option","review-missing")
    assert missing["changed"]=="UNRESOLVED_EXECUTABLE_QUOTE"
    assert missing["cash"]==cash_after_open
    assert missing["positions"]["TXO-TEST"]["quantity"]==1
    assert missing["equity"] is None
    replay_missing=run(runtime,"option","review-missing")
    assert replay_missing["cash"]==missing["cash"]
    assert replay_missing["event_ids"]==missing["event_ids"]
    closed=run(runtime,"option","review-close")
    assert closed["changed"]=="PAPER_RISK_CLOSE_FILLED"
    assert closed["positions"]["TXO-TEST"]["quantity"]==0
    close_events=closed["event_ids"]
    after=run(runtime,"option","snapshot")
    assert after["cash"]==closed["cash"]
    assert after["event_ids"]==close_events
    assert after["positions"]["TXO-TEST"]["quantity"]==0

def test_future_restart_dated_settlement_receipt_replay_has_no_duplicate_cash(tmp_path):
    runtime=tmp_path/"future-runtime"
    opened=run(runtime,"future","open")
    assert opened["changed"]["success"] is True
    cash_before=opened["cash"]
    settled=run(runtime,"future","settle")
    assert settled["changed"] is True
    expected=cash_before+(1020-1001)*200
    assert settled["cash"]==expected
    ids=settled["event_ids"]
    duplicate=run(runtime,"future","settle")
    assert duplicate["changed"] is False
    assert duplicate["cash"]==expected
    assert duplicate["event_ids"]==ids
    restarted=run(runtime,"future","snapshot")
    assert restarted["cash"]==expected
    assert restarted["event_ids"]==ids
    assert restarted["positions"]["TXF-TEST"]["assumptions"]["derivative_position"]["last_settlement_price"]==1020

def test_api_capability_quote_inventory_and_derivative_readback_remain_fail_closed(tmp_path):
    runtime=tmp_path/"api-runtime"
    runtime.mkdir()
    observed=datetime(2026,10,5,1,0,tzinfo=timezone.utc)
    raw={"RtCode":"0","RtData":{"QuoteList":[{
        "SymbolID":"TXO-TEST","Status":"","CDate":"20261005","CTime":"090000",
        "CBidPrice1":"100","CAskPrice1":"101","CBidSize1":"5","CAskSize1":"5",
        "CLastPrice":"100.5","CP":"C","StrikePrice":"20000"
    }]}}
    snap=decode_quote_snapshot(raw,source_url="https://mis.taifex.com.tw/futures/api/getQuoteListOption",
        market_type="1",observed_at=observed,is_fixture=True)
    inventory={"schema_version":1,"status":"SOURCE_INVENTORY_OBSERVED","observed_at":observed.isoformat(),
        "snapshots":[{"product":"TXO",**snap}],"failures":[],"quote_count":snap["quote_count"],
        "fresh_quote_count":snap["fresh_quote_count"],"paper_only":True,
        "broker_connected":False,"execution_enabled":False,
        "scope":"CONTRACT_QUOTE_INTAKE_ONLY_NOT_DERIVATIVE_ACTIVATION"}
    (runtime/"taifex_quote_inventory.json").write_text(json.dumps(inventory),encoding="utf-8")

    run(runtime,"option","open")
    app=create_app(workspace_root=ROOT,runtime_dir=runtime,fixture_mode=True,is_read_only=False)
    state=app.state.app_state
    state.paper_orders.experiments[SID]=PaperExperimentSettings(strategy_id=SID,enabled=True,
        initial_cash=1_000_000,base_currency="TWD",universe=["TXO-TEST"],
        allowed_buckets=[DecisionScope.SWING],max_position_notional=10_000_000)
    lifecycle=PaperDerivativeLifecycle(state.portfolio_manager,state.paper_orders,fixture_mode=True)
    lifecycle.replay()

    with TestClient(app) as client:
        caps=client.get("/api/paper/capabilities").json()
        quotes=client.get("/api/paper/derivative-quotes").json()
        readback=client.get("/api/paper/readback").json()
        portfolio=client.get("/api/portfolio").json()

    assert caps["capabilities"]["LONG_PREMIUM_OPTIONS"]["status"]=="UNAVAILABLE"
    assert caps["capabilities"]["FUTURES"]["status"]=="UNAVAILABLE"
    assert quotes["execution_enabled"] is False
    assert quotes["paper_only"] is True and quotes["broker_connected"] is False
    assert quotes["snapshots"][0]["quotes"][0]["symbol"]=="TXO-TEST"
    assert readback["paper_only"] is True and readback["broker_connected"] is False
    assert readback["portfolios"]["strategies"][SID]["swing"]["positions"]["TXO-TEST"]["quantity"]==1
    assert portfolio["paper_only"] is True
    policy=client.get("/api/paper/automation/policy").json()
    assert policy["derivative_trading_supported"] is False
    assert policy["broker_connected"] is False
