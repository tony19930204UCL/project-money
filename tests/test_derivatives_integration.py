"""Isolated fixture-only execution proof; not live acceptance evidence."""
from datetime import datetime, timedelta, timezone

import pytest

from cio_market_lab.domain.events import EventType
from cio_market_lab.domain.models import CIODecisionPacket, CIOProvenance, DecisionScope, OrderSide
from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner
from cio_market_lab.engine.cio_packet import sign_cio_packet
from cio_market_lab.engine.derivative_lifecycle import PaperDerivativeLifecycle
from cio_market_lab.engine.paper_derivatives import ContractSpec, DerivativeQuote
from cio_market_lab.engine.paper_orders import PaperExperimentSettings, PaperOrderService
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.events.store import EventStore

T0 = datetime(2026, 9, 29, 10, tzinfo=timezone.utc)
BUCKET = DecisionScope.SWING
STRATEGY = "isolated-derivatives"


def setup(tmp_path, symbol="OPT-1", initial_cash=1_000_000):
    store = EventStore(tmp_path / "events.sqlite")
    pm = PortfolioManager()
    pm.register_strategy(STRATEGY, initial_cash, unified_cash=True)
    orders = PaperOrderService(pm, store)
    orders.experiments[STRATEGY] = PaperExperimentSettings(
        strategy_id=STRATEGY, enabled=True, initial_cash=initial_cash,
        base_currency="TWD", universe=[symbol], max_position_notional=500_000,
    )
    return PaperDerivativeLifecycle(pm, orders, fixture_mode=True), pm, store


def option():
    return ContractSpec(symbol="OPT-1", underlying_symbol="ABC", instrument_type="OPTION",
                        option_right="CALL", strike=100, expiry=T0 + timedelta(hours=2),
                        multiplier=10, tick_size=1, pre_expiry_close_lead_seconds=3600)


def future(maintenance=50_000):
    return ContractSpec(symbol="FUT-1", underlying_symbol="ABC", instrument_type="FUTURE",
                        expiry=T0 + timedelta(days=5), multiplier=10, tick_size=1,
                        initial_margin_per_contract=100_000,
                        maintenance_margin_per_contract=maintenance)


def quote(sym, when=T0, bid=10, ask=11):
    return DerivativeQuote(symbol=sym, timestamp=when, observed_at=when,
                           bid=bid, ask=ask, last_price=bid,
                           is_fixture=True, source="isolated_fixture",
                           provenance={"authority": "isolated_test_fixture"})


def ledger(pm):
    return pm.get_strategy_ledger(STRATEGY, BUCKET)


def recorded(store):
    return [e.payload for _, e in store.get_events(event_type=EventType.POSITION_UPDATED)]


def test_option_open_mark_close_replay_single_cash_pool(tmp_path):
    svc, pm, store = setup(tmp_path)
    spec = option()
    opened = svc.execute(strategy_id=STRATEGY, bucket=BUCKET, spec=spec, quote=quote(spec.symbol),
                         side=OrderSide.BUY, quantity=2, order_id="open-1", now=T0)
    assert opened.success and opened.fill_price == 11
    assert ledger(pm).positions[spec.symbol].quantity == 2
    assert ledger(pm).cash < 1_000_000
    after_open = ledger(pm).cash
    t1 = T0 + timedelta(minutes=5)
    assert svc.review(strategy_id=STRATEGY, bucket=BUCKET, symbol=spec.symbol,
                      now=t1, quote=quote(spec.symbol, t1, 13, 14)) == "MARKED"
    assert ledger(pm).positions[spec.symbol].market_value == 260
    assert ledger(pm).equity == pytest.approx(ledger(pm).cash + 260)
    closed = svc.execute(strategy_id=STRATEGY, bucket=BUCKET, spec=spec,
                         quote=quote(spec.symbol, t1, 13, 14), side=OrderSide.SELL,
                         quantity=2, order_id="close-1", now=t1)
    assert closed.success and closed.fill_price == 13
    assert ledger(pm).positions[spec.symbol].quantity == 0
    final_cash = ledger(pm).cash
    assert final_cash > after_open
    assert len([x for x in recorded(store) if x["normalized_event_type"] == "DERIVATIVE_OPTION_CLOSED"]) == 1
    assert svc.replay() == 0
    assert ledger(pm).cash == final_cash
    fresh, new_pm, _ = setup(tmp_path)
    assert fresh.replay() == 3
    assert ledger(new_pm).cash == pytest.approx(final_cash)
    assert ledger(new_pm).positions[spec.symbol].quantity == 0


