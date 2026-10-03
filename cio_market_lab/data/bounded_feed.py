"""Process-isolated deadline for paper-runner market data calls.

A network library's socket timeout is not a wall-clock bound. The worker is
restricted to an adapter call; no runner, portfolio or order service crosses
this process boundary. A timed-out worker (and its process group) is killed
and reaped before the caller can proceed.
"""
from __future__ import annotations

import multiprocessing
import os
import signal
import time
from typing import Any


class FeedUnavailable(BaseException):
    """Fail closed even through legacy broad `except Exception` feed fallbacks."""


def _call_worker(conn: Any, adapter: Any, method: str, args: tuple, kwargs: dict) -> None:
    os.setsid()
    # Optional diagnostic is read-only and never includes credentials.
    import faulthandler
    if os.environ.get('CIO_FEED_DEBUG') == '1':
        faulthandler.dump_traceback_later(3, repeat=False)
    try:
        try:
            result = getattr(adapter, method)(*args, **kwargs)
            # Never transfer an unbounded history through the IPC boundary.
            if method == "get_bars" and isinstance(result, list):
                result = result[-32:]
            conn.send((True, result))
        except Exception as exc:
            conn.send((False, f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


def call_with_deadline(adapter: Any, method: str, args: tuple, kwargs: dict,
                       deadline: float) -> Any:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FeedUnavailable("FEED_UNAVAILABLE: stage deadline exhausted")
    # forkserver does not fork the multithreaded ASGI owner. Explicit local
    # fixture adapters may carry monkeypatched closures and use fork in tests.
    context = multiprocessing.get_context("fork" if getattr(adapter, "is_fixture", False) else "spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_call_worker, args=(child, adapter, method, args, kwargs))
    process.daemon = True
    try:
        process.start()
        child.close()
        remaining = max(0.0, deadline - time.monotonic())
        if not parent.poll(remaining):
            raise FeedUnavailable("FEED_UNAVAILABLE: market adapter deadline exceeded")
        try:
            ok, value = parent.recv()
        except (EOFError, OSError) as exc:
            raise FeedUnavailable("FEED_UNAVAILABLE: market adapter exited without data") from exc
        if not ok:
            raise FeedUnavailable(f"FEED_UNAVAILABLE: market adapter error: {value}")
        return value
    except (OSError, TypeError, ValueError) as exc:
        raise FeedUnavailable(f"FEED_UNAVAILABLE: worker startup failed: {type(exc).__name__}") from exc
    finally:
        parent.close()
        child.close()
        if process.pid is not None:
            # Worker calls setsid immediately; kill the entire session to stop
            # subprocesses spawned by a network library, not merely its leader.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.join(timeout=0.3)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.3)
