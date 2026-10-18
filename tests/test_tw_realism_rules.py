from __future__ import annotations

import pytest

from cio_market_lab.domain.models import DecisionScope, Market, OrderSide, OrderType, OrderOrigin, Position
from cio_market_lab.engine.execution import ExecutionCostConfig
from cio_market_lab.engine.paper_orders import PaperDataContext, PaperOrderRequest, PaperOrderService, PaperExperimentSettings
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.events.store import EventStore


def test_tw_day_trade_tax_is_half_delivery_tax() -> None:
    costs = ExecutionCostConfig()
    trade_value = 100_000.0

    assert costs.calculate_tax(Market.TW, OrderSide.SELL, trade_value) == 300.0
    assert costs.calculate_tax(Market.TW, OrderSide.SELL, trade_value, is_day_trade=True) == 150.0
    assert costs.calculate_tax(Market.TW, OrderSide.BUY, trade_value, is_day_trade=True) == 0.0


def test_paper_order_preview_applies_day_trade_tax_for_tw_intraday_sell() -> None:
    service = PaperOrderService(PortfolioManager(initial_cash_swing=200_000, initial_cash_intraday=200_000), EventStore(":memory:"))
    service.configure_experiment(PaperExperimentSettings(
        strategy_id="tw-realism",
        market=Market.TW,
        base_currency="TWD",
        initial_cash=200_000,
        max_position_notional=200_000,
        enabled=True,
        universe=["2330.TW"],
        mode=DecisionScope.INTRADAY,
    ))
    ledger = service.portfolio_manager.get_ledger(DecisionScope.INTRADAY, "tw-realism")
    ledger.positions["2330.TW"] = ledger.positions.get("2330.TW") or Position(
        symbol="2330.TW", bucket=DecisionScope.INTRADAY, currency="TWD", quantity=100,
    )
    preview = service.preview(PaperOrderRequest(
        symbol="2330.TW", market=Market.TW, bucket=DecisionScope.INTRADAY,
        side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=100,
        origin=OrderOrigin.STRATEGY, strategy_id="tw-realism", reason="test day trade tax",
        data=PaperDataContext(last_price=1000),
    ))
    assert preview["status"] == "APPROVED"
    assert preview["risk_decision"]["estimated_tax"] == pytest.approx(149.925)


def test_tw_day_trade_metadata_is_restricted_to_intraday_tw_orders() -> None:
    req = PaperOrderRequest(
        symbol="2330.TW",
        market=Market.TW,
        bucket=DecisionScope.INTRADAY,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=1,
        origin=OrderOrigin.MANUAL,
        reason="same-session simulated exit",
        audit_metadata={"tw_trade_mode": "DAY_TRADE"},
    )
    assert req.audit_metadata["tw_trade_mode"] == "DAY_TRADE"

    default_intraday = PaperOrderRequest(
        symbol="2330.TW",
        market=Market.TW,
        bucket=DecisionScope.INTRADAY,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=1,
        origin=OrderOrigin.MANUAL,
        reason="intraday entry defaults to day trade",
    )
    assert default_intraday.audit_metadata["tw_trade_mode"] == "DAY_TRADE"

    with pytest.raises(ValueError, match="DAY_TRADE_MODE_REQUIRES_TW_MARKET"):
        PaperOrderRequest(
            symbol="AAPL",
            market=Market.US,
            bucket=DecisionScope.INTRADAY,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=1,
            origin=OrderOrigin.MANUAL,
            reason="invalid market",
            audit_metadata={"tw_trade_mode": "DAY_TRADE"},
        )

    with pytest.raises(ValueError, match="DAY_TRADE_MODE_REQUIRES_INTRADAY_BUCKET"):
        PaperOrderRequest(
            symbol="2330.TW",
            market=Market.TW,
            bucket=DecisionScope.SWING,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=1,
            origin=OrderOrigin.MANUAL,
            reason="invalid bucket",
            audit_metadata={"tw_trade_mode": "DAY_TRADE"},
        )
