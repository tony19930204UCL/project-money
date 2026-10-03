"""TEST_ONLY real OS subprocess carrying the production-serialized CIO prompt."""
import json
import sys
from cio_market_lab.integrations import hermes_chat as bridge
from tests.hermes_test_doubles import mock_injected_transport_for_test
from tests.test_cio_gate_canonical import CountingExecutor
from tests.test_runtime_continuity import build_harness
from datetime import datetime, timezone

def test_prompt_bundle_crosses_real_subprocess(tmp_path, monkeypatch):
    capture = tmp_path / "captured_prompt.txt"
    fake = tmp_path / "fixture_child.py"
    response = mock_injected_transport_for_test("TEST_ONLY")
    fake.write_text("import sys,json\nfrom pathlib import Path\n"
        + "Path("+repr(str(capture))+").write_text(sys.stdin.read())\n"
        + "events="+repr(response["events"])+"\n"
        + "for event in events: print(json.dumps(event))\n")
    commands = []
    def fake_command(**kw):
        commands.append(kw)
        return [sys.executable, str(fake)]
    monkeypatch.setattr(bridge, "build_production_chat_command", fake_command)
    now=datetime(2026,9,28,2,0,tzinfo=timezone.utc)
    runner,_,_,adapter=build_harness(tmp_path,[now])
    adapter.set_bar("2330.TW",now,995,1005,990,1000)
    counter=CountingExecutor(now)
    runner.cio_executor=counter
    runner._run_cio_decision_path("fixture",runner.paper_orders.experiment_for("dynamic-desk"),"2330.TW",adapter.get_latest_bar("2330.TW"),{})
    req=counter.requests[0]
    bundle={"session_id":"stable-paper-session", "persisted_history":[{"case_id":"prior"}],
        "frozen_context":{"context_id":"dated-context","thesis":"TEST_ONLY frozen thesis"},
        "observed_lessons":[{"takeaway":"TEST_ONLY observed"}],"observed_outcomes":[{"no_fill":True,"realized_pnl":0}]}
    req=req.model_copy(update={"prior_lessons":[bundle]})
    executor=bridge.HermesCIODecisionExecutor(session_id="stable-paper-session",workspace_root=str(tmp_path),provider_id="openai-codex",model_id="gpt-6-astra",allow_fixture=True,escalation_reason="high_consequence")
    # Explicit TEST_ONLY transport keeps the global no-model guard intact.
    original_chat = next(cell.cell_contents for cell in bridge.run_hermes_cli_chat.__closure__
                         if callable(cell.cell_contents))
    def subprocess_fixture_transport(message, **kwargs):
        return original_chat(message, **kwargs)
    executor.transport = subprocess_fixture_transport
    packet=executor.request_decision(req)
    prompt=capture.read_text()
    assert all(x in prompt for x in ["stable-paper-session","dated-context","TEST_ONLY observed","no_fill","prior"])
    assert commands[0]["session_id"]=="stable-paper-session"
    assert commands[0]["provider"]=="openai-codex"
    assert commands[0]["model"]=="gpt-6-astra"
    assert packet.is_fixture
    assert executor.last_receipt is not None
