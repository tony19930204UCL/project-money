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
from cio_market_lab.data.base import MarketDataAdapter
from cio_market_lab.domain.events import EventEnvelope, EventType
from cio_market_lab.domain.models import (
    Bar,
    CIODecisionPacket,
    CIOProvenance,
    DecisionScope,
    OrderOrigin,
    OrderStatus,
    Quote,
)
from cio_market_lab.engine.cio_packet import sign_cio_packet
from cio_market_lab.engine.execution import ExecutionEngine
from cio_market_lab.engine.paper_orders import PaperExperimentSettings


class TestOnlyBrowserMarketAdapter(MarketDataAdapter):
    """Deterministic TEST_ONLY market data for browser source regression."""

    @property
    def source_name(self) -> str:
        return "TEST_ONLY_BROWSER_ADAPTER"

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def get_bars(self, symbol: str, start=None, end=None, timeframe="1D", limit=None):
        symbol = symbol.upper()
        if symbol == "ERROR.TW":
            raise RuntimeError("TEST_ONLY_BROWSER_NEGATIVE_RESPONSE")
        if symbol == "MISSING.TW":
            return []
        now = self._now()
        stale = symbol == "AAPL"
        quality = "fallback" if stale else "good"
        source = "TEST_ONLY_BROWSER_STALE_FIXTURE" if stale else "TEST_ONLY_BROWSER_FIXTURE"
        observed = now - timedelta(hours=2) if stale else now
        bars = []
        for i, close in enumerate((98.0, 99.0, 100.0, 101.0, 102.0, 103.0)):
            ts = now - timedelta(minutes=(6 - i) * 5)
            bars.append(Bar(
                symbol=symbol,
                timestamp=ts,
                observed_at=observed,
                open=close - 0.5,
                high=close + 1.0,
                low=close - 1.0,
                close=close,
                volume=1000 + i * 100,
                source=source,
                delay_seconds=7200.0 if stale else 0.0,
                quality=quality,
                is_stale=stale,
                is_fixture=True,
                is_synthetic=True,
            ))
        return bars[-limit:] if limit else bars

    def stream_bars(self, symbols):
        for symbol in symbols:
            bars = self.get_bars(symbol)
            if bars:
                yield bars[-1]

    def get_latest_bar(self, symbol: str):
        bars = self.get_bars(symbol)
        return bars[-1] if bars else None

    def get_latest_quote(self, symbol: str):
        bar = self.get_latest_bar(symbol)
        if bar is None:
            return None
        return Quote(
            symbol=bar.symbol,
            timestamp=bar.timestamp + timedelta(seconds=1),
            observed_at=self._now(),
            bid=bar.close - 0.1,
            ask=bar.close + 0.1,
            bid_size=100,
            ask_size=100,
            last_price=bar.close,
            last_size=10,
            source=bar.source,
            delay_seconds=bar.delay_seconds,
            is_stale=bar.is_stale,
            quality=bar.quality,
            is_synthetic=False,
            quote_id=f"TEST_ONLY_BROWSER_{bar.symbol}",
            source_capabilities={"is_fixture": True, "top_of_book": True},
        )


def _offline() -> None:
    os.environ["CIO_MARKET_LAB_OFFLINE"] = "1"
    os.environ["HERMES_OFFLINE"] = "1"
    os.environ["CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS"] = "1"
    os.environ.pop("CIO_MATERIAL_GATE_ENABLED", None)


def _prepare(workspace_root: Path, runtime_dir: Path) -> None:
    _offline()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(
        workspace_root=workspace_root,
        runtime_dir=runtime_dir,
        fixture_mode=True,
        is_read_only=False,
        market_adapter=TestOnlyBrowserMarketAdapter(),
    )
    app.state.app_state.runner.shutdown()


async def _watch_stop(server: uvicorn.Server, stop_file: Path) -> None:
    while not server.should_exit:
        if stop_file.exists():
            server.should_exit = True
            return
        await asyncio.sleep(0.05)


