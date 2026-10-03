"""Two-mode continuous operating candidate for Project Money.

Operating Modes:
1. OPEN_SESSION: Executes the standard market-open decision loop.
2. OFF_SESSION: Outside market hours, runs bounded post-close review, research refresh,
   candidate preparation, and next-session planning WITHOUT placing or simulating off-session orders.

Reliability guarantees:
- Degrades honestly when host or internet is unavailable (never hallucinates data).
- Resumes idempotently when service returns using durable disk checkpoints.
- Never touches live broker, executable quotes, simulated fills, or live portfolio logic.
"""
from __future__ import annotations

from datetime import datetime, date, timedelta, timezone
from enum import Enum
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Dict, List, Optional, Union
from urllib.error import HTTPError, URLError

from pydantic import BaseModel, Field

from cio_market_lab.engine.market_schedule import intraday_market_open
from cio_market_lab.research.free_adapters import (
    FreeSourceCoordinator,
    SecCompanyFactsAdapter,
    FinvizAdapter,
    StockAnalysisAdapter,
    CompaniesMarketCapAdapter,
    is_fixture_symbol,
)
from cio_market_lab.research.browser import PublicResearchInboxReader


class OperatingMode(str, Enum):
    OPEN_SESSION = "OPEN_SESSION"
    OFF_SESSION = "OFF_SESSION"


class NextSessionPlan(BaseModel):
    """Durable candidate plan formulated during off-session operating cycle."""
    plan_id: str
    session_date: str
    created_at: str
    updated_at: str
    operating_status: str  # "COMPLETED", "DEGRADED", "IN_PROGRESS"
    mode: str = "OFF_SESSION"
    universe: List[str] = Field(default_factory=list)
    candidates: List[Dict[str, Any]] = Field(default_factory=list)
    data_gaps: List[Dict[str, Any]] = Field(default_factory=list)
    discrepancies: List[Dict[str, Any]] = Field(default_factory=list)
    post_close_review: Dict[str, Any] = Field(default_factory=dict)
    next_session_guidelines: Dict[str, Any] = Field(default_factory=dict)
    orders_placed: int = 0
    simulated_fills: int = 0
    resumed_from_checkpoint: bool = False
    degradation_reason: Optional[str] = None


