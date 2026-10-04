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


def test_restore_preserves_global_and_strategy_order_metadata_and_is_idempotent(tmp_path):
    """Persisted audit metadata must survive restart without replacing order identity or double replay."""
    from datetime import datetime, timezone

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

    service.experiments["desk-tw"] = PaperExperimentSettings(
        strategy_id="desk-tw",
        market="TW",
        base_currency="TWD",
        initial_cash=500,
        universe=["2330.TW"],
    )
    pm.register_strategy("desk-tw", 500, currency="TWD")

    global_order = Order(
        order_id="TEST_ONLY_global_meta",
        currency="TWD",
        symbol="2330.TW",
        market="TW",
        bucket="swing",
        side="BUY",
        order_type="LIMIT",
        quantity=2,
        limit_price=101,
        origin="MANUAL",
        reason="TEST_ONLY global metadata",
        audit_metadata={"source": "TEST_ONLY", "nested": {"proof": "global"}},
    )
    pm.add_order(global_order)
    global_fill = Fill(
        fill_id="TEST_ONLY_global_meta_fill",
        order_id=global_order.order_id,
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
    pm.apply_fill(global_fill)
    global_before = pm.get_portfolio(DecisionScope.SWING)
    global_order_before = next(order for order in global_before.orders if order.order_id == global_order.order_id)
    assert global_order_before.status == OrderStatus.FILLED
    assert global_order_before.audit_metadata["filled_quantity"] == 2
    assert global_order_before.audit_metadata["nested"]["proof"] == "global"

    strategy_order = Order(
        order_id="TEST_ONLY_strategy_meta",
        currency="TWD",
        strategy_id="desk-tw",
        strategy_version="TEST_ONLY_V1",
        symbol="2330.TW",
        market="TW",
        bucket="swing",
        side="BUY",
        order_type="LIMIT",
        quantity=1,
        limit_price=101,
        origin="STRATEGY",
        reason="TEST_ONLY strategy metadata",
        audit_metadata={"source": "TEST_ONLY", "nested": {"proof": "strategy"}},
    )
    pm.add_order(strategy_order)
    strategy_fill = Fill(
        fill_id="TEST_ONLY_strategy_meta_fill",
        order_id=strategy_order.order_id,
        currency="TWD",
        symbol="2330.TW",
        bucket="swing",
        side=OrderSide.BUY,
        quantity=1,
        fill_price=100,
        fee=1,
        tax=0,
        slippage=0,
        timestamp=datetime.now(timezone.utc),
    )
    pm.apply_fill(strategy_fill, "desk-tw")
    strategy_before = pm.get_strategy_portfolio("desk-tw", DecisionScope.SWING)
    strategy_order_before = next(
        order for order in strategy_before.orders if order.order_id == strategy_order.order_id
    )
    assert strategy_order_before.status == OrderStatus.FILLED
    assert strategy_order_before.audit_metadata["filled_quantity"] == 1
    assert strategy_order_before.audit_metadata["nested"]["proof"] == "strategy"

    writer._persist_portfolios()

    restored_pm = PortfolioManager(initial_cash_swing=1000, initial_cash_intraday=1000)
    restored_service = PaperOrderService(restored_pm, EventStore(":memory:"))
    restored_service.experiments["desk-tw"] = service.experiments["desk-tw"]
    restored_pm.register_strategy("desk-tw", 500, currency="TWD")

    # Simulate EventStore reconstruction: existing order objects already exist
    # before persisted runner state is replayed and their identity must survive.
    event_global = global_order.model_copy(deep=True)
    event_global.audit_metadata = {"event_store": True}
    restored_pm.add_order(event_global)
    event_strategy = strategy_order.model_copy(deep=True)
    event_strategy.audit_metadata = {"event_store": True}
    restored_pm.add_order(event_strategy)

    reader = AutonomousPaperRunner.__new__(AutonomousPaperRunner)
    reader._runtime_dir = tmp_path
    reader.is_read_only = False
    reader.portfolio_manager = restored_pm
    reader.paper_orders = restored_service
    reader.corporate_actions = PaperCorporateActions(restored_pm, restored_service.event_store)
    reader._path = lambda name: tmp_path / name

    reader._restore_portfolios()

    restored_global = restored_pm.get_portfolio(DecisionScope.SWING)
    restored_global_order = next(
        order for order in restored_global.orders if order.order_id == global_order.order_id
    )
    assert restored_global_order is event_global
    assert restored_global_order.status == OrderStatus.FILLED
    assert restored_global_order.audit_metadata == global_order_before.audit_metadata

    restored_strategy = restored_pm.get_strategy_portfolio("desk-tw", DecisionScope.SWING)
    restored_strategy_order = next(
        order for order in restored_strategy.orders if order.order_id == strategy_order.order_id
    )
    assert restored_strategy_order is event_strategy
    assert restored_strategy_order.status == OrderStatus.FILLED
    assert restored_strategy_order.audit_metadata == strategy_order_before.audit_metadata

    global_snapshot = (
        restored_global.cash,
        [fill.fill_id for fill in restored_global.fills],
        restored_global_order.model_dump(mode="json"),
    )
    strategy_snapshot = (
        restored_strategy.cash,
        [fill.fill_id for fill in restored_strategy.fills],
        restored_strategy_order.model_dump(mode="json"),
    )

    reader._restore_portfolios()

    global_again = restored_pm.get_portfolio(DecisionScope.SWING)
    strategy_again = restored_pm.get_strategy_portfolio("desk-tw", DecisionScope.SWING)
    assert (
        global_again.cash,
        [fill.fill_id for fill in global_again.fills],
        next(
            order for order in global_again.orders
            if order.order_id == global_order.order_id
        ).model_dump(mode="json"),
    ) == global_snapshot
    assert (
        strategy_again.cash,
        [fill.fill_id for fill in strategy_again.fills],
        next(
            order for order in strategy_again.orders
            if order.order_id == strategy_order.order_id
        ).model_dump(mode="json"),
    ) == strategy_snapshot
    assert [fill.fill_id for fill in global_again.fills].count(global_fill.fill_id) == 1
    assert [fill.fill_id for fill in strategy_again.fills].count(strategy_fill.fill_id) == 1
