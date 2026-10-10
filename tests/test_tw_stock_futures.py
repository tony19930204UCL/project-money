"""End-to-end PAPER tests using the existing PaperDerivativesEngine."""
from datetime import datetime, timedelta, timezone

import pytest

from cio_market_lab.domain.models import OrderSide
from cio_market_lab.engine.paper_derivatives import (
    CostConfig, DerivativeInstrumentType, DerivativeQuote, PaperDerivativesEngine,
)
from cio_market_lab.engine.tw_stock_futures import make_stock_future_spec, stock_tick_size

NOW = datetime(2026, 10, 10, 2, tzinfo=timezone.utc)


def spec(mini=False, **kwargs):
    return make_stock_future_spec("2330", NOW + timedelta(days=30), mini=mini, reference_price=1000, **kwargs)


def quote(contract, price, at=NOW):
    return DerivativeQuote(symbol=contract.symbol, timestamp=at, bid=price, ask=price, last_price=price, is_fixture=True)


def engine():
    return PaperDerivativesEngine(cost_config=CostConfig(fee_rate=0, tax_rate=0, fee_per_contract=0, min_fee=0, slippage_ticks=0))


def open_position(contract, side=OrderSide.BUY, price=1000, cash=1_000_000):
    e = engine()
    result = e.attempt_execution(order_id="open", spec=contract, quote=quote(contract, price), side=side, quantity=1, available_cash=cash, as_of=NOW)
    assert result.success, result.rejection_reason
    assert result.position is not None
    return e, result.position


def test_contract_identity_and_assumption_rates():
    s = spec()
    assert s.instrument_type == DerivativeInstrumentType.FUTURE
    assert s.multiplier == 2000
    assert s.tick_size == 5
    assert s.currency == "TWD"
    assert s.initial_margin_rate == 0.135
    assert s.maintenance_margin_rate == 0.1035
    assert "SIMULATION_ASSUMPTION" in s.margin_rules_label


@pytest.mark.parametrize("price,tick", [(9.99, .01), (10, .05), (49.99, .05), (50, .1), (100, .5), (500, 1), (1000, 5)])
def test_reference_price_tick_ladder(price, tick):
    assert stock_tick_size(price) == tick


def test_overridable_margin_rates():
    s = spec(initial_margin_rate=.20, maintenance_margin_rate=.15)
    assert (s.initial_margin_rate, s.maintenance_margin_rate) == (.20, .15)


def test_long_open_and_daily_mtm():
    s = spec()
    e, pos = open_position(s)
    assert pos.quantity == 1
    assert pos.margin_locked == pytest.approx(270000)
    day2 = NOW + timedelta(days=1)
    marked = e.mark_to_market(pos, s, quote(s, 1010, day2), as_of=day2)
    assert marked.unrealized_pnl == pytest.approx(20000)
    settled = e.settle_daily_variation(pos, s, 1010, "2026-10-11", as_of=day2)
    assert settled.variation_pnl == pytest.approx(20000)
    assert settled.delta.cash_delta == pytest.approx(20000)
    assert pos.last_settlement_price == 1010
    assert e.settle_daily_variation(pos, s, 1010, "2026-10-12", as_of=day2 + timedelta(days=1)).variation_pnl == 0


def test_margin_call_below_maintenance():
    s = spec()
    e, pos = open_position(s)
    review = e.check_position_risk(pos, s, quote(s, 900), total_account_equity=200000, total_maintenance_required=1000 * 2000 * s.maintenance_margin_rate, as_of=NOW)
    assert review.margin_deficient
    assert review.liquidation_required
    assert any(event.event_type == "LIQUIDATION_REQUIRED" for event in review.events)


def test_close_realizes_only_unsettled_variation():
    s = spec()
    e, pos = open_position(s)
    day2 = NOW + timedelta(days=1)
    settled = e.settle_daily_variation(pos, s, 1010, "2026-10-11", as_of=day2)
    closed = e.attempt_execution(order_id="close", spec=s, quote=quote(s, 1020, day2), side=OrderSide.SELL, quantity=1, available_cash=1_000_000, existing_position=pos, as_of=day2)
    assert closed.success, closed.rejection_reason
    assert settled.variation_pnl == 20000
    assert closed.delta.realized_pnl_delta == pytest.approx(20000)
    assert closed.position.is_closed
    assert closed.position.quantity == 0
    assert closed.position.margin_locked == 0


def test_mini_multiplier_and_mtm():
    s = spec(mini=True)
    e, pos = open_position(s)
    assert s.multiplier == 100
    assert pos.margin_locked == pytest.approx(13500)
    result = e.settle_daily_variation(pos, s, 1010, "2026-10-11", as_of=NOW + timedelta(days=1))
    assert result.variation_pnl == pytest.approx(1000)


def test_short_profit_when_price_falls():
    s = spec()
    e, pos = open_position(s, side=OrderSide.SELL)
    assert pos.quantity == -1
    day2 = NOW + timedelta(days=1)
    result = e.settle_daily_variation(pos, s, 990, "2026-10-11", as_of=day2)
    assert result.variation_pnl == pytest.approx(20000)
    closed = e.attempt_execution(order_id="cover", spec=s, quote=quote(s, 985, day2), side=OrderSide.BUY, quantity=1, available_cash=1_000_000, existing_position=pos, as_of=day2)
    assert closed.success
    assert closed.delta.realized_pnl_delta == pytest.approx(10000)
    assert closed.position.is_closed


def test_insufficient_initial_margin_rejected():
    s = spec()
    e = engine()
    result = e.attempt_execution(order_id="low-cash", spec=s, quote=quote(s, 1000), side=OrderSide.BUY, quantity=1, available_cash=1000, as_of=NOW)
    assert not result.success
    assert result.rejection_reason == "MARGIN_DEFICIENCY"
