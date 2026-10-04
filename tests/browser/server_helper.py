from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket

import uvicorn

from cio_market_lab.api.app import create_app
from cio_market_lab.domain.events import EventEnvelope, EventType
from cio_market_lab.domain.models import (
    Bar,
    CIODecisionPacket,
    CIOProvenance,
    DecisionScope,
    Fill,
    OrderOrigin,
    OrderSide,
    OrderType,
    Quote,
)
from cio_market_lab.engine.autonomous_runner import DYNAMIC_DESK_ID
from cio_market_lab.engine.cio_packet import compute_packet_signature, sign_cio_packet
from cio_market_lab.engine.paper_orders import PaperDataContext, PaperExperimentSettings, PaperOrderRequest
from cio_market_lab.research.browser import FakeBrowserResearchAdapter, ResearchItem


def _test_only_research_adapter() -> FakeBrowserResearchAdapter:
    return FakeBrowserResearchAdapter([
        ResearchItem(
            id="TEST_ONLY_RESEARCH_FIXTURE",
            url="https://example.test/project-money/research-fixture",
            title="TEST_ONLY populated research fixture",
            claims=["TEST_ONLY verified browser acceptance content"],
            source_mode="fake",
            status="verified",
            related_symbols=["2330.TW"],
            provenance={"source": "TEST_ONLY_BROWSER_HARNESS"},
            hypothesis="TEST_ONLY browser acceptance only",
        )
    ])


class TestOnlyMarketAdapter:
    """Deterministic browser-harness adapter. Never reaches an external source."""

    source_name = "TEST_ONLY_BROWSER_ADAPTER"

    def __init__(self, mode: str = "normal") -> None:
        self.mode = mode
        self.offline_mode = True
        self.last_fetch_mode = f"TEST_ONLY_fixture_{mode}"
        self.last_error = None

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def get_bars(self, symbol, start=None, end=None, timeframe="1D", limit=80):
        if self.mode == "error" or symbol == "ERROR.TW":
            self.last_error = "TEST_ONLY forced adapter error"
            raise RuntimeError(self.last_error)
        if self.mode == "empty":
            return []
        now = self._now()
        stale = self.mode == "stale" or symbol == "STALE.TW"
        observed = now - (timedelta(hours=2) if stale else timedelta(seconds=5))
        return [
            Bar(
                symbol=symbol,
                timestamp=now - timedelta(minutes=5),
                observed_at=observed,
                open=99.0,
                high=101.0,
                low=98.5,
                close=100.0,
                volume=1000,
                source=self.source_name,
                quality="TEST_ONLY",
                is_stale=stale,
                is_fixture=True,
                is_synthetic=False,
            )
        ]

    def stream_bars(self, symbols):
        for symbol in symbols:
            yield from self.get_bars(symbol)

    def get_latest_bar(self, symbol):
        bars = self.get_bars(symbol, limit=1)
        return bars[-1] if bars else None

    def get_latest_quote(self, symbol):
        if symbol == "ERROR.TW":
            self.last_error = "TEST_ONLY forced quote error"
            return None
        now = self._now()
        stale = symbol == "STALE.TW"
        observed = now - (timedelta(hours=2) if stale else timedelta(seconds=2))
        return Quote(
            symbol=symbol,
            timestamp=observed,
            observed_at=observed,
            bid=99.9,
            ask=100.1,
            bid_size=10,
            ask_size=10,
            last_price=100.0,
            last_size=10,
            source=self.source_name,
            is_stale=stale,
            quality="TEST_ONLY",
            is_synthetic=False,
            source_capabilities={"is_fixture": True, "book": True},
            quote_id=f"TEST_ONLY-{symbol}",
        )

    def timeframe_metadata(self, timeframe):
        return {"timeframe": timeframe, "interval": "TEST_ONLY"}



