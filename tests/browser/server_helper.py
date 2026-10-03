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
from cio_market_lab.domain.models import Bar, DecisionScope, OrderOrigin, OrderSide, OrderType, Quote
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
) -> None:
    os.environ["CIO_MARKET_LAB_OFFLINE"] = "1"
    os.environ["HERMES_OFFLINE"] = "1"
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
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