class TwoModeContinuousOperatingCandidate:
    """Continuous operating engine managing open-session trading and off-session research planning."""

    def __init__(
        self,
        root: Optional[Path] = None,
        runner: Optional[Any] = None,
        coordinator: Optional[FreeSourceCoordinator] = None,
        research_reader: Optional[PublicResearchInboxReader] = None,
        plans_dir: Optional[Path] = None,
    ):
        self.root = root or Path.cwd()
        self.runner = runner
        self.coordinator = coordinator or FreeSourceCoordinator()
        self.research_reader = research_reader
        self.plans_dir = plans_dir or (self.root / "artifacts" / "free_source_integration_20260929" / "plans")
        try:
            self.plans_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def is_symbol_open(self, symbol: str, now: datetime) -> bool:
        """Check whether the primary exchange for a symbol is currently open."""
        return intraday_market_open(symbol, now)

    def get_operating_mode(
        self,
        symbols: Optional[List[str]] = None,
        now: Optional[datetime] = None,
    ) -> OperatingMode:
        """Determine operating mode based on current exchange hours across active universe."""
        now_dt = now or datetime.now(timezone.utc)
        test_symbols = symbols or ["2330.TW", "AAPL"]
        if any(self.is_symbol_open(s, now_dt) for s in test_symbols):
            return OperatingMode.OPEN_SESSION
        return OperatingMode.OFF_SESSION

    def run_open_session_step(
        self,
        strategy_id: str,
        symbols: Optional[List[str]] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Execute the standard market-open decision loop via the existing runner."""
        if self.runner is not None:
            return self.runner.run_one_cycle(strategy_id, symbols=symbols)
        return {
            "mode": OperatingMode.OPEN_SESSION.value,
            "status": "OPEN_SESSION_NO_RUNNER_CONFIGURED",
            "strategy_id": strategy_id,
            "symbols": symbols or [],
            "orders_count": 0,
            "fills_count": 0,
        }

    def _get_plan_path(self, session_date: str) -> Path:
        return self.plans_dir / f"next_session_plan_{session_date}.json"

    def load_existing_plan(self, session_date: str) -> Optional[NextSessionPlan]:
        path = self._get_plan_path(session_date)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return NextSessionPlan.model_validate(data)
        except Exception:
            return None

    def _save_plan_atomically(self, plan: NextSessionPlan) -> None:
        path = self._get_plan_path(plan.session_date)
        data = plan.model_dump(mode="json")
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self.plans_dir),
            prefix=f".tmp_{plan.plan_id}_",
            suffix=".tmp",
            delete=False,
        ) as tf:
            temp_path = Path(tf.name)
            json.dump(data, tf, indent=2, ensure_ascii=False)
            tf.flush()
            os.fsync(tf.fileno())
        temp_path.replace(path)

    def run_off_session_cycle(
        self,
        universe: List[str],
        now: Optional[datetime] = None,
        session_date: Optional[str] = None,
        force_refresh: bool = False,
        network_available: bool = True,
        portfolio_summary: Optional[Dict[str, Any]] = None,
    ) -> NextSessionPlan:
        """Bounded off-session cycle: post-close review, research refresh, candidate prep, next-session plan.
        
        Guarantees:
        - NEVER places or simulates off-session orders (orders_placed=0, simulated_fills=0).
        - Degrades honestly if host/internet is unavailable.
        - Resumes idempotently from durable disk checkpoint when service returns.
        """
        now_dt = now or datetime.now(timezone.utc)
        now_utc = now_dt if now_dt.tzinfo else now_dt.replace(tzinfo=timezone.utc)
        target_date = session_date or (now_utc.date() + timedelta(days=1)).isoformat()
        plan_id = f"plan-{target_date}"

        # 1. Idempotent check: load existing durable plan if present
        existing_plan = self.load_existing_plan(target_date)
        if existing_plan is not None and not force_refresh:
            if existing_plan.operating_status == "COMPLETED":
                # Fully completed plan already exists -> return idempotently without duplicate fetches
                return existing_plan
            # If plan was DEGRADED or incomplete, resume from checkpoint
            is_resuming = True
        else:
            is_resuming = False

        # 2. Bounded Post-Close Review (read-only, no orders)
        review_data: Dict[str, Any] = {
            "reviewed_at": now_utc.isoformat(),
            "positions_reviewed": 0,
            "cash_balance": 0.0,
            "risk_posture": "NORMAL_OFF_SESSION",
            "notes": "Post-close review completed; no off-session order simulation permitted.",
        }
        if portfolio_summary:
            review_data.update(portfolio_summary)
        elif self.runner and hasattr(self.runner, "portfolio_manager"):
            try:
                # Read-only snapshot of default strategy
                portfolio = self.runner.portfolio_manager.get_strategy_portfolio("dynamic-desk")
                review_data["positions_reviewed"] = len(portfolio.positions)
                review_data["cash_balance"] = portfolio.cash
                review_data["equity"] = portfolio.equity
            except Exception:
                pass

        # 3. Determine symbols to process (retrying only gaps if resuming)
        symbols_to_process = list(universe)
        prior_candidates = {}
        prior_gaps = []
        prior_discrepancies = []

        if is_resuming and existing_plan is not None:
            for c in existing_plan.candidates:
                prior_candidates[c["symbol"]] = c
            prior_discrepancies = list(existing_plan.discrepancies)
            # Only retry symbols that failed previously or are missing
            failed_symbols = {g["symbol"] for g in existing_plan.data_gaps if "symbol" in g}
            symbols_to_process = [s for s in universe if s in failed_symbols or s not in prior_candidates]

        # 4. Research Refresh & Honest Degradation
        all_candidates: List[Dict[str, Any]] = list(prior_candidates.values())
        all_gaps: List[Dict[str, Any]] = [g for g in prior_gaps if g.get("symbol") not in symbols_to_process]
        all_discrepancies: List[Dict[str, Any]] = list(prior_discrepancies)
        degraded = False
        degradation_reason = None

        if not network_available:
            # Honest degradation: record network failure explicitly, never synthesize data
            degraded = True
            degradation_reason = "HOST_OR_INTERNET_UNAVAILABLE: Network offline during off-session refresh"
            for sym in symbols_to_process:
                all_gaps.append({
                    "symbol": sym,
                    "source": "network",
                    "reason": "NETWORK_UNAVAILABLE: Connection offline; request not attempted",
                    "timestamp": now_utc.isoformat(),
                })
        else:
            for sym in symbols_to_process:
                if is_fixture_symbol(sym):
                    all_gaps.append({
                        "symbol": sym,
                        "source": "all",
                        "reason": "REJECTED_FIXTURE: Test fixture symbol rejected",
                        "timestamp": now_utc.isoformat(),
                    })
                    continue

                try:
                    refresh_res = self.coordinator.refresh_symbol(
                        sym, now_utc, reader=self.research_reader
                    )
                    gaps = refresh_res.get("gaps", [])
                    if gaps:
                        all_gaps.extend(gaps)

                    cross_checks = refresh_res.get("cross_checks", {})
                    disc = cross_checks.get("discrepancies", [])
                    for d in disc:
                        d["symbol"] = sym
                        all_discrepancies.append(d)

                    # Formulate candidate entry from verified facts and secondary discovery
                    sec_res = refresh_res.get("official_facts") or {}
                    fv_res = refresh_res.get("secondary_finviz") or {}
                    sa_res = refresh_res.get("secondary_stock_analysis") or {}
                    cmc_res = refresh_res.get("peer_market_cap") or {}

                    candidate = {
                        "symbol": sym,
                        "verified_primary_evidence": len(sec_res.get("evidence_ids", [])),
                        "official_facts_status": sec_res.get("status", "NOT_REQUESTED"),
                        "finviz_metrics": (fv_res.get("record") or {}).get("metrics", {}),
                        "stock_analysis_metrics": (sa_res.get("record") or {}).get("metrics", {}),
                        "peer_rank": ((cmc_res.get("record") or {}).get("metrics") or {}).get("rank"),
                        "discrepancies_count": len(disc),
                        "candidate_prepared_at": now_utc.isoformat(),
                        "readiness": "READY" if sec_res.get("status") == "SUCCESS" else "DATA_GAP",
                    }
                    # Replace or append candidate
                    all_candidates = [c for c in all_candidates if c["symbol"] != sym]
                    all_candidates.append(candidate)

                except (URLError, HTTPError, OSError, ConnectionError, TimeoutError) as net_err:
                    degraded = True
                    degradation_reason = f"NETWORK_OUTAGE:{type(net_err).__name__}: {net_err}"
                    all_gaps.append({
                        "symbol": sym,
                        "source": "network_fetch",
                        "reason": f"NETWORK_ERROR:{type(net_err).__name__}: {net_err}",
                        "timestamp": now_utc.isoformat(),
                    })
                except Exception as exc:
                    all_gaps.append({
                        "symbol": sym,
                        "source": "processing",
                        "reason": f"UNEXPECTED_ERROR:{type(exc).__name__}: {exc}",
                        "timestamp": now_utc.isoformat(),
                    })

        # Check overall operating status
        if degraded:
            status = "DEGRADED"
        elif all(c.get("readiness") == "READY" for c in all_candidates) and len(all_candidates) == len(universe):
            status = "COMPLETED"
        else:
            status = "COMPLETED" if not all_gaps else "DEGRADED"

        # 5. Next-Session Guidelines
        guidelines = {
            "session_date": target_date,
            "target_watchlist": [c["symbol"] for c in all_candidates if c.get("readiness") == "READY"],
            "orders_permitted_off_session": False,
            "execution_policy": "MARKET_OPEN_ONLY",
            "risk_limits": {
                "max_drawdown_allowed": 0.05,
                "off_session_orders_allowed": 0,
            },
        }

        # 6. Assemble Plan
        plan = NextSessionPlan(
            plan_id=plan_id,
            session_date=target_date,
            created_at=existing_plan.created_at if existing_plan else now_utc.isoformat(),
            updated_at=now_utc.isoformat(),
            operating_status=status,
            mode=OperatingMode.OFF_SESSION.value,
            universe=universe,
            candidates=all_candidates,
            data_gaps=all_gaps,
            discrepancies=all_discrepancies,
            post_close_review=review_data,
            next_session_guidelines=guidelines,
            orders_placed=0,
            simulated_fills=0,
            resumed_from_checkpoint=is_resuming,
            degradation_reason=degradation_reason,
        )

        # 7. Persist Atomically to Disk
        self._save_plan_atomically(plan)
        return plan

    def step(
        self,
        strategy_id: str = "dynamic-desk",
        universe: Optional[List[str]] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Unified continuous operating step: dispatches between open-session loop and off-session cycle."""
        now_dt = now or datetime.now(timezone.utc)
        mode = self.get_operating_mode(universe, now=now_dt)

        if mode == OperatingMode.OPEN_SESSION:
            result = self.run_open_session_step(strategy_id, symbols=universe, now=now_dt)
            return {
                "operating_mode": OperatingMode.OPEN_SESSION.value,
                "cycle_result": result,
                "orders_placed": result.get("orders_count", 0),
                "simulated_fills": result.get("fills_count", 0),
            }
        else:
            active_univ = universe or ["AAPL", "NVDA", "MSFT"]
            plan = self.run_off_session_cycle(active_univ, now=now_dt)
            return {
                "operating_mode": OperatingMode.OFF_SESSION.value,
                "plan_id": plan.plan_id,
                "plan_status": plan.operating_status,
                "candidates_count": len(plan.candidates),
                "data_gaps_count": len(plan.data_gaps),
                "orders_placed": plan.orders_placed,  # Always 0
                "simulated_fills": plan.simulated_fills,  # Always 0
                "resumed_from_checkpoint": plan.resumed_from_checkpoint,
            }