def test_pre_expiry_pending_missing_quote_then_executable_risk_close(tmp_path):
    svc, pm, store = setup(tmp_path)
    spec = option()
    svc.execute(strategy_id=STRATEGY, bucket=BUCKET, spec=spec, quote=quote(spec.symbol),
                side=OrderSide.BUY, quantity=1, order_id="open", now=T0)
    before = ledger(pm).cash
    at = T0 + timedelta(hours=1, minutes=30)
    assert svc.review(strategy_id=STRATEGY, bucket=BUCKET, symbol=spec.symbol, now=at) == "UNRESOLVED_EXECUTABLE_QUOTE"
    assert ledger(pm).cash == before
    assert ledger(pm).equity is None
    assert ledger(pm).positions[spec.symbol].quantity == 1
    assert svc.review(strategy_id=STRATEGY, bucket=BUCKET, symbol=spec.symbol,
                      now=at, quote=quote(spec.symbol, at, 8, 9)) == "PAPER_RISK_CLOSE_FILLED"
    assert ledger(pm).positions[spec.symbol].quantity == 0
    assert ledger(pm).cash > before
    assert len([x for x in recorded(store) if x["normalized_event_type"] == "PRE_EXPIRY_CLOSE_REQUIRED"]) == 1
    assert len([x for x in recorded(store) if x["normalized_event_type"] == "DERIVATIVE_OPTION_CLOSED"]) == 1
    pending = [x for x in recorded(store) if x["normalized_event_type"] == "DERIVATIVE_RISK_EXECUTION_PENDING"]
    assert len(pending) == 1 and pending[0]["execution_status"] == "PENDING_EXECUTION"
    closed_event = [x for x in recorded(store) if x["normalized_event_type"] == "DERIVATIVE_OPTION_CLOSED"][0]
    assert closed_event["risk_exit"] and closed_event["execution_status"] == "PAPER_RISK_CLOSE_FILLED"


def test_overdue_expiry_retained_no_fabricated_settlement_and_replay(tmp_path):
    svc, pm, store = setup(tmp_path)
    spec = option()
    svc.execute(strategy_id=STRATEGY, bucket=BUCKET, spec=spec, quote=quote(spec.symbol),
                side=OrderSide.BUY, quantity=1, order_id="open", now=T0)
    before = ledger(pm).cash
    overdue = T0 + timedelta(hours=3)
    for _ in range(2):
        assert svc.review(strategy_id=STRATEGY, bucket=BUCKET, symbol=spec.symbol,
                          now=overdue) == "UNRESOLVED_EXPIRY_DELIVERY_UNSUPPORTED"
    assert ledger(pm).cash == before
    assert ledger(pm).positions[spec.symbol].quantity == 1
    assert ledger(pm).equity is None
    assert len([x for x in recorded(store) if x["normalized_event_type"] == "EXPIRY_OVERDUE_UNRESOLVED"]) == 1
    assert ledger(pm).settle_expired_options(overdue, {"ABC": 500})[0]["cash_settlement"] is False
    assert ledger(pm).cash == before
    fresh, pm2, _ = setup(tmp_path)
    fresh.replay()
    assert ledger(pm2).cash == pytest.approx(before)
    assert ledger(pm2).positions[spec.symbol].quantity == 1
    with pytest.raises(ValueError, match="CONTRACT_EXPIRED"):
        raise ValueError(svc.execute(strategy_id=STRATEGY, bucket=BUCKET, spec=spec,
                                     quote=quote(spec.symbol, overdue), side=OrderSide.SELL,
                                     quantity=1, order_id="late", now=overdue).rejection_reason)


