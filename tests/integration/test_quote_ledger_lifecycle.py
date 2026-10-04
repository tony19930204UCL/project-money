"""Issue #7 portable quote -> ledger -> corporate action -> FX -> restart acceptance.

All observations are explicit TEST_ONLY fixtures. This module does not claim live quote,
broker, exchange-session or production-service acceptance.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
import subprocess
import sys

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
from cio_market_lab.engine.corporate_actions import CorporateAction, PaperCorporateActions
from cio_market_lab.engine.historical_fx import FxRateReceipt, FxReportingBlocked
from cio_market_lab.engine.paper_orders import (
    PaperDataContext,
    PaperExperimentSettings,
    PaperOrderRequest,
)
from tests.browser.server_helper import TestOnlyMarketAdapter
from tests.integration.test_process_restart_stream import _server
from tests.test_c08_quote_cutoff_20261002 import setup_book
from tests.test_source_aligned_next_bar import NOW, make_bar, make_order, packet, runner_harness


PROCESS_HELPER = Path(__file__).with_name("pending_execution_process_helper.py")
PROCESS_BASE = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


def _write_process_control(path: Path, *, now: datetime, quotes: dict, action_at: datetime | None = None) -> None:
    payload = {
        "now": now.isoformat(),
        "quotes": quotes,
        "action_at": (action_at or now).isoformat(),
    }
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _run_process_helper(
    root: Path,
    runtime: Path,
    control: Path,
    command: str,
    *flags: str,
    check: bool = True,
):
    proc = subprocess.run(
        [
            sys.executable,
            str(PROCESS_HELPER),
            command,
            "--workspace-root",
            str(root),
            "--runtime-dir",
            str(runtime),
            "--control",
            str(control),
            *flags,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"process helper failed rc={proc.returncode}: {proc.stderr}"
        )
    if proc.returncode != 0:
        return proc
    return json.loads(proc.stdout.splitlines()[-1])


def _strategy_state(payload: dict, strategy_id: str) -> dict:
    return payload["strategies"][strategy_id]


def _assert_replay_idempotent(before: dict, after: dict) -> None:
    for strategy_id in ("TEST_ONLY_RESTART_US", "TEST_ONLY_RESTART_TW"):
        left = _strategy_state(before, strategy_id)
        right = _strategy_state(after, strategy_id)
        assert [f["fill_id"] for f in right["fills"]] == [
            f["fill_id"] for f in left["fills"]
        ]
        assert right["cash"] == pytest.approx(left["cash"])
        assert right["positions"] == left["positions"]
        assert right["orders"] == left["orders"]
    assert after["corporate_event_ids"] == before["corporate_event_ids"]
    assert after["corporate_changed"] is False
    assert after["decisions"] == []


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
    order = service.find_order(order.order_id)
    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert order.filled_quantity == 1
    assert order.remaining_quantity == 1
    assert len(ledger.fills) == 1
    first_cash = ledger.cash
    first_fill_id = ledger.fills[0].fill_id
    assert ledger.fills[0].consumed_quote.source_quote_id == quote.quote_id
    assert ledger.fills[0].consumed_quote.session == session
    assert ledger.fills[0].consumed_quote.source == "fixture://c08_cutoff"
    assert ledger.fills[0].consumed_quote.observed_at == quote.observed_at
    assert ledger.fills[0].consumed_quote.source_capabilities["two_sided_book"] is True
    expected_fee = 20.0 if market == "TW" else 1.0
    assert ledger.fills[0].fill_price == pytest.approx(101.0505)
    assert ledger.fills[0].fee == pytest.approx(expected_fee)
    assert ledger.fills[0].tax == 0
    assert ledger.cash == pytest.approx(10_000 - 101.0505 - expected_fee)
    assert runner.process_pending_orders() == []
    assert ledger.cash == first_cash
    assert [f.fill_id for f in ledger.fills] == [first_fill_id]

    quote.quote_id = f"TEST_ONLY_{session}_Q2"
    quote.timestamp = clock["now"] + timedelta(seconds=1)
    quote.observed_at = quote.timestamp
    clock["now"] = quote.timestamp
    decisions = runner.process_pending_orders()
    assert [d.action for d in decisions] == ["BUY_FILLED"]
    order = service.find_order(order.order_id)
    assert order.remaining_quantity == 0
    assert order.status == OrderStatus.FILLED
    assert len(ledger.fills) == 2
    assert len({f.fill_id for f in ledger.fills}) == 2
    assert ledger.cash == pytest.approx(10_000 - 2 * 101.0505 - 2 * expected_fee)
    second_cash = ledger.cash
    assert runner.process_pending_orders() == []
    assert ledger.cash == second_cash
    assert len(ledger.fills) == 2


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
    old = next(o for o in service.all_orders() if o.strategy_id == "TEST_ONLY_native")
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
        lambda q: q.model_copy(update={
            "source": "fallback://TEST_ONLY",
            "source_capabilities": {},
        }),
        lambda q: q.model_copy(update={
            "source_capabilities": {
                **q.source_capabilities,
                "two_sided_book": False,
            }
        }),
    ],
    ids=["last-only", "stale", "fallback", "no-bbo-capability"],
)
def test_non_executable_quote_capabilities_fail_closed_through_real_caller(
    tmp_path, monkeypatch, mutator
):
    runner, pm, service, cfg, clock, data, quote = setup_book(
        tmp_path, monkeypatch, session="REGULAR", size=2
    )
    runner.allow_fixture_quotes = True
    quote.timestamp = clock["now"] - timedelta(seconds=1)
    quote.observed_at = clock["now"]
    p = packet("TEST_ONLY_BAD_CAP", NOW, paper_execution_model="QUOTE_BOOK")
    p = sign_cio_packet(p, signer_id="fixture-test-signer")
    submitted = runner.submit_cio_packet(p, strategy_id="TEST_ONLY_native")
    assert submitted.action == "BUY_PENDING"
    order = next(o for o in service.all_orders() if o.strategy_id == "TEST_ONLY_native")
    assert order.status == OrderStatus.PENDING

    clock["now"] += timedelta(seconds=2)
    bad = mutator(quote)
    bad.timestamp = clock["now"] - timedelta(seconds=1)
    bad.observed_at = clock["now"]
    monkeypatch.setattr(runner.market_adapter, "get_latest_quote", lambda _symbol: bad)
    assert runner.process_pending_orders() == []
    order = service.find_order(order.order_id)
    assert order.status == OrderStatus.PENDING
    assert order.filled_quantity == 0
    assert order.remaining_quantity == 2
    ledger = pm.get_strategy_ledger("TEST_ONLY_native", DecisionScope.SWING)
    assert not ledger.fills
    assert ledger.cash == 1000


@pytest.mark.parametrize(
    "session,symbol,market,currency",
    [
        ("EXTENDED", "MSFT", "US", "USD"),
        ("ODD_LOT", "2330.TW", "TW", "TWD"),
    ],
)
def test_non_regular_session_without_packet_authorization_stays_pending_end_to_end(
    tmp_path, monkeypatch, session, symbol, market, currency
):
    runner, pm, service, base_cfg, clock, data, quote = setup_book(
        tmp_path, monkeypatch, session=session, size=2
    )
    cfg = base_cfg.model_copy(update={
        "strategy_id": f"TEST_ONLY_NEG_{market}_{session}",
        "market": market,
        "base_currency": currency,
        "reporting_currency": currency,
        "universe": [symbol],
        "paper_execution_model": "QUOTE_BOOK",
    })
    runner.configure(cfg)
    runner.allow_fixture_quotes = True
    data["bar"] = data["bar"].model_copy(update={"symbol": symbol})
    quote.symbol = symbol
    quote.timestamp = clock["now"] - timedelta(seconds=1)
    quote.observed_at = clock["now"]
    p = packet("TEST_ONLY_SESSION_NEGATIVE", NOW, paper_execution_model="QUOTE_BOOK")
    p.selected_instrument = symbol
    p = sign_cio_packet(p, signer_id="fixture-test-signer")
    submitted = runner.submit_cio_packet(p, strategy_id=cfg.strategy_id)
    assert submitted.action == "BUY_PENDING"
    order = next(o for o in service.all_orders() if o.strategy_id == cfg.strategy_id)

    clock["now"] += timedelta(seconds=2)
    quote.timestamp = clock["now"] - timedelta(seconds=1)
    quote.observed_at = clock["now"]
    assert runner.process_pending_orders() == []
    order = service.find_order(order.order_id)
    assert order.status == OrderStatus.PENDING
    assert order.filled_quantity == 0
    ledger = pm.get_strategy_ledger(cfg.strategy_id, DecisionScope.SWING)
    assert not ledger.fills


def test_quote_source_timeout_fails_closed_through_submit_and_process(
    tmp_path, monkeypatch
):
    runner, pm, service, cfg, clock, data, quote = setup_book(
        tmp_path, monkeypatch, session="REGULAR", size=2
    )
    runner.allow_fixture_quotes = True
    quote.timestamp = clock["now"] - timedelta(seconds=1)
    quote.observed_at = clock["now"]
    p = packet("TEST_ONLY_TIMEOUT", NOW, paper_execution_model="QUOTE_BOOK")
    p = sign_cio_packet(p, signer_id="fixture-test-signer")
    assert runner.submit_cio_packet(p, strategy_id="TEST_ONLY_native").action == "BUY_PENDING"
    order = next(o for o in service.all_orders() if o.strategy_id == "TEST_ONLY_native")

    def timeout(_symbol):
        raise TimeoutError("TEST_ONLY quote source timeout")

    clock["now"] += timedelta(seconds=2)
    monkeypatch.setattr(runner.market_adapter, "get_latest_quote", timeout)
    assert runner.process_pending_orders() == []
    order = service.find_order(order.order_id)
    assert order.status == OrderStatus.PENDING
    assert order.filled_quantity == 0
    ledger = pm.get_strategy_ledger("TEST_ONLY_native", DecisionScope.SWING)
    assert not ledger.fills
    assert ledger.cash == 1000


def test_bar_reference_only_never_becomes_book_through_real_caller(
    tmp_path, monkeypatch
):
    clock = {"now": NOW}
    data = {
        "bar": make_bar(
            timestamp=NOW - timedelta(minutes=1),
            observed_at=NOW,
        )
    }
    runner, pm, service = runner_harness(tmp_path, clock, data)
    runner.allow_fixture_quotes = True
    cfg = service.experiment_for("TEST_ONLY_native").model_copy(
        update={"paper_execution_model": "QUOTE_BOOK"}
    )
    runner.configure(cfg)
    monkeypatch.setattr(runner.market_adapter, "get_latest_quote", lambda _symbol: None)
    p = packet("TEST_ONLY_BAR_ONLY", NOW, paper_execution_model="QUOTE_BOOK")
    p = sign_cio_packet(p, signer_id="fixture-test-signer")
    assert runner.submit_cio_packet(p, strategy_id="TEST_ONLY_native").action == "BUY_PENDING"
    order = next(o for o in service.all_orders() if o.strategy_id == "TEST_ONLY_native")

    clock["now"] += timedelta(minutes=2)
    data["bar"] = make_bar(
        timestamp=NOW + timedelta(minutes=1),
        observed_at=clock["now"],
    )
    assert runner.process_pending_orders() == []
    order = service.find_order(order.order_id)
    assert order.status == OrderStatus.PENDING
    assert order.filled_quantity == 0
    ledger = pm.get_strategy_ledger("TEST_ONLY_native", DecisionScope.SWING)
    assert not ledger.fills
    assert ledger.cash == 1000


def test_corporate_actions_native_cash_and_fx_fail_closed(tmp_path, monkeypatch):
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
        clock = {"now": datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)}
        state.runner._now_fn = lambda: clock["now"]
        state.paper_orders._now_fn = state.runner._now_fn
        state.runner.allow_fixture_quotes = True

        def current_bar(symbol):
            return Bar(
                symbol=symbol,
                timestamp=clock["now"] - timedelta(minutes=5),
                observed_at=clock["now"],
                open=100,
                high=102,
                low=99,
                close=100,
                volume=1000,
                source="fixture://ISSUE7_CA_BAR",
                quality="TEST_ONLY",
                is_fixture=True,
                is_synthetic=False,
            )

        quote_holder = {"value": None}
        monkeypatch.setattr(
            state.market_adapter,
            "get_latest_quote",
            lambda _symbol: quote_holder["value"],
        )
        monkeypatch.setattr(
            state.market_adapter,
            "get_latest_bar",
            lambda symbol: current_bar(symbol),
        )
        monkeypatch.setattr(
            state.market_adapter,
            "get_bars",
            lambda symbol, *args, **kwargs: [current_bar(symbol)],
        )

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
                paper_execution_model="QUOTE_BOOK",
            ))

        p = CIODecisionPacket(
            case_id="TEST_ONLY_CA_REAL_FILL",
            as_of=clock["now"],
            expiry=clock["now"] + timedelta(hours=1),
            thesis="TEST_ONLY real pending caller corporate-action seed",
            selected_instrument="2330.TW",
            action="BUY",
            quantity=2,
            strategy_version="TEST_ONLY_CA_V1",
            is_fixture=True,
            conditions={
                "paper_execution_model": "QUOTE_BOOK",
                "allow_partial_fills": True,
                "allow_odd_lot": True,
            },
        )
        p = sign_cio_packet(p, signer_id="fixture-test-signer")
        submitted = state.runner.submit_cio_packet(
            p, strategy_id="TEST_ONLY_TW_BOOK"
        )
        assert submitted.action == "BUY_PENDING"

        clock["now"] += timedelta(seconds=2)
        quote_holder["value"] = Quote(
            symbol="2330.TW",
            timestamp=clock["now"] - timedelta(seconds=1),
            observed_at=clock["now"],
            bid=100.0,
            ask=100.1,
            bid_size=2,
            ask_size=2,
            last_price=100.05,
            source="fixture://ISSUE7_CA_BOOK",
            quality="TEST_ONLY",
            session="ODD_LOT",
            quote_id="TEST_ONLY_CA_Q1",
            is_stale=False,
            is_synthetic=False,
            source_capabilities={
                "source": "fixture://ISSUE7_CA_BOOK",
                "two_sided_book": True,
                "size_backed": True,
                "exchange_session_attested": True,
                "entitlement_evidence_id": "TEST_ONLY_CA_EID",
                "entitlement_status": "TEST_ONLY",
                "supported_sessions": ["ODD_LOT"],
                "odd_lot_book": True,
            },
        )
        decisions = state.runner.process_pending_orders()
        assert [d.action for d in decisions] == ["BUY_FILLED"]

        tw_order = next(
            order for order in state.paper_orders.all_orders()
            if order.strategy_id == "TEST_ONLY_TW_BOOK"
        )
        assert tw_order.status == OrderStatus.FILLED
        tw = state.portfolio_manager.get_strategy_ledger(
            "TEST_ONLY_TW_BOOK", DecisionScope.SWING
        )
        us = state.portfolio_manager.get_strategy_ledger(
            "TEST_ONLY_US_BOOK", DecisionScope.SWING
        )
        assert len(tw.fills) == 1
        assert tw.fills[0].fill_price == pytest.approx(100.15005)
        assert tw.fills[0].fee == pytest.approx(20)
        assert tw.fills[0].tax == 0
        assert tw.positions["2330.TW"].quantity == 2
        assert tw.cash == pytest.approx(100_000 - 2 * 100.15005 - 20)
        tw_cash_before = tw.cash
        us_cash_before = us.cash
        now = tw.fills[0].timestamp

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
        events_before = state.event_store.count()
        qty_before = tw.positions["2330.TW"].quantity
        cash_before_gate = tw.cash
        state.runner.corporate_actions = PaperCorporateActions(
            state.portfolio_manager,
            state.event_store,
            fixture_mode=False,
        )
        with pytest.raises(ValueError, match="FIXTURE_ACTION_FORBIDDEN"):
            state.runner.apply_corporate_action(
                split,
                strategy_id="TEST_ONLY_TW_BOOK",
                bucket=DecisionScope.SWING,
                now=now + timedelta(days=1),
            )
        assert state.event_store.count() == events_before
        assert tw.positions["2330.TW"].quantity == qty_before
        assert tw.cash == cash_before_gate

        state.runner.corporate_actions = PaperCorporateActions(
            state.portfolio_manager,
            state.event_store,
            fixture_mode=True,
        )
        applied = state.runner.apply_corporate_action(
            split,
            strategy_id="TEST_ONLY_TW_BOOK",
            bucket=DecisionScope.SWING,
            now=now + timedelta(days=1),
        )
        assert applied is True
        assert tw.positions["2330.TW"].quantity == 4
        assert tw.positions["2330.TW"].average_entry_price == pytest.approx(50.075025)
        assert tw.positions["2330.TW"].current_price is None
        assert tw.positions["2330.TW"].unrealized_pnl is None
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
        assert tw.realized_pnl == pytest.approx(8)
        assert us.cash == us_cash_before
        assert us.realized_pnl == 0
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

        future = FxRateReceipt(
            "USD/TWD",
            Decimal("32"),
            as_of + timedelta(days=1),
            "TEST_ONLY future FX",
            "https://example.invalid/test-only-future-fx",
            (as_of + timedelta(days=1)).date(),
            provenance="TEST_ONLY",
        )
        with pytest.raises(FxReportingBlocked, match="future FX"):
            state.runner.report_strategy_nav(
                "TEST_ONLY_US_BOOK",
                as_of=as_of,
                rate_receipts=[future],
                allow_test_only=True,
            )
    finally:
        state.runner.shutdown()


def test_pending_execution_and_corporate_action_survive_real_process_restart(tmp_path):
    root = Path(__file__).resolve().parents[2]
    runtime = tmp_path / "runtime"
    control = tmp_path / "process-control.json"
    action_at = PROCESS_BASE + timedelta(seconds=2)

    _write_process_control(
        control,
        now=PROCESS_BASE,
        quotes={},
        action_at=action_at,
    )
    seeded = _run_process_helper(root, runtime, control, "seed")
    assert _strategy_state(seeded, "TEST_ONLY_RESTART_US")["orders"][0]["status"] == "PENDING"
    assert _strategy_state(seeded, "TEST_ONLY_RESTART_TW")["orders"][0]["status"] == "PENDING"
    assert not _strategy_state(seeded, "TEST_ONLY_RESTART_US")["fills"]
    assert not _strategy_state(seeded, "TEST_ONLY_RESTART_TW")["fills"]

    q1 = {
        "MSFT": {"quote_id": "TEST_ONLY_US_Q1", "session": "REGULAR", "size": 1},
        "2330.TW": {"quote_id": "TEST_ONLY_TW_Q1", "session": "ODD_LOT", "size": 2},
    }
    _write_process_control(
        control,
        now=PROCESS_BASE + timedelta(seconds=3),
        quotes=q1,
        action_at=action_at,
    )
    first = _run_process_helper(
        root, runtime, control, "advance", "--apply-action"
    )
    us_first = _strategy_state(first, "TEST_ONLY_RESTART_US")
    tw_first = _strategy_state(first, "TEST_ONLY_RESTART_TW")
    assert [d["action"] for d in first["decisions"]] == [
        "BUY_PARTIALLY_FILLED",
        "BUY_FILLED",
    ]
    assert us_first["orders"][0]["status"] == "PARTIALLY_FILLED"
    assert us_first["orders"][0]["audit_metadata"]["filled_quantity"] == 1
    assert len(us_first["fills"]) == 1
    assert us_first["fills"][0]["fill_price"] == pytest.approx(101.0505)
    assert us_first["fills"][0]["fee"] == pytest.approx(1)
    assert us_first["cash"] == pytest.approx(10_000 - 101.0505 - 1)
    assert us_first["positions"]["MSFT"]["quantity"] == 1
    assert tw_first["orders"][0]["status"] == "FILLED"
    assert len(tw_first["fills"]) == 1
    assert tw_first["fills"][0]["fill_price"] == pytest.approx(101.0505)
    assert tw_first["fills"][0]["fee"] == pytest.approx(20)
    assert tw_first["cash"] == pytest.approx(
        100_000 - 2 * 101.0505 - 20 + 4
    )
    assert tw_first["positions"]["2330.TW"]["quantity"] == 2
    assert first["corporate_changed"] is True
    assert len(first["corporate_event_ids"]) == 2

    replay = _run_process_helper(
        root, runtime, control, "advance", "--apply-action"
    )
    _assert_replay_idempotent(first, replay)

    _write_process_control(
        control,
        now=PROCESS_BASE + timedelta(seconds=6),
        quotes={
            "MSFT": {
                "quote_id": "TEST_ONLY_US_Q2",
                "session": "REGULAR",
                "size": 1,
            },
            "2330.TW": q1["2330.TW"],
        },
        action_at=action_at,
    )
    completed = _run_process_helper(
        root, runtime, control, "advance", "--apply-action"
    )
    us_done = _strategy_state(completed, "TEST_ONLY_RESTART_US")
    tw_done = _strategy_state(completed, "TEST_ONLY_RESTART_TW")
    assert [d["action"] for d in completed["decisions"]] == ["BUY_FILLED"]
    assert us_done["orders"][0]["status"] == "FILLED"
    assert us_done["orders"][0]["audit_metadata"]["filled_quantity"] == 2
    assert len(us_done["fills"]) == 2
    assert len({fill["fill_id"] for fill in us_done["fills"]}) == 2
    assert {
        fill["consumed_quote"]["source_quote_id"] for fill in us_done["fills"]
    } == {"TEST_ONLY_US_Q1", "TEST_ONLY_US_Q2"}
    assert us_done["cash"] == pytest.approx(
        10_000 - 2 * 101.0505 - 2
    )
    assert us_done["positions"]["MSFT"]["quantity"] == 2
    assert tw_done["cash"] == pytest.approx(tw_first["cash"])
    assert tw_done["positions"] == tw_first["positions"]
    assert completed["corporate_changed"] is False
    assert completed["corporate_event_ids"] == first["corporate_event_ids"]

    def snapshot(base_url: str):
        with httpx.Client(timeout=5) as client:
            return {
                "orders": client.get(f"{base_url}/api/paper/orders").json(),
                "readback": client.get(f"{base_url}/api/paper/readback").json(),
                "portfolio": client.get(f"{base_url}/api/portfolio").json(),
            }

    server_control = tmp_path / "server-control"
    with _server(root, runtime, server_control, read_only=True) as first_server:
        first_pid = first_server.pid
        before = snapshot(first_server.base_url)
    with _server(root, runtime, server_control, read_only=True) as second_server:
        assert second_server.pid != first_pid
        after = snapshot(second_server.base_url)
    assert after == before
    readback_ids = [fill["fill_id"] for fill in after["readback"]["fills"]]
    assert len(readback_ids) == len(set(readback_ids))
    assert set(readback_ids) >= {
        fill["fill_id"] for fill in us_done["fills"]
    }


def test_restart_chain_non_vacuity_requires_pending_caller_and_quote_dedup(tmp_path):
    root = Path(__file__).resolve().parents[2]
    control = tmp_path / "control.json"
    action_at = PROCESS_BASE + timedelta(seconds=2)

    blocked_runtime = tmp_path / "blocked-runtime"
    _write_process_control(
        control, now=PROCESS_BASE, quotes={}, action_at=action_at
    )
    _run_process_helper(root, blocked_runtime, control, "seed")
    _write_process_control(
        control,
        now=PROCESS_BASE + timedelta(seconds=3),
        quotes={
            "MSFT": {
                "quote_id": "TEST_ONLY_BLOCKED_Q1",
                "session": "REGULAR",
                "size": 1,
            },
            "2330.TW": {
                "quote_id": "TEST_ONLY_BLOCKED_TW_Q1",
                "session": "ODD_LOT",
                "size": 2,
            },
        },
        action_at=action_at,
    )
    blocked = _run_process_helper(
        root,
        blocked_runtime,
        control,
        "advance",
        "--break-pending",
        check=False,
    )
    assert blocked.returncode != 0
    assert "TEST_ONLY_PENDING_EXECUTION_BLOCKED" in blocked.stderr

    dedup_runtime = tmp_path / "dedup-runtime"
    _write_process_control(
        control, now=PROCESS_BASE, quotes={}, action_at=action_at
    )
    _run_process_helper(root, dedup_runtime, control, "seed")
    _write_process_control(
        control,
        now=PROCESS_BASE + timedelta(seconds=3),
        quotes={
            "MSFT": {
                "quote_id": "TEST_ONLY_DEDUP_Q1",
                "session": "REGULAR",
                "size": 1,
            },
            "2330.TW": {
                "quote_id": "TEST_ONLY_DEDUP_TW_Q1",
                "session": "ODD_LOT",
                "size": 2,
            },
        },
        action_at=action_at,
    )
    first = _run_process_helper(
        root, dedup_runtime, control, "advance", "--apply-action"
    )
    broken_replay = _run_process_helper(
        root,
        dedup_runtime,
        control,
        "advance",
        "--apply-action",
        "--break-dedup",
    )
    with pytest.raises(AssertionError):
        _assert_replay_idempotent(first, broken_replay)
    us_broken = _strategy_state(broken_replay, "TEST_ONLY_RESTART_US")
    assert len(us_broken["fills"]) == 2
    assert us_broken["cash"] < _strategy_state(first, "TEST_ONLY_RESTART_US")["cash"]
