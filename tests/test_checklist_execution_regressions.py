"""Deterministic regressions; these fixtures are NOT live acceptance receipts."""
from datetime import datetime, timezone
import pytest
from cio_market_lab.domain.models import DecisionScope
from cio_market_lab.engine.autonomous_runner import DYNAMIC_DESK_ID
from cio_market_lab.engine.paper_orders import PaperExperimentSettings
from tests.test_cio_owned_desk import build_test_runner

@pytest.mark.parametrize("symbol,at", [
    ("2330.TW", datetime(2026,10,1,6,0,tzinfo=timezone.utc)),
    ("MSFT", datetime(2026,10,1,20,30,tzinfo=timezone.utc)),
])
def test_direct_swing_matches_postclose_scheduled_window(tmp_path, monkeypatch, symbol, at):
    runner, orders, pm, adapter = build_test_runner(tmp_path, [at])
    monkeypatch.delenv("CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS", raising=False)
    settings = PaperExperimentSettings(strategy_id=DYNAMIC_DESK_ID if symbol.endswith(".TW") else "usd-session-fixture", enabled=True, universe=[symbol], mode="swing", base_currency="TWD" if symbol.endswith(".TW") else "USD")
    runner.configure(settings)
    due, slots = runner._scheduled_symbols(settings, at)
    assert symbol in due
    marker = lambda run_id, settings, symbol, bar, inputs: runner._decision(run_id, settings, symbol, "NO_TRADE", "FIXTURE_CIO_PATH_REACHED", inputs)
    monkeypatch.setattr(runner, "_run_cio_decision_path", marker)
    result = runner._run_symbol("fixture-direct", settings, symbol)
    assert result.reason == "FIXTURE_CIO_PATH_REACHED"
    assert not orders.all_orders()

@pytest.mark.parametrize("mode,at,expected", [
    ("swing", datetime(2026,10,1,3,0,tzinfo=timezone.utc), "SWING_REVIEW_WINDOW_CLOSED"),
    ("intraday", datetime(2026,10,1,6,0,tzinfo=timezone.utc), "MARKET_CLOSED_WEEKEND_OR_HOLIDAY"),
])
def test_direct_mode_cannot_cross_wrong_session_window(tmp_path, monkeypatch, mode, at, expected):
    runner, orders, pm, adapter = build_test_runner(tmp_path, [at])
    monkeypatch.delenv("CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS", raising=False)
    settings = PaperExperimentSettings(strategy_id=DYNAMIC_DESK_ID, enabled=True, universe=["2330.TW"], mode=mode)
    runner.configure(settings)
    monkeypatch.setattr(runner,"_run_cio_decision_path", lambda *args: pytest.fail("wrong-window CIO call"))
    result = runner._run_symbol("fixture-direct", settings, "2330.TW")
    assert result.reason == expected
    assert not orders.all_orders()

def test_live_legacy_quote_cannot_fabricate_depth(tmp_path, monkeypatch):
    at=datetime(2026,10,1,3,0,tzinfo=timezone.utc)
    runner, orders, pm, adapter=build_test_runner(tmp_path,[at])
    runner.allow_fixture_quotes=False
    quote=adapter.get_latest_quote("2330.TW")
    quote.source="public_without_capability"
    quote.source_capabilities={}
    quote.bid_size=quote.ask_size=0  # Explicit legacy no-depth negative fixture.
    adapter.quote_time=at
    bar=adapter.get_bars("2330.TW")[-1]
    monkeypatch.setattr(adapter,"get_latest_quote", lambda *args: quote)
    assert runner._find_eligible_later_quote("2330.TW",bar,1800) is None
    assert quote.bid_size == 0 and quote.ask_size == 0

def test_shared_schedule_does_not_fetch_or_record_closed_window(tmp_path, monkeypatch):
    at=datetime(2026,10,1,3,0,tzinfo=timezone.utc)
    runner,orders,pm,adapter=build_test_runner(tmp_path,[at])
    runner.configure(PaperExperimentSettings(strategy_id=DYNAMIC_DESK_ID,enabled=True,universe=["2330.TW"],mode="swing"))
    monkeypatch.setattr(adapter,"get_bars",lambda *a,**k:pytest.fail("closed-window fetch"))
    n=len(runner._decisions)
    result=runner.run_scheduled_cycle(DYNAMIC_DESK_ID)
    assert result["status"] == "NO_SESSION_DUE"
    assert result["run"] is None and len(runner._decisions)==n

def test_shared_swing_slot_deduplication_survives_runner_reconstruction(tmp_path,monkeypatch):
    at=datetime(2026,10,1,6,0,tzinfo=timezone.utc)
    runner,orders,pm,adapter=build_test_runner(tmp_path,[at])
    monkeypatch.delenv("CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS",raising=False)
    settings=PaperExperimentSettings(strategy_id=DYNAMIC_DESK_ID,enabled=True,universe=["2330.TW"],mode="swing")
    runner.configure(settings)
    marker=lambda run_id,settings,symbol,bar,inputs:runner._decision(run_id,settings,symbol,"NO_TRADE","FIXTURE_CIO_PATH_REACHED",inputs)
    monkeypatch.setattr(runner,"_run_cio_decision_path",marker)
    first=runner.run_scheduled_cycle(DYNAMIC_DESK_ID)
    assert first["consumed_session_slots"]
    assert runner.run_scheduled_cycle(DYNAMIC_DESK_ID)["run"] is None
    rebuilt,_,_,adapter2=build_test_runner(tmp_path,[at])
    rebuilt.configure(settings)
    monkeypatch.setattr(adapter2,"get_bars",lambda *a,**k:pytest.fail("persisted-slot duplicate fetch"))
    assert rebuilt.run_scheduled_cycle(DYNAMIC_DESK_ID)["run"] is None

def test_source_failure_does_not_consume_session_slot(tmp_path,monkeypatch):
    at=datetime(2026,10,1,6,0,tzinfo=timezone.utc)
    runner,orders,pm,adapter=build_test_runner(tmp_path,[at])
    runner.configure(PaperExperimentSettings(strategy_id=DYNAMIC_DESK_ID,enabled=True,universe=["2330.TW"],mode="swing"))
    def blocked(strategy_id,symbols):
        return {"run":{"status":"BLOCKED"},"decisions":[{"symbol":"2330.TW","reason":"FEED_UNAVAILABLE:fixture-source-failure"}]}
    monkeypatch.setattr(runner,"_run_one_cycle_locked",blocked)
    result=runner.run_scheduled_cycle(DYNAMIC_DESK_ID)
    assert result["retryable_source_failures"] == ["2330.TW"]
    assert result["consumed_session_slots"] == {}
    assert runner._scheduled_symbols(runner.paper_orders.experiment_for(DYNAMIC_DESK_ID),at)[0] == ["2330.TW"]