def test_futures_variation_and_margin_liquidation(tmp_path):
    svc, pm, store = setup(tmp_path, symbol="FUT-1")
    spec = future(maintenance=1_100_000)
    opened = svc.execute(strategy_id=STRATEGY, bucket=BUCKET, spec=spec,
                         quote=quote(spec.symbol, bid=9999, ask=10001), side=OrderSide.BUY,
                         quantity=1, order_id="future-open", now=T0)
    assert opened.success
    cash = ledger(pm).cash
    t1 = T0 + timedelta(minutes=5)
    assert svc.review(strategy_id=STRATEGY, bucket=BUCKET, symbol=spec.symbol,
                      now=t1, quote=quote(spec.symbol, t1, 10020, 10021)) == "PAPER_RISK_CLOSE_FILLED"
    assert ledger(pm).cash != cash
    assert ledger(pm).positions[spec.symbol].quantity == 0
    assert len([x for x in recorded(store) if x["normalized_event_type"] == "LIQUIDATION_REQUIRED"]) == 1
    assert len([x for x in recorded(store) if x["normalized_event_type"] == "DERIVATIVE_RISK_EXECUTION_PENDING"]) == 1


def test_futures_daily_settlement_once_and_missing_price_no_fill(tmp_path):
    svc, pm, store = setup(tmp_path, symbol="FUT-1")
    spec = future()
    assert svc.execute(strategy_id=STRATEGY, bucket=BUCKET, spec=spec,
                       quote=quote(spec.symbol, bid=9999, ask=10001), side=OrderSide.BUY,
                       quantity=1, order_id="open", now=T0).success
    at = T0 + timedelta(hours=1)
    assert svc.settle_futures_daily(strategy_id=STRATEGY, bucket=BUCKET, symbol=spec.symbol,
                                    settlement_price=10020, settlement_date="2026-09-29",
                                    quote=quote(spec.symbol, at, 10019, 10021), now=at)
    cash = ledger(pm).cash
    assert not svc.settle_futures_daily(strategy_id=STRATEGY, bucket=BUCKET, symbol=spec.symbol,
                                        settlement_price=10020, settlement_date="2026-09-29",
                                        quote=quote(spec.symbol, at, 10019, 10021), now=at)
    assert ledger(pm).cash == cash
    assert ledger(pm).positions[spec.symbol].assumptions["derivative_position"]["last_settlement_price"] == 10020
    assert svc.review(strategy_id=STRATEGY, bucket=BUCKET, symbol=spec.symbol,
                      now=at + timedelta(minutes=10)) == "UNRESOLVED_PRICE"
    assert ledger(pm).positions[spec.symbol].quantity == 1
    assert ledger(pm).equity is None
    fresh, pm2, _ = setup(tmp_path, symbol="FUT-1")
    fresh.replay()
    assert ledger(pm2).cash == pytest.approx(cash)
    assert ledger(pm2).equity is None


def test_futures_partial_close_and_add_preserve_unsettled_variation(tmp_path):
    svc, pm, store = setup(tmp_path, symbol="FUT-1")
    spec = future()
    assert svc.execute(strategy_id=STRATEGY, bucket=BUCKET, spec=spec,
                       quote=quote(spec.symbol, bid=9999, ask=10001), side=OrderSide.BUY,
                       quantity=2, order_id="open", now=T0).success
    at = T0 + timedelta(minutes=1)
    assert svc.execute(strategy_id=STRATEGY, bucket=BUCKET, spec=spec,
                       quote=quote(spec.symbol, at, 10020, 10021), side=OrderSide.SELL,
                       quantity=1, order_id="partial", now=at).success
    pos = ledger(pm).positions[spec.symbol]
    assert pos.assumptions["derivative_position"]["last_settlement_price"] == 10001
    assert pos.market_value == pytest.approx(190)
    at += timedelta(minutes=1)
    assert svc.execute(strategy_id=STRATEGY, bucket=BUCKET, spec=spec,
                       quote=quote(spec.symbol, at, 10030, 10031), side=OrderSide.BUY,
                       quantity=1, order_id="add", now=at).success
    pos = ledger(pm).positions[spec.symbol]
    assert pos.assumptions["derivative_position"]["last_settlement_price"] == 10016
    assert pos.market_value == pytest.approx(300)
    assert ledger(pm).equity == pytest.approx(ledger(pm).cash + 300)
    fresh, pm2, _ = setup(tmp_path, symbol="FUT-1")
    assert fresh.replay() == 3
    assert ledger(pm2).cash == pytest.approx(ledger(pm).cash)
    assert ledger(pm2).equity == pytest.approx(ledger(pm).equity)
    at += timedelta(minutes=1)
    assert svc.settle_futures_daily(strategy_id=STRATEGY, bucket=BUCKET, symbol=spec.symbol,
                                    settlement_price=10040, settlement_date="2026-09-29",
                                    quote=quote(spec.symbol, at, 10039, 10041), now=at)
    assert ledger(pm).cash == pytest.approx(ledger(pm2).cash + 480)
    assert ledger(pm).positions[spec.symbol].market_value == 0


