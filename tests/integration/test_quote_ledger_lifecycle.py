"""Issue #7 portable quote -> ledger -> corporate action -> FX -> restart acceptance.

All observations are explicit TEST_ONLY fixtures. This module does not claim live quote,
broker, exchange-session or production-service acceptance.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from cio_market_lab.api.app import create_app
from cio_market_lab.domain.models import (
    Bar,
    CIODecisionPacket,
    DecisionScope,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
    Quote,
)
from cio_market_lab.engine.cio_packet import sign_cio_packet
from cio_market_lab.engine.corporate_actions import CorporateAction
from cio_market_lab.engine.historical_fx import FxRateReceipt, FxReportingBlocked
from cio_market_lab.engine.paper_orders import (
    PaperDataContext,
    PaperExperimentSettings,
    PaperOrderRequest,
)
from tests.browser.server_helper import TestOnlyMarketAdapter, _fixture_fill
from tests.integration.test_process_restart_stream import _server
from tests.test_c08_quote_cutoff_20261002 import setup_book
from tests.test_source_aligned_next_bar import NOW, make_bar, make_order, packet, runner_harness


def _book_quote(
    symbol: str,
    now: datetime,
    *,
    session: str,
    size: float,
    quote_id: str,
    source: str = "fixture://ISSUE7_BOOK",
) -> Quote:
    return Quote(
        symbol=symbol,
        timestamp=now - timedelta(seconds=1),
        observed_at=now,
        bid=99.0,
        ask=100.0,
        bid_size=size,
        ask_size=size,
        last_price=99.5,
        source=source,
        quality="TEST_ONLY",
        session=session,
        quote_id=quote_id,
        is_stale=False,
        is_synthetic=False,
        source_capabilities={
            "source": source,
            "two_sided_book": True,
            "size_backed": True,
            "exchange_session_attested": True,
            "entitlement_evidence_id": f"TEST_ONLY_EID_{quote_id}",
            "entitlement_status": "TEST_ONLY",
            "supported_sessions": [session],
            "extended_hours_book": session == "EXTENDED",
            "odd_lot_book": session == "ODD_LOT",
        },
    )


@pytest.mark.parametrize(
    "session,symbol,market,currency,conditions",
    [
        ("REGULAR", "MSFT", "US", "USD", {}),
        ("EXTENDED", "MSFT", "US", "USD", {"allow_extended_hours": True}),
        ("ODD_LOT", "2330.TW", "TW", "TWD", {"allow_odd_lot": True}),
    ],
)
def test_quote_capacity_partial_then_fresh_quote_completes_once(
    tmp_path, monkeypatch, session, symbol, market, currency, conditions
):
    runner, pm, service, base_cfg, clock, data, quote = setup_book(
        tmp_path, monkeypatch, session=session, size=1
    )
    cfg = base_cfg.model_copy(
        update={
            "strategy_id": f"TEST_ONLY_{market}_{session}",
            "market": market,
            "base_currency": currency,
            "reporting_currency": currency,
            "universe": [symbol],
            "initial_cash": 10_000,
            "max_position_notional": 1_000,
            "paper_execution_model": "QUOTE_BOOK",
        }
    )
    runner.configure(cfg)
    runner.allow_fixture_quotes = True
    data["bar"] = data["bar"].model_copy(update={"symbol": symbol})
    quote.symbol = symbol
    quote.session = session
    quote.quote_id = f"TEST_ONLY_{session}_Q1"
    quote.source_capabilities.update({
        "supported_sessions": [session],
        "extended_hours_book": session == "EXTENDED",
        "odd_lot_book": session == "ODD_LOT",
    })
    # Submission happens before an eligible later quote exists, so the real
    # caller must create a durable PENDING order first.
    quote.timestamp = clock["now"] - timedelta(seconds=1)
    quote.observed_at = clock["now"]

    p = packet(
        f"TEST_ONLY_{session}_PARTIAL",
        NOW,
        paper_execution_model="QUOTE_BOOK",
        allow_partial_fills=True,
        **conditions,
    )
    p.selected_instrument = symbol
    p.quantity = 2
    p = sign_cio_packet(p, signer_id="fixture-test-signer")

    submitted = runner.submit_cio_packet(p, strategy_id=cfg.strategy_id)
    assert submitted.action == "BUY_PENDING"
    ledger = pm.get_strategy_ledger(cfg.strategy_id, DecisionScope.SWING)
    order = next(o for o in service.all_orders() if o.strategy_id == cfg.strategy_id)
    assert order.status == OrderStatus.PENDING
    assert not ledger.fills

    # First fresh eligible update exposes only one unit of attested capacity.
    clock["now"] += timedelta(seconds=2)
    quote.timestamp = clock["now"] - timedelta(seconds=1)
    quote.observed_at = clock["now"]
    first_decisions = runner.process_pending_orders()
    assert [item.action for item in first_decisions] == ["BUY_PARTIALLY_FILLED"]
    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert order.filled_quantity == 1
    assert order.remaining_quantity == 1
    assert len(ledger.fills) == 1
    first_cash = ledger.cash
    first_fill_id = ledger.fills[0].fill_id
    assert ledger.fills[0].consumed_quote.source_quote_id == quote.quote_id
    assert ledger.fills[0].consumed_quote.session == session
    assert runner.process_pending_orders() == []
    assert ledger.cash == first_cash
    assert [f.fill_id for f in ledger.fills] == [first_fill_id]

    quote.quote_id = f"TEST_ONLY_{session}_Q2"
    quote.timestamp = clock["now"] + timedelta(seconds=1)
    quote.observed_at = quote.timestamp
    clock["now"] = quote.timestamp
    decisions = runner.process_pending_orders()
    assert [d.action for d in decisions] == ["BUY_FILLED"]
    assert order.remaining_quantity == 0
    assert order.status == OrderStatus.FILLED
    assert len(ledger.fills) == 2
    assert len({f.fill_id for f in ledger.fills}) == 2
    assert ledger.cash < first_cash
    second_cash = ledger.cash
    assert runner.process_pending_orders() == []
    assert ledger.cash == second_cash
    assert len(ledger.fills) == 2


@pytest.mark.parametrize(
    "mutator",
    [
        lambda q: q.model_copy(update={
            "bid": None,
            "ask": None,
            "bid_size": 0,
            "ask_size": 0,
            "quality": "public_reported_last_sale",
            "source": "TEST_ONLY_PUBLIC_LAST_ONLY",
            "source_capabilities": {},
        }),
        lambda q: q.model_copy(update={"is_stale": True}),
        lambda q: q.model_copy(update={"source": "fallback://TEST_ONLY", "source_capabilities": {}}),
        lambda q: q.model_copy(update={
            "source_capabilities": {
                **q.source_capabilities,
                "two_sided_book": False,
            }
        }),
    ],
    ids=["last-only", "stale", "fallback", "no-bbo-capability"],
)
def test_partial_cancel_replace_terminates_old_remainder_and_links_new_order(
    tmp_path, monkeypatch
):
    runner, pm, service, cfg, clock, data, quote = setup_book(
        tmp_path, monkeypatch, session="REGULAR", size=1
    )
    runner.allow_fixture_quotes = True
    quote.timestamp = clock["now"] - timedelta(seconds=1)
    quote.observed_at = clock["now"]
    p = packet(
        "TEST_ONLY_REPLACE_PARTIAL",
        NOW,
        paper_execution_model="QUOTE_BOOK",
        allow_partial_fills=True,
    )
    p.quantity = 2
    p = sign_cio_packet(p, signer_id="fixture-test-signer")
    assert runner.submit_cio_packet(p, strategy_id="TEST_ONLY_native").action == "BUY_PENDING"

    clock["now"] += timedelta(seconds=2)
    quote.timestamp = clock["now"] - timedelta(seconds=1)
    quote.observed_at = clock["now"]
    assert [d.action for d in runner.process_pending_orders()] == ["BUY_PARTIALLY_FILLED"]
    old = next(o for o in service.all_orders() if o.reason.startswith("Main CIO"))
    assert old.status == OrderStatus.PARTIALLY_FILLED
    assert old.filled_quantity == 1
    assert old.remaining_quantity == 1

    replacement = service.cancel_replace(
        old.order_id,
        PaperOrderRequest(
            currency="USD",
            symbol="MSFT",
            market="US",
            bucket=DecisionScope.SWING,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=1,
            limit_price=102,
            origin=OrderOrigin.MAIN_CIO,
            strategy_id="TEST_ONLY_native",
            reason="TEST_ONLY partial remainder replacement",
            explicit_user_instruction=True,
            data=PaperDataContext(
                source="fixture://ISSUE7_REPLACE",
                observed_at=clock["now"],
                last_price=101,
            ),
        ),
    )
    old_after = service.find_order(old.order_id)
    assert old_after.status == OrderStatus.CANCELLED
    assert old_after.rejection_reason == "CANCEL_REPLACE"
    assert replacement.status == OrderStatus.PENDING
    assert replacement.audit_metadata["replaced_order_id"] == old.order_id
    assert replacement.audit_metadata["replaced_filled_quantity"] == 1
    assert replacement.audit_metadata["replaced_remaining_quantity"] == 1

    events = [
        event for _, event in service.event_store.get_events(limit=1000, strict=True)
    ]
    assert any(
        event.event_type.value == "ORDER_CANCELLED"
        and event.aggregate_id == old.order_id
        for event in events
    )
    assert any(
        event.event_type.value == "ORDER_REPLACED"
        and event.payload["old_order_id"] == old.order_id
        and event.payload["new_order_id"] == replacement.order_id
        for event in events
    )
    ledger = pm.get_strategy_ledger("TEST_ONLY_native", DecisionScope.SWING)
    assert len(ledger.fills) == 1
    cash_after_partial = ledger.cash
    assert runner.process_pending_orders() == []
    assert ledger.cash == cash_after_partial
    assert len(ledger.fills) == 1


def test_non_executable_quote_capabilities_never_promote_bar_or_last_to_book(
    tmp_path, monkeypatch, mutator
):
    runner, pm, service, cfg, clock, data, quote = setup_book(
        tmp_path, monkeypatch, session="REGULAR", size=2
    )
    runner.allow_fixture_quotes = True
    bad = mutator(quote)
    monkeypatch.setattr(runner.market_adapter, "get_latest_quote", lambda _symbol: bad)
    order = make_order(created_at=NOW)
    decision = packet("TEST_ONLY_BAD_CAP", NOW, paper_execution_model="QUOTE_BOOK")
    result = runner._resolve_cio_execution(cfg, decision, data["bar"], order)
    assert result is None
    assert not pm.get_strategy_ledger("TEST_ONLY_native", DecisionScope.SWING).fills


def test_bar_reference_only_never_becomes_executable_book(tmp_path, monkeypatch):
    runner, pm, service = runner_harness(
        tmp_path,
        {"now": NOW + timedelta(minutes=2)},
        {"bar": make_bar()},
    )
    runner.allow_fixture_quotes = True
    monkeypatch.setattr(runner.market_adapter, "get_latest_quote", lambda _symbol: None)
    order = make_order(created_at=NOW)
    p = packet("TEST_ONLY_BAR_ONLY", NOW, paper_execution_model="QUOTE_BOOK")
    cfg = service.experiment_for("TEST_ONLY_native").model_copy(
        update={"paper_execution_model": "QUOTE_BOOK"}
    )
    runner.configure(cfg)
    assert runner._resolve_cio_execution(cfg, p, make_bar(), order) is None
    assert not pm.get_strategy_ledger("TEST_ONLY_native", DecisionScope.SWING).fills


def test_corporate_actions_native_cash_and_fx_fail_closed(tmp_path):
    runtime = tmp_path / "runtime"
    adapter = TestOnlyMarketAdapter()
    app = create_app(
        workspace_root=Path(__file__).resolve().parents[2],
        runtime_dir=runtime,
        fixture_mode=True,
        is_read_only=False,
        market_adapter=adapter,
    )
    state = app.state.app_state
    try:
        now = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
        state.runner.allow_fixture_quotes = True
        for sid, market, currency, symbol, cash in [
            ("TEST_ONLY_TW_BOOK", "TW", "TWD", "2330.TW", 100_000),
            ("TEST_ONLY_US_BOOK", "US", "USD", "MSFT", 10_000),
        ]:
            state.runner.configure(PaperExperimentSettings(
                strategy_id=sid,
                enabled=True,
                market=market,
                base_currency=currency,
                reporting_currency="TWD",
                initial_cash=cash,
                universe=[symbol],
            ))

        tw_order = state.paper_orders.submit(PaperOrderRequest(
            currency="TWD",
            symbol="2330.TW",
            market="TW",
            bucket=DecisionScope.SWING,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=2,
            limit_price=101,
            origin=OrderOrigin.STRATEGY,
            strategy_id="TEST_ONLY_TW_BOOK",
            reason="TEST_ONLY corporate action seed",
            data=PaperDataContext(source="fixture://ISSUE7", last_price=100),
        ))
        _fixture_fill(state, tw_order.order_id, 2, "TEST_ONLY_CA_FILL", strategy_id="TEST_ONLY_TW_BOOK")
        tw = state.portfolio_manager.get_strategy_ledger("TEST_ONLY_TW_BOOK", DecisionScope.SWING)
        us = state.portfolio_manager.get_strategy_ledger("TEST_ONLY_US_BOOK", DecisionScope.SWING)
        tw_cash_before = tw.cash
        us_cash_before = us.cash

        split = CorporateAction(
            action_id="TEST_ONLY_SPLIT_2_FOR_1",
            symbol="2330.TW",
            currency="TWD",
            kind="SPLIT",
            effective_at=now + timedelta(days=1),
            observed_at=now + timedelta(days=1),
            ratio=2,
            is_fixture=True,
            provenance={"TEST_ONLY": True},
        )
        applied = state.runner.apply_corporate_action(
            split,
            strategy_id="TEST_ONLY_TW_BOOK",
            bucket=DecisionScope.SWING,
            now=now + timedelta(days=1),
        )
        assert applied is True
        assert tw.positions["2330.TW"].quantity == 4
        # ask=100.1, 5 bps slippage => 100.15005 fill; 2-for-1 halves basis/share.
        assert tw.positions["2330.TW"].average_entry_price == pytest.approx(50.075025)
        assert state.runner.apply_corporate_action(
            split,
            strategy_id="TEST_ONLY_TW_BOOK",
            bucket=DecisionScope.SWING,
            now=now + timedelta(days=1),
        ) is False
        assert tw.positions["2330.TW"].quantity == 4

        dividend = CorporateAction(
            action_id="TEST_ONLY_DIVIDEND",
            symbol="2330.TW",
            currency="TWD",
            kind="CASH_DIVIDEND",
            effective_at=now + timedelta(days=2),
            observed_at=now + timedelta(days=2),
            amount_per_share=2,
            payable_at=now + timedelta(days=3),
            is_fixture=True,
            provenance={"TEST_ONLY": True},
        )
        state.runner.apply_corporate_action(
            dividend,
            strategy_id="TEST_ONLY_TW_BOOK",
            bucket=DecisionScope.SWING,
            now=now + timedelta(days=3),
        )
        assert tw.cash == pytest.approx(tw_cash_before + 8)
        assert us.cash == us_cash_before
        assert state.runner.apply_corporate_action(
            dividend,
            strategy_id="TEST_ONLY_TW_BOOK",
            bucket=DecisionScope.SWING,
            now=now + timedelta(days=3),
        ) is False
        assert tw.cash == pytest.approx(tw_cash_before + 8)

        with pytest.raises(ValidationError):
            CorporateAction(
                action_id="TEST_ONLY_UNSUPPORTED",
                symbol="2330.TW",
                currency="TWD",
                kind="MERGER",
                effective_at=now,
                observed_at=now,
                provenance={"TEST_ONLY": True},
            )

        as_of = now + timedelta(days=3)
        receipt = FxRateReceipt(
            "USD/TWD",
            Decimal("31"),
            as_of,
            "TEST_ONLY FX",
            "https://example.invalid/test-only-fx",
            as_of.date(),
            provenance="TEST_ONLY",
        )
        with pytest.raises(FxReportingBlocked, match="missing FX"):
            state.runner.report_strategy_nav("TEST_ONLY_US_BOOK", as_of=as_of)
        with pytest.raises(FxReportingBlocked, match="TEST_ONLY"):
            state.runner.report_strategy_nav(
                "TEST_ONLY_US_BOOK", as_of=as_of, rate_receipts=[receipt]
            )
        usd_report = state.runner.report_strategy_nav(
            "TEST_ONLY_US_BOOK",
            as_of=as_of,
            rate_receipts=[receipt],
            allow_test_only=True,
        )
        assert Decimal(str(usd_report["native_nav"])) == Decimal("10000")
        assert Decimal(usd_report["reporting_nav"]) == Decimal("310000")
        assert us.cash == 10_000

        stale = FxRateReceipt(
            "USD/TWD",
            Decimal("30"),
            as_of - timedelta(days=10),
            "TEST_ONLY stale FX",
            "https://example.invalid/test-only-stale-fx",
            (as_of - timedelta(days=10)).date(),
            provenance="TEST_ONLY",
        )
        with pytest.raises(FxReportingBlocked, match="stale FX"):
            state.runner.report_strategy_nav(
                "TEST_ONLY_US_BOOK",
                as_of=as_of,
                rate_receipts=[stale],
                allow_test_only=True,
                max_age=timedelta(days=5),
            )
    finally:
        state.runner.shutdown()


def test_pending_partial_completed_http_readback_survives_real_process_restart(tmp_path):
    root = Path(__file__).resolve().parents[2]
    runtime = tmp_path / "runtime"
    control = tmp_path / "control"
    adapter = TestOnlyMarketAdapter()
    app = create_app(
        workspace_root=root,
        runtime_dir=runtime,
        fixture_mode=True,
        is_read_only=False,
        market_adapter=adapter,
    )
    state = app.state.app_state
    try:
        state.runner.allow_fixture_quotes = True
        state.runner.configure(PaperExperimentSettings(
            strategy_id="TEST_ONLY_RESTART_STATES",
            enabled=True,
            market="TW",
            base_currency="TWD",
            reporting_currency="TWD",
            initial_cash=100_000,
            universe=["2330.TW"],
        ))
        orders = []
        for receipt, qty in [
            ("TEST_ONLY_PENDING", 1),
            ("TEST_ONLY_PARTIAL", 2),
            ("TEST_ONLY_FILLED", 2),
        ]:
            orders.append(state.paper_orders.submit(PaperOrderRequest(
                currency="TWD",
                symbol="2330.TW",
                market="TW",
                bucket=DecisionScope.SWING,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=qty,
                limit_price=101,
                origin=OrderOrigin.STRATEGY,
                strategy_id="TEST_ONLY_RESTART_STATES",
                reason=receipt,
                audit_metadata={"fixture_receipt": receipt},
                data=PaperDataContext(source="fixture://ISSUE7_RESTART", last_price=100),
            )))
        _fixture_fill(
            state, orders[1].order_id, 1, "TEST_ONLY_RESTART_PARTIAL_FILL",
            strategy_id="TEST_ONLY_RESTART_STATES",
        )
        _fixture_fill(
            state, orders[2].order_id, 2, "TEST_ONLY_RESTART_FULL_FILL",
            strategy_id="TEST_ONLY_RESTART_STATES",
        )
        state.runner._persist_portfolios()
    finally:
        state.runner.shutdown()

    def snapshot(base_url: str):
        with httpx.Client(timeout=5) as client:
            return {
                "orders": client.get(f"{base_url}/api/paper/orders").json(),
                "readback": client.get(f"{base_url}/api/paper/readback").json(),
                "portfolio": client.get(f"{base_url}/api/portfolio").json(),
            }

    with _server(root, runtime, control, read_only=True) as first:
        first_pid = first.pid
        before = snapshot(first.base_url)
        status_by_reason = {o["reason"]: o["status"] for o in before["orders"]}
        assert status_by_reason["TEST_ONLY_PENDING"] == "PENDING"
        assert status_by_reason["TEST_ONLY_PARTIAL"] == "PARTIALLY_FILLED"
        assert status_by_reason["TEST_ONLY_FILLED"] == "FILLED"
        fill_ids = [f["fill_id"] for f in before["readback"]["fills"]]
        assert len(fill_ids) == len(set(fill_ids)) == 2

    with _server(root, runtime, control, read_only=True) as second:
        assert second.pid != first_pid
        after = snapshot(second.base_url)
        assert after == before
        fill_ids = [f["fill_id"] for f in after["readback"]["fills"]]
        assert len(fill_ids) == len(set(fill_ids)) == 2
