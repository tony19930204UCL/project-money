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
from cio_market_lab.domain.models import Bar, Quote


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
            timestamp=bar.timestamp,
            observed_at=bar.observed_at,
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
            is_synthetic=True,
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "serve"))
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--read-only", action="store_true")
    args = parser.parse_args()

    if args.command == "prepare":
        _prepare(args.workspace_root, args.runtime_dir)
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