def test_default_lifecycle_cannot_bypass_unavailable_gate(tmp_path):
    _, pm, store = setup(tmp_path, symbol="FUT-1")
    orders = PaperOrderService(pm, store)
    orders.experiments[STRATEGY] = PaperExperimentSettings(
        strategy_id=STRATEGY, enabled=True, initial_cash=1_000_000,
        base_currency="TWD", universe=["FUT-1"],
    )
    svc = PaperDerivativeLifecycle(pm, orders)
    real_label = quote("FUT-1", bid=9999, ask=10001).model_copy(update={"is_fixture": False})
    with pytest.raises(ValueError, match="DERIVATIVES_UNAVAILABLE_PENDING_ADAPTER_ACCEPTANCE"):
        svc.execute(strategy_id=STRATEGY, bucket=BUCKET, spec=future(), quote=real_label,
                    side=OrderSide.BUY, quantity=1, order_id="blocked", now=T0)
    with pytest.raises(ValueError, match="DERIVATIVES_UNAVAILABLE_PENDING_ADAPTER_ACCEPTANCE"):
        svc.settle_futures_daily(strategy_id=STRATEGY, bucket=BUCKET, symbol="FUT-1",
                                 settlement_price=10020, settlement_date="2026-09-29",
                                 quote=real_label, now=T0)
    assert store.count() == 0 and ledger(pm).cash == 1_000_000


def test_fixture_gate_no_naked_short_no_currency_or_notional_bypass(tmp_path):
    svc, pm, store = setup(tmp_path)
    spec = option()
    with pytest.raises(ValueError, match="NO_EXECUTABLE_CONTRACT_QUOTE"):
        svc.execute(strategy_id=STRATEGY, bucket=BUCKET, spec=spec,
                    quote=quote(spec.symbol).model_copy(update={"bid": None}),
                    side=OrderSide.BUY, quantity=1, order_id="missing", now=T0)
    with pytest.raises(ValueError, match="FIXTURE_MODE_MISMATCH"):
        svc.execute(strategy_id=STRATEGY, bucket=BUCKET, spec=spec,
                    quote=quote(spec.symbol).model_copy(update={"is_fixture": False}),
                    side=OrderSide.BUY, quantity=1, order_id="unlabelled", now=T0)
    short = svc.execute(strategy_id=STRATEGY, bucket=BUCKET, spec=spec, quote=quote(spec.symbol),
                        side=OrderSide.SELL, quantity=1, order_id="short", now=T0)
    assert not short.success and short.rejection_reason == "NAKED_OPTION_SHORT_FORBIDDEN"
    with pytest.raises(ValueError, match="DERIVATIVE_NOTIONAL_LIMIT"):
        svc.execute(strategy_id=STRATEGY, bucket=BUCKET, spec=spec,
                    quote=quote(spec.symbol, bid=49999, ask=50000),
                    side=OrderSide.BUY, quantity=1, order_id="too-big", now=T0)
    assert store.count() == 0 and ledger(pm).cash == 1_000_000


class ContractOnlyFixtureAdapter:
    """Deliberately has no spot-bar route for contracts."""
    def __init__(self):
        self.contract_quote = None

    def get_latest_bar(self, symbol):
        raise AssertionError("derivative must not request a spot bar")

    def get_derivative_quote(self, symbol):
        return self.contract_quote