def _fixture_fill(state, order_id: str, quantity: float, fill_id: str, *, strategy_id: str | None = None) -> None:
    order = state.paper_orders.find_order(order_id)
    if order is None:
        raise AssertionError(f"missing TEST_ONLY order {order_id}")
    fill = Fill(
        currency=order.currency,
        fill_id=fill_id,
        order_id=order.order_id,
        symbol=order.symbol,
        bucket=order.bucket,
        side=order.side,
        quantity=quantity,
        fill_price=100.0,
        fee=20.0,
        tax=0.0,
        slippage=0.0,
        timestamp=datetime.now(timezone.utc),
        provenance={"strategy_id": strategy_id} if strategy_id else {"fixture": "TEST_ONLY_BROWSER"},
        assumptions={"execution": "TEST_ONLY_BROWSER_FIXTURE", "slippage_embedded": True},
        quote_verification="TEST_ONLY_FIXTURE",
    )
    state.portfolio_manager.apply_fill(fill, strategy_id)
    state.event_store.append(EventEnvelope(
        event_type=EventType.ORDER_FILLED,
        aggregate_id=order.order_id,
        payload=fill.model_dump(mode="json"),
    ))


def _packet(case_id: str, *, expiry: datetime, actor_role: str = "CHIEF_INVESTMENT_OFFICER") -> CIODecisionPacket:
    now = datetime.now(timezone.utc)
    return CIODecisionPacket(
        case_id=case_id,
        as_of=now,
        evidence=["fixture://TEST_ONLY_BROWSER_CIO"],
        thesis="TEST_ONLY browser CIO paper decision",
        selected_instrument="2330.TW",
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=1.0,
        conditions={"strategy_id": DYNAMIC_DESK_ID, "allow_odd_lot": True},
        risk_assessment={"fixture": True},
        alternatives_considered=[],
        expiry=expiry,
        confidence=0.8,
        strategy_version="TEST_ONLY_CIO_V1",
        provenance=CIOProvenance(
            authority="MAIN_CIO",
            actor_role=actor_role,
            signer_id="fixture-test-signer",
            source="TEST_ONLY_BROWSER_SERVER",
        ),
        is_fixture=True,
    )