async def _serve(
    workspace_root: Path,
    runtime_dir: Path,
    ready_file: Path,
    stop_file: Path,
    read_only: bool,
) -> None:
    _offline()
    app = create_app(
        workspace_root=workspace_root,
        runtime_dir=runtime_dir,
        fixture_mode=True,
        is_read_only=read_only,
        market_adapter=TestOnlyBrowserMarketAdapter(),
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = int(sock.getsockname()[1])
    ready_file.parent.mkdir(parents=True, exist_ok=True)
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


def _open_test_app(workspace_root: Path, runtime_dir: Path):
    _offline()
    app = create_app(
        workspace_root=workspace_root,
        runtime_dir=runtime_dir,
        fixture_mode=True,
        is_read_only=False,
        market_adapter=TestOnlyBrowserMarketAdapter(),
    )
    app.state.app_state.runner.allow_fixture_quotes = True
    return app


def _seed_authorities(workspace_root: Path, runtime_dir: Path) -> dict:
    app = _open_test_app(workspace_root, runtime_dir)
    state = app.state.app_state
    try:
        strategy_id = "TEST_ONLY_browser_strategy"
        state.runner.configure(PaperExperimentSettings(
            strategy_id=strategy_id,
            strategy_name="TEST_ONLY Browser Strategy",
            style="aggressive_momentum",
            market="US",
            initial_cash=100_000.0,
            base_currency="USD",
            reporting_currency="USD",
            enabled=True,
            universe=["NVDA"],
            mode=DecisionScope.SWING,
            max_position_notional=10_000.0,
            max_data_age_seconds=3600.0,
        ))
        strategy_result = state.runner.run_one_cycle(strategy_id, symbols=["NVDA"])
        strategy_orders = [
            order for order in state.paper_orders.all_orders()
            if order.origin == OrderOrigin.STRATEGY and order.strategy_id == strategy_id
        ]
        if not strategy_orders:
            raise AssertionError(f"TEST_ONLY strategy runner created no STRATEGY order: {strategy_result}")

        cio_id = "TEST_ONLY_browser_cio"
        state.runner.configure(PaperExperimentSettings(
            strategy_id=cio_id,
            strategy_name="TEST_ONLY Browser CIO Desk",
            style="balanced_growth",
            market="TW",
            initial_cash=500_000.0,
            base_currency="TWD",
            reporting_currency="TWD",
            fx_to_reporting=1.0,
            enabled=True,
            universe=["2330.TW"],
            mode=DecisionScope.SWING,
            max_position_notional=50_000.0,
            max_data_age_seconds=3600.0,
        ))
        now = datetime.now(timezone.utc)
        packet = CIODecisionPacket(
            case_id="TEST_ONLY_browser_cio_case",
            as_of=now,
            evidence=["fixture://TEST_ONLY_BROWSER_CIO"],
            thesis="TEST_ONLY browser authority acceptance",
            selected_instrument="2330.TW",
            action="BUY",
            holding_horizon=DecisionScope.SWING,
            quantity=1.0,
            conditions={"strategy_id": cio_id},
            risk_assessment={"test_only": True},
            alternatives_considered=[],
            expiry=now + timedelta(hours=1),
            confidence=0.5,
            strategy_version="TEST_ONLY-browser-v1",
            provenance=CIOProvenance(
                authority="MAIN_CIO",
                actor_role="CHIEF_INVESTMENT_OFFICER",
                signer_id="fixture-test-signer",
                source="TEST_ONLY_BROWSER_SERVER_EXECUTOR",
            ),
            is_fixture=True,
        )
        sign_cio_packet(packet, signer_id="fixture-test-signer")
        cio_result = state.runner.submit_cio_packet(packet, strategy_id=cio_id)
        cio_orders = [
            order for order in state.paper_orders.all_orders()
            if order.origin == OrderOrigin.MAIN_CIO and order.strategy_id == cio_id
        ]
        if not cio_orders:
            raise AssertionError(f"TEST_ONLY signed CIO packet created no MAIN_CIO order: {cio_result}")

        return {
            "strategy_order_id": strategy_orders[-1].order_id,
            "cio_order_id": cio_orders[-1].order_id,
            "cio_case_id": packet.case_id,
        }
    finally:
        state.runner.shutdown()


def _advance_manual(workspace_root: Path, runtime_dir: Path) -> dict:
    app = _open_test_app(workspace_root, runtime_dir)
    state = app.state.app_state
    try:
        pending = [
            state.paper_orders.find_order(order.order_id)
            for order in state.paper_orders.all_orders()
            if order.origin == OrderOrigin.MANUAL and order.status == OrderStatus.PENDING
        ]
        pending = [order for order in pending if order is not None]
        if not pending:
            raise AssertionError("TEST_ONLY no pending MANUAL order to advance")
        order = pending[-1]
        bar = state.market_adapter.get_latest_bar(order.symbol)
        if bar is None:
            raise AssertionError("TEST_ONLY manual advance missing fixture bar")
        result = ExecutionEngine(state.paper_orders.cost_config).process_bar(bar, [order])
        if not result.fills:
            raise AssertionError(f"TEST_ONLY manual order did not fill: {order.model_dump(mode='json')}")
        for fill in result.fills:
            state.portfolio_manager.apply_fill(fill)
            state.event_store.append(EventEnvelope(
                event_type=EventType.ORDER_FILLED,
                aggregate_id=order.order_id,
                payload=fill.model_dump(mode="json"),
            ))
        state.runner._persist_portfolios()
        return {
            "order_id": order.order_id,
            "fill_ids": [fill.fill_id for fill in result.fills],
            "status": order.status.value,
        }
    finally:
        state.runner.shutdown()



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "serve", "seed-authorities", "advance-manual"))
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--read-only", action="store_true")
    args = parser.parse_args()

    if args.command == "prepare":
        _prepare(args.workspace_root, args.runtime_dir)
        return 0
    if args.command == "seed-authorities":
        print(json.dumps(_seed_authorities(args.workspace_root, args.runtime_dir), sort_keys=True))
        return 0
    if args.command == "advance-manual":
        print(json.dumps(_advance_manual(args.workspace_root, args.runtime_dir), sort_keys=True))
        return 0
    if args.ready_file is None or args.stop_file is None:
        parser.error("serve requires --ready-file and --stop-file")
    asyncio.run(_serve(
        args.workspace_root,
        args.runtime_dir,
        args.ready_file,
        args.stop_file,
        args.read_only,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