def runner_setup(tmp_path, clock, store=None, adapter=None, fixture=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = store or EventStore(tmp_path / "runner_events.sqlite")
    pm = PortfolioManager()
    po = PaperOrderService(pm, store)
    adapter = adapter or ContractOnlyFixtureAdapter()
    runner = AutonomousPaperRunner(tmp_path, pm, po, adapter, now_fn=lambda: clock[0],
                                   isolated_derivative_fixture_mode=fixture)
    if STRATEGY not in po.experiments:
        runner.configure(PaperExperimentSettings(strategy_id=STRATEGY, enabled=True,
                                                initial_cash=1_000_000, base_currency="TWD",
                                                universe=["OPT-1"], max_position_notional=500_000))
    return runner, pm, store, adapter


def test_runner_packet_risk_cycle_restart_and_unavailable_gate(tmp_path):
    clock = [T0]
    runner, pm, store, adapter = runner_setup(tmp_path, clock)
    spec = option()
    packet = CIODecisionPacket(case_id="fixture-option-open", as_of=T0,
                               expiry=T0 + timedelta(hours=2), thesis="fixture integration proof",
                               selected_instrument=spec.symbol, action="BUY", quantity=1,
                               conditions={"strategy_id": STRATEGY,
                                           "contract_spec": spec.model_dump(mode="json"),
                                           "derivative_quote": quote(spec.symbol).model_dump(mode="json")},
                               provenance=CIOProvenance(authority="MAIN_CIO"), is_fixture=True)
    sign_cio_packet(packet)
    outcome = runner.submit_cio_packet(packet)
    assert outcome.action == "BUY_FILLED", outcome.reason
    before = ledger(pm).cash
    assert ledger(pm).positions[spec.symbol].quantity == 1
    clock[0] = T0 + timedelta(hours=1, minutes=30)
    assert runner.review_derivative_positions(STRATEGY) == {"swing:OPT-1": "UNRESOLVED_EXECUTABLE_QUOTE"}
    assert ledger(pm).cash == before and ledger(pm).equity is None
    adapter.contract_quote = quote(spec.symbol, clock[0], 8, 9).model_dump(mode="json")
    assert runner.review_derivative_positions(STRATEGY) == {"swing:OPT-1": "PAPER_RISK_CLOSE_FILLED"}
    final = ledger(pm).cash
    assert ledger(pm).positions[spec.symbol].quantity == 0 and final > before
    assert any(x["normalized_event_type"] == "DERIVATIVE_RISK_EXECUTION_PENDING" for x in recorded(store))
    restarted, pm2, _, _ = runner_setup(tmp_path, clock, store=store, adapter=adapter)
    assert ledger(pm2).cash == pytest.approx(final)
    assert ledger(pm2).positions[spec.symbol].quantity == 0
    assert restarted.review_derivative_positions(STRATEGY) == {}
    assert ledger(pm2).cash == pytest.approx(final)

    # Even a well-formed fixture packet never opens in the default runtime.
    blocked_root = tmp_path / "blocked"
    blocked, blocked_pm, _, _ = runner_setup(blocked_root, [T0], fixture=False)
    non_fixture = packet.model_copy(deep=True)
    non_fixture.is_fixture = False
    non_fixture.conditions["derivative_quote"]["is_fixture"] = False
    sign_cio_packet(non_fixture)
    assert blocked.submit_cio_packet(non_fixture).reason == (
        "CAPABILITY_UNAVAILABLE:DERIVATIVES_PENDING_LIVE_PER_CONTRACT_SOURCE_AND_LIFECYCLE_ACCEPTANCE")
    assert ledger(blocked_pm).cash == 1_000_000
    resumed, resumed_pm, resumed_store, _ = runner_setup(blocked_root, [T0], fixture=False)
    assert resumed.paper_orders.experiments[STRATEGY].enabled
    assert resumed.submit_cio_packet(non_fixture).reason == (
        "CAPABILITY_UNAVAILABLE:DERIVATIVES_PENDING_LIVE_PER_CONTRACT_SOURCE_AND_LIFECYCLE_ACCEPTANCE")
    assert resumed_store.count() == 1  # experiment configuration only; no derivative fill
    assert ledger(resumed_pm).cash == 1_000_000