def _seed_order_flow_fixtures(state) -> dict:
    state.runner.allow_fixture_quotes = True

    existing = {
        item.audit_metadata.get("fixture_receipt"): item
        for item in state.paper_orders.all_orders()
        if isinstance(item.audit_metadata, dict) and item.audit_metadata.get("fixture_receipt")
    }

    if "TEST_ONLY_MANUAL_FILLED" not in existing:
        order = state.paper_orders.submit(PaperOrderRequest(
            currency="TWD",
            symbol="2330.TW",
            market="TW",
            bucket=DecisionScope.SWING,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=2.0,
            limit_price=101.0,
            origin=OrderOrigin.MANUAL,
            reason="TEST_ONLY manual filled readback",
            audit_metadata={"is_fixture": True, "fixture_receipt": "TEST_ONLY_MANUAL_FILLED"},
            data=PaperDataContext(source="fixture://TEST_ONLY_MANUAL_FILLED", last_price=100.0),
        ))
        _fixture_fill(state, order.order_id, 2.0, "TEST_ONLY_FILL_MANUAL_FULL")

    if "TEST_ONLY_MANUAL_PARTIAL" not in existing:
        order = state.paper_orders.submit(PaperOrderRequest(
            currency="TWD",
            symbol="2330.TW",
            market="TW",
            bucket=DecisionScope.SWING,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=4.0,
            limit_price=101.0,
            origin=OrderOrigin.MANUAL,
            reason="TEST_ONLY manual partial readback",
            audit_metadata={"is_fixture": True, "fixture_receipt": "TEST_ONLY_MANUAL_PARTIAL"},
            data=PaperDataContext(source="fixture://TEST_ONLY_MANUAL_PARTIAL", last_price=100.0),
        ))
        _fixture_fill(state, order.order_id, 2.0, "TEST_ONLY_FILL_MANUAL_PARTIAL")

    reg = state.registry.get_registered("opening_range_breakout")
    if reg is not None:
        state.runner.configure(PaperExperimentSettings(
            strategy_id=reg.id,
            strategy_name=reg.name,
            market="TW",
            base_currency="TWD",
            reporting_currency="TWD",
            enabled=True,
            universe=["2330.TW"],
            initial_cash=500_000.0,
            max_position_notional=100_000.0,
            allowed_buckets=[DecisionScope.SWING],
        ))
        if "TEST_ONLY_STRATEGY_ORDER" not in existing:
            order = state.paper_orders.submit(PaperOrderRequest(
                currency="TWD",
                symbol="2330.TW",
                market="TW",
                bucket=DecisionScope.SWING,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=1.0,
                limit_price=101.0,
                origin=OrderOrigin.STRATEGY,
                reason="TEST_ONLY registry-backed strategy readback",
                strategy_id=reg.id,
                strategy_version=reg.version,
                audit_metadata={"is_fixture": True, "fixture_receipt": "TEST_ONLY_STRATEGY_ORDER"},
                data=PaperDataContext(source="fixture://TEST_ONLY_STRATEGY_ORDER", last_price=100.0),
            ))
            _fixture_fill(state, order.order_id, 1.0, "TEST_ONLY_FILL_STRATEGY", strategy_id=reg.id)

    state.runner.configure(PaperExperimentSettings(
        strategy_id=DYNAMIC_DESK_ID,
        strategy_name="TEST_ONLY Main CIO Desk",
        market="TW",
        base_currency="TWD",
        reporting_currency="TWD",
        enabled=True,
        universe=["2330.TW"],
        initial_cash=500_000.0,
        max_position_notional=100_000.0,
        allowed_buckets=[DecisionScope.SWING],
    ))

    positive_case = "TEST_ONLY_CIO_POSITIVE"
    positive_record = state.runner.learning_store.get_record(positive_case)
    if positive_record is None:
        packet = _packet(
            positive_case,
            expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        sign_cio_packet(packet, signer_id="fixture-test-signer")
        positive_decision = state.runner.submit_cio_packet(packet, strategy_id=DYNAMIC_DESK_ID)
        positive = positive_decision.model_dump(mode="json")
    else:
        positive = {
            "action": "RESTORED",
            "reason": "TEST_ONLY existing signed CIO case",
            "order_id": positive_record.order_id,
            "case_id": positive_case,
        }

    baseline_fills = len(state.portfolio_manager.get_strategy_portfolio(DYNAMIC_DESK_ID, DecisionScope.SWING).fills)
    baseline_cash = state.portfolio_manager.get_strategy_portfolio(DYNAMIC_DESK_ID, DecisionScope.SWING).cash
    negatives = {}

    missing = _packet("TEST_ONLY_CIO_MISSING_SIG", expiry=datetime.now(timezone.utc) + timedelta(hours=1))
    negatives["missing_signature"] = state.runner.submit_cio_packet(missing, strategy_id=DYNAMIC_DESK_ID).model_dump(mode="json")

    invalid = _packet("TEST_ONLY_CIO_INVALID_SIG", expiry=datetime.now(timezone.utc) + timedelta(hours=1))
    invalid.provenance.signature = "TEST_ONLY_INVALID_SIGNATURE"
    negatives["invalid_signature"] = state.runner.submit_cio_packet(invalid, strategy_id=DYNAMIC_DESK_ID).model_dump(mode="json")

    expired = _packet("TEST_ONLY_CIO_EXPIRED", expiry=datetime.now(timezone.utc) - timedelta(seconds=1))
    sign_cio_packet(expired, signer_id="fixture-test-signer")
    negatives["expired"] = state.runner.submit_cio_packet(expired, strategy_id=DYNAMIC_DESK_ID).model_dump(mode="json")

    worker = _packet(
        "TEST_ONLY_CIO_WORKER",
        expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        actor_role="ENGINEERING_WORKER",
    )
    worker.provenance.signature = compute_packet_signature(worker)
    negatives["worker_authority"] = state.runner.submit_cio_packet(worker, strategy_id=DYNAMIC_DESK_ID).model_dump(mode="json")

    after_fills = len(state.portfolio_manager.get_strategy_portfolio(DYNAMIC_DESK_ID, DecisionScope.SWING).fills)
    after_cash = state.portfolio_manager.get_strategy_portfolio(DYNAMIC_DESK_ID, DecisionScope.SWING).cash
    state.runner._persist_portfolios()

    return {
        "positive": positive,
        "negative": negatives,
        "negative_invariants": {
            "fills_before": baseline_fills,
            "fills_after": after_fills,
            "cash_before": baseline_cash,
            "cash_after": after_cash,
        },
        "paper_only": True,
        "broker_connected": False,
    }


async def _watch_stop(server: uvicorn.Server, stop_file: Path) -> None:
    while not server.should_exit:
        if stop_file.exists():
            server.should_exit = True
            return
        await asyncio.sleep(0.05)


async def serve(
    root: Path,
    runtime: Path,
    ready_file: Path,
    stop_file: Path,
    writable: bool,
    market_mode: str,
    order_flow_fixtures: bool,
) -> None:
    os.environ["CIO_MARKET_LAB_OFFLINE"] = "1"
    os.environ["HERMES_OFFLINE"] = "1"
    if writable:
        os.environ["CIO_AUTONOMOUS_RUNNER_OWNER"] = "1"
        os.environ["CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS"] = "1"
    runtime.mkdir(parents=True, exist_ok=True)

    # Initialize only the disposable TEST_ONLY runtime so a subsequent read-only
    # app can open canonical stores without touching any user/runtime path.
    if not writable:
        initializer = create_app(
            workspace_root=root,
            runtime_dir=runtime,
            fixture_mode=True,
            is_read_only=False,
            market_adapter=TestOnlyMarketAdapter(market_mode),
        )
        init_state = initializer.state.app_state
        init_state.research_adapter = _test_only_research_adapter()
        if market_mode == "normal":
            init_state.runner.configure(PaperExperimentSettings(
                strategy_id="TEST_ONLY_BROWSER_EXPERIMENT",
                enabled=False,
                universe=["2330.TW"],
                initial_cash=500_000.0,
            ))
            if not init_state.paper_orders.all_orders():
                init_state.paper_orders.submit(PaperOrderRequest(
                    symbol="2330.TW",
                    market="TW",
                    bucket=DecisionScope.SWING,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity=1.0,
                    origin=OrderOrigin.MANUAL,
                    reason="TEST_ONLY_BROWSER_ORDER",
                    audit_metadata={
                        "is_fixture": True,
                        "fixture_receipt": "TEST_ONLY_BROWSER_ORDER",
                    },
                    data=PaperDataContext(
                        source="fixture://TEST_ONLY_BROWSER_ORDER",
                        last_price=100.0,
                        is_stale=False,
                        is_fallback=False,
                    ),
                ))
            init_state.runner._persist_portfolios()
        init_state.runner.shutdown()

    app = create_app(
        workspace_root=root,
        runtime_dir=runtime,
        fixture_mode=True,
        is_read_only=not writable,
        market_adapter=TestOnlyMarketAdapter(market_mode),
    )
    app.state.app_state.research_adapter = _test_only_research_adapter()
    if order_flow_fixtures:
        app.state.test_only_order_flow = _seed_order_flow_fixtures(app.state.app_state)

        @app.get("/api/test-only/order-flow")
        def test_only_order_flow():
            return app.state.test_only_order_flow

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = int(sock.getsockname()[1])
    ready_file.write_text(json.dumps({"pid": os.getpid(), "port": port}), encoding="utf-8")
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on"))
    watcher = asyncio.create_task(_watch_stop(server, stop_file))
    try:
        await server.serve(sockets=[sock])
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--writable", action="store_true")
    parser.add_argument("--order-flow-fixtures", action="store_true")
    parser.add_argument(
        "--market-mode",
        choices=("normal", "empty", "stale", "error"),
        default="normal",
    )
    args = parser.parse_args()
    asyncio.run(
        serve(
            args.root,
            args.runtime,
            args.ready_file,
            args.stop_file,
            args.writable,
            args.market_mode,
            args.order_flow_fixtures,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
