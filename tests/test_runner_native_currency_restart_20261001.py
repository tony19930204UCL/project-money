"""Offline TEST_ONLY runner settings/currency restoration tests."""
import json

import pytest

from cio_market_lab.domain.models import DecisionScope
from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner
from cio_market_lab.engine.corporate_actions import PaperCorporateActions
from cio_market_lab.engine.paper_orders import PaperExperimentSettings, PaperOrderService
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.events.store import EventStore


def test_runner_persists_and_restores_native_accounts_without_collapsing_markets(tmp_path):
    pm = PortfolioManager(initial_cash_swing=1000, initial_cash_intraday=0)
    orders = PaperOrderService(pm, EventStore(":memory:"))
    runner = AutonomousPaperRunner.__new__(AutonomousPaperRunner)
    runner._lock = __import__("threading").RLock()
    runner._runtime_dir = tmp_path
    runner.is_read_only = False
    runner.portfolio_manager = pm
    runner.paper_orders = orders
    runner._atomic_json = lambda name, payload: (tmp_path / name).write_text(json.dumps(payload))
    runner.configure(PaperExperimentSettings(strategy_id="desk-us", market="US", base_currency="USD", reporting_currency="TWD", initial_cash=1200, universe=["MSFT"]))
    runner.configure(PaperExperimentSettings(strategy_id="desk-tw", market="TW", base_currency="TWD", reporting_currency="TWD", initial_cash=34000, universe=["2330.TW"]))
    saved = json.loads((tmp_path / "experiment_settings.json").read_text())
    assert saved["desk-us"]["base_currency"] == "USD"
    assert saved["desk-tw"]["base_currency"] == "TWD"

    fresh = AutonomousPaperRunner.__new__(AutonomousPaperRunner)
    restored = fresh._migrate_settings(saved)
    assert set(restored) == {"desk-us", "desk-tw"}
    assert restored["desk-us"].base_currency == "USD"
    assert restored["desk-tw"].base_currency == "TWD"
    restored_pm = PortfolioManager(initial_cash_swing=1000)
    for cfg in restored.values():
        restored_pm.register_strategy(cfg.strategy_id, cfg.initial_cash, cfg.bucket_capital_allocations, currency=cfg.base_currency)
    assert restored_pm.get_strategy_ledger("desk-us", DecisionScope.SWING).currency == "USD"
    assert restored_pm.get_strategy_ledger("desk-tw", DecisionScope.SWING).currency == "TWD"


def test_runner_refuses_explicit_market_currency_mismatch_on_restore():
    runner = AutonomousPaperRunner.__new__(AutonomousPaperRunner)
    with pytest.raises(ValueError, match="STRATEGY_MARKET_CURRENCY_MISMATCH"):
        runner._migrate_settings({"bad": {"strategy_id": "bad", "market": "US", "base_currency": "TWD"}})


def test_legacy_settings_infer_market_native_currency_without_merging():
    runner = AutonomousPaperRunner.__new__(AutonomousPaperRunner)
    restored = runner._migrate_settings({
        "old-us": {"strategy_id": "old-us", "market": "US", "universe": ["MSFT"]},
        "old-tw": {"strategy_id": "old-tw", "market": "TW", "universe": ["2330.TW"]},
    })
    assert restored["old-us"].base_currency == "USD"
    assert restored["old-tw"].base_currency == "TWD"
    assert set(restored) == {"old-us", "old-tw"}

