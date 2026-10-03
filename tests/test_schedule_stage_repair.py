"""Separate deterministic fixture replay checks; never count these as live receipts."""

from tests.fixture_next_quote import submit_after_new_fixture_quote
from datetime import datetime, timedelta, timezone
import time

from cio_market_lab.domain.models import CIODecisionPacket, CIOProvenance, DecisionScope
from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner, DYNAMIC_DESK_ID
from cio_market_lab.engine.cio_packet import sign_cio_packet
from cio_market_lab.engine.paper_orders import PaperOrderService
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.engine.team_ops import TEAM_INITIAL_CAPITAL_TWD
from cio_market_lab.events.store import EventStore
from tests.test_cio_owned_desk import build_test_runner


def test_fixture_duplicate_order_rejected_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "isolated_hermes_home"))
    monkeypatch.setenv("CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS", "1")
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock)
    packet = CIODecisionPacket(
        case_id="fixture-restart-duplicate-001", as_of=clock[0],
        expiry=clock[0] + timedelta(hours=2), thesis="Deterministic replay probe",
        selected_instrument="2330.TW", action="BUY", quantity=10.0,
        provenance=CIOProvenance(authority="MAIN_CIO"), conditions={"allow_odd_lot": True}, is_fixture=True,
    )
    sign_cio_packet(packet)
    first = submit_after_new_fixture_quote(runner, clock, packet)
    assert first.action == "BUY_FILLED"
    cash_before_restart = pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING).cash
    assert len(orders.all_orders()) == 1

    restored_pm = PortfolioManager(
        initial_cash_swing=TEAM_INITIAL_CAPITAL_TWD, initial_cash_intraday=TEAM_INITIAL_CAPITAL_TWD,
    )
    restored_pm.register_strategy(DYNAMIC_DESK_ID, TEAM_INITIAL_CAPITAL_TWD, unified_cash=True)
    restored_orders = PaperOrderService(restored_pm, EventStore(":memory:"))
    restarted = AutonomousPaperRunner(
        root=tmp_path, portfolio_manager=restored_pm, paper_orders=restored_orders,
        market_adapter=adapter, now_fn=lambda: clock[0], team_initial_capital=TEAM_INITIAL_CAPITAL_TWD,
    )
    restarted.allow_fixture_quotes = True
    replay = restarted.submit_cio_packet(packet)
    assert replay.action == "NO_TRADE" and "DUPLICATE_CASE_ID" in replay.reason
    assert restored_pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING).cash == cash_before_restart
    assert len(restored_orders.all_orders()) == 1
    assert restored_orders.all_orders()[0].order_id == orders.all_orders()[0].order_id
    assert len(restored_pm.get_strategy_portfolio(DYNAMIC_DESK_ID, DecisionScope.SWING).fills) == 1


def test_schedule_gate_closed_is_not_runner_deadlock(tmp_path, monkeypatch):
    from cio_market_lab.engine.market_schedule import intraday_market_open
    from cio_market_lab.engine.paper_orders import PaperExperimentSettings

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "isolated_hermes_home"))
    clock = [datetime(2026, 9, 29, 8, 30, tzinfo=timezone.utc)]
    runner, orders, _, adapter = build_test_runner(tmp_path, clock)
    runner.configure(PaperExperimentSettings(
        strategy_id=DYNAMIC_DESK_ID, enabled=True, universe=["2330.TW"],
        mode="intraday", cadence_seconds=900,
    ))
    assert not intraday_market_open("2330.TW", clock[0])
    due, slots = runner._scheduled_symbols(runner.paper_orders.experiment_for(DYNAMIC_DESK_ID), clock[0])
    assert due == [] and slots == {}
    assert runner.history(DYNAMIC_DESK_ID)["runs"] == []
    assert orders.all_orders() == []
    assert adapter.current_time == clock[0]


def test_scheduled_fixture_feed_unavailable_finishes_without_order(tmp_path, monkeypatch):
    from cio_market_lab.engine.paper_orders import PaperExperimentSettings

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "isolated_hermes_home"))
    clock = [datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)]
    runner, orders, _, adapter = build_test_runner(tmp_path, clock)
    adapter.is_fixture = True
    monkeypatch.setattr(adapter, "get_bars", lambda *args, **kwargs: [])
    runner.configure(PaperExperimentSettings(
        strategy_id=DYNAMIC_DESK_ID, enabled=True, universe=["2330.TW"],
        mode="intraday", cadence_seconds=900,
    ))
    try:
        runner.start(DYNAMIC_DESK_ID)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not runner.history(DYNAMIC_DESK_ID)["runs"]:
            time.sleep(.05)
        history = runner.history(DYNAMIC_DESK_ID)
        assert len(history["runs"]) == 1
        assert history["runs"][0]["status"] == "COMPLETED"
        assert history["decisions"][0]["reason"] == "NO_TRADE_STALE_OR_SYNTHETIC_DATA"
        assert orders.all_orders() == []
    finally:
        runner.shutdown()
