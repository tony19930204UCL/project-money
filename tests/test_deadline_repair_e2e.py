"""Isolated deterministic repair acceptance. All packets and quotes are explicitly fixtures."""

from tests.fixture_next_quote import submit_after_new_fixture_quote
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("owned_desk_fixture", Path(__file__).with_name("test_cio_owned_desk.py"))
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
build_test_runner = _module.build_test_runner
from cio_market_lab.domain.models import CIODecisionPacket, CIOProvenance, DecisionScope
from cio_market_lab.engine.autonomous_runner import DYNAMIC_DESK_ID
from cio_market_lab.engine.cio_packet import sign_cio_packet
from cio_market_lab.engine.paper_orders import PaperOrderService
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.engine.team_ops import TEAM_INITIAL_CAPITAL_TWD
from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner
from cio_market_lab.events.store import EventStore


def packet(clock, case, symbol, action, horizon, quantity, conditions=None):
    item = CIODecisionPacket(
        case_id=case, as_of=clock[0], expiry=clock[0] + timedelta(hours=8),
        thesis="Isolated engineering lifecycle fixture (not investment research)",
        selected_instrument=symbol, action=action, holding_horizon=horizon,
        quantity=quantity, conditions={**(conditions or {}), "allow_odd_lot": True},
        provenance=CIOProvenance(authority="MAIN_CIO"), is_fixture=True,
    )
    sign_cio_packet(item)
    return item


def test_swing_a_and_intraday_b_share_cash_b_exits_and_next_decision_learns(tmp_path):
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock)
    a = packet(clock, "fixture-swing-a", "2330.TW", "BUY", DecisionScope.SWING, 200)
    b = packet(clock, "fixture-intraday-b", "2317.TW", "BUY", DecisionScope.INTRADAY, 100)
    assert submit_after_new_fixture_quote(runner, clock, a).action == "BUY_FILLED"
    second = submit_after_new_fixture_quote(runner, clock, b)
    assert second.action == "BUY_FILLED" and second.mode == DecisionScope.INTRADAY
    swing = pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING)
    intraday = pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.INTRADAY)
    entry_cash = swing.cash
    assert entry_cash == intraday.cash < TEAM_INITIAL_CAPITAL_TWD
    assert swing.positions[a.selected_instrument].quantity == 200
    assert intraday.positions[b.selected_instrument].quantity == 100
    assert submit_after_new_fixture_quote(runner, clock, b).action == "NO_TRADE"  # duplicate cannot double-fill

    clock[0] += timedelta(minutes=2)
    adapter.current_time = clock[0]
    adapter.quote_time = clock[0]
    adapter.quote_price = 1050.0
    close = packet(clock, "fixture-close-b", "2317.TW", "SELL", DecisionScope.INTRADAY, 100,
                   {"target_case_id": b.case_id})
    exit_decision = submit_after_new_fixture_quote(runner, clock, close)
    assert exit_decision.action == "SELL_FILLED", exit_decision.reason
    assert swing.positions[a.selected_instrument].quantity == 200
    assert intraday.positions[b.selected_instrument].quantity == 0
    assert swing.cash == intraday.cash > entry_cash
    record = runner.learning_store.get_record(b.case_id)
    assert record.status == "CLOSED" and record.outcome["realized_pnl"] > 0
    assert len(intraday.fills) >= 2
    assert abs(swing.cash - (TEAM_INITIAL_CAPITAL_TWD + sum(
        f.cash_flow for f in swing.fills + intraday.fills
    ))) < 0.01
    assert not [v for v in runner.build_decision_context_request(["2317.TW"]).prior_lessons if v["case_id"] == b.case_id]
    clock[0] += timedelta(seconds=1)
    ctx = runner.build_decision_context_request(["2317.TW"])
    assert any(v["case_id"] == b.case_id for v in ctx.prior_lessons)
    assert any(v["case_id"] == b.case_id for v in ctx.past_outcomes)

    restored_pm = PortfolioManager(initial_cash_swing=TEAM_INITIAL_CAPITAL_TWD,
                                   initial_cash_intraday=TEAM_INITIAL_CAPITAL_TWD)
    restored_pm.register_strategy(DYNAMIC_DESK_ID, TEAM_INITIAL_CAPITAL_TWD, unified_cash=True)
    restored = AutonomousPaperRunner(tmp_path, restored_pm,
                                     PaperOrderService(restored_pm, EventStore(":memory:")),
                                     adapter, now_fn=lambda: clock[0],
                                     team_initial_capital=TEAM_INITIAL_CAPITAL_TWD)
    assert restored_pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING).positions["2330.TW"].quantity == 200
    assert restored_pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING).cash == swing.cash
    assert any(v["case_id"] == b.case_id for v in restored.build_decision_context_request(["2317.TW"]).prior_lessons)


def test_pending_orders_reserve_shared_cash_and_reject_future_quote(tmp_path):
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    runner, orders, pm, adapter = build_test_runner(tmp_path, clock)
    adapter.quote_time = clock[0] + timedelta(seconds=5)
    for index, symbol in enumerate(("2330.TW", "2317.TW", "1101.TW", "2454.TW",
                                    "2303.TW", "2881.TW", "1301.TW", "2603.TW")):
        horizon = DecisionScope.SWING if index % 2 == 0 else DecisionScope.INTRADAY
        item = packet(clock, f"reserve-{index}", symbol, "BUY", horizon, 290)
        assert runner.submit_cio_packet(item).action == "BUY_PENDING"
    d = packet(clock, "reserve-double-use", "1216.TW", "BUY", DecisionScope.INTRADAY, 290)
    assert runner.submit_cio_packet(d).reason == "NO_TRADE_INSUFFICIENT_PAPER_CASH"
    assert pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING).cash == TEAM_INITIAL_CAPITAL_TWD
    assert runner._pending_buy_reserve(DYNAMIC_DESK_ID) > 2320000
    clock[0] += timedelta(seconds=6)
    adapter.current_time = clock[0]
    adapter.quote_time = clock[0]
    assert runner.process_pending_orders()  # reservation releases only on actual later fill
    assert pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING).cash == pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.INTRADAY).cash


def test_non_action_evaluates_only_after_horizon_and_later_quote(tmp_path):
    clock = [datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)]
    runner, _, pm, adapter = build_test_runner(tmp_path, clock)
    item = packet(clock, "hold-case", "2330.TW", "HOLD", DecisionScope.INTRADAY, 0)
    assert runner.submit_cio_packet(item).action == "NO_TRADE"
    clock[0] += timedelta(hours=6)
    assert runner.evaluate_elapsed_non_actions("2330.TW") == []  # quote predates evaluation horizon
    adapter.current_time = clock[0]
    adapter.quote_time = clock[0]
    adapter.quote_price = 1050
    assert runner.evaluate_elapsed_non_actions("2330.TW") == [item.case_id]
    assert runner.evaluate_elapsed_non_actions("2330.TW") == []
    assert pm.get_strategy_ledger(DYNAMIC_DESK_ID, DecisionScope.SWING).cash == TEAM_INITIAL_CAPITAL_TWD
    assert runner.learning_store.get_record(item.case_id).outcome["no_trade_pnl"] is True
    assert not [v for v in runner.build_decision_context_request(["2330.TW"]).prior_lessons if v["case_id"] == item.case_id]
    clock[0] += timedelta(seconds=1)
    assert any(v["case_id"] == item.case_id for v in runner.build_decision_context_request(["2330.TW"]).prior_lessons)
