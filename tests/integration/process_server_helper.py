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
from cio_market_lab.domain.models import CIODecisionPacket, CIOProvenance, DecisionScope, Fill, OrderOrigin, OrderSide, OrderType
from cio_market_lab.engine.paper_orders import PaperDataContext, PaperExperimentSettings, PaperOrderRequest

FIXTURE_RECEIPT = "TEST_ONLY_PROCESS_RESTART_STREAM"
STRATEGY_ID = "TEST_ONLY_restart_stream"
LEARNING_CASE_ID = "TEST_ONLY_restart_stream_case"
FILL_ID = "TEST_ONLY_restart_stream_fill"


def _offline() -> None:
    os.environ["CIO_MARKET_LAB_OFFLINE"] = "1"
    os.environ.pop("CIO_MATERIAL_GATE_ENABLED", None)


def _seed(workspace_root: Path, runtime_dir: Path) -> dict:
    _offline()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(workspace_root=workspace_root, runtime_dir=runtime_dir, is_read_only=False)
    state = app.state.app_state
    try:
        state.runner.configure(PaperExperimentSettings(
            strategy_id=STRATEGY_ID,
            enabled=True,
            universe=["2330.TW"],
            initial_cash=500_000.0,
        ))
        filled_order = state.paper_orders.submit(PaperOrderRequest(
            symbol="2330.TW",
            market="TW",
            bucket=DecisionScope.SWING,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=2.0,
            origin=OrderOrigin.MANUAL,
            reason="TEST_ONLY committed fill for process restart receipt",
            audit_metadata={"is_fixture": True, "fixture_receipt": FIXTURE_RECEIPT},
            data=PaperDataContext(
                source=f"fixture://{FIXTURE_RECEIPT}",
                last_price=100.0,
                is_stale=False,
                is_fallback=False,
            ),
        ))
        pending_order = state.paper_orders.submit(PaperOrderRequest(
            symbol="0050.TW",
            market="TW",
            bucket=DecisionScope.SWING,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=1.0,
            origin=OrderOrigin.MANUAL,
            reason="TEST_ONLY pending order for process restart receipt",
            audit_metadata={"is_fixture": True, "fixture_receipt": FIXTURE_RECEIPT},
            data=PaperDataContext(
                source=f"fixture://{FIXTURE_RECEIPT}",
                last_price=50.0,
                is_stale=False,
                is_fallback=False,
            ),
        ))
        fill = Fill(
            fill_id=FILL_ID,
            order_id=filled_order.order_id,
            symbol="2330.TW",
            bucket=DecisionScope.SWING,
            side=OrderSide.BUY,
            quantity=2.0,
            fill_price=100.0,
            timestamp=datetime.now(timezone.utc),
            cash_flow=-200.0,
        )
        state.portfolio_manager.apply_fill(fill)
        state.event_store.append(EventEnvelope(
            event_type=EventType.ORDER_FILLED,
            aggregate_id=filled_order.order_id,
            payload=fill.model_dump(mode="json"),
        ))
        packet = CIODecisionPacket(
            case_id=LEARNING_CASE_ID,
            as_of=datetime.now(timezone.utc),
            evidence=[f"fixture://{FIXTURE_RECEIPT}"],
            thesis="TEST_ONLY process restart learning receipt",
            selected_instrument="2330.TW",
            action="BUY",
            holding_horizon=DecisionScope.SWING,
            quantity=2.0,
            conditions={"fixture_receipt": FIXTURE_RECEIPT},
            risk_assessment={},
            alternatives_considered=[],
            expiry=datetime.now(timezone.utc) + timedelta(hours=1),
            confidence=0.5,
            strategy_version="TEST_ONLY-v1",
            provenance=CIOProvenance(
                authority="MAIN_CIO",
                actor_role="CHIEF_INVESTMENT_OFFICER",
                signer_id="TEST_ONLY",
                source="fixture",
            ),
            is_fixture=True,
        )
        state.runner.learning_store.record_decision(packet, {}, {})
        state.runner._persist_portfolios()
        swing = state.portfolio_manager.get_portfolio(DecisionScope.SWING)
        filled_events = state.event_store.get_events(event_type=EventType.ORDER_FILLED, limit=1000, strict=True)
        return {
            "fixture_receipt": FIXTURE_RECEIPT,
            "filled_order_id": filled_order.order_id,
            "pending_order_id": pending_order.order_id,
            "fill_id": FILL_ID,
            "learning_case_id": LEARNING_CASE_ID,
            "cash_after_fill": swing.cash,
            "event_count": state.event_store.count(),
            "order_filled_count": len(filled_events),
        }
    finally:
        state.runner.shutdown()


def _inspect(workspace_root: Path, runtime_dir: Path) -> dict:
    _offline()
    app = create_app(workspace_root=workspace_root, runtime_dir=runtime_dir, is_read_only=True)
    state = app.state.app_state
    try:
        record = state.runner.learning_store.get_record(LEARNING_CASE_ID)
        experiment = state.paper_orders.experiment_for(STRATEGY_ID)
        return {
            "learning_case_id": record.case_id if record else None,
            "learning_status": record.status if record else None,
            "experiment": experiment.model_dump(mode="json"),
            "event_count": state.event_store.count(),
        }
    finally:
        state.runner.shutdown()


async def _watch_stop_file(server: uvicorn.Server, stop_file: Path) -> None:
    while not server.should_exit:
        if stop_file.exists():
            server.should_exit = True
            return
        await asyncio.sleep(0.05)


async def _serve(workspace_root: Path, runtime_dir: Path, ready_file: Path, stop_file: Path, read_only: bool) -> None:
    _offline()
    app = create_app(workspace_root=workspace_root, runtime_dir=runtime_dir, is_read_only=read_only)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = int(sock.getsockname()[1])
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    ready_file.write_text(json.dumps({"pid": os.getpid(), "port": port}), encoding="utf-8")
    config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on")
    server = uvicorn.Server(config)
    watcher = asyncio.create_task(_watch_stop_file(server, stop_file))
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
    parser.add_argument("command", choices=("seed", "inspect", "serve"))
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--read-only", action="store_true")
    args = parser.parse_args()

    if args.command == "seed":
        print(json.dumps(_seed(args.workspace_root, args.runtime_dir), sort_keys=True))
        return 0
    if args.command == "inspect":
        print(json.dumps(_inspect(args.workspace_root, args.runtime_dir), sort_keys=True))
        return 0
    if args.ready_file is None or args.stop_file is None:
        parser.error("serve requires --ready-file and --stop-file")
    asyncio.run(_serve(args.workspace_root, args.runtime_dir, args.ready_file, args.stop_file, args.read_only))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