def test_actual_portfolio_restart_restores_native_usd_cash_and_replay_is_idempotent(tmp_path):
    from cio_market_lab.domain.models import Fill, OrderSide
    from cio_market_lab.engine.portfolio import PortfolioManager
    from cio_market_lab.engine.paper_orders import PaperOrderService
    from cio_market_lab.events.store import EventStore

    pm = PortfolioManager(initial_cash_swing=1000)
    service = PaperOrderService(pm, EventStore(":memory:"))
    writer = AutonomousPaperRunner.__new__(AutonomousPaperRunner)
    writer._runtime_dir = tmp_path
    writer.is_read_only = False
    writer.portfolio_manager = pm
    writer.paper_orders = service
    writer.corporate_actions = PaperCorporateActions(pm, service.event_store if hasattr(service, "event_store") else EventStore(":memory:"))
    writer._atomic_json = lambda name, payload: (tmp_path / name).write_text(json.dumps(payload))
    service.experiments["desk-us"] = PaperExperimentSettings(strategy_id="desk-us", market="US", base_currency="USD", initial_cash=1200, universe=["MSFT"])
    pm.register_strategy("desk-us", 1200, currency="USD")
    pm.get_strategy_ledger("desk-us", DecisionScope.SWING).add_order(__import__("cio_market_lab.domain.models", fromlist=["Order"]).Order(order_id="ord-us", currency="USD", strategy_id="desk-us", symbol="MSFT", market="US", bucket="swing", side="BUY", order_type="MARKET", quantity=2, origin="STRATEGY", reason="test"))
    fill = Fill(fill_id="fill-us-once", order_id="ord-us", currency="USD", symbol="MSFT", bucket="swing", side=OrderSide.BUY, quantity=2, fill_price=100, fee=1, tax=0, slippage=0, timestamp=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    pm.apply_fill(fill, "desk-us")
    writer._persist_portfolios()
    stored = json.loads((tmp_path / "portfolio_state.json").read_text())
    assert stored["cash_accounts"]["desk-us"]["currency"] == "USD"

    restored_pm = PortfolioManager(initial_cash_swing=1000)
    restored_service = PaperOrderService(restored_pm, EventStore(":memory:"))
    restored_service.experiments["desk-us"] = service.experiments["desk-us"]
    restored_pm.register_strategy("desk-us", 1200, currency="USD")
    fresh = AutonomousPaperRunner.__new__(AutonomousPaperRunner)
    fresh._runtime_dir = tmp_path
    fresh.is_read_only = False
    fresh.portfolio_manager = restored_pm
    fresh.paper_orders = restored_service
    fresh.corporate_actions = PaperCorporateActions(restored_pm, restored_service.event_store)
    fresh._path = lambda name: tmp_path / name
    fresh._restore_portfolios()
    ledger = restored_pm.get_strategy_ledger("desk-us", DecisionScope.SWING)
    before = (ledger.cash, len(ledger.orders), len(ledger.fills), ledger.positions["MSFT"].quantity)
    assert before == (999.0, 1, 1, 2.0)
    restored_pm.apply_fill(fill, "desk-us")
    after = (ledger.cash, len(ledger.orders), len(ledger.fills), ledger.positions["MSFT"].quantity)
    assert after == before

def test_native_cash_restore_currency_mismatch_fails_closed(tmp_path):
    from cio_market_lab.engine.portfolio import PortfolioManager
    from cio_market_lab.engine.paper_orders import PaperOrderService
    from cio_market_lab.events.store import EventStore
    pm = PortfolioManager(initial_cash_swing=1000)
    pm.register_strategy("desk-us", 1000, currency="USD")
    (tmp_path / "portfolio_state.json").write_text(json.dumps({"cash_accounts": {"desk-us": {"currency": "TWD", "cash": 12}}}))
    runner = AutonomousPaperRunner.__new__(AutonomousPaperRunner)
    runner._runtime_dir = tmp_path; runner.is_read_only = False; runner.portfolio_manager = pm
    runner.paper_orders = PaperOrderService(pm, EventStore(":memory:")); runner._path = lambda name: tmp_path / name
    with pytest.raises(ValueError, match="PORTFOLIO_RESTORE_FAIL_CLOSED"):
        runner._restore_portfolios()


def test_restore_preserves_terminal_order_status_and_persisted_spot_marks(tmp_path):
    """Regression: restart must not mutate final order state or drop evidenced NAV marks."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from cio_market_lab.domain.models import Fill, Order, OrderSide, OrderStatus
    from cio_market_lab.engine.paper_orders import PaperOrderService
    from cio_market_lab.engine.portfolio import PortfolioManager
    from cio_market_lab.events.store import EventStore

    pm = PortfolioManager(initial_cash_swing=1000, initial_cash_intraday=1000)
    service = PaperOrderService(pm, EventStore(":memory:"))
    writer = AutonomousPaperRunner.__new__(AutonomousPaperRunner)
    writer._runtime_dir = tmp_path
    writer.is_read_only = False
    writer.portfolio_manager = pm
    writer.paper_orders = service
    writer.corporate_actions = PaperCorporateActions(pm, service.event_store)
    writer._atomic_json = lambda name, payload: (tmp_path / name).write_text(json.dumps(payload))

    filled_order = Order(
        order_id="TEST_ONLY_restore_filled",
        currency="TWD",
        symbol="2330.TW",
        market="TW",
        bucket="swing",
        side="BUY",
        order_type="LIMIT",
        quantity=2,
        limit_price=110,
        origin="MANUAL",
        reason="TEST_ONLY terminal status restore",
    )
    pm.add_order(filled_order)
    fill = Fill(
        fill_id="TEST_ONLY_restore_fill",
        order_id=filled_order.order_id,
        currency="TWD",
        symbol="2330.TW",
        bucket="swing",
        side=OrderSide.BUY,
        quantity=2,
        fill_price=100,
        fee=1,
        tax=0,
        slippage=0,
        timestamp=datetime.now(timezone.utc),
    )
    pm.apply_fill(fill)
    assert pm.get_portfolio(DecisionScope.SWING).orders[0].status == OrderStatus.FILLED

    cancelled = Order(
        order_id="TEST_ONLY_restore_cancelled",
        currency="TWD",
        symbol="2330.TW",
        market="TW",
        bucket="swing",
        side="BUY",
        order_type="LIMIT",
        quantity=1,
        limit_price=90,
        origin="MANUAL",
        reason="TEST_ONLY cancelled status restore",
        status=OrderStatus.CANCELLED,
        rejection_reason="TEST_ONLY_FINAL_REASON",
    )
    pm.add_order(cancelled)

    pm.update_mark_to_market(SimpleNamespace(symbol="2330.TW", close=123.0, last_price=123.0))
    before = pm.get_portfolio(DecisionScope.SWING)
    assert before.equity is not None
    assert before.positions["2330.TW"].current_price == 123.0
    writer._persist_portfolios()

    restored_pm = PortfolioManager(initial_cash_swing=1000, initial_cash_intraday=1000)
    restored_service = PaperOrderService(restored_pm, EventStore(":memory:"))
    reader = AutonomousPaperRunner.__new__(AutonomousPaperRunner)
    reader._runtime_dir = tmp_path
    reader.is_read_only = False
    reader.portfolio_manager = restored_pm
    reader.paper_orders = restored_service
    reader.corporate_actions = PaperCorporateActions(restored_pm, restored_service.event_store)
    reader._path = lambda name: tmp_path / name
    reader._restore_portfolios()

    after = restored_pm.get_portfolio(DecisionScope.SWING)
    by_id = {order.order_id: order for order in after.orders}
    assert by_id["TEST_ONLY_restore_filled"].status == OrderStatus.FILLED
    assert by_id["TEST_ONLY_restore_cancelled"].status == OrderStatus.CANCELLED
    assert by_id["TEST_ONLY_restore_cancelled"].rejection_reason == "TEST_ONLY_FINAL_REASON"
    assert after.positions["2330.TW"].current_price == 123.0
    assert after.positions["2330.TW"].market_value == before.positions["2330.TW"].market_value
    assert after.positions["2330.TW"].unrealized_pnl == before.positions["2330.TW"].unrealized_pnl
    assert after.cash == before.cash
    assert after.equity == before.equity
    assert [item.fill_id for item in after.fills] == ["TEST_ONLY_restore_fill"]
