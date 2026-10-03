"""Regression tests pinning multicase accounting defects and verifying Main arithmetic specification.

Pinning defects BEFORE patch:
1. Double-debit of slippage in portfolio cash and realized PnL.
2. Cross-strategy case matching and un-scoped same-symbol matching.
3. Premature case closure and lesson finalization on partial close.
4. Cumulative ledger realized PnL assigned to per-case outcome instead of delta-based attribution,
   and full entry cost subtracted instead of proportional allocation.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
import pytest

from cio_market_lab.data.base import MarketDataAdapter
from cio_market_lab.domain.models import (
    Bar,
    CIODecisionPacket,
    CIOProvenance,
    DecisionScope,
    Fill,
    Market,
    OrderSide,
    Quote,
)
from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner
from cio_market_lab.engine.cio_packet import sign_cio_packet
from cio_market_lab.engine.paper_orders import (
    PaperExperimentSettings,
    PaperOrderService,
)
from cio_market_lab.engine.portfolio import (
    CanonicalCashAccount,
    Ledger,
    PortfolioManager,
)
from cio_market_lab.events.store import EventStore


class SyntheticQuoteAdapter(MarketDataAdapter):
    def __init__(self):
        self.quotes = {}
        self.bars = {}

    @property
    def source_name(self) -> str:
        return "synthetic-fixture-adapter"

    def set_quote(self, symbol: str, dt: datetime, price: float):
        self.quotes[symbol] = Quote(bid_size=1000, ask_size=1000,
            quote_id=f"TEST_ONLY_{symbol}_{(dt).isoformat()}",
            session="ODD_LOT" if (symbol).endswith(".TW") else "REGULAR",
            source_capabilities={"source": "fixture_authoritative", "two_sided_book": True,
                "size_backed": True, "exchange_session_attested": True,
                "entitlement_evidence_id": "TEST_ONLY_FIXTURE_ODD_LOT",
                "entitlement_status": "TEST_ONLY", "odd_lot_book": True,
                "supported_sessions": ["REGULAR", "ODD_LOT"]},
            
            symbol=symbol,
            timestamp=dt,
            observed_at=dt,
            bid=price,
            ask=price,
            last_price=price,
            source="fixture_authoritative",
            quality="good",
            is_stale=False,
            is_synthetic=False,
        )

    def set_bar(self, symbol: str, dt: datetime, o: float, h: float, l: float, c: float):
        b = Bar(
            symbol=symbol,
            timestamp=dt,
            observed_at=dt,
            open=o,
            high=h,
            low=l,
            close=c,
            volume=1000,
            source="synthetic_bar",
            quality="good",
        )
        self.bars.setdefault(symbol, []).append(b)

    def get_latest_quote(self, symbol: str):
        return self.quotes.get(symbol)

    def get_latest_bar(self, symbol: str) -> Optional[Bar]:
        bars = self.bars.get(symbol, [])
        return bars[-1] if bars else None

    def stream_bars(self, symbols: list):
        for s in symbols:
            for b in self.bars.get(s, []):
                yield b

    def get_bars(self, symbol: str, timeframe: str = "1D", limit: int = 100):
        return self.bars.get(symbol, [])[-limit:]


def test_regression_embedded_slippage_not_double_debited_in_portfolio():
    """Pin defect: portfolio.py lines 129-130 & 167-169 subtract fill.slippage when slippage is embedded."""
    initial_cash = 1_000_000.0
    cash_acc = CanonicalCashAccount(initial_cash=initial_cash)
    ledger = Ledger(DecisionScope.SWING, initial_cash=initial_cash, cash_account=cash_acc)

    # Buy fill with embedded slippage
    # effective fill price = 1000.5 (1000 + 0.5 slippage)
    # notional = 20 * 1000.5 = 20010.0
    # fee = 28.5143, tax = 0.0, informational slippage = 10.0
    buy_fill = Fill(
        fill_id="f-buy-1",
        order_id="o-buy-1",
        symbol="2330.TW",
        bucket=DecisionScope.SWING,
        side=OrderSide.BUY,
        quantity=20.0,
        fill_price=1000.5,
        fee=28.5143,
        tax=0.0,
        slippage=10.0,
        timestamp=datetime.now(timezone.utc),
        assumptions={"slippage_embedded": True, "timing_assumption": "authoritative_later_quote_slippage_adjusted"},
    )
    ledger.apply_fill(buy_fill)

    # Expected debit = 20010.0 + 28.5143 = 20038.5143
    # Expected remaining cash = 1000000 - 20038.5143 = 979961.4857
    expected_post_buy_cash = 1_000_000.0 - (20.0 * 1000.5 + 28.5143)
    assert abs(ledger.cash - expected_post_buy_cash) < 1e-4, f"Cash double-debited buy slippage: {ledger.cash} vs {expected_post_buy_cash}"

    # Sell fill with embedded slippage
    # effective exit price = 1049.475 (1050 - 0.525 slippage)
    # notional = 10 * 1049.475 = 10494.75
    # fee = 14.955, tax = 31.4843, informational slippage = 5.25
    sell_fill = Fill(
        fill_id="f-sell-1",
        order_id="o-sell-1",
        symbol="2330.TW",
        bucket=DecisionScope.SWING,
        side=OrderSide.SELL,
        quantity=10.0,
        fill_price=1049.475,
        fee=14.955,
        tax=31.4843,
        slippage=5.25,
        timestamp=datetime.now(timezone.utc),
        assumptions={"slippage_embedded": True, "timing_assumption": "authoritative_later_quote_slippage_adjusted"},
    )
    ledger.apply_fill(sell_fill)

    # Expected credit = 10494.75 - (14.955 + 31.4843) = 10448.3107
    expected_post_sell_cash = expected_post_buy_cash + (10494.75 - (14.955 + 31.4843))
    assert abs(ledger.cash - expected_post_sell_cash) < 1e-4, f"Cash double-debited sell slippage: {ledger.cash} vs {expected_post_sell_cash}"

    # Realized PnL on sell: (1049.475 - 1000.5) * 10 - (14.955 + 31.4843) = 489.75 - 46.4393 = 443.3107
    expected_realized = (1049.475 - 1000.5) * 10.0 - (14.955 + 31.4843)
    assert abs(ledger.realized_pnl - expected_realized) < 1e-4, f"Realized PnL double-debited slippage: {ledger.realized_pnl} vs {expected_realized}"


def test_regression_cross_strategy_and_ambiguous_scoping(tmp_path: Path):
    """Pin defect: runner lines 1388-1393 matches first same-symbol case across strategies or multiple open cases."""
    import os
    os.environ["CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS"] = "1"
    now = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
    clock = [now]
    adapter = SyntheticQuoteAdapter()
    pm = PortfolioManager(initial_cash_swing=1_000_000.0, initial_cash_intraday=1_000_000.0)
    strat_a = "strategy-alpha"
    strat_b = "strategy-beta"
    pm.register_strategy(strat_a, 1_000_000.0, unified_cash=True, currency="TWD")
    pm.register_strategy(strat_b, 1_000_000.0, unified_cash=True, currency="TWD")
    service = PaperOrderService(pm, EventStore(":memory:"))
    runner = AutonomousPaperRunner(tmp_path, pm, service, adapter, now_fn=lambda: clock[0], require_cio_provider=True)
    runner.configure(PaperExperimentSettings(strategy_id=strat_a, enabled=True, universe=["2330.TW"], initial_cash=1_000_000.0))
    runner.configure(PaperExperimentSettings(strategy_id=strat_b, enabled=True, universe=["2330.TW"], initial_cash=1_000_000.0, base_currency="TWD"))
    runner.allow_fixture_quotes = True  # Explicit isolated test-only opt-in.

    adapter.set_bar("2330.TW", now, 1000.0, 1005.0, 995.0, 1000.0)
    adapter.set_quote("2330.TW", now, 1000.0)

    # Strategy A opens 2330.TW
    pkt_a = CIODecisionPacket(
        case_id="case-strat-a-buy",
        as_of=now,
        evidence=["e1"],
        thesis="Strategy A long",
        selected_instrument="2330.TW",
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=10.0,
        conditions={**({"strategy_id": strat_a}), "allow_odd_lot": True},
        expiry=now + timedelta(hours=2),
        confidence=0.9,
        strategy_version="v1",
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER", signer_id="main-cio-key", source="ext"),
        is_fixture=True,
    )
    sign_cio_packet(pkt_a, signer_id="main-cio-key")
    runner.submit_cio_packet(pkt_a)  # BUY_PENDING
    clock[0] = now + timedelta(minutes=1)
    adapter.set_quote("2330.TW", clock[0], 1000.0)
    d_a_fill = runner.submit_cio_packet(pkt_a)  # BUY_FILLED
    assert d_a_fill.action == "BUY_FILLED"

    # Strategy B tries to SELL 2330.TW without target_case_id (Strategy B has NO open position)
    t_sell = clock[0] + timedelta(hours=1)
    clock[0] = t_sell
    adapter.set_bar("2330.TW", t_sell, 1050.0, 1055.0, 1045.0, 1050.0)
    adapter.set_quote("2330.TW", t_sell, 1050.0)

    pkt_b_sell = CIODecisionPacket(
        case_id="case-strat-b-sell",
        as_of=t_sell,
        evidence=["e2"],
        thesis="Strategy B sell",
        selected_instrument="2330.TW",
        action="SELL",
        holding_horizon=DecisionScope.SWING,
        quantity=10.0,
        conditions={**({"strategy_id": strat_b}), "allow_odd_lot": True},
        expiry=t_sell + timedelta(hours=2),
        confidence=0.9,
        strategy_version="v1",
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER", signer_id="main-cio-key", source="ext"),
        is_fixture=True,
    )
    sign_cio_packet(pkt_b_sell, signer_id="main-cio-key")

    # Under buggy code: Strategy B matches Strategy A's case-strat-a-buy!
    # Under correct code: MUST NOT cross strategy cases; must fail closed before any side effect
    d_b_sell = runner.submit_cio_packet(pkt_b_sell)
    assert d_b_sell.action in ("NO_TRADE", "SELL_REJECTED"), f"Strategy B illegally matched Strategy A's case: {d_b_sell.action}"


def test_ambiguous_matching_fails_closed_before_side_effects(tmp_path: Path):
    """Pin specification: multiple open cases for same strategy/bucket/symbol without target_case_id must fail closed."""
    import os
    os.environ["CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS"] = "1"
    now = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
    clock = [now]
    adapter = SyntheticQuoteAdapter()
    pm = PortfolioManager(initial_cash_swing=1_000_000.0, initial_cash_intraday=1_000_000.0)
    strat = "strategy-alpha"
    pm.register_strategy(strat, 1_000_000.0, unified_cash=True)
    service = PaperOrderService(pm, EventStore(":memory:"))
    runner = AutonomousPaperRunner(tmp_path, pm, service, adapter, now_fn=lambda: clock[0], require_cio_provider=True)
    runner.configure(PaperExperimentSettings(strategy_id=strat, enabled=True, universe=["2330.TW"], initial_cash=1_000_000.0))

    adapter.set_bar("2330.TW", now, 1000.0, 1005.0, 995.0, 1000.0)
    adapter.set_quote("2330.TW", now, 1000.0)

    # Open Case 1
    runner.allow_fixture_quotes = True
    pkt1 = CIODecisionPacket(
        case_id="case-open-1",
        as_of=now,
        evidence=["e1"],
        thesis="Long tranche 1",
        selected_instrument="2330.TW",
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=10.0,
        conditions={**({"strategy_id": strat}), "allow_odd_lot": True},
        expiry=now + timedelta(hours=2),
        confidence=0.9,
        strategy_version="v1",
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER", signer_id="main-cio-key", source="ext"),
        is_fixture=True,
    )
    sign_cio_packet(pkt1, signer_id="main-cio-key")
    runner.submit_cio_packet(pkt1)
    clock[0] = now + timedelta(minutes=1)
    adapter.set_quote("2330.TW", clock[0], 1000.0)
    d1 = runner.submit_cio_packet(pkt1)
    assert d1.action == "BUY_FILLED"

    # Open Case 2
    clock[0] = now + timedelta(minutes=2)
    adapter.set_bar("2330.TW", clock[0], 1000.0, 1005.0, 995.0, 1000.0)
    pkt2 = CIODecisionPacket(
        case_id="case-open-2",
        as_of=clock[0],
        evidence=["e2"],
        thesis="Long tranche 2",
        selected_instrument="2330.TW",
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=10.0,
        conditions={**({"strategy_id": strat}), "allow_odd_lot": True},
        expiry=clock[0] + timedelta(hours=2),
        confidence=0.9,
        strategy_version="v1",
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER", signer_id="main-cio-key", source="ext"),
        is_fixture=True,
    )
    sign_cio_packet(pkt2, signer_id="main-cio-key")
    runner.submit_cio_packet(pkt2)
    clock[0] = now + timedelta(minutes=3)
    adapter.set_quote("2330.TW", clock[0], 1000.0)
    d2 = runner.submit_cio_packet(pkt2)
    assert d2.action == "BUY_FILLED"

    ledger = pm.get_strategy_ledger(strat, DecisionScope.SWING)
    assert ledger.positions["2330.TW"].quantity == 20.0
    pre_sell_fills_count = len(ledger.fills)
    pre_sell_orders_count = len(ledger.orders)

    # Now attempt SELL without target_case_id -> Ambiguous!
    clock[0] = now + timedelta(hours=1)
    adapter.set_bar("2330.TW", clock[0], 1050.0, 1055.0, 1045.0, 1050.0)
    adapter.set_quote("2330.TW", clock[0], 1050.0)
    pkt_ambig_sell = CIODecisionPacket(
        case_id="case-ambig-sell",
        as_of=clock[0],
        evidence=["e3"],
        thesis="Ambiguous sell without target case",
        selected_instrument="2330.TW",
        action="SELL",
        holding_horizon=DecisionScope.SWING,
        quantity=10.0,
        conditions={**({"strategy_id": strat}), "allow_odd_lot": True},
        expiry=clock[0] + timedelta(hours=2),
        confidence=0.9,
        strategy_version="v1",
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER", signer_id="main-cio-key", source="ext"),
        is_fixture=True,
    )
    sign_cio_packet(pkt_ambig_sell, signer_id="main-cio-key")

    d_ambig = runner.submit_cio_packet(pkt_ambig_sell)
    assert d_ambig.action in ("NO_TRADE", "SELL_REJECTED")
    assert "AMBIGUOUS" in d_ambig.reason or "TARGET" in d_ambig.reason or "FAIL_CLOSED" in d_ambig.reason
    assert len(ledger.fills) == pre_sell_fills_count, "Side effect occurred: fill was created"
    assert len(ledger.orders) == pre_sell_orders_count, "Side effect occurred: order was created"


def test_partial_then_final_close_accounting_and_residual_basis(tmp_path: Path):
    """Verify partial close proportionally allocates entry costs, maintains residual basis,
    does not finalize lesson as full closure, and final close completes reconciliation with cash."""
    import os
    os.environ["CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS"] = "1"
    now = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
    clock = [now]
    adapter = SyntheticQuoteAdapter()
    initial_cash = 1_000_000.0
    pm = PortfolioManager(initial_cash_swing=initial_cash, initial_cash_intraday=initial_cash)
    strat = "strategy-alpha"
    pm.register_strategy(strat, initial_cash, unified_cash=True)
    service = PaperOrderService(pm, EventStore(":memory:"))
    runner = AutonomousPaperRunner(tmp_path, pm, service, adapter, now_fn=lambda: clock[0], require_cio_provider=True)
    runner.configure(PaperExperimentSettings(strategy_id=strat, enabled=True, universe=["2330.TW"], initial_cash=initial_cash))

    # Buy 20 shares at raw quote 1000.0
    runner.allow_fixture_quotes = True
    adapter.set_bar("2330.TW", now, 995.0, 1005.0, 995.0, 1000.0)
    adapter.set_quote("2330.TW", now, 1000.0)

    buy_pkt = CIODecisionPacket(
        case_id="case-buy-001",
        as_of=now,
        evidence=["ev1"],
        thesis="Breakout buy",
        selected_instrument="2330.TW",
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=20.0,
        conditions={**({"strategy_id": strat}), "allow_odd_lot": True},
        expiry=now + timedelta(hours=4),
        confidence=0.9,
        strategy_version="v1",
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER", signer_id="main-cio-key", source="ext"),
        is_fixture=True,
    )
    sign_cio_packet(buy_pkt, signer_id="main-cio-key")
    d_buy_pending = runner.submit_cio_packet(buy_pkt)
    assert d_buy_pending.action == "BUY_PENDING"

    # Fill Buy at t1 with quote 1000.0
    t1 = now + timedelta(minutes=5)
    clock[0] = t1
    adapter.set_quote("2330.TW", t1, 1000.0)
    d_buy_fill = runner.submit_cio_packet(buy_pkt)
    assert d_buy_fill.action == "BUY_FILLED"

    ledger = pm.get_strategy_ledger(strat, DecisionScope.SWING)
    assert ledger.positions["2330.TW"].quantity == 20.0

    # Independently computed fill math:
    # effective buy px = 1000.5, fee = 28.5143, cash debit = 20038.5143
    buy_fill = ledger.fills[0]
    assert buy_fill.fill_price == 1000.5
    assert abs(buy_fill.fee - 28.5143) < 1e-3
    assert abs(ledger.cash - (1_000_000.0 - 20038.5143)) < 1e-3
    cash_after_buy = ledger.cash

    # Partial Close: Sell 10 shares at quote 1050.0
    t2 = t1 + timedelta(days=1)
    clock[0] = t2
    adapter.set_bar("2330.TW", t2, 1045.0, 1055.0, 1040.0, 1048.0)
    adapter.set_quote("2330.TW", t2, 1048.0)

    part_close_pkt = CIODecisionPacket(
        case_id="case-close-part-001",
        as_of=t2,
        evidence=["ev2"],
        thesis="Partial profit taking",
        selected_instrument="2330.TW",
        action="SELL",
        holding_horizon=DecisionScope.SWING,
        quantity=10.0,
        conditions={**({"strategy_id": strat, "target_case_id": "case-buy-001"}), "allow_odd_lot": True},
        expiry=t2 + timedelta(hours=4),
        confidence=0.9,
        strategy_version="v1",
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER", signer_id="main-cio-key", source="ext"),
        is_fixture=True,
    )
    sign_cio_packet(part_close_pkt, signer_id="main-cio-key")
    d_part_pending = runner.submit_cio_packet(part_close_pkt)
    assert d_part_pending.action == "SELL_PENDING"

    t2_fill = t2 + timedelta(minutes=5)
    clock[0] = t2_fill
    adapter.set_quote("2330.TW", t2_fill, 1050.0)
    d_part_fill = runner.submit_cio_packet(part_close_pkt)
    assert d_part_fill.action == "SELL_FILLED"

    # Verify partial close state
    part_outcome = d_part_fill.inputs["outcome"]
    assert part_outcome["is_partial"] is True
    assert part_outcome["residual_quantity"] == 10.0
    # Allocated entry cost (50% of 28.5143 = 14.2571)
    assert abs(part_outcome["costs"]["entry_costs"] - 14.2571) < 1e-3
    assert abs(part_outcome["costs"]["residual_entry_costs"] - 14.2572) < 1e-3
    # Delta realized PnL = 438.2658, net realized PnL = 424.0087 (exit cost includes TW min_fee 20.0 + tax 31.4842)
    assert abs(part_outcome["realized_pnl"] - 438.2658) < 1e-3
    assert abs(part_outcome["net_realized_pnl"] - 424.0087) < 1e-3

    # Cash after partial close: cash_after_buy + (10 * 1049.475 - 51.4842)
    expected_cash_part = cash_after_buy + (10.0 * 1049.475 - 51.4842)
    assert abs(ledger.cash - expected_cash_part) < 1e-3

    # Opening case record status MUST NOT be CLOSED!
    open_rec = runner.learning_store.get_record("case-buy-001")
    assert open_rec.status == "PARTIALLY_CLOSED"
    # No full-closure lessons generated for partial close
    lessons = runner.learning_store._lessons
    assert not any(l.case_id == "case-buy-001" for l in lessons)

    # Final Close: Sell remaining 10 shares at quote 1060.0
    t3 = t2_fill + timedelta(days=1)
    clock[0] = t3
    adapter.set_bar("2330.TW", t3, 1055.0, 1065.0, 1050.0, 1058.0)
    adapter.set_quote("2330.TW", t3, 1058.0)

    final_close_pkt = CIODecisionPacket(
        case_id="case-close-final-001",
        as_of=t3,
        evidence=["ev3"],
        thesis="Final exit",
        selected_instrument="2330.TW",
        action="SELL",
        holding_horizon=DecisionScope.SWING,
        quantity=10.0,
        conditions={**({"strategy_id": strat, "target_case_id": "case-buy-001"}), "allow_odd_lot": True},
        expiry=t3 + timedelta(hours=4),
        confidence=0.9,
        strategy_version="v1",
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER", signer_id="main-cio-key", source="ext"),
        is_fixture=True,
    )
    sign_cio_packet(final_close_pkt, signer_id="main-cio-key")
    runner.submit_cio_packet(final_close_pkt)

    t3_fill = t3 + timedelta(minutes=5)
    clock[0] = t3_fill
    adapter.set_quote("2330.TW", t3_fill, 1060.0)
    d_final_fill = runner.submit_cio_packet(final_close_pkt)
    assert d_final_fill.action == "SELL_FILLED"

    final_outcome = d_final_fill.inputs["outcome"]
    assert final_outcome["is_partial"] is False
    assert final_outcome["residual_quantity"] == 0.0
    assert abs(final_outcome["costs"]["entry_costs"] - 14.2572) < 1e-3
    assert abs(final_outcome["realized_pnl"] - 537.9159) < 1e-3
    assert abs(final_outcome["net_realized_pnl"] - 523.6587) < 1e-3

    # Canonical cash reconciliation: total roundtrip cash gain == sum of net realized PnL
    total_cash_gain = ledger.cash - initial_cash
    sum_net_realized = part_outcome["net_realized_pnl"] + final_outcome["net_realized_pnl"]
    assert abs(total_cash_gain - 947.6674) < 1e-2
    assert abs(sum_net_realized - 947.6674) < 1e-2
    assert abs(total_cash_gain - sum_net_realized) < 1e-2

    # Case is now fully CLOSED and structured lesson exists
    assert open_rec.status == "CLOSED"
    final_lessons = [l for l in runner.learning_store._lessons if l.case_id == "case-buy-001"]
    assert len(final_lessons) == 1

    # Oversell / sell against fully closed case fails closed
    oversell_pkt = CIODecisionPacket(
        case_id="case-oversell-001",
        as_of=t3_fill + timedelta(hours=1),
        evidence=["ev4"],
        thesis="Illegal oversell",
        selected_instrument="2330.TW",
        action="SELL",
        holding_horizon=DecisionScope.SWING,
        quantity=5.0,
        conditions={**({"strategy_id": strat, "target_case_id": "case-buy-001"}), "allow_odd_lot": True},
        expiry=t3_fill + timedelta(hours=4),
        confidence=0.9,
        strategy_version="v1",
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER", signer_id="main-cio-key", source="ext"),
        is_fixture=True,
    )
    sign_cio_packet(oversell_pkt, signer_id="main-cio-key")
    d_oversell = runner.submit_cio_packet(oversell_pkt)
    assert d_oversell.action in ("NO_TRADE", "SELL_REJECTED")


def test_multicase_two_sequential_roundtrips_two_symbols_two_strategies(tmp_path: Path):
    """Test two sequential roundtrips across two symbols (2330.TW and AAPL) across two strategies.
    Verify delta-based attribution prevents cumulative contamination across trades and symbols."""
    import os
    os.environ["CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS"] = "1"
    now = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
    clock = [now]
    adapter = SyntheticQuoteAdapter()
    pm = PortfolioManager(initial_cash_swing=1_000_000.0, initial_cash_intraday=1_000_000.0)
    strat_a = "strategy-alpha"
    strat_b = "strategy-beta"
    pm.register_strategy(strat_a, 1_000_000.0, unified_cash=True, currency="TWD")
    pm.register_strategy(strat_b, 1_000_000.0, unified_cash=True, currency="USD")
    service = PaperOrderService(pm, EventStore(":memory:"))
    runner = AutonomousPaperRunner(tmp_path, pm, service, adapter, now_fn=lambda: clock[0], require_cio_provider=True)
    runner.configure(PaperExperimentSettings(strategy_id=strat_a, enabled=True, universe=["2330.TW"], initial_cash=1_000_000.0, base_currency="TWD"))
    runner.configure(PaperExperimentSettings(strategy_id=strat_b, enabled=True, universe=["AAPL"], initial_cash=1_000_000.0, base_currency="USD"))

    # --- ROUNDTRIP 1: Strategy A on 2330.TW ---
    runner.allow_fixture_quotes = True
    # Buy 10 @ 1000.0
    adapter.set_bar("2330.TW", now, 995.0, 1005.0, 995.0, 1000.0)
    adapter.set_quote("2330.TW", now, 1000.0)
    p_a1_buy = CIODecisionPacket(
        case_id="case-a1-buy", as_of=now, evidence=["e"], thesis="Strat A round 1 buy",
        selected_instrument="2330.TW", action="BUY", holding_horizon=DecisionScope.SWING,
        quantity=10.0, conditions={**({"strategy_id": strat_a}), "allow_odd_lot": True}, expiry=now + timedelta(hours=2),
        confidence=0.9, strategy_version="v1",
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER", signer_id="main-cio-key", source="ext"),
        is_fixture=True,
    )
    sign_cio_packet(p_a1_buy, signer_id="main-cio-key")
    runner.submit_cio_packet(p_a1_buy)
    clock[0] = now + timedelta(minutes=5)
    adapter.set_quote("2330.TW", clock[0], 1000.0)
    d_a1_buy = runner.submit_cio_packet(p_a1_buy)
    assert d_a1_buy.action == "BUY_FILLED"

    # Sell 10 @ 1050.0
    t_a1_exit = clock[0] + timedelta(days=1)
    clock[0] = t_a1_exit
    adapter.set_bar("2330.TW", t_a1_exit, 1045.0, 1055.0, 1040.0, 1048.0)
    adapter.set_quote("2330.TW", t_a1_exit, 1048.0)
    p_a1_sell = CIODecisionPacket(
        case_id="case-a1-sell", as_of=t_a1_exit, evidence=["e"], thesis="Strat A round 1 exit",
        selected_instrument="2330.TW", action="SELL", holding_horizon=DecisionScope.SWING,
        quantity=10.0, conditions={**({"strategy_id": strat_a, "target_case_id": "case-a1-buy"}), "allow_odd_lot": True},
        expiry=t_a1_exit + timedelta(hours=2), confidence=0.9, strategy_version="v1",
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER", signer_id="main-cio-key", source="ext"),
        is_fixture=True,
    )
    sign_cio_packet(p_a1_sell, signer_id="main-cio-key")
    runner.submit_cio_packet(p_a1_sell)
    clock[0] = t_a1_exit + timedelta(minutes=5)
    adapter.set_quote("2330.TW", clock[0], 1050.0)
    d_a1_sell = runner.submit_cio_packet(p_a1_sell)
    assert d_a1_sell.action == "SELL_FILLED"
    outcome_a1 = d_a1_sell.inputs["outcome"]
    # Net realized PnL = (1049.475 - 1000.5)*10 - exit_costs(20.0+31.4842) - entry_costs(20.0) = 418.2658
    assert abs(outcome_a1["net_realized_pnl"] - 418.2658) < 1e-3

    # --- ROUNDTRIP 2: Strategy B on AAPL (US stock) ---
    t_b1_start = clock[0] + timedelta(hours=2)
    clock[0] = t_b1_start
    adapter.set_bar("AAPL", t_b1_start, 150.0, 152.0, 149.0, 150.0)
    adapter.set_quote("AAPL", t_b1_start, 150.0)
    p_b1_buy = CIODecisionPacket(
        case_id="case-b1-buy", as_of=t_b1_start, evidence=["e"], thesis="Strat B buy AAPL",
        selected_instrument="AAPL", action="BUY", holding_horizon=DecisionScope.SWING,
        quantity=10.0, conditions={**({"strategy_id": strat_b}), "allow_odd_lot": True}, expiry=t_b1_start + timedelta(hours=2),
        confidence=0.9, strategy_version="v1",
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER", signer_id="main-cio-key", source="ext"),
        is_fixture=True,
    )
    sign_cio_packet(p_b1_buy, signer_id="main-cio-key")
    runner.submit_cio_packet(p_b1_buy)
    clock[0] = t_b1_start + timedelta(minutes=5)
    adapter.set_quote("AAPL", clock[0], 150.0)
    d_b1_buy = runner.submit_cio_packet(p_b1_buy)
    assert d_b1_buy.action == "BUY_FILLED"

    # Sell AAPL 10 @ 160.0
    t_b1_exit = clock[0] + timedelta(days=1)
    clock[0] = t_b1_exit
    adapter.set_bar("AAPL", t_b1_exit, 158.0, 162.0, 157.0, 159.0)
    adapter.set_quote("AAPL", t_b1_exit, 159.0)
    p_b1_sell = CIODecisionPacket(
        case_id="case-b1-sell", as_of=t_b1_exit, evidence=["e"], thesis="Strat B exit AAPL",
        selected_instrument="AAPL", action="SELL", holding_horizon=DecisionScope.SWING,
        quantity=10.0, conditions={**({"strategy_id": strat_b, "target_case_id": "case-b1-buy"}), "allow_odd_lot": True},
        expiry=t_b1_exit + timedelta(hours=2), confidence=0.9, strategy_version="v1",
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER", signer_id="main-cio-key", source="ext"),
        is_fixture=True,
    )
    sign_cio_packet(p_b1_sell, signer_id="main-cio-key")
    runner.submit_cio_packet(p_b1_sell)
    clock[0] = t_b1_exit + timedelta(minutes=5)
    adapter.set_quote("AAPL", clock[0], 160.0)
    d_b1_sell = runner.submit_cio_packet(p_b1_sell)
    assert d_b1_sell.action == "SELL_FILLED"
    outcome_b1 = d_b1_sell.inputs["outcome"]
    # Strategy B ledger is completely isolated from Strategy A
    ledger_b = pm.get_strategy_ledger(strat_b, DecisionScope.SWING)
    assert ledger_b.positions["AAPL"].quantity == 0.0

    # --- ROUNDTRIP 3: Strategy A SECOND trade on 2330.TW ---
    # Buy 10 @ 1100.0, Sell 10 @ 1120.0
    t_a2_start = clock[0] + timedelta(hours=2)
    clock[0] = t_a2_start
    adapter.set_bar("2330.TW", t_a2_start, 1095.0, 1105.0, 1090.0, 1100.0)
    adapter.set_quote("2330.TW", t_a2_start, 1100.0)
    p_a2_buy = CIODecisionPacket(
        case_id="case-a2-buy", as_of=t_a2_start, evidence=["e"], thesis="Strat A round 2 buy",
        selected_instrument="2330.TW", action="BUY", holding_horizon=DecisionScope.SWING,
        quantity=10.0, conditions={**({"strategy_id": strat_a}), "allow_odd_lot": True}, expiry=t_a2_start + timedelta(hours=2),
        confidence=0.9, strategy_version="v1",
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER", signer_id="main-cio-key", source="ext"),
        is_fixture=True,
    )
    sign_cio_packet(p_a2_buy, signer_id="main-cio-key")
    runner.submit_cio_packet(p_a2_buy)
    clock[0] = t_a2_start + timedelta(minutes=5)
    adapter.set_quote("2330.TW", clock[0], 1100.0)
    d_a2_buy = runner.submit_cio_packet(p_a2_buy)
    assert d_a2_buy.action == "BUY_FILLED"

    # Sell 10 @ 1120.0
    t_a2_exit = clock[0] + timedelta(days=1)
    clock[0] = t_a2_exit
    adapter.set_bar("2330.TW", t_a2_exit, 1115.0, 1125.0, 1110.0, 1118.0)
    adapter.set_quote("2330.TW", t_a2_exit, 1118.0)
    p_a2_sell = CIODecisionPacket(
        case_id="case-a2-sell", as_of=t_a2_exit, evidence=["e"], thesis="Strat A round 2 exit",
        selected_instrument="2330.TW", action="SELL", holding_horizon=DecisionScope.SWING,
        quantity=10.0, conditions={**({"strategy_id": strat_a, "target_case_id": "case-a2-buy"}), "allow_odd_lot": True},
        expiry=t_a2_exit + timedelta(hours=2), confidence=0.9, strategy_version="v1",
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER", signer_id="main-cio-key", source="ext"),
        is_fixture=True,
    )
    sign_cio_packet(p_a2_sell, signer_id="main-cio-key")
    runner.submit_cio_packet(p_a2_sell)
    clock[0] = t_a2_exit + timedelta(minutes=5)
    adapter.set_quote("2330.TW", clock[0], 1120.0)
    d_a2_sell = runner.submit_cio_packet(p_a2_sell)
    assert d_a2_sell.action == "SELL_FILLED"
    outcome_a2 = d_a2_sell.inputs["outcome"]

    # Crucial check: outcome_a2 realized_pnl reflects ONLY trade A2's delta, NOT cumulative ledger PnL!
    ledger_a = pm.get_strategy_ledger(strat_a, DecisionScope.SWING)
    # ledger_a.realized_pnl is the sum of A1 and A2
    assert ledger_a.realized_pnl > outcome_a2["realized_pnl"]
    # outcome_a2 realized_pnl matches only the delta for trade A2
    effective_buy = 1100.0 * 1.0005
    effective_sell = 1120.0 * 0.9995
    expected_a2_delta = (effective_sell - effective_buy) * 10.0 - (d_a2_sell.inputs["outcome"]["costs"]["exit_costs"])
    assert abs(outcome_a2["realized_pnl"] - round(expected_a2_delta, 4)) < 1e-3


def test_duplicate_replay_and_restart(tmp_path: Path):
    """Verify replay idempotency and restart preserve balances and produce no duplicate lessons."""
    import os
    os.environ["CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS"] = "1"
    now = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
    clock = [now]
    adapter = SyntheticQuoteAdapter()
    pm = PortfolioManager(initial_cash_swing=1_000_000.0, initial_cash_intraday=1_000_000.0)
    strat = "strategy-alpha"
    pm.register_strategy(strat, 1_000_000.0, unified_cash=True)
    service = PaperOrderService(pm, EventStore(":memory:"))
    runner = AutonomousPaperRunner(tmp_path, pm, service, adapter, now_fn=lambda: clock[0], require_cio_provider=True)
    runner.configure(PaperExperimentSettings(strategy_id=strat, enabled=True, universe=["2330.TW"], initial_cash=1_000_000.0))

    adapter.set_bar("2330.TW", now, 995.0, 1005.0, 995.0, 1000.0)
    adapter.set_quote("2330.TW", now, 1000.0)
    runner.allow_fixture_quotes = True
    pkt_buy = CIODecisionPacket(
        case_id="case-replay-buy", as_of=now, evidence=["e"], thesis="Replay test buy",
        selected_instrument="2330.TW", action="BUY", holding_horizon=DecisionScope.SWING,
        quantity=10.0, conditions={**({"strategy_id": strat}), "allow_odd_lot": True}, expiry=now + timedelta(hours=2),
        confidence=0.9, strategy_version="v1",
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER", signer_id="main-cio-key", source="ext"),
        is_fixture=True,
    )
    sign_cio_packet(pkt_buy, signer_id="main-cio-key")
    runner.submit_cio_packet(pkt_buy)
    clock[0] = now + timedelta(minutes=5)
    adapter.set_quote("2330.TW", clock[0], 1000.0)
    d_buy = runner.submit_cio_packet(pkt_buy)
    assert d_buy.action == "BUY_FILLED"

    ledger = pm.get_strategy_ledger(strat, DecisionScope.SWING)
    cash_after_buy = ledger.cash
    fills_count = len(ledger.fills)

    # Replay same packet immediately
    d_replay = runner.submit_cio_packet(pkt_buy)
    assert d_replay.action in ("NO_TRADE", "BUY_REJECTED")
    assert "DUPLICATE" in d_replay.reason
    assert ledger.cash == cash_after_buy
    assert len(ledger.fills) == fills_count

    # Close the trade
    t_close = clock[0] + timedelta(days=1)
    clock[0] = t_close
    adapter.set_bar("2330.TW", t_close, 1045.0, 1055.0, 1040.0, 1050.0)
    adapter.set_quote("2330.TW", t_close, 1048.0)
    pkt_close = CIODecisionPacket(
        case_id="case-replay-close", as_of=t_close, evidence=["e"], thesis="Replay test close",
        selected_instrument="2330.TW", action="SELL", holding_horizon=DecisionScope.SWING,
        quantity=10.0, conditions={**({"strategy_id": strat, "target_case_id": "case-replay-buy"}), "allow_odd_lot": True},
        expiry=t_close + timedelta(hours=2), confidence=0.9, strategy_version="v1",
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER", signer_id="main-cio-key", source="ext"),
        is_fixture=True,
    )
    sign_cio_packet(pkt_close, signer_id="main-cio-key")
    runner.submit_cio_packet(pkt_close)
    clock[0] = t_close + timedelta(minutes=5)
    adapter.set_quote("2330.TW", clock[0], 1050.0)
    d_close = runner.submit_cio_packet(pkt_close)
    assert d_close.action == "SELL_FILLED"

    cash_after_close = ledger.cash
    lessons_count = len(runner.learning_store._lessons)
    assert lessons_count == 1

    # Replay close packet
    d_close_replay = runner.submit_cio_packet(pkt_close)
    assert d_close_replay.action in ("NO_TRADE", "SELL_REJECTED")
    assert len(runner.learning_store._lessons) == lessons_count
    assert ledger.cash == cash_after_close

    # Restart: instantiate fresh runner with same persistence files
    new_pm = PortfolioManager(initial_cash_swing=1_000_000.0, initial_cash_intraday=1_000_000.0)
    new_pm.register_strategy(strat, 1_000_000.0, unified_cash=True)
    new_service = PaperOrderService(new_pm, EventStore(":memory:"))
    restarted_runner = AutonomousPaperRunner(tmp_path, new_pm, new_service, adapter, now_fn=lambda: clock[0], require_cio_provider=True)
    # Learning store reloaded
    reloaded_rec = restarted_runner.learning_store.get_record("case-replay-buy")
    assert reloaded_rec is not None
    assert reloaded_rec.status == "CLOSED"
    assert len(restarted_runner.learning_store._lessons) == 1

    # Submitting close against closed case after restart fails closed
    d_restart_close = restarted_runner.submit_cio_packet(pkt_close)
    assert d_restart_close.action in ("NO_TRADE", "SELL_REJECTED")
    assert len(restarted_runner.learning_store._lessons) == 1

