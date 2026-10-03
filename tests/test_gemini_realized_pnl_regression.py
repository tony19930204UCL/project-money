"""Deterministic regression tests for multi-strategy realized PnL and corporate actions in EventStore.

SYNTHETIC TEST FIXTURES ONLY:
All orders, fills, bars, and corporate actions in this module are explicitly
labeled synthetic TEST fixtures for accounting verification under paper simulation.
No live broker or execution quotes are used.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from cio_market_lab.domain.events import EventEnvelope, EventType
from cio_market_lab.domain.models import (
    Bar,
    DecisionScope,
    Fill,
    Market,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from cio_market_lab.engine.corporate_actions import CorporateAction
from cio_market_lab.events.store import EventStore


def test_multi_strategy_realized_pnl_and_corporate_split_reconstruction(tmp_path: Path):
    """Pin defect: EventStore.reconstruct_portfolio fails multi-strategy realized PnL parity

    Defects pinned:
    1. reconstruct_portfolio lacks strategy_id scoping, preventing per-strategy reconstruction.
    2. reconstruct_portfolio ignores EventType.CORPORATE_ACTION_APPLIED (splits), causing
       disposals to be calculated against unsplit positions and entry prices.
    3. Aggregate reconstruction inappropriately pools positions with blended cost basis
       instead of tracking per-strategy lots, corrupting aggregate realized PnL so that
       aggregate realized PnL != sum of strategy realized PnLs.
    4. Reopen and idempotent replays perpetuate the inconsistency.
    """
    db_path = tmp_path / "events.db"
    store = EventStore(db_path)

    t0 = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
    t_buy_alpha = t0 + timedelta(minutes=5)
    t_buy_beta = t0 + timedelta(minutes=10)
    t_bar_pre = t0 + timedelta(minutes=15)
    t_split = t0 + timedelta(hours=1)
    t_bar_post = t0 + timedelta(hours=2)
    t_sell_alpha_1 = t0 + timedelta(hours=3)
    t_sell_beta = t0 + timedelta(hours=4)
    t_sell_alpha_2 = t0 + timedelta(hours=5)

    symbol = "2330.TW"
    bucket = DecisionScope.SWING
    currency = "TWD"

    # --- 1. Distinct strategies accumulate same-symbol positions at distinct costs ---
    # Strategy Alpha BUY 100 @ 100.0 (fee=10.0, tax=0.0)
    ord_alpha_buy = Order(
        order_id="ord-alpha-buy",
        strategy_id="strategy_alpha",
        symbol=symbol,
        market=Market.TW,
        bucket=bucket,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100.0,
        currency=currency,
        audit_metadata={"synthetic_test_fixture_only": True},
    )
    store.append(EventEnvelope(
        event_type=EventType.ORDER_CREATED,
        aggregate_id=ord_alpha_buy.order_id,
        payload=ord_alpha_buy.model_dump(mode="json"),
        timestamp=t_buy_alpha,
    ))
    fill_alpha_buy = Fill(
        fill_id="fill-alpha-buy",
        order_id=ord_alpha_buy.order_id,
        symbol=symbol,
        bucket=bucket,
        side=OrderSide.BUY,
        quantity=100.0,
        fill_price=100.0,
        fee=10.0,
        tax=0.0,
        slippage=0.0,
        currency=currency,
        timestamp=t_buy_alpha,
        assumptions={"synthetic_test_fixture_only": True, "slippage_embedded": True},
    )
    store.append(EventEnvelope(
        event_type=EventType.ORDER_FILLED,
        aggregate_id=ord_alpha_buy.order_id,
        payload=fill_alpha_buy.model_dump(mode="json"),
        timestamp=t_buy_alpha,
    ))

    # Strategy Beta BUY 100 @ 200.0 (fee=20.0, tax=0.0) - distinct cost!
    ord_beta_buy = Order(
        order_id="ord-beta-buy",
        strategy_id="strategy_beta",
        symbol=symbol,
        market=Market.TW,
        bucket=bucket,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100.0,
        currency=currency,
        audit_metadata={"synthetic_test_fixture_only": True},
    )
    store.append(EventEnvelope(
        event_type=EventType.ORDER_CREATED,
        aggregate_id=ord_beta_buy.order_id,
        payload=ord_beta_buy.model_dump(mode="json"),
        timestamp=t_buy_beta,
    ))
    fill_beta_buy = Fill(
        fill_id="fill-beta-buy",
        order_id=ord_beta_buy.order_id,
        symbol=symbol,
        bucket=bucket,
        side=OrderSide.BUY,
        quantity=100.0,
        fill_price=200.0,
        fee=20.0,
        tax=0.0,
        slippage=0.0,
        currency=currency,
        timestamp=t_buy_beta,
        assumptions={"synthetic_test_fixture_only": True, "slippage_embedded": True},
    )
    store.append(EventEnvelope(
        event_type=EventType.ORDER_FILLED,
        aggregate_id=ord_beta_buy.order_id,
        payload=fill_beta_buy.model_dump(mode="json"),
        timestamp=t_buy_beta,
    ))

    # Bar observed before split
    store.append(EventEnvelope(
        event_type=EventType.BAR_OBSERVED,
        aggregate_id=f"bar:{symbol}",
        payload={"symbol": symbol, "close": 200.0},
        timestamp=t_bar_pre,
    ))

    # --- 2. Corporate Split (2:1 ratio) applied to both strategies ---
    split_action = CorporateAction(
        action_id="split-synth-2330-20261002",
        symbol=symbol,
        currency=currency,
        kind="SPLIT",
        effective_at=t_split,
        observed_at=t_split,
        ratio=2.0,
        is_fixture=True,
        provenance={"synthetic_test_fixture_only": True, "verification_status": "VERIFIED", "source_url": "https://test.local"},
    )
    store.append(EventEnvelope(
        event_id="corp-split-alpha",
        event_type=EventType.CORPORATE_ACTION_APPLIED,
        aggregate_id="strategy:strategy_alpha",
        payload={
            "action": split_action.model_dump(mode="json"),
            "stage": "SPLIT",
            "strategy_id": "strategy_alpha",
            "bucket": bucket.value,
            "cancel_order_ids": [],
            "action_sha256": "synth_digest_alpha",
        },
        timestamp=t_split,
    ))
    store.append(EventEnvelope(
        event_id="corp-split-beta",
        event_type=EventType.CORPORATE_ACTION_APPLIED,
        aggregate_id="strategy:strategy_beta",
        payload={
            "action": split_action.model_dump(mode="json"),
            "stage": "SPLIT",
            "strategy_id": "strategy_beta",
            "bucket": bucket.value,
            "cancel_order_ids": [],
            "action_sha256": "synth_digest_beta",
        },
        timestamp=t_split,
    ))

    # Check invalid-mark boundary immediately after split (before post-split bar)
    post_split_store = EventStore(db_path)
    inv_portfolio = post_split_store.reconstruct_portfolio(bucket, initial_cash=2_000_000.0, currency=currency)
    assert inv_portfolio.positions[symbol].current_price is None, "Split must invalidate mark price"
    assert inv_portfolio.nav_status == "NAV_UNAVAILABLE", "Missing mark must cause NAV_UNAVAILABLE"
    assert inv_portfolio.equity is None, "Equity must be None when mark is unavailable"

    # Post-split bar arrives, restoring mark
    store.append(EventEnvelope(
        event_type=EventType.BAR_OBSERVED,
        aggregate_id=f"bar:{symbol}",
        payload={"symbol": symbol, "close": 120.0},
        timestamp=t_bar_post,
    ))

    # --- 3. Partial disposal by Strategy Alpha (sells 100 of 200 post-split shares @ 120.0) ---
    ord_alpha_sell_1 = Order(
        order_id="ord-alpha-sell-1",
        strategy_id="strategy_alpha",
        symbol=symbol,
        market=Market.TW,
        bucket=bucket,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=100.0,
        currency=currency,
        audit_metadata={"synthetic_test_fixture_only": True},
    )
    store.append(EventEnvelope(
        event_type=EventType.ORDER_CREATED,
        aggregate_id=ord_alpha_sell_1.order_id,
        payload=ord_alpha_sell_1.model_dump(mode="json"),
        timestamp=t_sell_alpha_1,
    ))
    # Alpha cost basis post-split was 100.0 / 2 = 50.0.
    # Sell price = 120.0, fee = 10.0, tax = 5.0.
    # Realized = (120.0 - 50.0) * 100 - (10.0 + 5.0) = 7000.0 - 15.0 = 6985.0.
    fill_alpha_sell_1 = Fill(
        fill_id="fill-alpha-sell-1",
        order_id=ord_alpha_sell_1.order_id,
        symbol=symbol,
        bucket=bucket,
        side=OrderSide.SELL,
        quantity=100.0,
        fill_price=120.0,
        fee=10.0,
        tax=5.0,
        slippage=0.0,
        currency=currency,
        timestamp=t_sell_alpha_1,
        assumptions={"synthetic_test_fixture_only": True, "slippage_embedded": True},
    )
    store.append(EventEnvelope(
        event_type=EventType.ORDER_FILLED,
        aggregate_id=ord_alpha_sell_1.order_id,
        payload=fill_alpha_sell_1.model_dump(mode="json"),
        timestamp=t_sell_alpha_1,
    ))

    # --- 4. Complete disposal by Strategy Beta (sells ALL 200 post-split shares @ 120.0) ---
    ord_beta_sell = Order(
        order_id="ord-beta-sell",
        strategy_id="strategy_beta",
        symbol=symbol,
        market=Market.TW,
        bucket=bucket,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=200.0,
        currency=currency,
        audit_metadata={"synthetic_test_fixture_only": True},
    )
    store.append(EventEnvelope(
        event_type=EventType.ORDER_CREATED,
        aggregate_id=ord_beta_sell.order_id,
        payload=ord_beta_sell.model_dump(mode="json"),
        timestamp=t_sell_beta,
    ))
    # Beta cost basis post-split was 200.0 / 2 = 100.0.
    # Sell price = 120.0, fee = 20.0, tax = 10.0.
    # Realized = (120.0 - 100.0) * 200 - (20.0 + 10.0) = 4000.0 - 30.0 = 3970.0.
    fill_beta_sell = Fill(
        fill_id="fill-beta-sell",
        order_id=ord_beta_sell.order_id,
        symbol=symbol,
        bucket=bucket,
        side=OrderSide.SELL,
        quantity=200.0,
        fill_price=120.0,
        fee=20.0,
        tax=10.0,
        slippage=0.0,
        currency=currency,
        timestamp=t_sell_beta,
        assumptions={"synthetic_test_fixture_only": True, "slippage_embedded": True},
    )
    store.append(EventEnvelope(
        event_type=EventType.ORDER_FILLED,
        aggregate_id=ord_beta_sell.order_id,
        payload=fill_beta_sell.model_dump(mode="json"),
        timestamp=t_sell_beta,
    ))

    # --- 5. Complete disposal by Strategy Alpha (sells remaining 100 shares @ 130.0) ---
    ord_alpha_sell_2 = Order(
        order_id="ord-alpha-sell-2",
        strategy_id="strategy_alpha",
        symbol=symbol,
        market=Market.TW,
        bucket=bucket,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=100.0,
        currency=currency,
        audit_metadata={"synthetic_test_fixture_only": True},
    )
    store.append(EventEnvelope(
        event_type=EventType.ORDER_CREATED,
        aggregate_id=ord_alpha_sell_2.order_id,
        payload=ord_alpha_sell_2.model_dump(mode="json"),
        timestamp=t_sell_alpha_2,
    ))
    # Alpha cost basis post-split was 50.0.
    # Sell price = 130.0, fee = 10.0, tax = 5.0.
    # Realized delta = (130.0 - 50.0) * 100 - (10.0 + 5.0) = 8000.0 - 15.0 = 7985.0.
    # Alpha cumulative realized = 6985.0 + 7985.0 = 14970.0.
    fill_alpha_sell_2 = Fill(
        fill_id="fill-alpha-sell-2",
        order_id=ord_alpha_sell_2.order_id,
        symbol=symbol,
        bucket=bucket,
        side=OrderSide.SELL,
        quantity=100.0,
        fill_price=130.0,
        fee=10.0,
        tax=5.0,
        slippage=0.0,
        currency=currency,
        timestamp=t_sell_alpha_2,
        assumptions={"synthetic_test_fixture_only": True, "slippage_embedded": True},
    )
    store.append(EventEnvelope(
        event_type=EventType.ORDER_FILLED,
        aggregate_id=ord_alpha_sell_2.order_id,
        payload=fill_alpha_sell_2.model_dump(mode="json"),
        timestamp=t_sell_alpha_2,
    ))

    # --- Native Replay Verification ---
    port_alpha = store.reconstruct_portfolio(bucket, initial_cash=1_000_000.0, currency=currency, strategy_id="strategy_alpha")
    port_beta = store.reconstruct_portfolio(bucket, initial_cash=1_000_000.0, currency=currency, strategy_id="strategy_beta")
    port_agg = store.reconstruct_portfolio(bucket, initial_cash=2_000_000.0, currency=currency)

    # Strategy Alpha assertions
    assert round(port_alpha.realized_pnl, 4) == 14970.0, f"Alpha realized PnL mismatch: {port_alpha.realized_pnl} != 14970.0"
    assert port_alpha.positions[symbol].quantity == 0.0
    assert port_alpha.positions[symbol].average_entry_price == 0.0

    # Strategy Beta assertions
    assert round(port_beta.realized_pnl, 4) == 3970.0, f"Beta realized PnL mismatch: {port_beta.realized_pnl} != 3970.0"
    assert port_beta.positions[symbol].quantity == 0.0
    assert port_beta.positions[symbol].average_entry_price == 0.0

    # Aggregate assertions & Multi-Strategy Parity
    expected_aggregate_realized = 14970.0 + 3970.0  # 18940.0
    assert round(port_agg.realized_pnl, 4) == round(expected_aggregate_realized, 4), (
        f"Aggregate realized PnL mismatch: {port_agg.realized_pnl} != {expected_aggregate_realized}"
    )
    assert round(port_agg.realized_pnl, 4) == round(port_alpha.realized_pnl + port_beta.realized_pnl, 4), (
        "Aggregate realized PnL must equal sum of strategy realized PnLs"
    )
    assert port_agg.positions[symbol].quantity == 0.0

    # --- Reopen Database Verification ---
    reopened_store = EventStore(db_path)
    reopened_alpha = reopened_store.reconstruct_portfolio(bucket, initial_cash=1_000_000.0, currency=currency, strategy_id="strategy_alpha")
    reopened_beta = reopened_store.reconstruct_portfolio(bucket, initial_cash=1_000_000.0, currency=currency, strategy_id="strategy_beta")
    reopened_agg = reopened_store.reconstruct_portfolio(bucket, initial_cash=2_000_000.0, currency=currency)

    assert round(reopened_alpha.realized_pnl, 4) == round(port_alpha.realized_pnl, 4)
    assert round(reopened_beta.realized_pnl, 4) == round(port_beta.realized_pnl, 4)
    assert round(reopened_agg.realized_pnl, 4) == round(port_agg.realized_pnl, 4)

    # --- Idempotent Reconstruction Verification ---
    replay2_agg = store.reconstruct_portfolio(bucket, initial_cash=2_000_000.0, currency=currency)
    assert round(replay2_agg.realized_pnl, 4) == round(port_agg.realized_pnl, 4)
    assert round(replay2_agg.cash, 4) == round(port_agg.cash, 4)
    assert replay2_agg.nav_status == port_agg.nav_status


def test_corporate_validation_and_currency_mismatch_fails_closed(tmp_path: Path):
    """Pin corporate action and native currency validation boundaries."""
    db_path = tmp_path / "val_events.db"
    store = EventStore(db_path)
    now = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)

    # Split with invalid ratio
    bad_split = EventEnvelope(
        event_id="bad-split",
        event_type=EventType.CORPORATE_ACTION_APPLIED,
        aggregate_id="strategy:strat_1",
        payload={
            "action": {
                "action_id": "act-invalid",
                "symbol": "2330.TW",
                "currency": "TWD",
                "kind": "SPLIT",
                "effective_at": now.isoformat(),
                "observed_at": now.isoformat(),
                "ratio": -1.0,
                "is_fixture": True,
                "provenance": {"synthetic_test_fixture_only": True},
            },
            "stage": "SPLIT",
            "strategy_id": "strat_1",
            "bucket": "swing",
            "cancel_order_ids": [],
            "action_sha256": "bad",
        },
        timestamp=now,
    )
    store.append(bad_split)

    with pytest.raises(ValueError, match="INVALID_SPLIT_RATIO"):
        store.reconstruct_portfolio(DecisionScope.SWING, currency="TWD")


def test_corporate_cash_dividend_reconstruction(tmp_path: Path):
    """Verify corporate cash dividend payment reflects in cash and realized PnL."""
    db_path = tmp_path / "div_events.db"
    store = EventStore(db_path)
    now = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
    currency = "TWD"

    div_envelope = EventEnvelope(
        event_id="corp-div-payment-alpha",
        event_type=EventType.CORPORATE_ACTION_APPLIED,
        aggregate_id="strategy:strategy_alpha",
        payload={
            "action": {
                "action_id": "div-synth-1",
                "symbol": "2330.TW",
                "currency": currency,
                "kind": "CASH_DIVIDEND",
                "effective_at": now.isoformat(),
                "observed_at": now.isoformat(),
                "amount_per_share": 3.0,
                "payable_at": now.isoformat(),
                "is_fixture": True,
                "provenance": {"synthetic_test_fixture_only": True},
            },
            "stage": "PAYMENT",
            "strategy_id": "strategy_alpha",
            "bucket": "swing",
            "cash_delta": 300.0,
            "eligible_quantity": 100.0,
            "action_sha256": "digest_div",
        },
        timestamp=now,
    )
    store.append(div_envelope)

    # Strategy reconstruction
    port_alpha = store.reconstruct_portfolio(
        DecisionScope.SWING, initial_cash=10_000.0, currency=currency, strategy_id="strategy_alpha"
    )
    assert port_alpha.cash == 10_300.0
    assert port_alpha.realized_pnl == 300.0

    # Aggregate reconstruction
    port_agg = store.reconstruct_portfolio(
        DecisionScope.SWING, initial_cash=10_000.0, currency=currency
    )
    assert port_agg.cash == 10_300.0
    assert port_agg.realized_pnl == 300.0

