"""Constructed offline tests only; not live quotes or PAPER acceptance."""
from datetime import datetime, timezone
import pytest
from cio_market_lab.domain.models import Bar, DecisionScope, Market, OrderOrigin, OrderSide, OrderType
from cio_market_lab.engine.execution import ExecutionEngine
from cio_market_lab.engine.paper_orders import PaperOrderService, PaperOrderRequest, PaperDataContext
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.events.store import EventStore


def request(**overrides):
    data = dict(symbol="MSFT", market=Market.US, currency="USD", bucket=DecisionScope.SWING,
                side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=2,
                origin=OrderOrigin.MANUAL, reason="TEST_ONLY native currency",
                data=PaperDataContext(last_price=100, source="TEST_ONLY"))
    data.update(overrides)
    return PaperOrderRequest(**data)


def fill_order(order):
    bar = Bar(symbol="MSFT", timestamp=datetime.now(timezone.utc), observed_at=datetime.now(timezone.utc), open=100,
              high=101, low=99, close=100, volume=100, source="TEST_ONLY")
    return ExecutionEngine().process_bar(bar, [order]).fills[0]


def test_usd_order_to_execution_to_native_cash():
    pm = PortfolioManager(initial_cash_swing=1000, initial_cash_intraday=0, currency="USD")
    svc = PaperOrderService(pm, EventStore(":memory:"))
    order = svc.submit(request())
    fill = fill_order(order)
    assert order.currency == fill.currency == "USD"
    pm.apply_fill(fill)
    ledger = pm.get_ledger(DecisionScope.SWING)
    assert ledger.cash == pytest.approx(1000 - 2 * fill.fill_price - fill.fee)
    assert ledger.positions["MSFT"].quantity == 2
    assert ledger.get_portfolio().currency == "USD"
    assert len(svc.all_orders()) == 1


def test_usd_strategy_does_not_pollute_twd_aggregate():
    pm = PortfolioManager(initial_cash_swing=1000, initial_cash_intraday=0)
    pm.register_strategy("usd", 1000, currency="USD")
    svc = PaperOrderService(pm, EventStore(":memory:"))
    order = svc.submit(request(strategy_id="usd"))
    assert svc.find_order(order.order_id) is not None
    assert len(svc.all_orders()) == 1
    fill = fill_order(order)
    pm.apply_fill(fill, "usd")
    native = pm.get_strategy_ledger("usd", DecisionScope.SWING)
    assert native.currency == "USD"
    assert native.cash == pytest.approx(1000 - 2 * fill.fill_price - fill.fee)
    assert pm.get_ledger(DecisionScope.SWING).cash == 1000
    assert pm.get_ledger(DecisionScope.SWING).fills == []
    assert pm.get_strategy_ledger("usd", DecisionScope.INTRADAY).cash == native.cash
    assert pm.reconcile_canonical_cash("usd") == pytest.approx(native.cash)
    with pytest.raises(ValueError, match="CURRENCY_MISMATCH"):
        pm.register_strategy("usd", 1000, currency="TWD")


def test_explicit_mismatch_rejected_without_mutation():
    pm = PortfolioManager(initial_cash_swing=1000)
    svc = PaperOrderService(pm, EventStore(":memory:"))
    with pytest.raises(ValueError, match="CURRENCY_MISMATCH"):
        svc.submit(request())
    assert svc.all_orders() == []
    assert pm.get_ledger(DecisionScope.SWING).cash == 1000


def test_invalid_us_twd_native_request_rejected():
    pm = PortfolioManager(initial_cash_swing=1000)
    svc = PaperOrderService(pm, EventStore(":memory:"))
    with pytest.raises(ValueError, match="MARKET_CURRENCY_MISMATCH"):
        svc.submit(request(currency="TWD"))


def test_strategy_mismatch_does_not_partially_apply_aggregate_fill():
    pm = PortfolioManager(initial_cash_swing=1000)
    pm.register_strategy("usd", 1000, currency="USD")
    usdpm = PortfolioManager(initial_cash_swing=1000, currency="USD")
    order = PaperOrderService(usdpm, EventStore(":memory:")).submit(request())
    fill = fill_order(order).model_copy(update={"currency": "TWD"})
    with pytest.raises(ValueError, match="CURRENCY_MISMATCH"):
        pm.apply_fill(fill, "usd")
    assert pm.get_ledger(DecisionScope.SWING).cash == 1000
    assert pm.get_ledger(DecisionScope.SWING).fills == []


def test_native_strategy_order_can_be_cancelled():
    pm = PortfolioManager()
    pm.register_strategy("usd", 1000, currency="USD")
    svc = PaperOrderService(pm, EventStore(":memory:"))
    order = svc.submit(request(strategy_id="usd", order_type=OrderType.LIMIT, limit_price=100))
    cancelled = svc.cancel(order.order_id)
    assert cancelled.status.value == "CANCELLED"
    assert svc.all_orders()[0].status.value == "CANCELLED"
