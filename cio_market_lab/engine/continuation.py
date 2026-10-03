"""Local continuation entrypoint using existing runtime.

Provides:
- Idempotent runtime file lock (preventing concurrent processes/races).
- Bounded retries for transient runner errors.
- Atomic state checkpoints after each cycle.
- Explicit terminal states (COMPLETED, BLOCKED, INTERRUPTED, HALTED_MAX_RETRIES, HALTED_KILL_SWITCH, HALTED_LOCK_FAILED, ERROR).
- Interruption recovery with single persistent canonical cash and deduplicated fills.
- Multi-cycle execution ensuring learning outcomes from prior cycles are consumed on subsequent cycles.
- Fail closed on corrupt checkpoint or strategy mismatch.
- Prevents shared canonical cash from being counted multiple times across buckets in equity metrics.
- Bounded executable CLI for driving ContinuationRunner via subprocess or terminal.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
import threading
from typing import Any, Dict, List, Optional
import uuid

from pydantic import BaseModel, Field

from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner, DYNAMIC_DESK_ID
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.engine.paper_orders import PaperExperimentSettings, PaperOrderService


class ContinuationLockAcquisitionError(RuntimeError):
    """Raised when the idempotent continuation lock cannot be acquired."""
    pass


class CorruptCheckpointError(RuntimeError):
    """Raised when continuation checkpoint file is corrupt or invalid."""
    pass


class StrategyMismatchError(RuntimeError):
    """Raised when continuation checkpoint belongs to a different strategy."""
    pass


class ContinuationCheckpoint(BaseModel):
    """Atomic checkpoint for continuation state."""
    checkpoint_id: str = Field(default_factory=lambda: f"chk-{uuid.uuid4()}")
    strategy_id: str
    cycle_index: int
    target_cycles: int
    last_run_id: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    terminal_state: str = "NON_TERMINAL"
    canonical_cash: float
    portfolio_equity: float
    open_positions: int
    retries_attempted: int = 0
    runs: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


import time


class ContinuationLock:
    """Idempotent process-level and thread-safe re-entrant file lock for the runtime environment."""

    def __init__(self, lock_path: Path):
        self.lock_path = Path(lock_path).resolve()
        self._fd: Optional[int] = None
        self._is_locked: bool = False
        self._thread_lock = threading.RLock()
        self._owner_thread: Optional[int] = None
        self._depth: int = 0

    def acquire(self, timeout: Optional[float] = None) -> bool:
        blocking = timeout is not None and timeout > 0
        wait_sec = timeout if blocking else -1
        acquired_thread = self._thread_lock.acquire(blocking=blocking, timeout=wait_sec)
        if not acquired_thread:
            raise ContinuationLockAcquisitionError(
                f"Continuation lock already held on {self.lock_path} by another thread"
            )

        current_thread = threading.get_ident()
        if self._is_locked and self._owner_thread == current_thread:
            self._depth += 1
            return True

        if self._is_locked and self._owner_thread != current_thread:
            self._thread_lock.release()
            raise ContinuationLockAcquisitionError(
                f"Continuation lock already held on {self.lock_path} by thread {self._owner_thread}"
            )

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = None
        try:
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            start_time = time.time()
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, OSError) as exc:
                    if timeout is not None and timeout > 0:
                        if (time.time() - start_time) < timeout:
                            time.sleep(0.02)
                            continue
                    raise exc

            payload = json.dumps({
                "pid": os.getpid(),
                "thread_id": current_thread,
                "locked_at": datetime.now(timezone.utc).isoformat(),
            }) + "\n"
            os.ftruncate(fd, 0)
            os.write(fd, payload.encode("utf-8"))
            self._fd = fd
            self._is_locked = True
            self._owner_thread = current_thread
            self._depth = 1
            return True
        except (BlockingIOError, OSError) as exc:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
            self._thread_lock.release()
            raise ContinuationLockAcquisitionError(
                f"Continuation lock already held on {self.lock_path}: {exc}"
            )

    def release(self) -> None:
        if not self._is_locked:
            return
        current_thread = threading.get_ident()
        if self._owner_thread != current_thread:
            return

        self._depth -= 1
        if self._depth > 0:
            self._thread_lock.release()
            return

        try:
            if self._fd is not None:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                    os.close(self._fd)
                except Exception:
                    pass
                self._fd = None
        finally:
            self._is_locked = False
            self._owner_thread = None
            self._depth = 0
            self._thread_lock.release()

    def __enter__(self) -> ContinuationLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()

    @property
    def is_locked(self) -> bool:
        return self._is_locked


class ContinuationRunner:
    """Continuation entrypoint driving bounded cycles on the existing runtime."""

    def __init__(
        self,
        runner: AutonomousPaperRunner,
        strategy_id: str = DYNAMIC_DESK_ID,
        max_retries: int = 3,
        runtime_dir: Optional[Path] = None,
        checkpoint_filename: str = "continuation_checkpoint.json",
    ) -> None:
        self.runner = runner
        self.strategy_id = strategy_id
        self.max_retries = max(1, max_retries)
        self.runtime_dir = runtime_dir or runner.runtime_dir
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_file = self.runtime_dir / checkpoint_filename
        self.lock_file = self.runtime_dir / "continuation.lock"
        if hasattr(runner, "writer_lock") and runner.writer_lock is not None:
            self.lock = runner.writer_lock
        else:
            self.lock = ContinuationLock(self.lock_file)
            if hasattr(runner, "writer_lock"):
                runner.writer_lock = self.lock

    def load_checkpoint(self, expected_strategy_id: Optional[str] = None) -> Optional[ContinuationCheckpoint]:
        """Load latest checkpoint if present. Fails closed on corrupt data or strategy mismatch."""
        if not self.checkpoint_file.exists():
            return None
        try:
            content = self.checkpoint_file.read_text(encoding="utf-8")
            raw = json.loads(content)
            chk = ContinuationCheckpoint.model_validate(raw)
        except Exception as exc:
            raise CorruptCheckpointError(
                f"Corrupt checkpoint at {self.checkpoint_file}: {exc}"
            ) from exc

        target_strategy = expected_strategy_id if expected_strategy_id is not None else self.strategy_id
        if target_strategy and chk.strategy_id != target_strategy:
            raise StrategyMismatchError(
                f"Checkpoint strategy mismatch: expected {target_strategy}, found {chk.strategy_id}"
            )
        return chk

    def save_checkpoint(self, checkpoint: ContinuationCheckpoint) -> None:
        """Persist checkpoint atomically and record snapshot event in canonical event store."""
        tmp = self.checkpoint_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(checkpoint.model_dump(mode="json"), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.checkpoint_file)
        if hasattr(self.runner, "paper_orders") and hasattr(self.runner.paper_orders, "event_store"):
            es = self.runner.paper_orders.event_store
            if es is not None:
                from cio_market_lab.domain.events import EventEnvelope, EventType
                es.append(EventEnvelope(
                    event_type=EventType.PORTFOLIO_SNAPSHOT,
                    aggregate_id=f"strategy:{checkpoint.strategy_id}",
                    payload=checkpoint.model_dump(mode="json"),
                ))

    def _get_current_metrics(self) -> tuple[float, float, int]:
        """Extract canonical cash, equity, and open positions from existing runtime.

        Enforces invariant: shared canonical cash is never counted multiple times across buckets.
        """
        pm: PortfolioManager = self.runner.portfolio_manager
        # Canonical cash account for desk or aggregate
        if self.strategy_id in pm._strategy_cash_accounts:
            cash = pm._strategy_cash_accounts[self.strategy_id].cash
        else:
            cash = pm._canonical_cash.cash

        allowed_buckets = self.runner.paper_orders.experiment_for(self.strategy_id).allowed_buckets

        # Use pm.get_strategy_equity to ensure shared canonical cash is counted exactly once
        equity = pm.get_strategy_equity(self.strategy_id, allowed_buckets)
        if equity is None:
            equity = cash

        open_pos = 0
        for bucket in allowed_buckets:
            p = pm.get_strategy_portfolio(self.strategy_id, bucket)
            open_pos += sum(1 for x in p.positions.values() if x.quantity > 0)
        return cash, equity, open_pos

    def run(
        self,
        max_cycles: int = 1,
        symbols: Optional[List[str]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        """Execute bounded multi-cycle continuation with recovery and checkpoints."""
        # 1. Acquire idempotent lock
        try:
            self.lock.acquire()
        except ContinuationLockAcquisitionError as err:
            return {
                "status": "HALTED_LOCK_FAILED",
                "terminal_state": "HALTED_LOCK_FAILED",
                "error": str(err),
                "completed_cycles": 0,
                "target_cycles": max_cycles,
                "runs": [],
                "checkpoint": None,
            }

        try:
            # 2. Check kill switch
            if self.runner.paper_orders.kill_switch:
                cash, eq, pos = self._get_current_metrics()
                chk = ContinuationCheckpoint(
                    strategy_id=self.strategy_id,
                    cycle_index=0,
                    target_cycles=max_cycles,
                    terminal_state="HALTED_KILL_SWITCH",
                    canonical_cash=cash,
                    portfolio_equity=eq,
                    open_positions=pos,
                )
                self.save_checkpoint(chk)
                return {
                    "status": "HALTED_KILL_SWITCH",
                    "terminal_state": "HALTED_KILL_SWITCH",
                    "completed_cycles": 0,
                    "target_cycles": max_cycles,
                    "runs": [],
                    "checkpoint": chk.model_dump(mode="json"),
                }

            # 3. Check for existing checkpoint (Interruption recovery)
            # Fail closed on corrupt checkpoint or strategy mismatch rather than silently reset
            try:
                prior_checkpoint = self.load_checkpoint()
            except (CorruptCheckpointError, StrategyMismatchError) as err:
                return {
                    "status": "ERROR",
                    "terminal_state": "ERROR",
                    "error": str(err),
                    "completed_cycles": 0,
                    "target_cycles": max_cycles,
                    "runs": [],
                    "checkpoint": None,
                }

            start_cycle = 0
            accumulated_runs: List[str] = []

            if prior_checkpoint is not None:
                start_cycle = prior_checkpoint.cycle_index
                accumulated_runs = list(prior_checkpoint.runs)
                # Re-load runtime portfolios ensuring single ledger & cash are restored
                self.runner._restore_portfolios()

            target_cycles = start_cycle + max_cycles
            cycle_runs: List[str] = list(accumulated_runs)
            current_retries = 0

            # 4. Multi-cycle bounded loop
            for cycle_idx in range(start_cycle, target_cycles):
                # Check for interruption
                if stop_event is not None and stop_event.is_set():
                    cash, eq, pos = self._get_current_metrics()
                    chk = ContinuationCheckpoint(
                        strategy_id=self.strategy_id,
                        cycle_index=cycle_idx,
                        target_cycles=target_cycles,
                        last_run_id=cycle_runs[-1] if cycle_runs else None,
                        terminal_state="INTERRUPTED",
                        canonical_cash=cash,
                        portfolio_equity=eq,
                        open_positions=pos,
                        runs=cycle_runs,
                    )
                    self.save_checkpoint(chk)
                    return {
                        "status": "INTERRUPTED",
                        "terminal_state": "INTERRUPTED",
                        "completed_cycles": cycle_idx,
                        "target_cycles": target_cycles,
                        "runs": cycle_runs,
                        "checkpoint": chk.model_dump(mode="json"),
                    }

                cycle_success = False
                cycle_result: Optional[Dict[str, Any]] = None
                cycle_error: Optional[str] = None

                # Bounded retries loop for the cycle
                for retry in range(self.max_retries):
                    current_retries = retry
                    try:
                        cycle_result = self.runner.run_one_cycle(
                            self.strategy_id,
                            symbols=symbols,
                        )
                        run_info = cycle_result.get("run", {})
                        run_status = run_info.get("status")

                        if run_status == "BLOCKED":
                            # Explicitly blocked (e.g. no CIO provider or risk block)
                            # This is an explicit terminal state, not a transient retryable crash
                            cycle_runs.append(run_info.get("run_id", ""))
                            cash, eq, pos = self._get_current_metrics()
                            chk = ContinuationCheckpoint(
                                strategy_id=self.strategy_id,
                                cycle_index=cycle_idx + 1,
                                target_cycles=target_cycles,
                                last_run_id=run_info.get("run_id"),
                                terminal_state="BLOCKED",
                                canonical_cash=cash,
                                portfolio_equity=eq,
                                open_positions=pos,
                                retries_attempted=retry,
                                runs=cycle_runs,
                                metadata={"reason": run_info.get("reason")},
                            )
                            self.save_checkpoint(chk)
                            return {
                                "status": "BLOCKED",
                                "terminal_state": "BLOCKED",
                                "reason": run_info.get("reason"),
                                "completed_cycles": cycle_idx + 1,
                                "target_cycles": target_cycles,
                                "runs": cycle_runs,
                                "checkpoint": chk.model_dump(mode="json"),
                            }

                        if run_status == "COMPLETED":
                            cycle_runs.append(run_info.get("run_id", ""))
                            cycle_success = True
                            break
                        else:
                            # Unexpected status, retry
                            cycle_error = f"Unexpected run status: {run_status}"

                    except Exception as exc:
                        cycle_error = str(exc)

                if not cycle_success:
                    # Retries exhausted
                    cash, eq, pos = self._get_current_metrics()
                    chk = ContinuationCheckpoint(
                        strategy_id=self.strategy_id,
                        cycle_index=cycle_idx,
                        target_cycles=target_cycles,
                        last_run_id=cycle_runs[-1] if cycle_runs else None,
                        terminal_state="HALTED_MAX_RETRIES",
                        canonical_cash=cash,
                        portfolio_equity=eq,
                        open_positions=pos,
                        retries_attempted=self.max_retries,
                        runs=cycle_runs,
                        metadata={"error": cycle_error},
                    )
                    self.save_checkpoint(chk)
                    return {
                        "status": "HALTED_MAX_RETRIES",
                        "terminal_state": "HALTED_MAX_RETRIES",
                        "error": cycle_error,
                        "completed_cycles": cycle_idx,
                        "target_cycles": target_cycles,
                        "runs": cycle_runs,
                        "checkpoint": chk.model_dump(mode="json"),
                    }

                # Save cycle checkpoint
                cash, eq, pos = self._get_current_metrics()
                chk = ContinuationCheckpoint(
                    strategy_id=self.strategy_id,
                    cycle_index=cycle_idx + 1,
                    target_cycles=target_cycles,
                    last_run_id=cycle_runs[-1] if cycle_runs else None,
                    terminal_state="IN_PROGRESS" if (cycle_idx + 1) < target_cycles else "COMPLETED",
                    canonical_cash=cash,
                    portfolio_equity=eq,
                    open_positions=pos,
                    retries_attempted=current_retries,
                    runs=cycle_runs,
                )
                self.save_checkpoint(chk)

            # 5. All cycles completed
            cash, eq, pos = self._get_current_metrics()
            final_chk = ContinuationCheckpoint(
                strategy_id=self.strategy_id,
                cycle_index=target_cycles,
                target_cycles=target_cycles,
                last_run_id=cycle_runs[-1] if cycle_runs else None,
                terminal_state="COMPLETED",
                canonical_cash=cash,
                portfolio_equity=eq,
                open_positions=pos,
                runs=cycle_runs,
            )
            self.save_checkpoint(final_chk)

            return {
                "status": "COMPLETED",
                "terminal_state": "COMPLETED",
                "completed_cycles": target_cycles,
                "target_cycles": target_cycles,
                "runs": cycle_runs,
                "checkpoint": final_chk.model_dump(mode="json"),
            }

        finally:
            self.lock.release()


def main(argv: Optional[List[str]] = None) -> int:
    """Bounded executable CLI for ContinuationRunner using existing runtime bootstrap."""
    parser = argparse.ArgumentParser(description="Bounded Execution CLI for ContinuationRunner")
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=1,
        help="Maximum number of cycles to execute (bounded)",
    )
    parser.add_argument(
        "--strategy-id",
        default=DYNAMIC_DESK_ID,
        help=f"Target strategy ID to continue (default: {DYNAMIC_DESK_ID})",
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated symbols to evaluate",
    )
    parser.add_argument(
        "--runtime-dir",
        default=None,
        help="Runtime directory path (defaults to CIO_MARKET_LAB_RUNTIME_DIR or data/runtime)",
    )
    parser.add_argument("--material-trust-manifest", help="Independent MAIN_CIO source-capture approval manifest; never auto-derived from packets")
    parser.add_argument(
        "--material-packet-root",
        default=None,
        help="Opt in to the Stage D verified-packet material gate; no default/live packet path",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum retries for transient failures before halting",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write output JSON",
    )

    args = parser.parse_args(argv)

    # Existing runtime bootstrap: resolve single canonical runtime path before any construction/loading
    workspace_root = Path(os.environ.get("CIO_WORKSPACE_ROOT", Path.cwd())).resolve()
    if args.runtime_dir:
        runtime_dir_path = Path(args.runtime_dir).resolve()
    elif os.environ.get("CIO_MARKET_LAB_RUNTIME_DIR"):
        runtime_dir_path = Path(os.environ["CIO_MARKET_LAB_RUNTIME_DIR"]).resolve()
    else:
        runtime_dir_path = (workspace_root / "data" / "runtime").resolve()
    runtime_dir_path.mkdir(parents=True, exist_ok=True)
    os.environ["CIO_MARKET_LAB_RUNTIME_DIR"] = str(runtime_dir_path)

    from cio_market_lab.engine.team_ops import TEAM_INITIAL_CAPITAL_TWD
    from cio_market_lab.data.market_data import CompositeMarketDataAdapter
    from cio_market_lab.events.store import EventStore
    from cio_market_lab.integrations.hermes_chat import (
        DEFAULT_PINNED_MODEL_ID,
        DEFAULT_PINNED_PROVIDER_ID,
        HermesCIODecisionExecutor,
    )

    # Initialize existing runtime components (no parallel ledger!)
    pm = PortfolioManager(
        initial_cash_swing=TEAM_INITIAL_CAPITAL_TWD,
        initial_cash_intraday=TEAM_INITIAL_CAPITAL_TWD,
    )
    event_store = EventStore(runtime_dir_path / "events.db")
    paper_orders = PaperOrderService(pm, event_store)

    # No fixture default! Use CompositeMarketDataAdapter with real/offline configuration from env
    offline_mode = os.environ.get("CIO_MARKET_LAB_OFFLINE", "0") == "1"
    market_adapter = CompositeMarketDataAdapter(offline_mode=offline_mode)

    cio_provider = os.getenv("CIO_PROVIDER_ID", DEFAULT_PINNED_PROVIDER_ID)
    cio_model = os.getenv("CIO_MODEL_ID", DEFAULT_PINNED_MODEL_ID)
    cio_executor = HermesCIODecisionExecutor(
        session_id=os.getenv("CIO_HERMES_SESSION_ID", "cio-market-lab"),
        workspace_root=os.getenv("CIO_HERMES_WORKSPACE"),
        timeout_seconds=int(os.getenv("CIO_HERMES_TIMEOUT_SECONDS", "240")),
        provider_id=cio_provider,
        model_id=cio_model,
    )

    # Explicit packet-root opt-in only; defaults preserve the existing runner path.
    material_provider = None
    if args.material_packet_root:
        from cio_market_lab.engine.stage_d_observation import make_packet_observation_provider
        if not args.material_trust_manifest:
            raise ValueError("material packet mode requires independent CIO source approval manifest")
        material_provider = make_packet_observation_provider(Path(args.material_packet_root), trusted_manifest=Path(args.material_trust_manifest))

    runner = AutonomousPaperRunner(
        root=workspace_root,
        portfolio_manager=pm,
        paper_orders=paper_orders,
        market_adapter=market_adapter,
        cio_executor=cio_executor,
        require_cio_provider=True,
        runtime_dir=runtime_dir_path,
        material_observation_provider=material_provider,
        material_gate_enabled=material_provider is not None,
    )

    # Configure target strategy if not already present
    if args.strategy_id not in runner.paper_orders.experiments:
        runner.configure(
            PaperExperimentSettings(
                strategy_id=args.strategy_id,
                enabled=True,
                # The bootstrap capital is denominated in TWD. A USD desk needs
                # separately evidenced native funding, not relabelled TWD cash.
                universe=[s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else ["2330.TW"],
                market="TW",
                base_currency="TWD",
                initial_cash=TEAM_INITIAL_CAPITAL_TWD,
            )
        )

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None

    cont_runner = ContinuationRunner(
        runner=runner,
        strategy_id=args.strategy_id,
        max_retries=args.max_retries,
        runtime_dir=runtime_dir_path,
    )

    result = cont_runner.run(max_cycles=args.max_cycles, symbols=symbols)

    output_json = json.dumps(result, indent=2)
    print(output_json)

    if args.output:
        out_p = Path(args.output).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(output_json, encoding="utf-8")

    if result.get("status") in ("ERROR", "HALTED_MAX_RETRIES", "HALTED_LOCK_FAILED"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
