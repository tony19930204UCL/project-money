from __future__ import annotations

"""Persistent, bounded, simulation-only autonomous experiment runner."""

import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field

from cio_market_lab.domain.events import EventEnvelope, EventType
from cio_market_lab.domain.models import (
    Bar,
    CIODecisionContextRequest,
    CIODecisionPacket,
    CIOLessonRecord,
    CIOProvenance,
    ConsumedQuoteEvidence,
    DecisionScope,
    Fill,
    Market,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
    Quote,
)
from cio_market_lab.engine.stage_d_observation import apply_verified_quote_edges
from cio_market_lab.engine.source_aligned_execution import ResolvedPaperExecution, resolve_next_bar_open
from cio_market_lab.engine.cio_packet import validate_cio_packet
from cio_market_lab.engine.decision_learning import CIODecisionLearningStore
from cio_market_lab.engine.capabilities import get_derivative_capabilities_report
from cio_market_lab.engine.derivative_lifecycle import PaperDerivativeLifecycle
from cio_market_lab.engine.paper_orders import (
    PaperDataContext,
    PaperExperimentSettings,
    PaperOrderRequest,
    PaperOrderService,
)
from cio_market_lab.engine.portfolio import PortfolioManager, is_slippage_embedded
from cio_market_lab.engine.market_schedule import intraday_market_open, swing_session_slot
from cio_market_lab.engine.automation_policy import (
    AutomationRiskPolicy,
    ExitAction,
    LLMPolicyBoundary,
    LLMVerdict,
)
from cio_market_lab.data.base import MarketDataAdapter
from cio_market_lab.data.bounded_feed import FeedUnavailable, call_with_deadline
from cio_market_lab.data.market_data import SymbolCatalog
from cio_market_lab.engine.team_ops import (
    TEAM_INITIAL_CAPITAL_TWD,
    CanonicalPositionRow,
    CanonicalTeamOpsSnapshot,
    DurableQuoteSnapshot,
    TeamOpsSnapshotBuilder,
    compute_team_posture,
    load_canonical_snapshot,
    save_canonical_snapshot,
    to_public_team_ops_snapshot,
)

DYNAMIC_DESK_ID: str = "dynamic-desk"
DYNAMIC_DESK_NAME: str = "Autonomous Adaptive Paper Execution Desk"
LEGACY_STRATEGY_IDS = {
    "aggressive-momentum-us",
    "concentrated-high-beta-us",
    "balanced-growth-us",
    "defensive-cash-etf-us",
    "aggressive-momentum-tw",
    "concentrated-high-beta-tw",
    "balanced-growth-tw",
    "defensive-income-tw",
    "tw-momentum-long-01",
    "tw-mean-revert-01",
    "tw-breakout-swing-01",
    "tw-intraday-scalp-01",
    "us-tech-momentum-01",
    "us-value-pullback-01",
    "us-breakout-trend-01",
    "us-intraday-orb-01",
}



class ExperimentDecision(BaseModel):
    decision_id: str = Field(default_factory=lambda: f"decision-{uuid.uuid4()}")
    run_id: str
    strategy_id: str
    symbol: str
    market: Market
    mode: DecisionScope
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    action: str
    reason: str
    inputs: Dict[str, Any] = Field(default_factory=dict)
    order_id: Optional[str] = None
    quantity: float = 0.0
    price: Optional[float] = None
    terminal_status: str = "NON_TERMINAL"


class ExperimentRun(BaseModel):
    run_id: str
    strategy_id: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    status: str = "RUNNING"
    decisions_count: int = 0
    orders_count: int = 0
    fills_count: int = 0
    reason: str = ""


class EquitySnapshot(BaseModel):
    run_id: str
    strategy_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    bucket: DecisionScope
    cash: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    open_positions: int


class TeamEquitySnapshot(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    equity_twd: float


class AutonomousPaperRunner:
    """Runs only local paper fills and never has a broker/network execution path."""

    def __init__(
        self,
        root: Path,
        portfolio_manager: PortfolioManager,
        paper_orders: PaperOrderService,
        market_adapter: MarketDataAdapter,
        now_fn=None,
        exit_policy: Optional[AutomationRiskPolicy] = None,
        llm_policy: Optional[LLMPolicyBoundary] = None,
        team_initial_capital: Optional[float] = None,
        require_cio_provider: bool = True,
        cio_executor: Optional[Any] = None,
        runtime_dir: Optional[Union[str, Path]] = None,
        is_read_only: bool = False,
        research_reader: Optional[Any] = None,
        isolated_derivative_fixture_mode: bool = False,
        isolated_corporate_fixture_mode: bool = False,
        material_observation_provider: Optional[Any] = None,
        material_gate_enabled: bool = False,
        cio_session_id: str = "project-money-main-cio",
        frozen_decision_context: Optional[Any] = None,
    ) -> None:
        default_root = Path(__file__).resolve().parent.parent.parent
        if runtime_dir is not None:
            self._runtime_dir = Path(runtime_dir).resolve()
        elif Path(root).resolve() != default_root.resolve():
            self._runtime_dir = (Path(root) / "data" / "runtime").resolve()
        else:
            configured = os.environ.get("CIO_MARKET_LAB_RUNTIME_DIR")
            self._runtime_dir = Path(configured).resolve() if configured else (Path(root) / "data" / "runtime").resolve()
        self.is_read_only = is_read_only
        self.material_observation_provider = material_observation_provider
        self.material_gate_enabled = material_gate_enabled
        self.cio_session_id = cio_session_id
        self.frozen_decision_context = frozen_decision_context
        if not self.is_read_only:
            self._runtime_dir.mkdir(parents=True, exist_ok=True)
            from cio_market_lab.engine.continuation import ContinuationLock
            self.writer_lock: Optional[ContinuationLock] = ContinuationLock(self._runtime_dir / "continuation.lock")
        else:
            self.writer_lock = None
        self.portfolio_manager = portfolio_manager
        self.paper_orders = paper_orders
        self.derivative_lifecycle = PaperDerivativeLifecycle(
            portfolio_manager, paper_orders, fixture_mode=isolated_derivative_fixture_mode,
        )
        from cio_market_lab.engine.corporate_actions import PaperCorporateActions
        self.corporate_actions = PaperCorporateActions(
            portfolio_manager, paper_orders.event_store, fixture_mode=isolated_corporate_fixture_mode,
        )
        self.market_adapter = market_adapter
        self.team_initial_capital = (
            team_initial_capital
            if team_initial_capital is not None
            else getattr(portfolio_manager, "team_initial_capital", TEAM_INITIAL_CAPITAL_TWD)
        )
        self.exit_policy = exit_policy or AutomationRiskPolicy()
        self.llm_policy = llm_policy or LLMPolicyBoundary()
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        if now_fn is not None and hasattr(self.paper_orders, "_now_fn"):
            self.paper_orders._now_fn = self._now_fn
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._active: Dict[str, Dict[str, Any]] = {}
        self._runs: List[ExperimentRun] = []
        self._decisions: List[ExperimentDecision] = []
        self._equity: List[EquitySnapshot] = []
        self._team_equity_history: List[TeamEquitySnapshot] = []
        self._scheduled_slots: Dict[str, str] = {}
        self._canonical_version: int = 0
        self._durable_quotes: Dict[str, DurableQuoteSnapshot] = {}
        self._last_canonical_snapshot: Optional[Dict[str, Any]] = None
        self.learning_store = CIODecisionLearningStore(
            self.runtime_dir, getattr(self.paper_orders, "event_store", None), read_only=self.is_read_only
        )
        from cio_market_lab.research.browser import PublicResearchInboxReader
        inbox_dir = self.runtime_dir / "research_inbox"
        if not self.is_read_only:
            try:
                inbox_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        self.research_reader = research_reader or PublicResearchInboxReader(inbox_dir=inbox_dir)
        from cio_market_lab.research.official import OfficialResearchProducer
        self.official_research = OfficialResearchProducer()
        self.cio_executor: Optional[Any] = cio_executor
        self._staged_packets: Dict[str, CIODecisionPacket] = {}
        self.require_cio_provider: bool = require_cio_provider
        self.last_receipt: Optional[Any] = None
        self._load()

    def apply_corporate_action(self, action, *, strategy_id, bucket, now):
        """Apply only evidenced paper actions, serialized with all runner writers."""
        if self.is_read_only:
            raise PermissionError('READ_ONLY_CORPORATE_ACTION')
        from contextlib import nullcontext
        with self.writer_lock if self.writer_lock is not None else nullcontext():
            with self._lock:
                changed = self.corporate_actions.apply(action, strategy_id=strategy_id, bucket=bucket, now=now)
                self._persist_portfolios()
                return changed

    @property
    def runtime_dir(self) -> Path:
        return self._runtime_dir

    @runtime_dir.setter
    def runtime_dir(self, value: Union[str, Path]) -> None:
        self._runtime_dir = Path(value).resolve()
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(self, "learning_store") and self.learning_store is not None:
            self.learning_store.runtime_dir = self._runtime_dir
            self.learning_store._log_file = self._runtime_dir / "cio_learning.jsonl"
            self.learning_store._lessons_file = self._runtime_dir / "cio_lessons.jsonl"
            self.learning_store._records.clear()
            self.learning_store._lessons.clear()
            self.learning_store._processed_case_ids.clear()
            self.learning_store._load()
        if hasattr(self, "research_reader") and self.research_reader is not None:
            self.research_reader.set_inbox_dir(self._runtime_dir / "research_inbox")

    def set_cio_executor(self, executor: Optional[Any]) -> None:
        self.cio_executor = executor

    def stage_cio_packet(self, packet: CIODecisionPacket) -> None:
        self._staged_packets[packet.selected_instrument] = packet

    def clear_staged_packets(self) -> None:
        self._staged_packets.clear()

    @staticmethod
    def _packet_instrument_identity(packet: CIODecisionPacket) -> tuple[str, Optional[str]]:
        """Use reviewed catalog metadata for spot eligibility; never infer it from a bar or ticker shape.

        A caller's equity label, a configured universe, and the catalog's on-demand
        equity fallback are not evidence that a symbol is a cash security.
        """
        spec = packet.conditions.get("contract_spec")
        declared = packet.conditions.get("instrument_type")
        spec_type = spec.get("instrument_type") if isinstance(spec, dict) else None
        if declared is not None and not isinstance(declared, str):
            return "UNKNOWN", None
        if spec_type is not None and not isinstance(spec_type, str):
            return "CONFLICT", None
        if ("contract_spec" in packet.conditions or "derivative_quote" in packet.conditions
                or declared in {"OPTION", "FUTURE"}):
            if declared is not None and spec_type is not None and declared != spec_type:
                return "CONFLICT", None
            if spec_type is not None and spec_type not in {"OPTION", "FUTURE"}:
                return "CONFLICT", None
            return "DERIVATIVE", spec_type or declared
        if declared is not None and declared not in {"EQUITY", "ETF"}:
            return "UNKNOWN", None
        try:
            metadata = SymbolCatalog().lookup(packet.selected_instrument)
        except (TypeError, ValueError):
            return "UNKNOWN", None
        if metadata["coverage"] != "catalog" or metadata["symbol"] != packet.selected_instrument:
            return "UNKNOWN", None
        if declared is not None and declared.lower() != metadata["asset_type"]:
            return "CONFLICT", None
        if metadata["asset_type"] in {"equity", "etf"}:
            return "SPOT", metadata["asset_type"]
        return "UNKNOWN", None

    def _migrate_settings(self, values: Dict[str, Any]) -> Dict[str, PaperExperimentSettings]:
        """Load legacy settings without collapsing independent native-currency desks."""
        if not values:
            return {}
        if DYNAMIC_DESK_ID in values and len(values) == 1:
            desk_data = values[DYNAMIC_DESK_ID]
            return {
                DYNAMIC_DESK_ID: desk_data if isinstance(desk_data, PaperExperimentSettings) else PaperExperimentSettings.model_validate(desk_data)
            }
        result: Dict[str, PaperExperimentSettings] = {}
        for key, value in values.items():
            item = value if isinstance(value, PaperExperimentSettings) else PaperExperimentSettings.model_validate(value)
            # Validation owns legacy inference. Preserve native contract currency.
            expected = "TWD" if item.market == Market.TW else "USD"
            if item.base_currency != expected:
                raise ValueError(f"STRATEGY_MARKET_CURRENCY_MISMATCH:{item.strategy_id}")
            result[item.strategy_id] = item
        return result

    def reload_settings_if_needed(self) -> None:
        """Reload experiment settings from disk to converge across processes."""
        settings_path = self._path("experiment_settings.json")
        if settings_path.exists():
            try:
                raw = json.loads(settings_path.read_text(encoding="utf-8"))
                values = raw.get("strategies", raw) if isinstance(raw, dict) else {}
                migrated = self._migrate_settings(values)
                for item in migrated.values():
                    self.paper_orders.experiments[item.strategy_id] = item
                    self.portfolio_manager.register_strategy(
                        item.strategy_id, item.initial_cash, item.bucket_capital_allocations,
                        currency=item.base_currency,
                    )
            except Exception:
                pass


    def _now(self) -> datetime:
        value = self._now_fn()
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def _path(self, name: str) -> Path:
        return self.runtime_dir / name

    def _append_jsonl(self, name: str, item: BaseModel) -> None:
        if getattr(self, "is_read_only", False):
            return
        with self._path(name).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item.model_dump(mode="json"), sort_keys=True) + "\n")

    def _load_jsonl(self, name: str, model: type[BaseModel]) -> List[Any]:
        path = self._path(name)
        if not path.exists():
            return []
        result: List[Any] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    result.append(model.model_validate(json.loads(line)))
                except Exception:
                    continue
        return result

    def _atomic_json(self, name: str, payload: Any) -> None:
        if getattr(self, "is_read_only", False):
            return
        target = self._path(name)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)

    def _load(self) -> None:
        settings_path = self._path("experiment_settings.json")
        if settings_path.exists():
            try:
                raw = json.loads(settings_path.read_text(encoding="utf-8"))
                values = raw.get("strategies", raw) if isinstance(raw, dict) else {}
                migrated = self._migrate_settings(values)
                for value in migrated.values():
                    self.paper_orders.experiments[value.strategy_id] = value
                for value in self.paper_orders.experiments.values():
                    self.portfolio_manager.register_strategy(
                        value.strategy_id, value.initial_cash, value.bucket_capital_allocations,
                        currency=value.base_currency,
                    )
            except Exception:
                pass
        self._runs = self._load_jsonl("runs.jsonl", ExperimentRun)
        self._decisions = self._load_jsonl("decisions.jsonl", ExperimentDecision)
        self._equity = self._load_jsonl("equity_snapshots.jsonl", EquitySnapshot)
        self._team_equity_history = self._load_jsonl("team_equity_snapshots.jsonl", TeamEquitySnapshot)
        slots_path = self._path("scheduled_slots.json")
        if slots_path.exists():
            try:
                self._scheduled_slots = json.loads(slots_path.read_text(encoding="utf-8"))
            except Exception:
                self._scheduled_slots = {}
        disk_snapshot = load_canonical_snapshot(self.runtime_dir)
        if disk_snapshot and isinstance(disk_snapshot.get("version"), int):
            self._canonical_version = max(self._canonical_version, disk_snapshot["version"])
        self._restore_portfolios()
        self.corporate_actions.replay()
        self.derivative_lifecycle.replay()

    def _restore_portfolios(self) -> None:
        path = self._path("portfolio_state.json")
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            cash_accounts_data = raw.get("cash_accounts", {})
            for strategy_id, acct_data in cash_accounts_data.items():
                if strategy_id in self.portfolio_manager._strategy_cash_accounts:
                    acct = self.portfolio_manager._strategy_cash_accounts[strategy_id]
                    persisted_currency = acct_data.get("currency")
                    if persisted_currency is not None and str(persisted_currency).upper() != acct.currency:
                        raise ValueError(f"PERSISTED_CASH_CURRENCY_MISMATCH:{strategy_id}")
                    if "currency" not in acct_data and acct.currency == "USD":
                        raise ValueError(f"LEGACY_NATIVE_CURRENCY_UNVERIFIED:{strategy_id}")
                    acct.cash = float(acct_data.get("cash", acct.cash))
                    acct._applied_fill_ids = set(acct_data.get("applied_fill_ids", []))

            aggregate = raw.get("aggregate", raw)

            def replay_persisted_receipts(ledger, payload):
                # Snapshot orders record their *final* status, not their status
                # when each historical receipt arrived. EventStore may already
                # have reconstructed aggregate orders/fills before runner state
                # is restored, so preserve identity instead of replaying the
                # same receipt twice.
                from cio_market_lab.domain.models import Order
                existing_orders = {order.order_id: order for order in ledger.orders}
                existing_fill_ids = {fill.fill_id for fill in ledger.fills}
                saved_orders = []
                for order_data in payload.get("orders", []):
                    persisted = Order.model_validate(order_data)
                    order = existing_orders.get(persisted.order_id)
                    if order is None:
                        order = persisted
                        order.status = OrderStatus.PENDING
                        ledger.add_order(order)
                        existing_orders[order.order_id] = order
                    saved_orders.append((order, persisted.status, persisted.rejection_reason))
                for fill_data in payload.get("fills", []):
                    fill = Fill.model_validate(fill_data)
                    if fill.fill_id in existing_fill_ids:
                        continue
                    ledger.apply_fill(fill)
                    existing_fill_ids.add(fill.fill_id)
                for order, status, reason in saved_orders:
                    order.status = status
                    order.rejection_reason = reason

            for bucket_value, payload in aggregate.items():
                if bucket_value not in {DecisionScope.SWING.value, DecisionScope.INTRADAY.value}:
                    continue
                bucket = DecisionScope(bucket_value)
                ledger = self.portfolio_manager.get_ledger(bucket)
                prices = payload.get("latest_prices", {})
                for sym, px in prices.items():
                    ledger._latest_prices[sym] = float(px)
                replay_persisted_receipts(ledger, payload)
                ledger._recalculate_equity()

            for strategy_id, buckets in raw.get("strategies", {}).items():
                settings = self.paper_orders.experiments.get(strategy_id)
                for bucket_value, payload in buckets.items():
                    bucket = DecisionScope(bucket_value)
                    ledger = self.portfolio_manager.get_strategy_ledger(
                        strategy_id,
                        bucket,
                        settings.initial_cash if settings else None,
                    )
                    prices = payload.get("latest_prices", {})
                    for sym, px in prices.items():
                        ledger._latest_prices[sym] = float(px)
                    replay_persisted_receipts(ledger, payload)
                    from cio_market_lab.domain.models import Position
                    for sym, pos_data in payload.get("derivative_positions", {}).items():
                        ledger.positions[sym] = Position.model_validate(pos_data)
                    ledger._recalculate_equity()
            self.corporate_actions.restore(raw.get('corporate_state', {}))
            self.portfolio_manager._applied_derivative_keys = set(raw.get("applied_derivative_keys", []))
            for strategy_id in self.portfolio_manager.strategy_ids():
                for bucket in (DecisionScope.SWING, DecisionScope.INTRADAY):
                    ledger = self.portfolio_manager.get_strategy_ledger(strategy_id, bucket)
                    for pos in ledger.positions.values():
                        if pos.quantity and "derivative_position" in pos.assumptions:
                            pos.current_price = pos.market_value = pos.unrealized_pnl = None
                    ledger._recalculate_equity()
        except Exception as exc:
            raise ValueError(f"PORTFOLIO_RESTORE_FAIL_CLOSED:{exc}") from exc

    def _persist_portfolios(self) -> None:
        if getattr(self, "is_read_only", False):
            return
        state: Dict[str, Any] = {"aggregate": {}, "strategies": {}, "cash_accounts": {},
                                 "applied_derivative_keys": sorted(getattr(self.portfolio_manager, "_applied_derivative_keys", set()))}
        for bucket in (DecisionScope.SWING, DecisionScope.INTRADAY):
            portfolio = self.portfolio_manager.get_portfolio(bucket)
            ledger = self.portfolio_manager.get_ledger(bucket)
            state["aggregate"][bucket.value] = {
                "orders": [o.model_dump(mode="json") for o in portfolio.orders],
                "fills": [f.model_dump(mode="json") for f in portfolio.fills],
                "latest_prices": dict(ledger._latest_prices),
            }
        for strategy_id in self.portfolio_manager.strategy_ids():
            state["strategies"][strategy_id] = {}
            for bucket in (DecisionScope.SWING, DecisionScope.INTRADAY):
                portfolio = self.portfolio_manager.get_strategy_portfolio(strategy_id, bucket)
                ledger = self.portfolio_manager.get_strategy_ledger(strategy_id, bucket)
                state["strategies"][strategy_id][bucket.value] = {
                    "orders": [o.model_dump(mode="json") for o in portfolio.orders],
                    "fills": [f.model_dump(mode="json") for f in portfolio.fills],
                    "latest_prices": dict(ledger._latest_prices),
                    "derivative_positions": {sym: pos.model_dump(mode="json") for sym, pos in ledger.positions.items()
                                             if "derivative_position" in pos.assumptions},
                }
            if strategy_id in self.portfolio_manager._strategy_cash_accounts:
                acct = self.portfolio_manager._strategy_cash_accounts[strategy_id]
                state["cash_accounts"][strategy_id] = {
                    "initial_cash": acct.initial_cash,
                    "cash": acct.cash,
                    "currency": acct.currency,
                    "applied_fill_ids": list(acct._applied_fill_ids),
                }
        state['corporate_state'] = self.corporate_actions.snapshot()
        self._atomic_json("portfolio_state.json", state)

    def persist_settings(self) -> None:
        if getattr(self, "is_read_only", False):
            return
        self._atomic_json(
            "experiment_settings.json",
            {key: value.model_dump(mode="json") for key, value in self.paper_orders.experiments.items()},
        )

    def configure(self, settings: PaperExperimentSettings) -> PaperExperimentSettings:
        if getattr(self, "is_read_only", False):
            raise PermissionError("RUNNER_READONLY_INSTANCE: Cannot configure experiments on read-only instance")
        if getattr(self, "writer_lock", None) is not None:
            with self.writer_lock:
                return self._configure_locked(settings)
        return self._configure_locked(settings)

    def _configure_locked(self, settings: PaperExperimentSettings) -> PaperExperimentSettings:
        with self._lock:
            if settings.base_currency == "USD" and any(symbol.endswith((".TW", ".TWO")) for symbol in settings.universe):
                raise ValueError("UNSUPPORTED_MIXED_NATIVE_CURRENCY: USD desk cannot debit Taiwan securities")
            if settings.base_currency == "TWD" and any(
                    SymbolCatalog().lookup(symbol).get("coverage") == "catalog"
                    and SymbolCatalog().lookup(symbol).get("market") == "US"
                    for symbol in settings.universe):
                raise ValueError("UNSUPPORTED_NATIVE_CURRENCY: US securities require a separate USD desk")
            # Preflight before recording a configured event; a rejected currency
            # change must leave experiment state and history untouched.
            existing = self.portfolio_manager._strategy_ledgers.get(settings.strategy_id, {})
            if any(ledger.currency != settings.base_currency for ledger in existing.values()):
                raise ValueError("CURRENCY_MISMATCH: cannot change an existing strategy currency")
            value = self.paper_orders.configure_experiment(settings)
            self.portfolio_manager.register_strategy(
                value.strategy_id, value.initial_cash, value.bucket_capital_allocations,
                currency=value.base_currency,
            )
            self.persist_settings()
            return value

    def report_strategy_nav(self, strategy_id: str, *, as_of: datetime, rate_receipts=(),
                            max_age: timedelta = timedelta(days=5), allow_test_only: bool = False) -> Dict[str, Any]:
        """Report native NAV separately; never debit cash or sum incompatible currencies."""
        from dataclasses import asdict
        from cio_market_lab.engine.historical_fx import convert_nav, FxReportingBlocked
        settings = self.paper_orders.experiments[strategy_id]
        native = self.portfolio_manager.get_strategy_equity(strategy_id)
        if native is None:
            raise FxReportingBlocked("NAV_UNAVAILABLE: missing valuation mark")
        currency = settings.base_currency
        receipts = list(rate_receipts)
        if not allow_test_only and any(r.provenance == "TEST_ONLY" for r in receipts):
            raise FxReportingBlocked("TEST_ONLY_FX_NOT_ALLOWED")
        result = {"strategy_id": strategy_id, "as_of": as_of.isoformat(),
                  "native_nav": native, "native_currency": currency,
                  "reporting_currency": settings.reporting_currency}
        if currency == settings.reporting_currency:
            result.update(reporting_nav=native, conversion_receipt=None)
        else:
            converted = convert_nav(strategy_id=strategy_id, native_amount=native,
                native_currency=currency, reporting_currency=settings.reporting_currency,
                rate_receipts=receipts, as_of=as_of, max_age=max_age)
            result.update(reporting_nav=str(converted.reporting_amount),
                conversion_receipt=json.loads(json.dumps(asdict(converted.conversion_receipt), default=str)))
        return result

    def select_active_playbook(
        self,
        tw_regime: str,
        us_regime: str,
        data_quality: str,
        tw_open: bool,
        us_open: bool,
        gross_exposure: float,
        momentum: float,
        liquidity: str,
        now: datetime,
        cadence_seconds: float = 3600.0,
    ) -> Dict[str, Any]:
        """Deterministic playbook selection for the adaptive autonomous paper-execution desk.

        Chooses active playbook from current regime, data quality, session state,
        liquidity, momentum, and existing exposure rather than fixed labels.
        """
        quality_upper = data_quality.upper()
        # 1. Fail-closed on degraded data quality
        if quality_upper in {"STALE", "SYNTHETIC", "MISSING", "FAIL_CLOSED"}:
            next_rev = now + timedelta(seconds=min(cadence_seconds, 300.0))
            return {
                "playbook_id": "CAPITAL_PRESERVATION",
                "playbook_name": "資本防禦 (Capital Preservation / Fail-Closed)",
                "description": "數據品質異常，強制觸發安全閉合保護本金",
                "selection_rationale": f"數據品質為 {data_quality}，依據確定性風控原則閉合新部位進場，啟動資本防禦劇本。",
                "risk_multiplier": 0.0,
                "allow_new_entries": False,
                "target_cash_pct": 100.0,
                "allowed_buckets": [],
                "selected_at": now.isoformat(),
                "next_review_time": next_rev.isoformat(),
            }

        # 2. Exposure ceiling protection
        if gross_exposure >= 0.85:
            next_rev = now + timedelta(seconds=cadence_seconds)
            return {
                "playbook_id": "CAPITAL_PRESERVATION",
                "playbook_name": "資本防禦 (Capital Preservation / Exposure Cap)",
                "description": "總曝險超過上限門檻，全面停止擴張部位",
                "selection_rationale": f"總持倉曝險達 {gross_exposure:.1%}，達到或超過 85% 風控警戒線，強制執行防禦劇本以保護本金。",
                "risk_multiplier": 0.0,
                "allow_new_entries": False,
                "target_cash_pct": 25.0,
                "allowed_buckets": [],
                "selected_at": now.isoformat(),
                "next_review_time": next_rev.isoformat(),
            }

        # 3. High volatility / Bear market tension
        volatile_or_bear = any(
            r in {"HIGH_VOLATILITY", "BEAR_TREND", "CORRECTION", "CLOSED_RISK_OFF"}
            for r in (tw_regime.upper(), us_regime.upper())
        )
        if volatile_or_bear:
            next_rev = now + timedelta(seconds=cadence_seconds)
            if gross_exposure > 0.40:
                return {
                    "playbook_id": "CAPITAL_PRESERVATION",
                    "playbook_name": "資本防禦 (Volatile Regime Defense)",
                    "description": "市場體系高波動或熊市，縮減風險乘數並維持高現金比",
                    "selection_rationale": f"市場處於高波動/修正體系 (TW: {tw_regime}, US: {us_regime}) 且現有曝險達 {gross_exposure:.1%}，停止主動加倉。",
                    "risk_multiplier": 0.0,
                    "allow_new_entries": False,
                    "target_cash_pct": 70.0,
                    "allowed_buckets": [],
                    "selected_at": now.isoformat(),
                    "next_review_time": next_rev.isoformat(),
                }
            else:
                return {
                    "playbook_id": "DEFENSIVE_INCOME",
                    "playbook_name": "防禦收益 (Defensive Cash & Income ETF)",
                    "description": "高波動環境下僅允許低波動高股息與短期票券ETF標的",
                    "selection_rationale": f"市場波動上升但現有曝險偏低 ({gross_exposure:.1%})，僅允許防禦收益標的（如公債與防禦ETF）。",
                    "risk_multiplier": 0.25,
                    "allow_new_entries": False,
                    "target_cash_pct": 60.0,
                    "allowed_buckets": [DecisionScope.SWING],
                    "selected_at": now.isoformat(),
                    "next_review_time": next_rev.isoformat(),
                }

        # 4. Session state: All sessions closed
        if not tw_open and not us_open:
            next_rev = now + timedelta(seconds=min(cadence_seconds, 1800.0))
            return {
                "playbook_id": "SESSION_STANDBY",
                "playbook_name": "休市待命 (Session Standby & Valuation)",
                "description": "台美市場均休市，保持現有部位估值與跨市場結算",
                "selection_rationale": "台股與美股皆處於休市時段，維持部位靜態估值，待命於下個開盤會話。",
                "risk_multiplier": 0.0,
                "allow_new_entries": False,
                "target_cash_pct": 50.0,
                "allowed_buckets": [],
                "selected_at": now.isoformat(),
                "next_review_time": next_rev.isoformat(),
            }

        # 5. Active sessions: assess momentum, liquidity, and exposure
        next_rev = now + timedelta(seconds=cadence_seconds)
        if momentum > 0.025 and gross_exposure < 0.20:
            return {
                "playbook_id": "CONCENTRATED_HIGH_BETA",
                "playbook_name": "高Beta集中佈局 (Concentrated High Beta)",
                "description": "極低曝險時捕捉領先高Beta成長標的超額報酬",
                "selection_rationale": f"動能顯著飆升 (+{momentum:.2%}) 且在倉部位極低 ({gross_exposure:.1%})，切換至高Beta突破劇本集中進取。",
                "risk_multiplier": 1.0,
                "allow_new_entries": True,
                "target_cash_pct": 15.0,
                "allowed_buckets": [DecisionScope.SWING, DecisionScope.INTRADAY],
                "selected_at": now.isoformat(),
                "next_review_time": next_rev.isoformat(),
            }

        if momentum > 0.015 and liquidity == "HIGH" and gross_exposure < 0.35:
            return {
                "playbook_id": "AGGRESSIVE_MOMENTUM",
                "playbook_name": "積極動能突破 (Aggressive Momentum)",
                "description": "市場動能強勁且流動性充沛，把握突破行情加速成長",
                "selection_rationale": f"市場呈現強勁正向動能 (+{momentum:.2%})，流動性充沛，總曝險偏低 ({gross_exposure:.1%})，切換至積極動能劇本。",
                "risk_multiplier": 1.0,
                "allow_new_entries": True,
                "target_cash_pct": 20.0,
                "allowed_buckets": [DecisionScope.SWING, DecisionScope.INTRADAY],
                "selected_at": now.isoformat(),
                "next_review_time": next_rev.isoformat(),
            }

        if gross_exposure >= 0.70:
            return {
                "playbook_id": "BALANCED_GROWTH",
                "playbook_name": "謹慎持倉 (Throttled Balanced)",
                "description": "部位接近上限，收緊進場門檻以防守為主",
                "selection_rationale": f"持倉曝險已達 {gross_exposure:.1%}，維持穩健劇本但收緊進場門檻，優先監控獲利平倉。",
                "risk_multiplier": 0.25,
                "allow_new_entries": False,
                "target_cash_pct": 20.0,
                "allowed_buckets": [DecisionScope.SWING],
                "selected_at": now.isoformat(),
                "next_review_time": next_rev.isoformat(),
            }

        # Normal condition: Balanced Growth
        return {
            "playbook_id": "BALANCED_GROWTH",
            "playbook_name": "穩健平衡成長 (Balanced Growth)",
            "description": "均衡配置台美核心權值與科技成長標的，維持穩定淨值累積",
            "selection_rationale": f"市場體系平穩 (TW: {tw_regime}, US: {us_regime})，數據新鮮，現有曝險適中 ({gross_exposure:.1%})，執行穩健平衡成長劇本。",
            "risk_multiplier": 0.60,
            "allow_new_entries": True,
            "target_cash_pct": 35.0,
            "allowed_buckets": [DecisionScope.SWING],
            "selected_at": now.isoformat(),
            "next_review_time": next_rev.isoformat(),
        }

    def get_functional_desk_roles(
        self,
        playbook: Dict[str, Any],
        tw_regime: str,
        us_regime: str,
        data_quality: str,
        is_open: bool,
        gross_exposure: float,
        next_review_time: str,
    ) -> List[Dict[str, Any]]:
        is_working = any(state.get("processing") for state in self._active.values())
        return [
            {
                "role_id": "cio_strategist",
                "role_name": "Macro & Portfolio Architect",
                "title": "宏觀策略長 (Main CIO)",
                "scope": "總體體系判定 · 劇本動態切換 · 30天評估窗口治理",
                "status": "WORKING" if is_working else "MONITORING",
                "current_task": f"評估 TW/US 體系 ({tw_regime}/{us_regime})，維持 {playbook.get('playbook_name', '穩健成長')} 運作",
                "next_review_time": next_review_time,
            },
            {
                "role_id": "tactical_execution",
                "role_name": "Tactical Execution Officer",
                "title": "程序化執行席 (Execution Desk)",
                "scope": "台美跨市場紙盤撮合 · 買賣訂單排程 · 滑價成本控制",
                "status": "WORKING" if is_working else ("STANDBY" if is_open else "MONITORING"),
                "current_task": "監控標的流動性與動能突破，執行本地紙盤隔離委託" if is_open else "市場休市，待命於下一個交易會話",
                "next_review_time": next_review_time,
            },
            {
                "role_id": "risk_sentinel",
                "role_name": "Risk & Capital Sentinel",
                "title": "風控防禦席 (Risk Sentinel)",
                "scope": "單一TWD資金池保護 · 10%回撤防線 · 券商斷線隔離",
                "status": "ACTIVE",
                "current_task": f"總曝險 {gross_exposure:.1%} · 風險乘數 {playbook.get('risk_multiplier', 1.0)} · 熔斷監視中",
                "next_review_time": next_review_time,
            },
            {
                "role_id": "data_watcher",
                "role_name": "Data Telemetry Watcher",
                "title": "數據品質監理 (Data Telemetry)",
                "scope": "報價即時性驗證 · 拒絕Stale/Synthetic · 會話閘門判定",
                "status": "ACTIVE",
                "current_task": f"數據品質 {data_quality} · 異常即刻閉合",
                "next_review_time": next_review_time,
            },
        ]

    def set_llm_evaluator(self, evaluator) -> None:
        """Attach a bounded evaluator; it can veto/review but never size or submit orders."""
        self.llm_policy = LLMPolicyBoundary(evaluator)

    def _persist_scheduled_slots(self) -> None:
        self._atomic_json("scheduled_slots.json", self._scheduled_slots)

    def _scheduled_symbols(self, settings: PaperExperimentSettings, now: datetime) -> tuple[List[str], Dict[str, str]]:
        """Return symbols that may be evaluated now without fetching closed-market data."""

        if settings.mode == DecisionScope.INTRADAY:
            return [symbol for symbol in settings.universe if intraday_market_open(symbol, now)], {}

        eligible: List[str] = []
        slots: Dict[str, str] = {}
        for symbol in settings.universe:
            slot = swing_session_slot(symbol, now)
            key = f"{settings.strategy_id}|{symbol}"
            if slot and self._scheduled_slots.get(key) != slot:
                eligible.append(symbol)
                slots[key] = slot
        return eligible, slots

    def _is_fresh(self, bar: Optional[Bar], max_age_seconds: Optional[float] = None, intraday: bool = False) -> bool:
        if bar is None or bar.is_stale or bar.timestamp is None or bar.observed_at is None:
            return False
        quality = (bar.quality or "").lower()
        source = (bar.source or "").lower()
        if "synthetic" in quality or "synthetic" in source or "fallback" in source:
            return False
        if intraday and ("eod" in quality or "daily" in quality):
            return False
        now = self._now()
        exchange_at = bar.timestamp if bar.timestamp.tzinfo else bar.timestamp.replace(tzinfo=timezone.utc)
        observed_at = bar.observed_at if bar.observed_at.tzinfo else bar.observed_at.replace(tzinfo=timezone.utc)
        if exchange_at > now or observed_at > now:
            return False
        threshold = max_age_seconds if max_age_seconds is not None else 172800.0
        if intraday:
            threshold = min(threshold, 1800.0)
            event_at = exchange_at
        else:
            # Historical daily analysis is not an executable quote. Keep the
            # existing bounded observation freshness for swing research.
            event_at = observed_at
        return 0 <= (now - event_at).total_seconds() <= threshold

    @staticmethod
    def _market(symbol: str) -> Market:
        return Market.TW if symbol.upper().endswith((".TW", ".TWO")) else Market.US

    def _decision(self, run_id: str, settings: PaperExperimentSettings, symbol: str, action: str, reason: str, inputs: Dict[str, Any], **kwargs: Any) -> ExperimentDecision:
        horizon = inputs.get("holding_horizon")
        mode = DecisionScope.INTRADAY if horizon == "intraday" else DecisionScope.SWING if horizon in {"swing", "long_term"} else settings.mode
        value = ExperimentDecision(
            run_id=run_id,
            strategy_id=settings.strategy_id,
            symbol=symbol,
            market=self._market(symbol),
            mode=mode,
            action=action,
            reason=reason,
            inputs=inputs,
            timestamp=self._now(),
            **kwargs,
        )
        self._decisions.append(value)
        self._append_jsonl("decisions.jsonl", value)
        return value

    def _pending_buy_reserve(self, strategy_id: str, exclude_case_id: Optional[str] = None) -> float:
        """Reserve residual authorized quantity, not the already acquired shares."""
        reserved = 0.0
        for record in self.learning_store._records.values():
            if record.case_id == exclude_case_id or record.packet.action.upper() != "BUY":
                continue
            order = self.paper_orders.find_order(record.order_id) if record.order_id else None
            if order is None or order.strategy_id != strategy_id or order.status not in {OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED}:
                continue
            remaining = order.remaining_quantity
            if remaining <= 0:
                continue
            reference = record.pre_decision_quotes.get(record.packet.selected_instrument)
            price = order.limit_price or reference
            if not isinstance(price, (float, int)):
                reserved += float(record.pre_decision_quotes.get("reserved_cash", 0.0)) * remaining / order.quantity
            else:
                notional = remaining * float(price)
                reserved += notional + self.paper_orders.cost_config.calculate_fee(order.market, notional)
        return reserved

    def _review_daily_unarmed_plan(self, run_id, settings, symbol):
        """Submit genuine WAIT research to CIO; no executable book or trade is required.

        Return None only when the ordinary armed/execution path owns the symbol.
        A NO_TRADE hypothesis is not a fill or an execution authorization.
        """
        provider = getattr(self, "material_observation_provider", None)
        if provider is None or not hasattr(provider, "context_for"):
            return None
        now = self._now()
        try:
            observation = provider(symbol=symbol, inputs={}, now=now)
        except Exception as exc:
            return self._decision(run_id, settings, symbol, "NO_TRADE", "BLOCKED_DAILY_PLAN_SOURCE_UNAVAILABLE", {"error_type": type(exc).__name__}, terminal_status="TERMINAL_RISK_BLOCK")
        if not observation.get("research_only"):
            return None
        context = provider.context_for(symbol)
        if context is None:
            return self._decision(run_id, settings, symbol, "NO_TRADE", "BLOCKED_DAILY_PLAN_CONTEXT_MISSING", observation, terminal_status="TERMINAL_RISK_BLOCK")
        from .cio_session import CIOSessionHistory, MaterialDeltaGate
        from ..domain.models import CIODecisionContextRequest
        from .cio_packet import validate_cio_packet
        # Context identities contain exchange suffix punctuation. Keep the exact
        # identity in the frozen payload; use a deterministic safe storage key,
        # not lossy replacement and not a relaxed session/path safety validator.
        import hashlib
        storage_key = context.context_id
        if not storage_key or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in storage_key):
            storage_key = "context-" + hashlib.sha256(context.context_id.encode("utf-8")).hexdigest()
        context_store = CIOSessionHistory(self.runtime_dir / "daily_contexts", storage_key)
        context_store.freeze(context)
        gate = MaterialDeltaGate(CIOSessionHistory(self.runtime_dir / "cio_session", self.cio_session_id))
        # Daily session identity is a research-review trigger, never a buy trigger.
        material = dict(observation)
        material["official_material_ids"] = list(material.get("official_material_ids") or []) + [context.context_id]
        material["session_id"] = self.cio_session_id
        signal = gate.evaluate(material)
        inputs = dict(observation, frozen_context_id=context.context_id, daily_plan_review=True,
                      executable_book_required=False, research_review_only=True)
        self.evaluate_elapsed_non_actions(symbol)
        if not signal["should_call"]:
            return self._decision(run_id, settings, symbol, "NO_TRADE", f"CIO material gate: {signal['reason']}", inputs, terminal_status="TERMINAL_NO_TRADE")
        executor = getattr(self, "cio_executor", None)
        if executor is None or not executor.is_available():
            return self._decision(run_id, settings, symbol, "NO_TRADE", "BLOCKED_NO_CIO_DECISION_PROVIDER", inputs, terminal_status="TERMINAL_RISK_BLOCK")
        now = self._now()
        portfolio = self.portfolio_manager.get_strategy_portfolio(settings.strategy_id, settings.mode)
        native = {"strategy_id": settings.strategy_id, "currency": portfolio.currency,
                  "cash": portfolio.cash, "initial_cash": portfolio.initial_cash,
                  "equity": portfolio.equity,
                  "positions": {key: value.model_dump(mode="json") for key, value in portfolio.positions.items()}}
        reference = {}
        reference_evidence = {}
        analysis_history = []
        bar = None
        # Read actual daily history before CIO review. Never translate OHLC
        # history into an executable book or manufacture a missing baseline.
        try:
            fetched = self._market_call("get_bars", symbol, timeframe="1M", limit=8)
            if isinstance(fetched, (list, tuple)):
                analysis_history = [b.model_dump(mode="json") for b in fetched
                                    if hasattr(b, "model_dump")]
                bar = fetched[-1] if fetched else None
        except Exception:
            pass
        if bar is None:
            try:
                bar = self._market_call("get_latest_bar", symbol)
            except Exception:
                pass
        try:
            timestamp = bar.timestamp if bar.timestamp.tzinfo else bar.timestamp.replace(tzinfo=timezone.utc)
            source = str(bar.source or "")
            fixture = getattr(bar, "is_fixture", False) or any(x in source.lower() for x in ("fixture", "test_only"))
            if (bar.symbol == symbol and math.isfinite(bar.close) and bar.close > 0
                    and not getattr(bar, "is_stale", False)
                    and not getattr(bar, "is_synthetic", False)
                    and not any(x in source.lower() for x in ("synthetic", "fallback", "replay", "missing"))
                    and source and (not fixture or getattr(self, "allow_fixture_quotes", False))
                    and 0 <= (now - timestamp).total_seconds() <= 7 * 86400):
                reference[symbol] = bar.close
                reference_evidence = {"symbol": symbol, "price": bar.close, "timestamp": timestamp.isoformat(),
                                      "source": source, "purpose": "ANALYSIS_COUNTERFACTUAL_BASELINE_NOT_EXECUTABLE_BOOK"}
        except (TypeError, ValueError, AttributeError):
            pass
        # The learning-store public API returns serialized lesson dictionaries.
        # Preserve them for the authenticated caller, including matured WAIT
        # counterfactual outcomes; never coerce a lesson into a simulated fill.
        prior_lessons = self.learning_store.retrieve_context_lessons(symbol=symbol, as_of=now)
        past_outcomes = [record.model_dump(mode="json") for record in self.learning_store._records.values() if record.packet.selected_instrument == symbol and record.packet.as_of <= now][-10:]
        snapshot = {"frozen_daily_context": context.model_dump(mode="json"),
                    "daily_research_plan": observation, "native_portfolio": native,
                    "analysis_reference": reference_evidence,
                    "analysis_history": analysis_history,
                    "analysis_history_authority": "ANALYSIS_ONLY_NOT_EXECUTION_BOOK",
                    "execution_armed": False,
                    "decision_scope": "research_triage", "allowed_actions": ["NO_TRADE", "REJECT"]}
        # The daemon run_id is stable across days. Bind each request to the
        # immutable source context so later sessions cannot reuse a prior case.
        context_revision = hashlib.sha256(context.context_id.encode('utf-8')).hexdigest()[:16]
        request = CIODecisionContextRequest(
            request_id=f"daily-plan-{run_id}-{symbol}-{context_revision}", timestamp=now, universe=[symbol],
            canonical_portfolio=native, verified_quotes={symbol: reference_evidence} if reference_evidence else {},
            verified_research=context.model_dump(mode="json")["official_source_lineage"],
            research_gaps=[{"symbol": symbol, "gaps": observation.get("missing_evidence", []), "stance": "WAIT", "armed": False}],
            prior_lessons=prior_lessons, past_outcomes=past_outcomes,
            rejected_opportunities=[alternative for record in past_outcomes for alternative in record.get("rejected_opportunities", [])],
            tactical_risk_limits={"max_position_notional": settings.max_position_notional, "execution_armed": False},
            tool_eligibility={"cash": {"eligible": True}, "new_order": {"eligible": False, "reason": "DAILY_PLAN_RESEARCH_ONLY_WAIT"}},
            predecision_snapshot=snapshot,
        )
        try:
            packet = executor.request_decision(request)
        except Exception as exc:
            inputs["provider_error_type"] = type(exc).__name__
            return self._decision(run_id, settings, symbol, "NO_TRADE", "BLOCKED_DAILY_PLAN_CIO_PROVIDER", inputs, terminal_status="TERMINAL_RISK_BLOCK")
        receipt = getattr(executor, "last_receipt", None)
        valid, validation_reason = validate_cio_packet(packet, now=now, processed_case_ids=self.learning_store.processed_case_ids)
        errors = [] if valid else [validation_reason]
        if packet.is_fixture and not getattr(self, "allow_fixture_quotes", False):
            errors.append("DAILY_PLAN_FIXTURE_PACKET_FORBIDDEN")
        if packet.selected_instrument != symbol:
            errors.append("DAILY_PLAN_INSTRUMENT_MISMATCH")
        if packet.action.upper() not in {"NO_TRADE", "REJECT"} or packet.quantity != 0:
            errors.append("DAILY_PLAN_UNARMED_ACTION_FORBIDDEN")
        if receipt is None or not getattr(receipt, "readback_verified", False) or getattr(receipt, "case_id", None) != packet.case_id:
            errors.append("DAILY_PLAN_AUTHENTICATED_RECEIPT_MISSING")
        if errors:
            inputs["validation_errors"] = errors
            return self._decision(run_id, settings, symbol, "NO_TRADE", "BLOCKED_DAILY_PLAN_PACKET_VALIDATION", inputs, terminal_status="TERMINAL_RISK_BLOCK")
        if packet.case_id in self.learning_store.processed_case_ids:
            return self._decision(run_id, settings, symbol, "NO_TRADE", "BLOCKED_DUPLICATE_CIO_CASE_ID", inputs, terminal_status="TERMINAL_RISK_BLOCK")
        horizon = getattr(packet.holding_horizon, "value", str(packet.holding_horizon))
        duration = {"intraday": timedelta(hours=6), "swing": timedelta(days=7),
                    "long_term": timedelta(days=30), "cash": timedelta(days=1)}.get(horizon, timedelta(days=7))
        packet.predecision_snapshot = snapshot
        packet.conditions.update({"observation_deadline": (packet.as_of + duration).isoformat(),
                                  "daily_context_id": context.context_id, "no_fill": True,
                                  "observation_status": "AWAITING_ELAPSED_OUTCOME" if reference else "WAITING_FOR_BASELINE_NO_COUNTERFACTUAL_CLAIM",
                                  "analysis_reference": reference_evidence})
        # Delivery is an audit fact, not application. Only the authenticated
        # executor packet's explicit, evidenced acknowledgement can claim use.
        delivered_lesson_ids = [
            str(lesson.get("lesson_id")) for lesson in prior_lessons
            if isinstance(lesson, dict) and lesson.get("lesson_id")
        ]
        raw_applied = packet.conditions.get("applied_lesson_ids", [])
        if not isinstance(raw_applied, list) or any(not isinstance(x, str) for x in raw_applied):
            errors.append("DAILY_PLAN_INVALID_APPLIED_LESSON_IDS")
        offered = set(delivered_lesson_ids)
        if isinstance(raw_applied, list) and any(not isinstance(x, str) or x not in offered for x in raw_applied):
            errors.append("DAILY_PLAN_UNOFFERED_APPLIED_LESSON_ID")
        reasons = packet.conditions.get("applied_lesson_reasons", {})
        evidence = packet.conditions.get("applied_lesson_evidence", {})
        if isinstance(raw_applied, list) and raw_applied and (
            not isinstance(reasons, dict) or not isinstance(evidence, dict) or
            any(not isinstance(x, str) or not isinstance(reasons.get(x), str) or not reasons.get(x, "").strip() or not isinstance(evidence.get(x), str) or not evidence.get(x, "").strip() for x in raw_applied)
        ):
            errors.append("DAILY_PLAN_APPLIED_LESSON_EVIDENCE_MISSING")
        if errors:
            inputs["validation_errors"] = errors
            return self._decision(run_id, settings, symbol, "NO_TRADE", "BLOCKED_DAILY_PLAN_PACKET_VALIDATION", inputs, terminal_status="TERMINAL_RISK_BLOCK")
        applied_lesson_ids = list(raw_applied)
        packet.conditions["delivered_lesson_ids"] = delivered_lesson_ids
        self.learning_store.record_decision(
            packet, native, reference, applied_lesson_ids=applied_lesson_ids
        )
        self.last_receipt = receipt
        inputs.update({"case_id": packet.case_id, "provider_id": getattr(receipt, "provider_id", None),
                       "model_id": getattr(receipt, "model_id", None), "cio_receipt": receipt.model_dump(mode="json"),
                       "observation_deadline": packet.conditions["observation_deadline"],
                       "observation_status": packet.conditions["observation_status"]})
        gate.record_call(material, {"success": True, "case_id": packet.case_id, "reason": "AUTHENTICATED_DAILY_WAIT_REVIEW"})
        return self._decision(run_id, settings, symbol, "NO_TRADE", f"CIO daily research review: {packet.action}", inputs, terminal_status="TERMINAL_NO_TRADE")

    def evaluate_elapsed_non_actions(self, symbol: str) -> List[str]:
        """Close held/rejected hypotheses only after a later, observed market quote."""
        now = self._now()
        candidates = []
        durations = {"intraday": timedelta(hours=6), "swing": timedelta(days=7),
                     "long_term": timedelta(days=30), "cash": timedelta(days=1)}
        for record in self.learning_store._records.values():
            packet = record.packet
            if (packet.selected_instrument == symbol and packet.action.upper() in {"HOLD", "REJECT", "NO_TRADE"}
                    and record.status == "ACTIVE"):
                horizon = getattr(packet.holding_horizon, "value", str(packet.holding_horizon))
                deadline = packet.as_of + durations.get(horizon, timedelta(days=7))
                frozen_deadline = packet.conditions.get("observation_deadline")
                if frozen_deadline:
                    try:
                        parsed = datetime.fromisoformat(str(frozen_deadline))
                        if parsed.tzinfo is None or parsed < packet.as_of:
                            continue  # Invalid deadline cannot authorize closure.
                        deadline = parsed
                    except (TypeError, ValueError):
                        continue
                if now >= deadline and packet.selected_instrument != "CASH":
                    candidates.append((record, deadline))
        if not candidates:
            return []
        closed = []
        # A case without a valid baseline still expires. Its terminal result is
        # unknown, never a fabricated counterfactual or a trading-success label.
        measurable = []
        for record, deadline in candidates:
            start = record.pre_decision_quotes.get(symbol)
            if isinstance(start, (int, float)) and math.isfinite(start) and start > 0:
                measurable.append((record, deadline))
                continue
            outcome = {"kind": "NON_ACTION_OBSERVATION", "action": record.packet.action,
                       "symbol": symbol, "baseline_price": None, "observed_price": None,
                       "counterfactual_price_change_pct": None, "realized_pnl": 0.0,
                       "no_fill": True, "as_of": now.isoformat(),
                       "observation_deadline": deadline.isoformat(),
                       "evaluation_status": "UNOBSERVABLE_NO_BASELINE"}
            self.learning_store.record_outcome(
                record.case_id, outcome,
                prediction_vs_outcome={"prediction": record.packet.thesis, "outcome": outcome,
                                       "prediction_accurate": None},
                lessons=["No valid pre-decision baseline existed; do not infer missed return or trade accuracy."],
                as_of=now, validation_status="INSUFFICIENT_BASELINE_TERMINAL")
            closed.append(record.case_id)
        if not measurable:
            return closed
        try:
            quote = self._market_call("get_latest_quote", symbol)
        except Exception:
            return closed
        if (quote is None or quote.last_price is None or not math.isfinite(quote.last_price)
                or quote.last_price <= 0 or quote.is_stale or quote.is_synthetic):
            return closed
        source = (quote.source or "").lower()
        fixture = getattr(quote, "is_fixture", False) or any(x in source for x in ("fixture", "test_only"))
        if (not source or any(token in source for token in ("fallback", "synthetic", "replay"))
                or (fixture and not getattr(self, "allow_fixture_quotes", False))):
            return closed
        quote_at = quote.timestamp if quote.timestamp.tzinfo else quote.timestamp.replace(tzinfo=timezone.utc)
        observed = quote.observed_at if quote.observed_at.tzinfo else quote.observed_at.replace(tzinfo=timezone.utc)
        if quote_at > now or observed > now or (now - observed).total_seconds() > 172800:
            return closed
        evaluated = list(closed)
        for record, deadline in measurable:
            if quote_at < deadline:
                continue
            base = record.pre_decision_quotes.get(symbol)
            if not isinstance(base, (float, int)) or base <= 0:
                continue
            change_pct = round((quote.last_price / base - 1) * 100, 4)
            self.learning_store.record_outcome(
                record.case_id,
                {"as_of": quote_at.isoformat(), "exit_reason": "NON_ACTION_EVALUATION_ONLY",
                 "reference_price": base, "evaluation_price": quote.last_price,
                 "counterfactual_price_change_pct": change_pct, "realized_pnl": 0.0,
                 "no_fill": True, "no_trade_pnl": True},
                lessons=[f"Non-action {record.packet.action} evaluated after horizon; observed price change {change_pct}% is counterfactual, not realized profit."],
                as_of=quote_at,
            )
            evaluated.append(record.case_id)
        return evaluated

    def _available_observed_book_size(self, quote: Quote, side: OrderSide) -> float:
        """Exact exchange observation capacity shared across strategies and restarts."""
        size = quote.ask_size if side == OrderSide.BUY else quote.bid_size
        if not isinstance(size, (float, int)) or not math.isfinite(size) or size <= 0:
            return 0.0
        key = (quote.symbol, quote.source, quote.timestamp, quote.bid, quote.ask, quote.session)
        consumed, seen = 0.0, set()
        for strategy_id in self.portfolio_manager.strategy_ids():
            for ledger in self.portfolio_manager.get_all_strategy_portfolios(strategy_id).values():
                for prior in ledger.fills:
                    evidence = prior.consumed_quote
                    if prior.fill_id in seen or prior.side != side or evidence is None:
                        continue
                    seen.add(prior.fill_id)
                    if (evidence.symbol, evidence.source, evidence.exchange_at, evidence.bid, evidence.ask, evidence.session) == key:
                        consumed += prior.quantity
        return max(0.0, float(size) - consumed)

    def _record_cio_execution_fragment(self, case_id, order, fill, strategy_id):
        ledger = self.portfolio_manager.get_strategy_ledger(strategy_id, fill.bucket)
        fragments = [item for item in ledger.fills if item.order_id == order.order_id]
        total_qty = sum(item.quantity for item in fragments)
        summary = fill.model_dump(mode="json")
        if len(fragments) > 1:
            summary.update({"aggregate_kind": "cumulative_order_fill_summary_not_an_executable_fill",
                            "quantity": total_qty, "timestamp": fragments[0].timestamp.isoformat(),
                            "fill_price": sum(item.quantity * item.fill_price for item in fragments) / total_qty,
                            "fee": sum(item.fee for item in fragments), "tax": sum(item.tax for item in fragments),
                            "slippage": sum(item.slippage for item in fragments),
                            "cash_flow": sum(item.cash_flow for item in fragments),
                            "consumed_quote": None, "quote_verification": "AGGREGATED_FRAGMENTS"})
        summary["execution_fragments"] = [item.model_dump(mode="json") for item in fragments]
        costs = {"fee": sum(item.fee for item in fragments), "tax": sum(item.tax for item in fragments),
                 "slippage": sum(item.slippage for item in fragments),
                 "total_cost": sum(item.fee + item.tax + item.slippage for item in fragments)}
        rec = self.learning_store.record_fill(case_id, order.order_id, summary, costs)
        if rec is not None and order.status == OrderStatus.PARTIALLY_FILLED:
            rec.status = "PARTIALLY_FILLED"
            self.learning_store._persist_all()

    def _resolve_cio_execution(self, settings, packet, bar, order, *, entry_time=None):
        """One declared PAPER model. Bars are never converted into quotes."""
        max_age = settings.max_data_age_seconds
        if order.status not in {OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED}:
            return None
        remaining = order.remaining_quantity
        if remaining <= 0:
            return None
        order = order.model_copy(update={"quantity": remaining, "status": OrderStatus.PENDING})
        if settings.paper_execution_model == "NEXT_BAR_OPEN":
            if packet.conditions.get("paper_execution_model") != "NEXT_BAR_OPEN":
                return None
            cutoff = max(packet.as_of, order.created_at)
            if entry_time is not None:
                cutoff = max(cutoff, entry_time)
            try:
                bars = self.market_adapter.get_bars(order.symbol, start=cutoff, end=self._now())
            except Exception:
                return None
            candidates = sorted((b for b in bars if b.symbol == order.symbol
                                 and b.timestamp > cutoff and b.timestamp <= self._now()),
                                key=lambda b: b.timestamp)
            if not candidates:
                return None
            for selected in candidates:
                used = 0.0
                for sid in self.portfolio_manager.strategy_ids():
                    for portfolio in self.portfolio_manager.get_all_strategy_portfolios(sid).values():
                        for fill in portfolio.fills:
                            sb = fill.assumptions.get("source_bar", {})
                            try:
                                # Durable model JSON uses Z; datetime.isoformat uses
                                # +00:00. Compare the event instant, not spelling.
                                source_time = datetime.fromisoformat(str(sb.get("timestamp", "")).replace("Z", "+00:00"))
                            except (TypeError, ValueError):
                                continue
                            if (sb.get("symbol") == selected.symbol and sb.get("source") == selected.source
                                    and source_time == selected.timestamp):
                                used += fill.quantity
                resolved = resolve_next_bar_open(selected, order, self.paper_orders.cost_config,
                    now=self._now(), decision_at=packet.as_of,
                    allow_fixture=bool(packet.is_fixture and self.allow_fixture_quotes),
                    max_age_seconds=max_age, used_volume=used)
                if resolved is not None:
                    return resolved
            return None
        quote = self._find_eligible_later_quote(packet.selected_instrument, bar, max_age_seconds=max_age,
            allow_extended_hours=(order.market == Market.US and packet.conditions.get("allow_extended_hours") is True),
            allow_odd_lot=(order.market == Market.TW and packet.conditions.get("allow_odd_lot") is True))
        # BBO execution, like NEXT_BAR_OPEN, must follow both the signed
        # decision and order creation. A quote newer than the analysis bar
        # can still predate authorization; never back-fill against that book.
        cutoff = max(packet.as_of, order.created_at)
        if entry_time is not None:
            cutoff = max(cutoff, entry_time)
        if quote is None or quote.timestamp <= cutoff:
            return None
        fractional = packet.conditions.get("allow_fractional_shares") is True
        if order.market == Market.US:
            if fractional and quote.source_capabilities.get("fractional_shares") is not True:
                return None
            if not fractional and not float(remaining).is_integer():
                return None
        if order.market == Market.TW:
            odd = packet.conditions.get("allow_odd_lot") is True
            if odd:
                if (quote.session != "ODD_LOT" or quote.source_capabilities.get("odd_lot_book") is not True
                        or remaining >= 1000 or not float(remaining).is_integer()):
                    return None
            elif quote.session != "REGULAR" or remaining % 1000 != 0:
                return None
        available = self._available_observed_book_size(quote, order.side)
        if available < remaining:
            if packet.conditions.get("allow_partial_fills") is not True:
                return None
            step = 1000.0 if order.market == Market.TW and packet.conditions.get("allow_odd_lot") is not True else 1.0
            qty = (max(0.0, available) if packet.conditions.get("allow_fractional_shares") is True and order.market == Market.US
                   else math.floor(max(0.0, available) / step) * step)
            if qty <= 0:
                return None
            order = order.model_copy(update={"quantity": min(remaining, qty)})
        consumed = self._consume_book(quote, bar, order)
        if consumed is None:
            return None
        evidence, slip, effective = consumed
        return ResolvedPaperExecution(
            timestamp=quote.timestamp, base_price=evidence.ask if order.side == OrderSide.BUY else evidence.bid,
            effective_price=effective, per_share_slippage=slip, quote_evidence=evidence,
            verification="BOOK_BOUND_TEST_ONLY" if evidence.is_fixture else "BOOK_BOUND",
            assumptions={"timing_assumption": "authoritative_later_quote_slippage_adjusted",
                         "executed_quantity": order.quantity, "remaining_quantity_before_fill": remaining,
                         "quote_timestamp": quote.timestamp.isoformat(), "slippage_embedded": True})

    def _find_eligible_later_quote(
        self,
        symbol: str,
        signal_bar: Bar,
        max_age_seconds: Optional[float] = None,
        *, allow_extended_hours: bool = False, allow_odd_lot: bool = False,
    ) -> Optional[Quote]:
        """Fetch quote strictly later than signal_bar and verify it is authoritative."""
        quote: Optional[Quote] = None
        try:
            quote = self._market_call("get_latest_quote", symbol)
        except Exception:
            quote = None

        if quote is None or quote.symbol != symbol:
            return None

        # Quote timestamp must be strictly later than signal bar timestamp
        quote_ts = quote.timestamp if quote.timestamp.tzinfo else quote.timestamp.replace(tzinfo=timezone.utc)
        bar_ts = signal_bar.timestamp if signal_bar.timestamp.tzinfo else signal_bar.timestamp.replace(tzinfo=timezone.utc)
        if quote_ts <= bar_ts or quote_ts > self._now():
            return None

        # Authoritative checks: non-stale, non-synthetic, non-fallback
        if quote.is_stale or quote.is_synthetic:
            return None

        quality = (quote.quality or "").lower()
        source = (quote.source or "").lower()
        if "chart_close_proxy" in quality or "synthetic" in quality or "synthetic" in source or "fallback" in source or source == "missing" or "fixture" in source and not getattr(self, "allow_fixture_quotes", False):
            return None

        now = self._now()
        now_ts = now if now.tzinfo else now.replace(tzinfo=timezone.utc)

        fixture = "fixture" in source or getattr(self.market_adapter, "is_fixture", False)
        fixture_allowed = fixture and getattr(self, "allow_fixture_quotes", False)

        # Execution observations are not historical analysis bars, including
        # swing orders outside the exchange session.  Fixture/test-mode quotes
        # honour the caller's max_age_seconds to support deterministic time-
        # stepping without an artificial 60-second wall.  Public reported last-
        # sale quotes (e.g. CNBC) likewise use the caller's threshold; only
        # bare "last_sale" (chart close proxy) is rejected below.
        if fixture_allowed or "public_reported_last_sale" in quality:
            threshold = max_age_seconds if max_age_seconds is not None else 172800.0
        else:
            threshold = min(max_age_seconds if max_age_seconds is not None else 172800.0, 60.0)
        if not 0 <= (now_ts - quote_ts).total_seconds() <= threshold:
            return None

        # observed_at / tzinfo sanity
        if not quote.observed_at:
            return None
        obs = quote.observed_at if quote.observed_at.tzinfo else quote.observed_at.replace(tzinfo=timezone.utc)
        q_ts_tz = quote_ts
        if not 0 <= (now_ts - obs).total_seconds() <= threshold or obs < q_ts_tz:
            return None

        # Quality and session rejections
        if "proxy" in quality and "public_reported" not in quality:
            return None
        if quality == "last_sale" and "public_reported" not in quality:
            return None
        if "last_sale" in quality and "public_reported" not in quality:
            return None
        if fixture and not fixture_allowed:
            return None
        if quote.session != "REGULAR":
            caps = quote.source_capabilities
            scopes = caps.get("supported_sessions", []) if isinstance(caps, dict) else []
            if not isinstance(scopes, list) or quote.session not in scopes:
                return None
            if quote.session == "EXTENDED":
                if allow_extended_hours is not True or caps.get("extended_hours_book") is not True:
                    return None
            elif quote.session == "ODD_LOT":
                if allow_odd_lot is not True or caps.get("odd_lot_book") is not True:
                    return None
            else:
                return None


        # If explicit source_capabilities are provided on the quote:
        # validate strict BBO capabilities without fabricating or overriding depth/ids.
        if quote.source_capabilities:
            caps = quote.source_capabilities
            if (not quote.quote_id or not quote.source or not isinstance(caps, dict) or caps.get("source") != quote.source
                    or caps.get("two_sided_book") is not True or caps.get("size_backed") is not True
                    or caps.get("exchange_session_attested") is not True or not caps.get("entitlement_evidence_id")
                    or caps.get("entitlement_status") != ("TEST_ONLY" if fixture else "VERIFIED")):
                return None
            if (any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0
                    for value in (quote.bid, quote.ask, quote.bid_size, quote.ask_size))
                    or quote.bid > quote.ask):
                return None
            return quote

        # Verified public reported last-sale (CNBC): valid quote observation,
        # non-two-sided book (will not fill orders unless a two-sided book exists).
        if "public_reported_last_sale" in quality and not fixture_allowed:
            if not quote.source:
                return None
            return quote

        # Only explicitly enabled fixture quotes may use legacy synthetic
        # depth. A live observation without attested capabilities must never
        # be promoted into a VERIFIED book by filling in missing fields.
        if not fixture_allowed:
            return None
        # Legacy test / fixture adapters without explicit source_capabilities:
        if quote.quote_id == "":  # Explicitly rejected empty quote_id
            return None
        if quote.bid is None and quote.last_price and quote.last_price > 0:
            quote.bid = quote.last_price
        if quote.ask is None and quote.last_price and quote.last_price > 0:
            quote.ask = quote.last_price
        if (quote.bid is None or quote.ask is None
                or not isinstance(quote.bid, (int, float)) or not isinstance(quote.ask, (int, float))
                or not math.isfinite(quote.bid) or not math.isfinite(quote.ask)
                or quote.bid <= 0 or quote.ask <= 0 or quote.bid > quote.ask):
            return None

        # Check for one-sided book explicitly configured by adapter
        if (quote.bid_size <= 0 and quote.ask_size > 0) or (quote.ask_size <= 0 and quote.bid_size > 0):
            return None

        if not quote.quote_id:
            quote.quote_id = f"auto-{uuid.uuid4()}"
        if quote.bid_size <= 0:
            quote.bid_size = 1000.0
        if quote.ask_size <= 0:
            quote.ask_size = 1000.0
        quote.source_capabilities = {
            "source": quote.source,
            "two_sided_book": True,
            "size_backed": True,
            "exchange_session_attested": True,
            "entitlement_evidence_id": f"fixture-eid-{uuid.uuid4()}",
            "entitlement_status": "TEST_ONLY" if fixture else "VERIFIED",
        }
        if not quote.session:
            quote.session = "REGULAR"

        return quote

    def _consume_book(self, quote: Quote, bar: Bar, order: Any) -> Optional[tuple[ConsumedQuoteEvidence, float, float]]:
        """Bind one exact side/size/limit-eligible book snapshot before posting a fill."""
        if (order.status != OrderStatus.PENDING or order.symbol != quote.symbol
                or order.quantity <= 0 or order.order_type not in (OrderType.MARKET, OrderType.LIMIT)):
            return None
        size = self._available_observed_book_size(quote, order.side)
        base = quote.ask if order.side == OrderSide.BUY else quote.bid
        if order.quantity > size or base is None:
            return None
        slip, effective = self.paper_orders.cost_config.calculate_slippage(base, order.side)
        if order.limit_price is not None and (
            (order.side == OrderSide.BUY and effective > order.limit_price)
            or (order.side == OrderSide.SELL and effective < order.limit_price)
        ):
            return None
        evidence = ConsumedQuoteEvidence(
            consumed_id=f"consumed-{uuid.uuid4()}", source_quote_id=quote.quote_id,
            symbol=quote.symbol, source=quote.source, exchange_at=quote.timestamp,
            observed_at=quote.observed_at, bid=quote.bid, ask=quote.ask,
            bid_size=quote.bid_size, ask_size=quote.ask_size,
            source_capabilities=json.loads(json.dumps(quote.source_capabilities)),
            is_synthetic=quote.is_synthetic,
            is_fixture="fixture" in quote.source.lower() or getattr(self.market_adapter, "is_fixture", False),
            signal_bar_at=bar.timestamp, session=quote.session,
        )
        return evidence, slip, effective

    FEED_STAGE_SECONDS = 8.0

    def _market_call(self, method: str, symbol: str, **kwargs: Any) -> Any:
        """Bound all feed reads in a live/explicit-fixture decision stage."""
        deadline = getattr(self, "_feed_deadline", None)
        if deadline is not None and (
            getattr(self.market_adapter, "offline_mode", None) is False
            or getattr(self.market_adapter, "is_fixture", False)
        ):
            if time.monotonic() >= deadline:
                raise FeedUnavailable("FEED_UNAVAILABLE: stage deadline exhausted")
            cache = getattr(self, "_stage_market_cache", None)
            key = (method, symbol, json.dumps(kwargs, sort_keys=True))
            if cache is not None and key in cache:
                return cache[key]
            # Reuse the same observed intraday analysis bar, never as a book.
            # Do not bypass fixture adapter methods used by timeout regressions.
            if cache is not None and method == "get_latest_bar" and not getattr(self.market_adapter, "is_fixture", False):
                for (cached_method, cached_symbol, cached_kwargs), value in cache.items():
                    if (cached_method == "get_bars" and cached_symbol == symbol
                            and json.loads(cached_kwargs).get("timeframe", "1D") == kwargs.get("timeframe", "1D") and value):
                        return value[-1]
            value = call_with_deadline(self.market_adapter, method, (symbol,), kwargs, deadline)
            if cache is not None:
                cache[key] = value
            return value
        return getattr(self.market_adapter, method)(symbol, **kwargs)

    def _run_symbol(self, run_id: str, settings: PaperExperimentSettings, symbol: str) -> ExperimentDecision:
        # YahooAdapter's timeframe values describe chart ranges: 1D returns
        # 15-minute bars, while 1M returns daily bars.  Keep the execution
        # horizon explicit so an intraday strategy never evaluates daily bars
        # and a swing strategy never reacts to the final 15-minute candle.
        analysis_timeframe = "1D" if settings.mode == DecisionScope.INTRADAY else "1M"
        analysis_interval = "15m" if settings.mode == DecisionScope.INTRADAY else "1d"
        bar_limit = 32 if settings.mode == DecisionScope.INTRADAY else 8
        try:
            bars = self._market_call("get_bars",
                symbol,
                timeframe=analysis_timeframe,
                limit=bar_limit,
            )
        except TypeError:
            bars = self._market_call("get_bars", symbol)
            bars = bars[-bar_limit:]
        bar = bars[-1] if bars else None
        inputs: Dict[str, Any] = {
            "bar_count": len(bars),
            "analysis_timeframe": analysis_timeframe,
            "analysis_interval": analysis_interval,
            "source": bar.source if bar else None,
            "quality": bar.quality if bar else "missing",
            "is_stale": bar.is_stale if bar else True,
            "bar_timestamp": bar.timestamp.isoformat() if bar else None,
            "close": bar.close if bar else None,
            # Preserve the bars already fetched for analysis. A bar count and
            # last price alone give the CIO no trend/volume evidence. These
            # candles never qualify as later execution-book observations.
            "analysis_history": [b.model_dump(mode="json") for b in bars],
        }
        if os.getenv("CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS") != "1":
            now = self._now()
            if settings.mode == DecisionScope.SWING:
                slot = swing_session_slot(symbol, now)
                inputs["swing_session_slot"] = slot
                session_allowed = slot is not None
                closed_reason = "SWING_REVIEW_WINDOW_CLOSED"
            else:
                session_allowed = intraday_market_open(symbol, now)
                closed_reason = "MARKET_CLOSED_WEEKEND_OR_HOLIDAY"
            if not session_allowed:
                return self._decision(
                    run_id, settings, symbol, "NO_TRADE", closed_reason,
                    inputs, terminal_status="TERMINAL_NO_TRADE",
                )
        max_age = getattr(settings, "max_data_age_seconds", 172800.0)
        if not self._is_fresh(bar, max_age_seconds=max_age, intraday=settings.mode == DecisionScope.INTRADAY):
            return self._decision(run_id, settings, symbol, "NO_TRADE", "NO_TRADE_STALE_OR_SYNTHETIC_DATA", inputs, terminal_status="TERMINAL_NO_TRADE")
        assert bar is not None
        ledger = self.portfolio_manager.get_strategy_ledger(
            settings.strategy_id, settings.mode, settings.initial_cash
        )
        self.portfolio_manager.update_mark_to_market(bar)
        is_cio_owned = (
            settings.strategy_id == DYNAMIC_DESK_ID
            or self.require_cio_provider
            or getattr(settings, "cio_owned", False)
            or getattr(settings, "decision_lifecycle", None) == "cio_owned"
        )
        if is_cio_owned:
            return self._run_cio_decision_path(run_id, settings, symbol, bar, inputs)
        position = ledger.positions.get(symbol)
        if position and position.quantity > 0:
            exit_decision = self.exit_policy.evaluate(
                symbol=symbol,
                bucket=settings.mode,
                quantity=position.quantity,
                average_entry_price=position.average_entry_price,
                current_price=bar.close,
                now=self._now(),
            )
            inputs["exit_policy"] = exit_decision.model_dump(mode="json")
            if exit_decision.action != ExitAction.HOLD:
                self.paper_orders.event_store.append(EventEnvelope(
                    event_type=EventType.AUTOMATION_EXIT_TRIGGERED,
                    aggregate_id=symbol,
                    payload=exit_decision.model_dump(mode="json"),
                ))
            if exit_decision.action == ExitAction.HOLD:
                return self._decision(run_id, settings, symbol, "NO_TRADE", exit_decision.reason, inputs, terminal_status="TERMINAL_NO_TRADE")
            obs_age = (self._now() - bar.observed_at).total_seconds() if bar.observed_at else bar.delay_seconds
            effective_age = max(bar.delay_seconds, obs_age)
            req = PaperOrderRequest(
                currency=settings.base_currency,
                symbol=symbol,
                market=self._market(symbol),
                bucket=settings.mode,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=position.quantity,
                origin=OrderOrigin.STRATEGY,
                strategy_id=settings.strategy_id,
                strategy_version="runner-v2",
                reason=exit_decision.reason,
                data=PaperDataContext(source=bar.source, observed_at=bar.observed_at or self._now(), age_seconds=effective_age, last_price=bar.close, is_stale=False, is_fallback=False),
                audit_metadata={"runner": "autonomous_paper_v2", "exit_action": exit_decision.action.value},
            )
            try:
                order = self.paper_orders.submit(req)
            except ValueError as exc:
                return self._decision(run_id, settings, symbol, "NO_TRADE", f"NO_TRADE_EXIT_REJECTED:{exc}", inputs, terminal_status="TERMINAL_RISK_BLOCK")

            eligible_quote = self._find_eligible_later_quote(symbol, bar, max_age_seconds=max_age)
            consumed = self._consume_book(eligible_quote, bar, order) if eligible_quote else None
            if consumed is None:
                inputs["order_status"] = "PENDING"
                inputs["fill_pending_reason"] = "WAITING_FOR_LATER_AUTHORITATIVE_QUOTE"
                return self._decision(
                    run_id, settings, symbol, "SELL_PENDING", f"{exit_decision.reason}_PENDING_QUOTE",
                    inputs, order_id=order.order_id, quantity=position.quantity, terminal_status="NON_TERMINAL"
                )

            mkt = self._market(symbol)
            evidence, per_share_slip, effective_px = consumed
            trade_val = float(position.quantity) * effective_px
            fee = self.paper_orders.cost_config.calculate_fee(mkt, trade_val)
            tax = self.paper_orders.cost_config.calculate_tax(mkt, OrderSide.SELL, trade_val)
            total_slip = round(float(position.quantity) * per_share_slip, 4)
            fill = Fill(
                currency=order.currency,
                fill_id=f"fill-{uuid.uuid4()}",
                order_id=order.order_id,
                symbol=symbol,
                bucket=settings.mode,
                side=OrderSide.SELL,
                quantity=position.quantity,
                fill_price=effective_px,
                fee=fee,
                tax=tax,
                slippage=total_slip,
                timestamp=eligible_quote.timestamp,
                consumed_quote=evidence,
                quote_verification="BOOK_BOUND_TEST_ONLY" if evidence.is_fixture else "BOOK_BOUND",
                assumptions={
                    "execution": "local_paper_only",
                    "market": mkt.value,
                    "timing_assumption": "authoritative_later_quote_slippage_adjusted",
                    "exit_reason": exit_decision.reason,
                    "bar_timestamp": bar.timestamp.isoformat(),
                    "quote_timestamp": eligible_quote.timestamp.isoformat(),
                    "base_price": evidence.bid,
                    "slippage_bps": self.paper_orders.cost_config.slippage_bps,
                    "fee_rate": self.paper_orders.cost_config.fee_rate_tw if mkt == Market.TW else self.paper_orders.cost_config.fee_rate_us,
                    "tax_rate": self.paper_orders.cost_config.get_tax_rate(mkt, OrderSide.SELL),
                },
            )
            self.portfolio_manager.apply_fill(fill, settings.strategy_id)
            self.paper_orders.event_store.append(EventEnvelope(event_type=EventType.ORDER_FILLED, aggregate_id=order.order_id, payload=fill.model_dump(mode="json")))
            self._persist_portfolios()
            return self._decision(run_id, settings, symbol, "SELL_FILLED", exit_decision.reason, inputs, order_id=order.order_id, quantity=fill.quantity, price=effective_px, terminal_status="TERMINAL_FILLED")
        baseline = ledger.initial_cash
        if baseline - ledger.equity >= settings.max_daily_loss:
            return self._decision(run_id, settings, symbol, "NO_TRADE", "NO_TRADE_MAX_DAILY_LOSS", inputs, terminal_status="TERMINAL_RISK_BLOCK")
        open_positions = sum(1 for p in ledger.positions.values() if p.quantity > 0)
        if open_positions >= settings.max_open_positions:
            return self._decision(run_id, settings, symbol, "NO_TRADE", "NO_TRADE_MAX_OPEN_POSITIONS", inputs, terminal_status="TERMINAL_RISK_BLOCK")

        # Evaluate adaptive desk playbook restrictions
        desk_playbook = getattr(self, "_active_playbook_override", None)
        if desk_playbook is None:
            tw_open = intraday_market_open("2330.TW", self._now()) or (os.getenv("CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS") == "1")
            us_open = intraday_market_open("AAPL", self._now()) or (os.getenv("CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS") == "1")
            eq = ledger.equity if (ledger.equity and ledger.equity > 0) else ledger.initial_cash
            pos_mv = sum(p.market_value or 0 for p in ledger.positions.values() if p.quantity > 0)
            gross_exp = pos_mv / eq if eq > 0 else 0.0
            desk_playbook = self.select_active_playbook(
                tw_regime="TRENDING_BULL" if tw_open else "CLOSED",
                us_regime="TRENDING_BULL" if us_open else "CLOSED",
                data_quality="FRESH" if self._is_fresh(bar, max_age_seconds=max_age) else "STALE",
                tw_open=tw_open,
                us_open=us_open,
                gross_exposure=gross_exp,
                momentum=0.02,
                liquidity="HIGH",
                now=self._now(),
                cadence_seconds=settings.cadence_seconds,
            )

        inputs["active_playbook"] = desk_playbook["playbook_id"]
        inputs["playbook_name"] = desk_playbook["playbook_name"]
        inputs["playbook_rationale"] = desk_playbook["selection_rationale"]

        if not desk_playbook.get("allow_new_entries", True):
            return self._decision(
                run_id, settings, symbol, "NO_TRADE",
                f"NO_TRADE_PLAYBOOK_CAPITAL_PRESERVATION:{desk_playbook['playbook_id']}",
                inputs, terminal_status="TERMINAL_RISK_BLOCK"
            )

        # CIO-owned lifecycle: AGY is engineering worker, NOT investment decision owner.
        is_cio_owned = (
            settings.strategy_id == DYNAMIC_DESK_ID
            or self.require_cio_provider
            or getattr(settings, "cio_owned", False)
            or getattr(settings, "decision_lifecycle", None) == "cio_owned"
        )
        if is_cio_owned:
            return self._run_cio_decision_path(run_id, settings, symbol, bar, inputs)

        previous = bars[-2].close if len(bars) >= 2 else bar.close
        if settings.mode == DecisionScope.INTRADAY:
            signal = len(bars) >= 4 and bar.close >= max(item.high for item in bars[-4:-1])
            reason = "INTRADAY_BREAKOUT_CONFIRMED" if signal else "NO_TRADE_INTRADAY_BREAKOUT_NOT_CONFIRMED"
        else:
            signal = bar.close > previous
            reason = "SWING_CLOSE_ABOVE_PREVIOUS" if signal else "NO_TRADE_SWING_RULE_NOT_CONFIRMED"
        inputs.update({"previous_close": previous, "mode_rule": settings.mode.value, "signal": signal})
        if not signal:
            return self._decision(run_id, settings, symbol, "NO_TRADE", reason, inputs, terminal_status="TERMINAL_NO_TRADE")
        llm_review = self.llm_policy.review(settings.mode, {**inputs, "symbol": symbol, "proposed_action": "BUY"})
        inputs["llm_review"] = llm_review.model_dump(mode="json")
        self.paper_orders.event_store.append(EventEnvelope(
            event_type=EventType.LLM_REVIEW_RECORDED,
            aggregate_id=symbol,
            payload=llm_review.model_dump(mode="json"),
        ))
        if settings.mode == DecisionScope.INTRADAY and llm_review.verdict != LLMVerdict.ALLOW:
            return self._decision(run_id, settings, symbol, "NO_TRADE", f"NO_TRADE_LLM_VETO:{llm_review.reason}", inputs, terminal_status="TERMINAL_RISK_BLOCK")
        quantity = math.floor(settings.max_position_notional / bar.close)
        if quantity < 1:
            return self._decision(run_id, settings, symbol, "NO_TRADE", "NO_TRADE_MAX_POSITION_NOTIONAL_BELOW_ONE_SHARE", inputs, terminal_status="TERMINAL_RISK_BLOCK")
        quantity = min(quantity, math.floor(ledger.cash / bar.close))
        if quantity < 1:
            return self._decision(run_id, settings, symbol, "NO_TRADE", "NO_TRADE_INSUFFICIENT_PAPER_CASH", inputs, terminal_status="TERMINAL_RISK_BLOCK")
        obs_age = (self._now() - bar.observed_at).total_seconds() if bar.observed_at else bar.delay_seconds
        effective_age = max(bar.delay_seconds, obs_age)
        req = PaperOrderRequest(
            currency=settings.base_currency,
            symbol=symbol,
            market=self._market(symbol),
            bucket=settings.mode,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=float(quantity),
            origin=OrderOrigin.STRATEGY,
            strategy_id=settings.strategy_id,
            strategy_version="runner-v2",
            reason=reason,
            data=PaperDataContext(source=bar.source, observed_at=bar.observed_at or self._now(), age_seconds=effective_age, last_price=bar.close, is_stale=False, is_fallback=False),
            audit_metadata={"runner": "autonomous_paper_v2", "signal_rule": settings.mode.value, "llm_authority": "veto_or_review_only"},
        )
        try:
            order = self.paper_orders.submit(req)
        except ValueError as exc:
            return self._decision(run_id, settings, symbol, "NO_TRADE", f"NO_TRADE_ORDER_REJECTED:{exc}", inputs, terminal_status="TERMINAL_RISK_BLOCK")

        eligible_quote = self._find_eligible_later_quote(symbol, bar, max_age_seconds=max_age)
        consumed = self._consume_book(eligible_quote, bar, order) if eligible_quote else None
        if consumed is None:
            inputs["order_status"] = "PENDING"
            inputs["fill_pending_reason"] = "WAITING_FOR_LATER_AUTHORITATIVE_QUOTE"
            return self._decision(
                run_id, settings, symbol, "BUY_PENDING", f"{reason}_PENDING_QUOTE",
                inputs, order_id=order.order_id, quantity=float(quantity), terminal_status="NON_TERMINAL"
            )

        mkt = self._market(symbol)
        evidence, per_share_slip, effective_px = consumed
        projected = float(quantity) * effective_px
        if (projected > settings.max_position_notional
                or projected + self.paper_orders.cost_config.calculate_fee(mkt, projected) > ledger.cash):
            return self._decision(run_id, settings, symbol, "BUY_PENDING", "LATER_BOOK_EXCEEDS_CASH_OR_NOTIONAL", inputs,
                                  order_id=order.order_id, terminal_status="NON_TERMINAL")
        trade_val = float(quantity) * effective_px
        fee = self.paper_orders.cost_config.calculate_fee(mkt, trade_val)
        tax = self.paper_orders.cost_config.calculate_tax(mkt, OrderSide.BUY, trade_val)
        total_slip = round(float(quantity) * per_share_slip, 4)
        fill = Fill(
            currency=order.currency,
            fill_id=f"fill-{uuid.uuid4()}",
            order_id=order.order_id,
            symbol=symbol,
            bucket=settings.mode,
            side=OrderSide.BUY,
            quantity=float(quantity),
            fill_price=effective_px,
            fee=fee,
            tax=tax,
            slippage=total_slip,
            timestamp=eligible_quote.timestamp,
            consumed_quote=evidence,
            quote_verification="BOOK_BOUND_TEST_ONLY" if evidence.is_fixture else "BOOK_BOUND",
            assumptions={
                "execution": "local_paper_only",
                "market": mkt.value,
                "timing_assumption": "authoritative_later_quote_slippage_adjusted",
                "bar_timestamp": bar.timestamp.isoformat(),
                "quote_timestamp": eligible_quote.timestamp.isoformat(),
                "base_price": evidence.ask,
                "slippage_bps": self.paper_orders.cost_config.slippage_bps,
                "fee_rate": self.paper_orders.cost_config.fee_rate_tw if mkt == Market.TW else self.paper_orders.cost_config.fee_rate_us,
                "tax_rate": self.paper_orders.cost_config.get_tax_rate(mkt, OrderSide.BUY),
            },
        )
        self.portfolio_manager.apply_fill(fill, settings.strategy_id)
        self.paper_orders.event_store.append(EventEnvelope(event_type=EventType.ORDER_FILLED, aggregate_id=order.order_id, payload=fill.model_dump(mode="json")))
        self._persist_portfolios()
        inputs["fill_price"] = effective_px
        return self._decision(run_id, settings, symbol, "BUY_FILLED", reason, inputs, order_id=order.order_id, quantity=float(quantity), price=effective_px, terminal_status="TERMINAL_FILLED")

    def _run_cio_decision_path(
        self,
        run_id: str,
        settings: PaperExperimentSettings,
        symbol: str,
        bar: Bar,
        inputs: Dict[str, Any],
    ) -> ExperimentDecision:
        """Run decision path for CIO-owned desks. Resolves self._now() without undefined local variable."""
        now = self._now()
        session_gate = None
        observation = None
        # 1. Check for staged CIO packet for this symbol
        packet = self._staged_packets.pop(symbol, None)

        # 2. If no staged packet, query the configured CIO executor if present
        if packet is None and self.cio_executor is not None:
            if hasattr(self.cio_executor, "is_available") and not self.cio_executor.is_available():
                inputs["decision_lifecycle"] = "BLOCKED_CIO_EXECUTOR_UNAVAILABLE"
                self.paper_orders.event_store.append(EventEnvelope(
                    event_type=EventType.CIO_DECISION_BLOCKED,
                    aggregate_id=symbol,
                    payload={"symbol": symbol, "reason": "CIO_EXECUTOR_UNAVAILABLE", "desk": settings.strategy_id},
                ))
                return self._decision(
                    run_id, settings, symbol, "NO_TRADE",
                    "BLOCKED_CIO_EXECUTOR_UNAVAILABLE: Configured startup CIO executor is unavailable on host runtime.",
                    inputs, terminal_status="TERMINAL_RISK_BLOCK"
                )
            inputs["elapsed_non_actions"] = self.evaluate_elapsed_non_actions(symbol)
            inputs["official_research_acquisition"] = self.official_research.acquire(
                [symbol], self.research_reader, now
            )
            ctx_req = self.build_decision_context_request([symbol])
            ctx_req.predecision_snapshot["analysis_history"] = inputs.get("analysis_history", [])
            ctx_req.predecision_snapshot["analysis_history_authority"] = "ANALYSIS_ONLY_NOT_EXECUTION_BOOK"
            ctx_req.predecision_snapshot["regime_evidence"] = {
                "source": "market_calendar_placeholder",
                "verified_market_regime": False,
                "restriction": "Do not treat TRENDING_BULL/calendar-open as verified bullish market evidence.",
            }
            session_gate = None
            observation = None
            if self.material_gate_enabled:
                from cio_market_lab.engine.cio_session import CIOSessionHistory, MaterialDeltaGate, FrozenDecisionContext
                history = CIOSessionHistory(self.runtime_dir / "cio_session", self.cio_session_id)
                session_gate = MaterialDeltaGate(history)
                if self.material_observation_provider is None:
                    inputs["material_delta_gate"] = {"status": "BLOCKED_CONTEXT_OR_DATA_UNAVAILABLE", "should_call": False}
                    return self._decision(run_id, settings, symbol, "NO_TRADE", "CIO material gate: BLOCKED_CONTEXT_OR_DATA_UNAVAILABLE", inputs, terminal_status="TERMINAL_RISK_BLOCK")
                try:
                    observation = self.material_observation_provider(symbol=symbol, inputs=inputs, now=now)
                    if isinstance(observation, dict):
                        observation = {**observation, "symbol": symbol, "session_id": self.cio_session_id}
                except Exception:
                    observation = None
                if not isinstance(observation, dict) or not observation:
                    inputs["material_delta_gate"] = {"status": "BLOCKED_DATA_UNAVAILABLE", "should_call": False}
                    return self._decision(run_id, settings, symbol, "NO_TRADE", "CIO material gate: BLOCKED_DATA_UNAVAILABLE", inputs, terminal_status="TERMINAL_RISK_BLOCK")
                context = self.frozen_decision_context
                if context is None and hasattr(self.material_observation_provider, "context_for"):
                    context = self.material_observation_provider.context_for(symbol)
                    if context is not None:
                        import hashlib
                        identity = context.context_id if hasattr(context, "context_id") else context["context_id"]
                        contexts = history.root / "contexts"
                        contexts.mkdir(exist_ok=True)
                        history.context_file = contexts / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
                # A persisted frozen context is authoritative across restarts; fresh packet
                # timestamps must not rewrite it or create spurious session changes.
                if history.context_file.exists():
                    context = history.load_context()
                if context is None:
                    inputs["material_delta_gate"] = {"status": "BLOCKED_CONTEXT_OR_DATA_UNAVAILABLE", "should_call": False}
                    return self._decision(run_id, settings, symbol, "NO_TRADE", "CIO material gate: BLOCKED_CONTEXT_OR_DATA_UNAVAILABLE", inputs, terminal_status="TERMINAL_RISK_BLOCK")
                if not isinstance(context, FrozenDecisionContext): context = FrozenDecisionContext.model_validate(context)
                inputs["frozen_decision_context"] = context.model_dump(mode="json")
                history.freeze(context)
                if "entry_edge" in observation:
                    # Frozen approved thresholds remain authoritative after restart.
                    frozen_rules = context.model_dump(mode="json")
                    observation = {**observation, "buy_zone": frozen_rules["entry_zone"],
                                   "invalidation_condition": frozen_rules["invalidation"].get("condition")}
                    observation = apply_verified_quote_edges(
                        observation, self.get_durable_quote(symbol), now,
                        allow_fixture=self.allow_fixture_quotes,
                    )
                    inputs["quote_edges"] = observation
                    if observation.get("quote_edge_status") == "BLOCKED_QUOTE_UNAVAILABLE":
                        inputs["material_delta_gate"] = {"status": "BLOCKED_QUOTE_UNAVAILABLE", "should_call": False}
                        return self._decision(run_id, settings, symbol, "NO_TRADE",
                            "CIO material gate: BLOCKED_QUOTE_UNAVAILABLE", inputs,
                            terminal_status="TERMINAL_RISK_BLOCK")
                gate = session_gate.evaluate(observation, data_available=inputs.get("material_data_available", True))
                inputs["material_delta_gate"] = gate
                if not gate["should_call"]:
                    return self._decision(run_id, settings, symbol, "NO_TRADE", f"CIO material gate: {gate['status']}", inputs, terminal_status="TERMINAL_RISK_BLOCK")
                ctx_req = ctx_req.model_copy(update={"prior_lessons": [*ctx_req.prior_lessons, {"session_id": history.session_id, "persisted_history": history.history(), "frozen_context": history.load_context().model_dump(mode="json"), "material_observation": observation, "observed_lessons": self.learning_store.retrieve_context_lessons(symbol=symbol, as_of=now, limit=5), "observed_outcomes": self.learning_store.retrieve_past_outcomes(symbol=symbol, as_of=now, limit=5)}]})
            try:
                # The model has its own bound; network execution starts a new
                # feed stage after model latency, with no pre-model quote reuse.
                self._feed_deadline = None
                packet = self.cio_executor.request_decision(ctx_req)
                self._feed_deadline = time.monotonic() + self.FEED_STAGE_SECONDS
                self._stage_market_cache = {}
                self.last_receipt = getattr(self.cio_executor, "last_receipt", None)
            except Exception as exc:
                inputs["safe_fallback"] = "SAFE_FALLBACK_UNSUPPORTED_RESPONSE"
                return self._decision(
                    run_id, settings, symbol, "NO_TRADE",
                    f"SAFE_FALLBACK_UNSUPPORTED_RESPONSE: Unsupported CIO response or execution error: {exc}",
                    inputs, terminal_status="TERMINAL_RISK_BLOCK"
                )

        # 3. If no CIO decision packet exists, fail closed with explicit BLOCKED status
        if packet is None:
            inputs["decision_lifecycle"] = "BLOCKED_NO_CIO_DECISION_PROVIDER"
            inputs["prior_lessons"] = self.learning_store.retrieve_context_lessons(symbol=symbol, as_of=now, limit=3)
            self.paper_orders.event_store.append(EventEnvelope(
                event_type=EventType.CIO_DECISION_BLOCKED,
                aggregate_id=symbol,
                payload={"symbol": symbol, "reason": "NO_CIO_DECISION_PROVIDER", "desk": settings.strategy_id},
            ))
            return self._decision(
                run_id, settings, symbol, "NO_TRADE",
                "BLOCKED_NO_CIO_DECISION_PROVIDER: AGY engineering worker cannot generate investment verdicts. No configured CIO decision provider.",
                inputs, terminal_status="TERMINAL_RISK_BLOCK"
            )

        if session_gate is not None and observation is not None:
            valid, validation_reason = validate_cio_packet(packet, now=now, processed_case_ids=set())
            if valid and packet.selected_instrument == symbol:
                receipt = self.last_receipt
                session_gate.record_call(observation, {"success": True, "provider": getattr(receipt, "provider_id", None), "model": getattr(receipt, "model_id", None), "fixture": bool(getattr(packet, "is_fixture", False)), "case_id": packet.case_id})

        # 4. Execute the externally supplied CIO packet
        return self._execute_cio_packet(run_id, settings, packet, bar, inputs)

    def resolve_canonical_valuation_fx(
        self,
        currency: str = "USD",
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Resolve canonical valuation FX source shared by accounting.

        Order of precedence:
        1. Authoritative market FX quote (with observed_at, source_timestamp, source)
        2. Configured assumption shared by accounting (is_simulated=True, label="configured_assumption")
        3. Missing (remains missing, never silently defaulting to 32.0)
        """
        now = now or self._now()
        # Retained receipt files survive runner reconstruction. As-of selection
        # validates BOTH acquisition time and source date; never backdate intake.
        if currency == "USD":
            from cio_market_lab.engine.historical_fx import FxRateReceipt, lookup_rate_as_of, FxReportingBlocked
            from datetime import date
            from decimal import Decimal
            receipts = list(getattr(self, "canonical_fx_receipts", ()))
            path = self.runtime_dir / "canonical_fx_receipts.json"
            if path.exists():
                try:
                    for raw in json.loads(path.read_text(encoding="utf-8")):
                        value = dict(raw)
                        value["rate"] = Decimal(str(value["rate"]))
                        value["observed_at"] = datetime.fromisoformat(value["observed_at"])
                        value["source_date"] = date.fromisoformat(value["source_date"])
                        receipts.append(FxRateReceipt(**value))
                except (ValueError, TypeError, KeyError):
                    return {"status": "MISSING", "rate": None, "label": "INVALID_FX_RECEIPT"}
            if receipts:
                try:
                    selected = lookup_rate_as_of(receipts, "USD/TWD", now,
                        getattr(self, "canonical_fx_max_age", timedelta(days=5)))
                    if selected.provenance == "TEST_ONLY" and not getattr(self, "allow_test_only_fx", False):
                        raise FxReportingBlocked("TEST_ONLY FX not authorized")
                    return {
                        "status": "AVAILABLE", "rate": float(selected.rate), "currency": "USD",
                        "source": selected.source, "source_tier": selected.provenance,
                        "source_url": selected.source_url, "label": "historical_fx_receipt",
                        "is_simulated": selected.provenance == "TEST_ONLY",
                        "observed_at": selected.observed_at.isoformat(),
                        "source_timestamp": selected.source_date.isoformat(),
                    }
                except FxReportingBlocked as exc:
                    return {"status": "MISSING", "rate": None, "label": "FX_UNAVAILABLE", "reason": str(exc)}

        from math import isfinite
        # 1. Check market data adapter for authoritative FX quote
        for fx_sym in [f"{currency}TWD=X", f"{currency}/TWD", f"{currency}TWD"]:
            try:
                quote = self.get_durable_quote(fx_sym)
                if (
                    quote
                    and quote.source != "missing"
                    and quote.observed_at <= now
                    and (quote.bar_time or quote.observed_at) <= now
                    and now - quote.observed_at <= getattr(self, "canonical_fx_max_age", timedelta(days=5))
                    and now - (quote.bar_time or quote.observed_at) <= getattr(self, "canonical_fx_max_age", timedelta(days=5))
                    and quote.last_price is not None
                    and quote.last_price > 0
                    and isfinite(quote.last_price)
                    and ("test_only" not in quote.source.lower() or getattr(self, "allow_test_only_fx", False))
                    and not quote.is_stale
                    and not quote.is_synthetic
                    and "synthetic" not in (quote.quality or "").lower()
                    and "fallback" not in (quote.source or "").lower()
                    and "fixture" not in (quote.source or "").lower()
                    and "replay" not in (quote.source or "").lower()
                ):
                    return {
                        "status": "AVAILABLE",
                        "currency": currency,
                        "reporting_currency": "TWD",
                        "rate": quote.last_price,
                        "source": quote.source,
                        "source_tier": "market",
                        "label": "market",
                        "is_simulated": "test_only" in quote.source.lower(),
                        "observed_at": quote.observed_at.isoformat() if quote.observed_at else None,
                        "source_timestamp": quote.bar_time.isoformat() if quote.bar_time else (quote.observed_at.isoformat() if quote.observed_at else None),
                    }
            except Exception:
                pass

        # Only explicitly authorized TEST_ONLY runtimes may use assumptions.
        if not getattr(self, "allow_test_only_fx", False):
            return {"status": "MISSING", "rate": None, "source": "missing", "source_tier": "missing",
                    "label": "TEST_ONLY_ASSUMPTIONS_DISABLED", "is_simulated": False}

        # 2. Check accounting configured assumption
        # Check runner-level configured_fx_rates or fx_rates
        runner_fx = getattr(self, "configured_fx_rates", None) or getattr(self, "fx_rates", None)
        if isinstance(runner_fx, dict) and currency in runner_fx and runner_fx[currency] is not None:
            return {
                "status": "AVAILABLE",
                "currency": currency,
                "reporting_currency": "TWD",
                "rate": float(runner_fx[currency]),
                "source": "configured_assumption",
                "source_tier": "configured_assumption",
                "label": "configured_assumption",
                "is_simulated": True,
                "observed_at": None,
                "source_timestamp": None,
            }

        # Check experiments configured FX
        for s_id, s_cfg in self.paper_orders.experiments.items():
            cfg_rates = getattr(s_cfg, "fx_rates", None)
            if isinstance(cfg_rates, dict) and currency in cfg_rates and cfg_rates[currency] is not None:
                return {
                    "status": "AVAILABLE",
                    "currency": currency,
                    "reporting_currency": "TWD",
                    "rate": float(cfg_rates[currency]),
                    "source": "configured_assumption",
                    "source_tier": "configured_assumption",
                    "label": "configured_assumption",
                    "is_simulated": True,
                    "observed_at": None,
                    "source_timestamp": None,
                }
            if s_cfg.base_currency == currency and s_cfg.fx_to_reporting and s_cfg.fx_to_reporting > 1.0:
                return {
                    "status": "AVAILABLE",
                    "currency": currency,
                    "reporting_currency": "TWD",
                    "rate": float(s_cfg.fx_to_reporting),
                    "source": "configured_assumption",
                    "source_tier": "configured_assumption",
                    "label": "configured_assumption",
                    "is_simulated": True,
                    "observed_at": None,
                    "source_timestamp": None,
                }

        # 3. Missing source: remains missing! Never silently 32.0.
        return {
            "status": "MISSING",
            "currency": currency,
            "reporting_currency": "TWD",
            "rate": None,
            "source": "missing",
            "source_tier": "missing",
            "label": "missing",
            "is_simulated": False,
            "observed_at": None,
            "source_timestamp": None,
        }

    def build_decision_context_request(self, symbols: Optional[List[str]] = None, read_only: bool = False) -> CIODecisionContextRequest:
        """Construct structured decision context for CIO evaluation."""
        now = self._now()
        settings = self.paper_orders.experiment_for(DYNAMIC_DESK_ID)
        target_symbols = symbols or list(settings.universe)
        snap = self.get_canonical_team_ops(is_read_only=True) if read_only else None
        quotes: Dict[str, Any] = {}
        for s in target_symbols:
            if read_only:
                stored = (snap or {}).get("quotes", {}).get(s)
                if stored:
                    q = DurableQuoteSnapshot.model_validate(stored)
                    event_at = q.bar_time or q.observed_at
                    if event_at is not None:
                        event_at = event_at if event_at.tzinfo else event_at.replace(tzinfo=timezone.utc)
                    threshold = 1800.0  # quotes are executable observations, not historical bars
                    quotes[s] = q.model_copy(update={
                        "is_stale": q.is_stale or event_at is None or not 0 <= (now - event_at).total_seconds() <= threshold,
                        "age_seconds": max(0.0, (now - event_at).total_seconds()) if event_at else 999999.0,
                    }).model_dump(mode="json")
            else:
                q = self.get_durable_quote(s)
                if q:
                    quotes[s] = q.model_dump(mode="json")

        if snap is None and read_only:
            raise RuntimeError("SNAPSHOT_UNAVAILABLE: read-only context cannot regenerate canonical NAV")
        if snap is None:
            snap = self.generate_canonical_team_ops(now, refresh_symbols=set(target_symbols))
        sym_focus = symbols[0] if symbols and len(symbols) == 1 else None
        prior_lessons = self.learning_store.retrieve_context_lessons(symbol=sym_focus, as_of=now, limit=5)
        past_outcomes = self.learning_store.retrieve_past_outcomes(symbol=sym_focus, as_of=now, limit=5)
        rejected = self.learning_store.retrieve_rejected_opportunities(as_of=now, limit=5)

        verified_research, research_gaps = self.research_reader.get_verified_research_for_symbols(
            target_symbols, now=now
        )

        from cio_market_lab.engine.capabilities import get_derivative_capabilities_report
        cap_report = get_derivative_capabilities_report()
        tool_eligibility = {
            "CASH_EQUITY": {"status": "AVAILABLE", "description": "TWSE/TPEx and US spot cash equities"},
            "SPOT_ETF": {"status": "AVAILABLE", "description": "TW and US spot ETFs"},
            "LONG_PREMIUM_OPTIONS": {
                "status": "UNAVAILABLE",
                "exact_gaps": cap_report.capabilities.get("LONG_PREMIUM_OPTIONS", {}).exact_gaps
                if hasattr(cap_report.capabilities.get("LONG_PREMIUM_OPTIONS"), "exact_gaps")
                else ["Missing option quotes and dynamic series registry"],
            },
            "FUTURES": {
                "status": "UNAVAILABLE",
                "exact_gaps": cap_report.capabilities.get("FUTURES", {}).exact_gaps
                if hasattr(cap_report.capabilities.get("FUTURES"), "exact_gaps")
                else ["Missing futures feed and point multipliers registry"],
            },
            "CASH": {"status": "AVAILABLE", "description": "Hold cash, zero market risk"},
        }

        from cio_market_lab.engine.team_ops import TeamOpsSnapshotBuilder
        instrument_currencies = {
            s: TeamOpsSnapshotBuilder.currency_for_symbol(s) for s in target_symbols
        }

        if read_only:
            saved_fx = snap.get("portfolio", {}).get("fx_accounting", {})
            fx_at = saved_fx.get("source_timestamp")
            try:
                fx_age = (now - datetime.fromisoformat(fx_at)).total_seconds() if fx_at else float("inf")
            except (TypeError, ValueError):
                fx_age = float("inf")
            valid_fx = saved_fx.get("usd_twd_rate") is not None and not saved_fx.get("is_simulated") and 0 <= fx_age <= 1800
            usd_fx_info = {"rate": saved_fx.get("usd_twd_rate") if valid_fx else None,
                           "source": saved_fx.get("source") if valid_fx else "missing_or_stale_snapshot",
                           "source_tier": "market" if valid_fx else "missing",
                           "label": "market" if valid_fx else "missing",
                           "is_simulated": False,
                           "observed_at": saved_fx.get("observed_at") if valid_fx else None,
                           "source_timestamp": fx_at if valid_fx else None}
        else:
            usd_fx_info = self.resolve_canonical_valuation_fx("USD", now)
        fx_rates: Dict[str, float] = {"TWD": 1.0}
        usd_twd_rate: Optional[float] = usd_fx_info.get("rate")
        if usd_twd_rate is not None:
            fx_rates["USD"] = usd_twd_rate

        max_notional_twd = 500000.0
        max_loss_twd = 50000.0
        if usd_twd_rate is not None and usd_twd_rate > 0:
            max_notional_usd = round(max_notional_twd / usd_twd_rate, 2)
            max_loss_usd = round(max_loss_twd / usd_twd_rate, 2)
        else:
            max_notional_usd = None
            max_loss_usd = None

        sizing_gaps: List[Dict[str, Any]] = []
        for s in target_symbols:
            c = instrument_currencies.get(s, "TWD")
            if c == "USD" and usd_twd_rate is None:
                gap_entry = {
                    "symbol": s,
                    "currency": "USD",
                    "gap_status": "EXPLICIT_SIZING_GAP",
                    "gap_type": "MISSING_VALUATION_FX",
                    "reason": "Missing evidenced USD/TWD valuation FX source; USD sizing cannot be authorized without evidenced rate",
                }
                sizing_gaps.append(gap_entry)
                research_gaps.append(gap_entry)

        fx_accounting = {
            "reporting_currency": "TWD",
            "rates": fx_rates,
            "rates_to_reporting": fx_rates,
            "usd_twd_rate": usd_twd_rate,
            "source": usd_fx_info.get("source", "missing"),
            "source_tier": usd_fx_info.get("source_tier", "missing"),
            "source_label": usd_fx_info.get("label", "missing"),
            "is_simulated": usd_fx_info.get("is_simulated", False),
            "observed_at": usd_fx_info.get("observed_at"),
            "source_timestamp": usd_fx_info.get("source_timestamp"),
            "sizing_gaps": sizing_gaps,
            "sizing_guidance": {
                "reporting_currency": "TWD",
                "instrument_currencies": instrument_currencies,
                "max_position_notional_twd": max_notional_twd,
                "max_position_notional_usd": max_notional_usd,
                "max_daily_loss_twd": max_loss_twd,
                "max_daily_loss_usd": max_loss_usd,
                "sizing_gaps": sizing_gaps,
            },
        }

        tactical_risk_limits = {
            "max_position_notional": max_notional_twd,
            "max_daily_loss": max_loss_twd,
            "reporting_currency": "TWD",
            "max_position_notional_twd": max_notional_twd,
            "max_position_notional_usd": max_notional_usd,
            "max_daily_loss_twd": max_loss_twd,
            "max_daily_loss_usd": max_loss_usd,
            "fx_rate_usd_twd": usd_twd_rate,
        }

        portfolio_data = snap.get("portfolio", {})
        if isinstance(portfolio_data, dict):
            portfolio_data.setdefault("fx_rates", fx_rates)
            portfolio_data.setdefault("fx_accounting", fx_accounting)

        pre_snap = {
            "canonical_portfolio": portfolio_data,
            "quotes": quotes,
            "posture": snap.get("posture", {}),
            "tactical_risk_limits": tactical_risk_limits,
            "fx_rates": fx_rates,
            "fx_accounting": fx_accounting,
            "as_of": now.isoformat(),
        }

        return CIODecisionContextRequest(
            request_id=f"ctx-req-{uuid.uuid4()}",
            timestamp=now,
            universe=target_symbols,
            canonical_portfolio=portfolio_data,
            verified_quotes=quotes,
            verified_research=verified_research,
            research_gaps=research_gaps,
            prior_lessons=prior_lessons,
            past_outcomes=past_outcomes,
            rejected_opportunities=rejected,
            tactical_risk_limits=tactical_risk_limits,
            desk_posture=snap.get("posture", {}),
            holding_horizons_available=["intraday", "swing", "long_term", "cash"],
            tool_eligibility=tool_eligibility,
            predecision_snapshot=pre_snap,
            predecision_version="v1",
            fx_rates=fx_rates,
            fx_accounting=fx_accounting,
            sizing_gaps=sizing_gaps,
        )

    def submit_cio_packet(
        self,
        packet: CIODecisionPacket,
        run_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
    ) -> ExperimentDecision:
        """Directly submit an externally supplied CIO decision packet."""
        if getattr(self, "is_read_only", False):
            raise PermissionError("RUNNER_READONLY_INSTANCE: Cannot submit CIO packet on read-only instance")
        if getattr(self, "writer_lock", None) is not None:
            with self.writer_lock:
                return self._submit_cio_packet_locked(packet, run_id, strategy_id)
        return self._submit_cio_packet_locked(packet, run_id, strategy_id)

    def _submit_cio_packet_locked(
        self,
        packet: CIODecisionPacket,
        run_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
    ) -> ExperimentDecision:
        strat_id = strategy_id or packet.conditions.get("strategy_id") or DYNAMIC_DESK_ID
        settings = self.paper_orders.experiment_for(strat_id)
        r_id = run_id or f"run-cio-{uuid.uuid4()}"
        now = self._now()
        identity, _ = self._packet_instrument_identity(packet)
        bar = self._market_call("get_latest_bar", packet.selected_instrument) if identity == "SPOT" else None
        if bar is None and identity == "SPOT":
            return self._decision(
                r_id, settings, packet.selected_instrument, "NO_TRADE",
                "NO_TRADE_MISSING_AUTHORITATIVE_BAR", {"case_id": packet.case_id},
                terminal_status="TERMINAL_RISK_BLOCK",
            )
        inputs = {
            "source": "external_cio_packet",
            "case_id": packet.case_id,
            "action": packet.action,
            "thesis": packet.thesis,
            "close": bar.close if bar is not None else None,
        }
        return self._execute_cio_packet(r_id, settings, packet, bar, inputs)

    def _execute_cio_packet(
        self,
        run_id: str,
        settings: PaperExperimentSettings,
        packet: CIODecisionPacket,
        bar: Optional[Bar],
        inputs: Dict[str, Any],
    ) -> ExperimentDecision:
        now = self._now()
        existing_rec = self.learning_store.get_record(packet.case_id)
        case_order = self.paper_orders.find_order(existing_rec.order_id) if existing_rec and existing_rec.order_id else None
        if existing_rec is not None and existing_rec.packet.model_dump(mode="json") != packet.model_dump(mode="json"):
            return self._decision(run_id, settings, packet.selected_instrument, "NO_TRADE",
                "CIO_PACKET_REJECTED:CASE_AUTHORIZATION_MISMATCH", inputs, terminal_status="TERMINAL_RISK_BLOCK")
        requested_quantity = case_order.remaining_quantity if case_order is not None else float(packet.quantity or 0.0)

        # 1. Validate packet (excluding self from duplicate check if progressing an existing pending case)
        effective_processed = self.learning_store.processed_case_ids
        if existing_rec is not None and (existing_rec.fill is None or (case_order is not None and case_order.status == OrderStatus.PARTIALLY_FILLED)):
            effective_processed = effective_processed - {packet.case_id}
        is_valid, validation_err = validate_cio_packet(
            packet, now=now, processed_case_ids=effective_processed
        )
        if not is_valid:
            inputs["cio_packet_rejected"] = validation_err
            return self._decision(
                run_id, settings, packet.selected_instrument, "NO_TRADE",
                f"CIO_PACKET_REJECTED:{validation_err}",
                inputs, terminal_status="TERMINAL_RISK_BLOCK"
            )

        # A CIO-owned desk must honor a capital-preservation veto even when
        # the packet arrives through the staged/provider path (which bypasses
        # the legacy local-signal playbook gate in _run_symbol).
        active_playbook = getattr(self, "_active_playbook_override", None)
        if packet.action.upper() == "BUY" and active_playbook is not None and not active_playbook.get("allow_new_entries", True):
            inputs["active_playbook"] = active_playbook["playbook_id"]
            return self._decision(
                run_id, settings, packet.selected_instrument, "NO_TRADE",
                f"NO_TRADE_PLAYBOOK_CAPITAL_PRESERVATION:{active_playbook['playbook_id']}",
                inputs, terminal_status="TERMINAL_RISK_BLOCK",
            )

        # 2. Check capability gaps (Constraint 15) & genuine caller-supplied parameters
        identity, instrument_type = self._packet_instrument_identity(packet)
        if identity in {"UNKNOWN", "CONFLICT"}:
            inputs["instrument_identity"] = identity
            return self._decision(
                run_id, settings, packet.selected_instrument, "NO_TRADE",
                f"NO_TRADE_INSTRUMENT_IDENTITY_{identity}", inputs,
                terminal_status="TERMINAL_RISK_BLOCK",
            )
        if identity == "SPOT" and (bar is None or bar.symbol != packet.selected_instrument):
            return self._decision(
                run_id, settings, packet.selected_instrument, "NO_TRADE",
                "NO_TRADE_MISSING_AUTHORITATIVE_BAR", inputs,
                terminal_status="TERMINAL_RISK_BLOCK",
            )
        if identity == "DERIVATIVE":
            caller_spec = packet.conditions.get("contract_spec")
            caller_quote = packet.conditions.get("derivative_quote")

            # Check if caller genuinely supplied full contract identity, quote provenance/timestamp, bid/ask, multiplier, margin/cost assumptions, and expiry constraints
            spec_valid = False
            quote_valid = False
            if isinstance(caller_spec, dict):
                spec_valid = all(caller_spec.get(k) for k in ("symbol", "underlying_symbol", "instrument_type", "expiry", "multiplier", "tick_size"))

            if isinstance(caller_quote, dict):
                has_prov = isinstance(caller_quote.get("provenance"), dict) and bool(caller_quote["provenance"].get("authority"))
                has_ts = bool(caller_quote.get("timestamp"))
                has_two_sided = caller_quote.get("bid") is not None and caller_quote.get("ask") is not None
                not_stale = not caller_quote.get("is_stale", False)
                not_fixture = not caller_quote.get("is_fixture", False) or (packet.is_fixture and self.derivative_lifecycle.fixture_mode)
                if has_prov and has_ts and has_two_sided and not_stale and not_fixture:
                    quote_valid = True

            if not (spec_valid and quote_valid):
                cap_report = get_derivative_capabilities_report()
                cap_key = "FUTURES" if instrument_type == "FUTURE" else "LONG_PREMIUM_OPTIONS"
                cap_detail = cap_report.capabilities.get(cap_key)
                gap_msg = f"CAPABILITY_UNAVAILABLE:{cap_key} permitted by client for research but unavailable: {'; '.join(cap_detail.exact_gaps) if cap_detail else 'Missing contract identity'}"
                if cap_detail:
                    inputs["capability_gap"] = cap_detail.model_dump(mode="json")
                return self._decision(
                    run_id, settings, packet.selected_instrument, "NO_TRADE",
                    gap_msg,
                    inputs, terminal_status="TERMINAL_RISK_BLOCK"
                )

            if not self.derivative_lifecycle.fixture_mode:
                inputs["runtime_eligibility"] = "UNAVAILABLE_PENDING_ADAPTER_ACCEPTANCE"
                return self._decision(
                    run_id, settings, packet.selected_instrument, "NO_TRADE",
                    "CAPABILITY_UNAVAILABLE:DERIVATIVES_PENDING_LIVE_PER_CONTRACT_SOURCE_AND_LIFECYCLE_ACCEPTANCE",
                    inputs, terminal_status="TERMINAL_RISK_BLOCK",
                )

            # Genuine caller-supplied parameters provided -> execute via PaperDerivativesEngine successor
            from cio_market_lab.engine.paper_derivatives import ContractSpec, DerivativeQuote
            try:
                spec_obj = ContractSpec.model_validate(caller_spec)
                quote_obj = DerivativeQuote.model_validate(caller_quote)
            except (TypeError, ValueError) as exc:
                return self._decision(run_id, settings, packet.selected_instrument, "NO_TRADE",
                                      f"DERIVATIVE_SPEC_OR_QUOTE_INVALID:{exc}", inputs,
                                      terminal_status="TERMINAL_RISK_BLOCK")
            if spec_obj.symbol != packet.selected_instrument:
                return self._decision(run_id, settings, packet.selected_instrument, "NO_TRADE",
                                      "DERIVATIVE_SYMBOL_MISMATCH", inputs, terminal_status="TERMINAL_RISK_BLOCK")
            side = OrderSide.BUY if packet.action.upper() == "BUY" else OrderSide.SELL
            if packet.action.upper() not in {"BUY", "SELL"}:
                return self._decision(run_id, settings, packet.selected_instrument, "NO_TRADE",
                                      "DERIVATIVE_ACTION_NOT_EXECUTABLE", inputs,
                                      terminal_status="TERMINAL_NO_TRADE")

            horizon_val = packet.holding_horizon.value if hasattr(packet.holding_horizon, "value") else str(packet.holding_horizon).lower()
            exec_bucket = DecisionScope.INTRADAY if horizon_val == "intraday" else DecisionScope.SWING
            order_uid = f"derivative-{packet.case_id}"
            try:
                res = self.derivative_lifecycle.execute(
                    strategy_id=settings.strategy_id, bucket=exec_bucket, order_id=order_uid,
                    spec=spec_obj, quote=quote_obj, side=side, quantity=packet.quantity, now=now,
                )
            except ValueError as exc:
                return self._decision(run_id, settings, packet.selected_instrument, "NO_TRADE",
                                      f"DERIVATIVE_EXECUTION_REJECTED:{exc}", inputs,
                                      terminal_status="TERMINAL_RISK_BLOCK")

            if not res.success:
                return self._decision(
                    run_id, settings, packet.selected_instrument, "NO_TRADE",
                    f"DERIVATIVE_EXECUTION_REJECTED:{res.rejection_reason}",
                    inputs, terminal_status="TERMINAL_RISK_BLOCK"
                )

            # Record pre-decision beliefs in learning store
            if existing_rec is None:
                snap = self.generate_canonical_team_ops(now)
                pre_port = snap.get("portfolio", {})
                pre_quotes = {packet.selected_instrument: inputs.get("close")}
                applied_ids = getattr(packet, "applied_lesson_ids", []) or packet.conditions.get("applied_lesson_ids", [])
                dec_delta = getattr(packet, "decision_delta", {}) or packet.conditions.get("decision_delta", {})
                existing_rec = self.learning_store.record_decision(
                    packet, pre_port, pre_quotes, applied_lesson_ids=applied_ids, decision_delta=dec_delta
                )

            self._persist_portfolios()
            self.learning_store.record_fill(
                case_id=packet.case_id,
                order_id=order_uid,
                fill_dict={
                    "fill_price": res.fill_price,
                    "quantity": res.executed_quantity,
                    "symbol": spec_obj.symbol,
                    "side": side.value,
                    "cash_flow": res.cash_flow,
                },
                costs_dict={"fee": res.fee, "tax": res.tax, "slippage": res.slippage, "total_cost": res.fee + res.tax},
            )
            inputs["fill_price"] = res.fill_price
            inputs["executed_quantity"] = res.executed_quantity
            action_status = "BUY_FILLED" if side == OrderSide.BUY else "SELL_FILLED"
            return self._decision(
                run_id, settings, packet.selected_instrument, action_status,
                f"DERIVATIVE_{side.value}_FILLED:{packet.thesis}",
                inputs, order_id=order_uid, quantity=res.executed_quantity, price=res.fill_price, terminal_status="TERMINAL_FILLED"
            )

        # 3. Record pre-decision beliefs in learning store (only if not already recorded)
        if existing_rec is None:
            # Beliefs must use the authenticated frozen decision input, not a
            # post-model revaluation. Re-fetching here both rewrites the basis
            # and can discard a valid NO_TRADE receipt on a feed timeout.
            frozen = getattr(packet, "predecision_snapshot", {}) or {}
            if isinstance(frozen.get("canonical_portfolio"), dict) and frozen["canonical_portfolio"]:
                pre_port = frozen["canonical_portfolio"]
                pre_quotes = frozen.get("quotes", {}) or {packet.selected_instrument: inputs.get("close")}
            else:
                snap = self.generate_canonical_team_ops(now)
                pre_port = snap.get("portfolio", {})
                pre_quotes = {packet.selected_instrument: inputs.get("close")}
            applied_ids = getattr(packet, "applied_lesson_ids", []) or packet.conditions.get("applied_lesson_ids", [])
            dec_delta = getattr(packet, "decision_delta", {}) or packet.conditions.get("decision_delta", {})
            existing_rec = self.learning_store.record_decision(
                packet, pre_port, pre_quotes, applied_lesson_ids=applied_ids, decision_delta=dec_delta
            )

        action = packet.action.upper()
        inputs["cio_thesis"] = packet.thesis
        inputs["cio_case_id"] = packet.case_id
        inputs["cio_confidence"] = packet.confidence
        horizon_val = packet.holding_horizon.value if hasattr(packet.holding_horizon, "value") else str(packet.holding_horizon).lower()
        inputs["holding_horizon"] = horizon_val
        inputs["cio_catalyst"] = getattr(packet, "catalyst", None)
        inputs["cio_invalidation"] = getattr(packet, "invalidation", None)
        inputs["cio_risk_budget_rationale"] = getattr(packet, "risk_budget_rationale", None)

        if action in {"HOLD", "REJECT", "NO_TRADE"}:
            return self._decision(
                run_id, settings, packet.selected_instrument, "NO_TRADE",
                f"CIO_{action}:{packet.thesis}",
                inputs, terminal_status="TERMINAL_NO_TRADE"
            )

        if horizon_val == "cash" or packet.selected_instrument.upper() == "CASH":
            return self._decision(
                run_id, settings, packet.selected_instrument, "NO_TRADE",
                f"CIO_HOLD_CASH:{packet.thesis}",
                inputs, terminal_status="TERMINAL_NO_TRADE"
            )

        exec_bucket = DecisionScope.INTRADAY if horizon_val == "intraday" else DecisionScope.SWING
        ledger = self.portfolio_manager.get_strategy_ledger(
            settings.strategy_id, exec_bucket, settings.initial_cash
        )

        if (settings.paper_execution_model == "NEXT_BAR_OPEN"
                and packet.conditions.get("paper_execution_model") != "NEXT_BAR_OPEN"):
            return self._decision(run_id, settings, packet.selected_instrument, "NO_TRADE",
                "NO_TRADE_BAR_SIMULATION_REQUIRES_EXPLICIT_CIO_OPT_IN", inputs,
                terminal_status="TERMINAL_RISK_BLOCK")

        if action == "BUY":
            cost = requested_quantity * bar.close
            frozen = inputs.get("frozen_decision_context")
            if isinstance(frozen, dict):
                ceiling = frozen.get("exposure_ceiling")
                if isinstance(ceiling, bool) or not isinstance(ceiling, (int,float)) or not math.isfinite(ceiling) or ceiling <= 0:
                    return self._decision(run_id,settings,packet.selected_instrument,"NO_TRADE",
                        "NO_TRADE_FROZEN_RESEARCH_ONLY_OR_INVALID_EXPOSURE_CEILING",inputs,terminal_status="TERMINAL_RISK_BLOCK")
                # This implementation's strategy ledger uses a single cash unit.
                # Cross-currency PAPER execution needs explicit ledger identity;
                # a forex quote alone cannot repair an undeclared cash unit.
                market = self._market(packet.selected_instrument)
                currency = "TWD" if market == "TW" else "USD"
                reporting = str(getattr(settings,"reporting_currency","TWD")).upper()
                if currency != reporting:
                    return self._decision(run_id,settings,packet.selected_instrument,"NO_TRADE",
                        "NO_TRADE_FROZEN_CAP_CROSS_CURRENCY_LEDGER_UNVERIFIED",inputs,terminal_status="TERMINAL_RISK_BLOCK")
                existing_notional = sum(float(pos.quantity)*float(inputs.get("close") or bar.close)
                    for pos in ledger.positions.values() if pos.symbol == packet.selected_instrument)
                if any(pos.symbol != packet.selected_instrument and float(pos.quantity) != 0 for pos in ledger.positions.values()):
                    return self._decision(run_id,settings,packet.selected_instrument,"NO_TRADE",
                        "NO_TRADE_FROZEN_CAP_FULL_PORTFOLIO_MARKS_UNAVAILABLE",inputs,terminal_status="TERMINAL_RISK_BLOCK")
                nav = float(ledger.cash) + existing_notional
                if cost + existing_notional > nav * ceiling:
                    return self._decision(run_id,settings,packet.selected_instrument,"NO_TRADE",
                        "NO_TRADE_FROZEN_EXPOSURE_CEILING_EXCEEDED",inputs,terminal_status="TERMINAL_RISK_BLOCK")
            est_fee = self.paper_orders.cost_config.calculate_fee(self._market(packet.selected_instrument), cost)
            reserved = self._pending_buy_reserve(settings.strategy_id, exclude_case_id=packet.case_id)
            if cost + est_fee > ledger.cash - reserved:
                return self._decision(
                    run_id, settings, packet.selected_instrument, "NO_TRADE",
                    "NO_TRADE_INSUFFICIENT_PAPER_CASH",
                    inputs, terminal_status="TERMINAL_RISK_BLOCK"
                )
            if cost > settings.max_position_notional:
                return self._decision(
                    run_id, settings, packet.selected_instrument, "NO_TRADE",
                    "NO_TRADE_MAX_POSITION_NOTIONAL_EXCEEDED",
                    inputs, terminal_status="TERMINAL_RISK_BLOCK"
                )

            obs_age = (now - bar.observed_at).total_seconds() if bar.observed_at else bar.delay_seconds
            effective_age = max(bar.delay_seconds, obs_age)

            req = PaperOrderRequest(
                currency=settings.base_currency,
                symbol=packet.selected_instrument,
                market=self._market(packet.selected_instrument),
                bucket=exec_bucket,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT if packet.conditions.get("entry_limit_price") is not None else OrderType.MARKET,
                limit_price=packet.conditions.get("entry_limit_price"),
                quantity=float(packet.quantity),
                origin=OrderOrigin.MAIN_CIO,
                strategy_id=settings.strategy_id,
                strategy_version=packet.strategy_version,
                explicit_user_instruction=True,
                reason=f"CIO_THESIS:{packet.thesis}",
                data=PaperDataContext(
                    source=bar.source,
                    observed_at=bar.observed_at or now,
                    age_seconds=effective_age,
                    last_price=bar.close,
                    is_stale=False,
                    is_fallback=False,
                ),
                audit_metadata={
                    "runner": "autonomous_paper_v2",
                    "case_id": packet.case_id,
                    "confidence": packet.confidence,
                    "authority": packet.provenance.authority,
                    "is_fixture": packet.is_fixture,
                },
            )

            # Reuse existing pending order if progressing pending case
            order = None
            if existing_rec and existing_rec.order_id:
                order = self.paper_orders.find_order(existing_rec.order_id)
            if order is None:
                try:
                    order = self.paper_orders.submit(req)
                    if existing_rec:
                        existing_rec.order_id = order.order_id
                        existing_rec.pre_decision_quotes["reserved_cash"] = round(cost + est_fee, 4)
                        self.learning_store._persist_all()
                except ValueError as exc:
                    return self._decision(
                        run_id, settings, packet.selected_instrument, "NO_TRADE",
                        f"NO_TRADE_ORDER_REJECTED:{exc}", inputs, terminal_status="TERMINAL_RISK_BLOCK"
                    )

            resolved = self._resolve_cio_execution(settings, packet, bar, order)
            if resolved is None:
                inputs["order_status"] = "PENDING"
                inputs["fill_pending_reason"] = ("WAITING_FOR_LATER_SOURCE_BAR_SIMULATION"
                    if settings.paper_execution_model == "NEXT_BAR_OPEN" else "WAITING_FOR_LATER_AUTHORITATIVE_QUOTE")
                if existing_rec:
                    existing_rec.order_id = order.order_id
                    self.learning_store._persist_all()
                self._persist_portfolios()
                return self._decision(
                    run_id, settings, packet.selected_instrument, "BUY_PENDING",
                    f"CIO_BUY_PENDING_QUOTE:{packet.thesis}",
                    inputs, order_id=order.order_id, quantity=float(packet.quantity), terminal_status="NON_TERMINAL"
                )

            mkt = self._market(packet.selected_instrument)
            evidence, per_share_slip, effective_px = resolved.quote_evidence, resolved.per_share_slippage, resolved.effective_price
            executed_qty = float(resolved.assumptions.get("executed_quantity", requested_quantity))
            trade_val = executed_qty * effective_px
            fee = self.paper_orders.cost_config.calculate_fee(mkt, trade_val)
            tax = self.paper_orders.cost_config.calculate_tax(mkt, OrderSide.BUY, trade_val)
            if trade_val + fee + tax > ledger.cash - reserved or trade_val > settings.max_position_notional:
                return self._decision(
                    run_id, settings, packet.selected_instrument, "NO_TRADE",
                    "NO_TRADE_LATER_QUOTE_EXCEEDS_RESERVED_CASH_OR_NOTIONAL",
                    inputs, order_id=order.order_id, terminal_status="TERMINAL_RISK_BLOCK",
                )
            total_slip = round(executed_qty * per_share_slip, 4)
            if isinstance(frozen, dict):
                existing_value = sum(pos.quantity * effective_px for pos in ledger.positions.values()
                                     if pos.symbol == packet.selected_instrument)
                if trade_val + existing_value > (ledger.cash + existing_value) * frozen["exposure_ceiling"]:
                    return self._decision(run_id, settings, packet.selected_instrument, "NO_TRADE",
                        "NO_TRADE_FILL_EXCEEDS_FROZEN_EXPOSURE_CEILING", inputs,
                        order_id=order.order_id, terminal_status="TERMINAL_RISK_BLOCK")
            fill = Fill(
                currency=order.currency,
                fill_id=f"fill-{uuid.uuid4()}",
                order_id=order.order_id,
                symbol=packet.selected_instrument,
                bucket=packet.holding_horizon,
                side=OrderSide.BUY,
                quantity=executed_qty,
                fill_price=effective_px,
                fee=fee,
                tax=tax,
                slippage=total_slip,
                timestamp=resolved.timestamp,
                consumed_quote=evidence,
                quote_verification=resolved.verification,
                assumptions={
                    "execution": "local_paper_only",
                    "market": mkt.value,
                    "case_id": packet.case_id,
                    "authority": "MAIN_CIO",
                    "timing_assumption": "authoritative_later_quote_slippage_adjusted",
                    "bar_timestamp": bar.timestamp.isoformat(),
                    "base_price": resolved.base_price,
                    "is_fixture": packet.is_fixture,
                    "slippage_embedded": True,
                    **resolved.assumptions,
                },
            )
            self.portfolio_manager.apply_fill(fill, settings.strategy_id)
            self.paper_orders.event_store.append(
                EventEnvelope(
                    event_type=EventType.ORDER_FILLED,
                    aggregate_id=order.order_id,
                    payload=fill.model_dump(mode="json"),
                )
            )
            self._persist_portfolios()
            costs_dict = {"fee": fee, "tax": tax, "slippage": total_slip, "total_cost": fee + tax + total_slip}
            order = self.paper_orders.find_order(order.order_id) or order
            self._record_cio_execution_fragment(packet.case_id, order, fill, settings.strategy_id)
            inputs["fill_price"] = effective_px
            return self._decision(
                run_id, settings, packet.selected_instrument, "BUY_PARTIALLY_FILLED" if order.status == OrderStatus.PARTIALLY_FILLED else "BUY_FILLED",
                f"CIO_BUY:{packet.thesis}",
                inputs, order_id=order.order_id, quantity=executed_qty, price=effective_px,
                terminal_status="NON_TERMINAL" if order.status == OrderStatus.PARTIALLY_FILLED else "TERMINAL_FILLED"
            )

        if action == "SELL":
            pos = ledger.positions.get(packet.selected_instrument)
            if not pos or pos.quantity <= 0:
                return self._decision(
                    run_id, settings, packet.selected_instrument, "NO_TRADE",
                    "NO_TRADE_NO_POSITION_TO_SELL",
                    inputs, terminal_status="TERMINAL_RISK_BLOCK"
                )
            sell_qty = min(requested_quantity, pos.quantity)
            entry_px_before_fill = pos.average_entry_price

            # Strict case scoping: match before any side effect
            target_case_id = packet.conditions.get("target_case_id") or packet.conditions.get("close_case_id")
            target_rec = None

            if target_case_id:
                cand = self.learning_store.get_record(target_case_id)
                if not cand:
                    return self._decision(
                        run_id, settings, packet.selected_instrument, "NO_TRADE",
                        f"CIO_CASE_NOT_FOUND: target_case_id {target_case_id} not found in learning store",
                        inputs, terminal_status="TERMINAL_RISK_BLOCK"
                    )
                cand_strat = cand.packet.conditions.get("strategy_id") or getattr(cand.packet, "strategy_id", None) or settings.strategy_id
                if cand.packet.conditions.get("strategy_id") and cand.packet.conditions.get("strategy_id") != settings.strategy_id:
                    return self._decision(
                        run_id, settings, packet.selected_instrument, "NO_TRADE",
                        f"CIO_CROSS_STRATEGY_BLOCKED: case {target_case_id} belongs to {cand.packet.conditions.get('strategy_id')}, not {settings.strategy_id}",
                        inputs, terminal_status="TERMINAL_RISK_BLOCK"
                    )
                if cand.packet.selected_instrument != packet.selected_instrument:
                    return self._decision(
                        run_id, settings, packet.selected_instrument, "NO_TRADE",
                        f"CIO_SYMBOL_MISMATCH: target case {target_case_id} symbol {cand.packet.selected_instrument} != {packet.selected_instrument}",
                        inputs, terminal_status="TERMINAL_RISK_BLOCK"
                    )
                if cand.packet.holding_horizon != packet.holding_horizon:
                    return self._decision(
                        run_id, settings, packet.selected_instrument, "NO_TRADE",
                        f"CIO_BUCKET_MISMATCH: target case {target_case_id} bucket {cand.packet.holding_horizon} != {packet.holding_horizon}",
                        inputs, terminal_status="TERMINAL_RISK_BLOCK"
                    )
                if cand.status == "CLOSED":
                    return self._decision(
                        run_id, settings, packet.selected_instrument, "NO_TRADE",
                        f"CIO_CASE_ALREADY_CLOSED: target case {target_case_id} is already fully closed",
                        inputs, terminal_status="TERMINAL_RISK_BLOCK"
                    )
                target_rec = cand
            else:
                matching_candidates = []
                for cid, r in self.learning_store._records.items():
                    r_strat = r.packet.conditions.get("strategy_id") or getattr(r.packet, "strategy_id", None) or settings.strategy_id
                    if (
                        r_strat == settings.strategy_id
                        and r.packet.selected_instrument == packet.selected_instrument
                        and r.packet.holding_horizon == packet.holding_horizon
                        and r.packet.action.upper() == "BUY"
                        and r.status in ("FILLED", "PARTIALLY_CLOSED", "PARTIALLY_FILLED", "EXPIRED_PARTIAL", "CANCELLED_PARTIAL")
                    ):
                        matching_candidates.append(r)

                if len(matching_candidates) == 0:
                    return self._decision(
                        run_id, settings, packet.selected_instrument, "NO_TRADE",
                        f"CIO_NO_MATCHING_OPEN_CASE: no open case for {packet.selected_instrument} in strategy {settings.strategy_id} {packet.holding_horizon.value}",
                        inputs, terminal_status="TERMINAL_NO_TRADE"
                    )
                elif len(matching_candidates) > 1:
                    # Ambiguous case matching must fail closed before any side effect
                    return self._decision(
                        run_id, settings, packet.selected_instrument, "NO_TRADE",
                        f"CIO_AMBIGUOUS_CASE_MATCH: multiple open cases exist for {packet.selected_instrument} in strategy {settings.strategy_id} {packet.holding_horizon.value}; target_case_id required",
                        inputs, terminal_status="TERMINAL_RISK_BLOCK"
                    )
                else:
                    target_rec = matching_candidates[0]
                    target_case_id = target_rec.case_id

            open_qty = float(target_rec.fill.get("quantity", sell_qty)) if target_rec and target_rec.fill else sell_qty
            already_closed_qty = sum(float(o.get("quantity", 0.0)) for o in getattr(target_rec, "outcomes", []))
            remaining_qty = max(0.0, open_qty - already_closed_qty)
            if sell_qty > remaining_qty + 1e-6:
                return self._decision(
                    run_id, settings, packet.selected_instrument, "NO_TRADE",
                    f"CIO_OVERSELL_BLOCKED: sell quantity {sell_qty} exceeds remaining open quantity {remaining_qty} for case {target_case_id}",
                    inputs, terminal_status="TERMINAL_RISK_BLOCK"
                )

            is_partial = (sell_qty < remaining_qty - 1e-6)
            residual_qty = max(0.0, remaining_qty - sell_qty)

            # Reuse existing pending order if progressing pending case
            order = None
            if existing_rec and existing_rec.order_id:
                order = self.paper_orders.find_order(existing_rec.order_id)
            if order is None:
                req = PaperOrderRequest(
                    currency=settings.base_currency,
                    symbol=packet.selected_instrument,
                    market=self._market(packet.selected_instrument),
                    bucket=packet.holding_horizon,
                    side=OrderSide.SELL,
                    order_type=OrderType.LIMIT if packet.conditions.get("exit_limit_price") is not None else OrderType.MARKET,
                    limit_price=packet.conditions.get("exit_limit_price"),
                    quantity=sell_qty,
                    origin=OrderOrigin.MAIN_CIO,
                    strategy_id=settings.strategy_id,
                    strategy_version=packet.strategy_version,
                    explicit_user_instruction=True,
                    reason=f"CIO_EXIT:{packet.thesis}",
                    data=PaperDataContext(
                        source=bar.source,
                        observed_at=bar.observed_at or now,
                        age_seconds=max(bar.delay_seconds, (now - bar.observed_at).total_seconds() if bar.observed_at else 0.0),
                        last_price=bar.close,
                        is_stale=False,
                        is_fallback=False,
                    ),
                    audit_metadata={"case_id": packet.case_id, "authority": packet.provenance.authority, "is_fixture": packet.is_fixture},
                )
                try:
                    order = self.paper_orders.submit(req)
                    if existing_rec:
                        existing_rec.order_id = order.order_id
                except ValueError as exc:
                    return self._decision(
                        run_id, settings, packet.selected_instrument, "NO_TRADE",
                        f"NO_TRADE_ORDER_REJECTED:{exc}", inputs, terminal_status="TERMINAL_RISK_BLOCK"
                    )

            entry_time = None
            if target_rec and target_rec.fill and target_rec.fill.get("timestamp"):
                entry_time = datetime.fromisoformat(str(target_rec.fill["timestamp"]))
                if entry_time.tzinfo is None:
                    entry_time = entry_time.replace(tzinfo=timezone.utc)
            resolved = self._resolve_cio_execution(settings, packet, bar, order, entry_time=entry_time)
            if resolved is None:
                inputs["order_status"] = "PENDING"
                inputs["fill_pending_reason"] = ("WAITING_FOR_LATER_SOURCE_BAR_SIMULATION"
                    if settings.paper_execution_model == "NEXT_BAR_OPEN" else "WAITING_FOR_LATER_AUTHORITATIVE_QUOTE")
                if existing_rec:
                    existing_rec.order_id = order.order_id
                    self.learning_store._persist_all()
                self._persist_portfolios()
                return self._decision(
                    run_id, settings, packet.selected_instrument, "SELL_PENDING",
                    f"CIO_SELL_PENDING_QUOTE:{packet.thesis}",
                    inputs, order_id=order.order_id, quantity=sell_qty, terminal_status="NON_TERMINAL"
                )
            mkt = self._market(packet.selected_instrument)
            evidence, per_share_slip, effective_px = resolved.quote_evidence, resolved.per_share_slippage, resolved.effective_price
            sell_qty = float(resolved.assumptions.get("executed_quantity", sell_qty))
            is_partial = sell_qty < remaining_qty - 1e-6
            residual_qty = max(0.0, remaining_qty - sell_qty)
            trade_val = sell_qty * effective_px
            fee = self.paper_orders.cost_config.calculate_fee(mkt, trade_val)
            tax = self.paper_orders.cost_config.calculate_tax(mkt, OrderSide.SELL, trade_val)
            total_slip = round(sell_qty * per_share_slip, 4)
            fill = Fill(
                currency=order.currency,
                fill_id=f"fill-{uuid.uuid4()}",
                order_id=order.order_id,
                symbol=packet.selected_instrument,
                bucket=packet.holding_horizon,
                side=OrderSide.SELL,
                quantity=sell_qty,
                fill_price=effective_px,
                fee=fee,
                tax=tax,
                slippage=total_slip,
                timestamp=resolved.timestamp,
                consumed_quote=evidence,
                quote_verification=resolved.verification,
                assumptions={
                    "case_id": packet.case_id,
                    "target_case_id": target_case_id,
                    "is_fixture": packet.is_fixture,
                    "timing_assumption": "side_aware_book_slippage_adjusted",
                    "base_price": resolved.base_price,
                    "slippage_embedded": True,
                    **resolved.assumptions,
                },
            )

            entry_px = float(target_rec.fill.get("fill_price", entry_px_before_fill)) if target_rec and target_rec.fill else entry_px_before_fill
            holding_hours = 0.0
            if target_rec and target_rec.fill:
                entry_ts_raw = target_rec.fill.get("timestamp")
                if entry_ts_raw:
                    try:
                        e_ts = datetime.fromisoformat(str(entry_ts_raw))
                        if e_ts.tzinfo is None:
                            e_ts = e_ts.replace(tzinfo=timezone.utc)
                        q_ts = resolved.timestamp
                        holding_hours = max(0.0, round((q_ts - e_ts).total_seconds() / 3600.0, 2))
                    except Exception:
                        pass

            close_fraction = (sell_qty / open_qty) if open_qty > 0 else 1.0
            if target_rec and target_rec.costs:
                total_entry_cost = float(target_rec.costs.get("fee", 0.0)) + float(target_rec.costs.get("tax", 0.0))
                total_entry_slip = float(target_rec.costs.get("slippage", 0.0))
            elif target_rec and target_rec.fill:
                total_entry_cost = float(target_rec.fill.get("fee", 0.0)) + float(target_rec.fill.get("tax", 0.0))
                total_entry_slip = float(target_rec.fill.get("slippage", 0.0))
            else:
                total_entry_cost = 0.0
                total_entry_slip = 0.0

            allocated_entry_cost = round(total_entry_cost * close_fraction, 4)
            allocated_entry_slip = round(total_entry_slip * close_fraction, 4)
            prev_allocated_entry_costs = sum(float(o.get("costs", {}).get("entry_costs", 0.0)) for o in getattr(target_rec, "outcomes", []))
            residual_entry_costs = max(0.0, round(total_entry_cost - prev_allocated_entry_costs - allocated_entry_cost, 4))

            exit_cost = round(fee + tax, 4)
            total_costs = round(allocated_entry_cost + exit_cost, 4)
            delta_realized = round((effective_px - entry_px) * sell_qty - exit_cost, 4)
            delta_net_realized = round(delta_realized - allocated_entry_cost, 4)
            return_pct = round(((effective_px / entry_px) - 1.0) * 100.0, 4) if entry_px > 0 else 0.0

            # Derive benchmark attribution ONLY where benchmark inputs genuinely exist and period aligned
            bm_ret = packet.conditions.get("benchmark_return_pct")
            bm_sym = packet.conditions.get("benchmark_symbol")
            is_bm_net = packet.conditions.get("benchmark_is_net", False)
            bm_costs = packet.conditions.get("benchmark_costs")

            if bm_ret is None and bm_sym:
                try:
                    bm_q = self._market_call("get_latest_quote", bm_sym)
                    if bm_q and "benchmark_entry_price" in packet.conditions:
                        q_ts = bm_q.timestamp if bm_q.timestamp.tzinfo else bm_q.timestamp.replace(tzinfo=timezone.utc)
                        e_ts = eligible_quote.timestamp if eligible_quote.timestamp.tzinfo else eligible_quote.timestamp.replace(tzinfo=timezone.utc)
                        if abs((q_ts - e_ts).total_seconds()) <= 3600.0:
                            b_entry = float(packet.conditions["benchmark_entry_price"])
                            if b_entry > 0 and bm_q.last_price:
                                raw_bm = round(((bm_q.last_price / b_entry) - 1.0) * 100.0, 4)
                                if is_bm_net:
                                    if bm_costs is not None:
                                        bm_ret = round(raw_bm - float(bm_costs), 4)
                                    else:
                                        bm_ret = None
                                else:
                                    bm_ret = raw_bm
                except Exception:
                    bm_ret = None
            elif bm_ret is not None and is_bm_net and bm_costs is None:
                bm_ret = None

            if bm_ret is not None:
                trade_ret = ((delta_net_realized / (entry_px * sell_qty)) * 100.0) if (is_bm_net and entry_px > 0 and sell_qty > 0) else return_pct
                alpha_bps = round((trade_ret - float(bm_ret)) * 100.0, 2)
                attribution_dict = {
                    "status": "DERIVED",
                    "benchmark_status": "AVAILABLE",
                    "benchmark_symbol": bm_sym or "SYNTHETIC_BENCHMARK",
                    "benchmark_return_pct": float(bm_ret),
                    "trade_return_pct": return_pct,
                    "alpha_bps": alpha_bps,
                    "thesis_validation": packet.thesis,
                    "is_net": bool(is_bm_net),
                }
            else:
                attribution_dict = {
                    "status": "UNAVAILABLE",
                    "benchmark_status": "UNAVAILABLE",
                    "alpha_bps": None,
                    "market_beta_bps": None,
                    "reason": "BENCHMARK_NET_COST_UNAVAILABLE" if is_bm_net else "NO_BENCHMARK_INPUT",
                    "thesis_validation": packet.thesis,
                }

            self.portfolio_manager.apply_fill(fill, settings.strategy_id)
            self.paper_orders.event_store.append(
                EventEnvelope(event_type=EventType.ORDER_FILLED, aggregate_id=order.order_id, payload=fill.model_dump(mode="json"))
            )
            self._persist_portfolios()
            costs_dict = {"fee": fee, "tax": tax, "slippage": total_slip, "total_cost": total_slip + fee + tax}
            order = self.paper_orders.find_order(order.order_id) or order
            self._record_cio_execution_fragment(packet.case_id, order, fill, settings.strategy_id)
            outcome_dict = {
                "case_id": target_case_id,
                "entry_price": entry_px,
                "exit_price": effective_px,
                "quantity": sell_qty,
                "residual_quantity": round(residual_qty, 4),
                "is_partial": is_partial,
                "realized_pnl": delta_realized,
                "net_realized_pnl": delta_net_realized,
                "return_pct": return_pct,
                "costs": {
                    "entry_costs": allocated_entry_cost,
                    "exit_costs": exit_cost,
                    "total_costs": total_costs,
                    "residual_entry_costs": residual_entry_costs,
                    "informational_slippage": {
                        "entry_slippage": allocated_entry_slip,
                        "exit_slippage": total_slip,
                        "total_slippage": round(allocated_entry_slip + total_slip, 4),
                    },
                },
                "holding_period_hours": holding_hours,
                "closed_at": resolved.timestamp.isoformat(),
            }
            if is_partial:
                lesson_text = f"CIO partial decision {target_case_id} ({packet.thesis}): closed {sell_qty} of {packet.selected_instrument} with realized PnL {delta_realized:.2f}, remaining {residual_qty:.2f}."
                lessons_list = []
            else:
                lesson_text = f"CIO decision {target_case_id} ({packet.thesis}): closed {packet.selected_instrument} with realized PnL {delta_realized:.2f} ({return_pct:.2f}%), benchmark attribution {attribution_dict.get('status')}."
                lessons_list = [lesson_text]

            self.learning_store.record_outcome(
                case_id=target_case_id,
                outcome_dict=outcome_dict,
                attribution_dict=attribution_dict,
                lessons=lessons_list,
                is_partial=is_partial,
                as_of=now,
            )
            self.learning_store.record_outcome(packet.case_id, outcome_dict,
                attribution_dict=attribution_dict, lessons=[],
                is_partial=order.status == OrderStatus.PARTIALLY_FILLED, as_of=now)
            inputs["fill_price"] = effective_px
            inputs["realized_pnl"] = delta_realized
            inputs["net_realized_pnl"] = delta_net_realized
            inputs["outcome"] = outcome_dict
            inputs["attribution"] = attribution_dict
            return self._decision(
                run_id, settings, packet.selected_instrument, "SELL_PARTIALLY_FILLED" if order.status == OrderStatus.PARTIALLY_FILLED else "SELL_FILLED",
                f"CIO_SELL:{packet.thesis}",
                inputs, order_id=order.order_id, quantity=sell_qty, price=effective_px,
                terminal_status="NON_TERMINAL" if order.status == OrderStatus.PARTIALLY_FILLED else "TERMINAL_FILLED"
            )

        return self._decision(
            run_id, settings, packet.selected_instrument, "NO_TRADE",
            f"NO_TRADE_UNHANDLED_ACTION:{action}",
            inputs, terminal_status="TERMINAL_NO_TRADE"
        )

    def process_pending_orders(self) -> List[ExperimentDecision]:
        """Advance residuals only; cancellation and expiry never reverse posted fills."""
        if self.is_read_only:
            raise PermissionError("RUNNER_READONLY_INSTANCE")
        decisions = []
        with self._lock:
            for case_id in list(self.learning_store.processed_case_ids):
                rec = self.learning_store.get_record(case_id)
                if rec is None or rec.status not in {"ACTIVE", "PARTIALLY_FILLED", "PARTIALLY_CLOSED"} or rec.packet.action.upper() not in {"BUY", "SELL"}:
                    continue
                pending = self.paper_orders.find_order(rec.order_id) if rec.order_id else None
                if pending is None:
                    continue
                if pending.status == OrderStatus.CANCELLED:
                    rec.status = "CANCELLED_PARTIAL" if rec.fill else "CANCELLED"
                    self.learning_store._persist_all()
                    continue
                if pending.status not in {OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED}:
                    continue
                if self._now() > rec.packet.expiry:
                    pending.status = OrderStatus.EXPIRED
                    pending.rejection_reason = "PACKET_EXPIRED_RESIDUAL_CANCELLED"
                    rec.status = "EXPIRED_PARTIAL" if rec.fill else "EXPIRED"
                    self.learning_store._persist_all()
                    self._persist_portfolios()
                    continue
                d = self.submit_cio_packet(rec.packet, strategy_id=pending.strategy_id)
                if d.terminal_status == "TERMINAL_RISK_BLOCK":
                    pending.status = OrderStatus.REJECTED
                    pending.rejection_reason = d.reason
                    rec.status = "REJECTED_PARTIAL" if rec.fill else "REJECTED"
                    self.learning_store._persist_all()
                    self._persist_portfolios()
                if d.action in {"BUY_FILLED", "SELL_FILLED", "BUY_PARTIALLY_FILLED", "SELL_PARTIALLY_FILLED"} or d.terminal_status == "TERMINAL_RISK_BLOCK":
                    decisions.append(d)
        return decisions

    def review_derivative_positions(self, strategy_id: str, quotes: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        """Advance held-contract risk, never substitute a spot/underlying quote."""
        if self.is_read_only:
            raise PermissionError("RUNNER_READONLY_INSTANCE")
        if getattr(self, "writer_lock", None) is not None:
            with self.writer_lock:
                return self._review_derivative_positions_locked(strategy_id, quotes)
        return self._review_derivative_positions_locked(strategy_id, quotes)

    def _review_derivative_positions_locked(self, strategy_id: str, quotes: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        from cio_market_lab.engine.paper_derivatives import DerivativeQuote
        result: Dict[str, str] = {}
        for bucket in (DecisionScope.SWING, DecisionScope.INTRADAY):
            ledger = self.portfolio_manager.get_strategy_ledger(strategy_id, bucket)
            for sym, pos in list(ledger.positions.items()):
                if not pos.quantity or "derivative_position" not in pos.assumptions:
                    continue
                candidate = (quotes or {}).get(sym)
                if candidate is None:
                    contract_feed = getattr(self.market_adapter, "get_derivative_quote", None)
                    if callable(contract_feed):
                        try:
                            candidate = contract_feed(sym)
                        except Exception:
                            candidate = None
                try:
                    quote = DerivativeQuote.model_validate(candidate) if candidate is not None else None
                except (TypeError, ValueError):
                    quote = None
                result[f"{bucket.value}:{sym}"] = self.derivative_lifecycle.review(
                    strategy_id=strategy_id, bucket=bucket, symbol=sym, now=self._now(), quote=quote,
                )
        if result:
            self._persist_portfolios()
        return result

    def run_scheduled_cycle(self, strategy_id: str) -> Dict[str, Any]:
        """Shared session-aware entry for daemon and isolated PAPER driver."""
        if getattr(self, "is_read_only", False):
            raise PermissionError("RUNNER_READONLY_INSTANCE: Cannot execute paper cycle on read-only instance")
        if getattr(self, "writer_lock", None) is not None:
            with self.writer_lock:
                return self._run_scheduled_cycle_locked(strategy_id)
        return self._run_scheduled_cycle_locked(strategy_id)

    def _run_scheduled_cycle_locked(self, strategy_id: str) -> Dict[str, Any]:
        # Lock ordering matches run_one_cycle: writer lease, then process lock.
        # Slot selection, cycle execution and slot persistence are indivisible.
        with self._lock:
            settings = self.paper_orders.experiment_for(strategy_id)
            now = self._now()
            symbols, slots = self._scheduled_symbols(settings, now)
            if not symbols:
                return {"run": None, "decisions": [], "status": "NO_SESSION_DUE",
                        "paper_only": True, "broker_connected": False}
            result = self._run_one_cycle_locked(strategy_id, symbols=symbols)
            source_failure_markers = ("FEED_UNAVAILABLE", "NO_TRADE_STALE_OR_SYNTHETIC_DATA")
            source_failed = {d["symbol"] for d in result.get("decisions", [])
                             if any(marker in d.get("reason", "") for marker in source_failure_markers)}
            # A disabled/expired/incomplete cycle has no decision for a symbol.
            # Never burn its slot merely because no feed error was reported.
            decided_symbols = {d["symbol"] for d in result.get("decisions", [])}
            completed_slots = {key: slot for key, slot in slots.items()
                               if key.split("|", 1)[1] in decided_symbols
                               and key.split("|", 1)[1] not in source_failed}
            if completed_slots:
                self._scheduled_slots.update(completed_slots)
                self._persist_scheduled_slots()
            result["consumed_session_slots"] = completed_slots
            result["retryable_source_failures"] = sorted(source_failed)
            return result

    def run_one_cycle(self, strategy_id: str, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        if getattr(self, "is_read_only", False):
            raise PermissionError("RUNNER_READONLY_INSTANCE: Cannot execute paper cycle on read-only instance")
        if getattr(self, "writer_lock", None) is not None:
            with self.writer_lock:
                return self._run_one_cycle_locked(strategy_id, symbols)
        return self._run_one_cycle_locked(strategy_id, symbols)

    def _run_one_cycle_locked(self, strategy_id: str, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        with self._lock:
            settings = self.paper_orders.experiment_for(strategy_id)
            started = self._now()
            run = ExperimentRun(run_id=f"run-{uuid.uuid4()}", strategy_id=strategy_id, started_at=started)
            self._runs.append(run)
            derivative_risk = self._review_derivative_positions_locked(strategy_id)
            if not settings.enabled:
                run.status, run.reason = "DISABLED", "EXPERIMENT_DISABLED"
            elif settings.expires_at and settings.expires_at <= started:
                run.status, run.reason = "EXPIRED", "EXPERIMENT_EXPIRED"
            else:
                cycle_symbols = settings.universe if symbols is None else symbols
                decisions = []
                for symbol in cycle_symbols:
                    self._feed_deadline = time.monotonic() + self.FEED_STAGE_SECONDS
                    self._stage_market_cache = {}
                    try:
                        daily_review = self._review_daily_unarmed_plan(run.run_id, settings, symbol)
                        decisions.append(daily_review if daily_review is not None else self._run_symbol(run.run_id, settings, symbol))
                    except FeedUnavailable as exc:
                        decisions.append(self._decision(
                            run.run_id, settings, symbol, "NO_TRADE", str(exc),
                            {"source": "unavailable", "quality": "missing"},
                            terminal_status="TERMINAL_RISK_BLOCK",
                        ))
                    finally:
                        self._feed_deadline = None
                        self._stage_market_cache = None
                run.decisions_count = len(decisions)
                run.orders_count = sum(1 for d in decisions if d.action in {"BUY_FILLED", "SELL_FILLED", "BUY_PENDING", "SELL_PENDING"} or d.order_id is not None)
                run.fills_count = sum(1 for d in decisions if d.action in {"BUY_FILLED", "SELL_FILLED"})
                all_blocked = all("BLOCKED_" in d.reason or "FEED_UNAVAILABLE" in d.reason for d in decisions) if decisions else False
                if all_blocked:
                    run.status = "BLOCKED"
                    primary_reason = decisions[0].reason.split(":")[0] if decisions else "BLOCKED_NO_CIO_DECISION_PROVIDER"
                    run.reason = primary_reason
                else:
                    run.status = "COMPLETED"
                    run.reason = "CYCLE_COMPLETED"
                for bucket in settings.allowed_buckets:
                    p = self.portfolio_manager.get_strategy_portfolio(strategy_id, bucket)
                    snap = EquitySnapshot(run_id=run.run_id, strategy_id=strategy_id, bucket=bucket, cash=p.cash, equity=p.equity, realized_pnl=p.realized_pnl, unrealized_pnl=p.unrealized_pnl, open_positions=sum(1 for x in p.positions.values() if x.quantity > 0))
                    self._equity.append(snap)
                    self._append_jsonl("equity_snapshots.jsonl", snap)
            run.completed_at = self._now()
            self._append_jsonl("runs.jsonl", run)
            has_executor = self.cio_executor is not None and getattr(self.cio_executor, "is_available", lambda: False)()
            has_receipt = self.last_receipt is not None and getattr(self.last_receipt, "readback_verified", False)
            return {
                "run": run.model_dump(mode="json"),
                "decisions": [d.model_dump(mode="json") for d in self._decisions if d.run_id == run.run_id],
                "paper_only": True,
                "broker_connected": False,
                "autonomous_capital_decisions": False,
                "autonomous_paper_execution": (has_executor and has_receipt),
                "executor_configured": self.cio_executor is not None,
                "readback_receipt_present": has_receipt,
                "decision_owner": "MAIN_CIO",
                "worker_role": "ENGINEERING_WORKER_ONLY",
                "derivative_risk": derivative_risk,
            }

    def competition_summary(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Return the fixed 30-day mixed TW/US scoreboard normalized to TWD."""
        now = now or self._now()
        start = datetime(2026, 9, 26, tzinfo=timezone.utc)
        end = datetime(2026, 10, 26, tzinfo=timezone.utc)
        days_remaining = max(0, math.ceil((end - now).total_seconds() / 86400))
        rows: List[Dict[str, Any]] = []
        for strategy_id, settings in self.paper_orders.experiments.items():
            portfolio = self.portfolio_manager.get_strategy_portfolio(strategy_id, settings.mode)
            equity = portfolio.equity
            unrealized = portfolio.unrealized_pnl
            fx_info = ({"status": "AVAILABLE", "rate": 1.0} if settings.base_currency == settings.reporting_currency
                       else self.resolve_canonical_valuation_fx(settings.base_currency, now))
            fx = (float(fx_info["rate"]) if fx_info.get("status") == "AVAILABLE"
                  and fx_info.get("rate") is not None else None)
            currency_ok = portfolio.currency == settings.base_currency
            if equity is None:
                pnl = None
                return_pct = None
                equity_rounded = None
                unrealized_rounded = None
                equity_reporting = None  # Unpriced positions are not valued at initial cash.
                pnl_reporting = None
            else:
                pnl = equity - portfolio.initial_cash
                return_pct = round((pnl / portfolio.initial_cash) * 100, 6) if portfolio.initial_cash else 0.0
                equity_rounded = round(equity, 4)
                unrealized_rounded = round(unrealized, 4) if unrealized is not None else None
                equity_reporting = round(equity * fx, 4) if fx is not None and currency_ok else None
                pnl_reporting = round(pnl * fx, 4) if fx is not None and currency_ok else None
            rows.append({
                "strategy_id": strategy_id,
                "name": settings.strategy_name,
                "style": settings.style,
                "market": settings.market.value,
                "base_currency": settings.base_currency,
                "reporting_currency": settings.reporting_currency,
                "fx_to_reporting": fx,
                "fx_detail": fx_info,
                "accounting_currency": portfolio.currency,
                "reporting_status": "CURRENCY_MISMATCH" if not currency_ok else ("FX_UNAVAILABLE" if fx is None else "OK"),
                "universe": settings.universe,
                "initial_cash": round(portfolio.initial_cash, 4),
                "cash": round(portfolio.cash, 4),
                "equity": equity_rounded,
                "pnl": round(pnl, 4) if pnl is not None else None,
                "return_pct": return_pct,
                "realized_pnl": round(portfolio.realized_pnl, 4),
                "unrealized_pnl": unrealized_rounded,
                "open_positions": sum(1 for p in portfolio.positions.values() if p.quantity > 0),
                "initial_cash_reporting": round(portfolio.initial_cash * fx, 4) if fx is not None and currency_ok else None,
                "cash_reporting": round(portfolio.cash * fx, 4) if fx is not None and currency_ok else None,
                "equity_reporting": equity_reporting,
                "pnl_reporting": round(pnl_reporting, 4) if pnl_reporting is not None else None,
            })
        for row in rows:
            if row["reporting_status"] == "CURRENCY_MISMATCH":
                for field in ("initial_cash", "cash", "equity", "pnl", "return_pct", "realized_pnl", "unrealized_pnl"):
                    row[field] = None
        rows.sort(key=lambda row: (row["return_pct"] if row["return_pct"] is not None else float("-inf"), row["strategy_id"]), reverse=True)
        for index, row in enumerate(rows, 1):
            row["rank"] = index
        # A row's reporting currency is not necessarily the competition's TWD.
        # Native USD remains valid in its own ledger, but is not TWD without FX.
        native_totals: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if row["reporting_status"] == "CURRENCY_MISMATCH":
                continue
            currency = row["accounting_currency"]
            group = native_totals.setdefault(currency, {
                "currency": currency, "initial_cash": 0.0, "cash": 0.0,
                "equity": 0.0, "strategy_ids": [],
            })
            group["strategy_ids"].append(row["strategy_id"])
            group["initial_cash"] += row["initial_cash"]
            group["cash"] += row["cash"]
            if row["equity"] is None:
                group["equity"] = None
            elif group["equity"] is not None:
                group["equity"] += row["equity"]
        for group in native_totals.values():
            for field in ("initial_cash", "cash", "equity"):
                if group[field] is not None:
                    group[field] = round(group[field], 4)
        nav_reasons = []
        for row in rows:
            if row["reporting_status"] == "CURRENCY_MISMATCH":
                nav_reasons.append("CURRENCY_MISMATCH:" + row["strategy_id"])
            elif row["reporting_currency"] != "TWD" or row["fx_to_reporting"] is None:
                nav_reasons.append("FX_UNAVAILABLE:" + row["accounting_currency"] + "/TWD")
            elif row["equity_reporting"] is None:
                nav_reasons.append("UNPRICED_EQUITY:" + row["strategy_id"])
        nav_reasons = sorted(set(nav_reasons))
        has_unavailable = bool(nav_reasons)
        nav_status = ("UNAVAILABLE_FX" if any(reason.startswith("FX_UNAVAILABLE:") for reason in nav_reasons)
                      else ("UNAVAILABLE" if has_unavailable else "VALID"))
        eligible = [row for row in rows if row["reporting_currency"] == "TWD"]
        # The competition total is a TWD reporting figure: every participating
        # ledger must have a valid same-as-of conversion, not merely rows
        # already denominated in TWD. Native totals remain independently valid.
        if has_unavailable:
            eligible = []
        total_initial = sum(row["initial_cash_reporting"] for row in eligible if row["initial_cash_reporting"] is not None)
        total_equity = sum(row["equity_reporting"] for row in eligible if row["equity_reporting"] is not None)
        total_cash = sum(row["cash_reporting"] for row in eligible if row["cash_reporting"] is not None)
        if has_unavailable:
            total_pnl = None
            total_return_pct = None
        else:
            total_pnl = total_equity - total_initial
            total_return_pct = round((total_pnl / total_initial) * 100, 6) if total_initial else 0.0
        recent = [d.model_dump(mode="json") for d in self._decisions if d.strategy_id in {r["strategy_id"] for r in rows}][-20:]
        return {
            "paper_only": True,
            "broker_connected": False,
            "autonomous_capital_decisions": False,
            "is_canonical": False,
            "canonical_reference": "/api/paper/team-ops",
            "nav_status": nav_status,
            "nav_unavailable_reasons": nav_reasons,
            "native_totals": native_totals,
            "competition": {"name": "TW + US Autonomous Paper Team Competition", "start": start.isoformat(), "end": end.isoformat(), "days_remaining": days_remaining, "reporting_currency": "TWD"},
            "total": {"currency": "TWD", "cash": None if has_unavailable else round(total_cash, 4), "initial_cash": None if has_unavailable else round(total_initial, 4), "equity": None if has_unavailable else round(total_equity, 4), "pnl": round(total_pnl, 4) if total_pnl is not None else None, "return_pct": total_return_pct},
            "leaderboard": rows,
            "recent_actions": recent,
        }

    def get_durable_quote(self, symbol: str, *, refresh: bool = True) -> DurableQuoteSnapshot:
        mkt = self._market(symbol)
        now = self._now()
        is_open = intraday_market_open(symbol, now)

        cached = self._durable_quotes.get(symbol)
        if not refresh:
            if cached is None:
                return DurableQuoteSnapshot(symbol=symbol, market=mkt, source="missing",
                    observed_at=now, session="CLOSED", last_price=None,
                    age_seconds=999999.0, quality="missing", is_stale=True)
            event_at = cached.bar_time or cached.observed_at
            age = max(0.0, (now - event_at).total_seconds()) if event_at else 999999.0
            return cached.model_copy(update={"age_seconds": age,
                "is_stale": cached.is_stale or age > (1800.0 if is_open else 172800.0)})
        if cached is not None:
            age = max(0.0, (now - cached.observed_at).total_seconds()) if cached.observed_at else cached.age_seconds
            is_stale = cached.is_stale or (is_open and age > 172800.0) or (age > 172800.0)
            cached_src = (cached.source or "").lower()
            cached_qual = (cached.quality or "").lower()
            cached_is_auth = (
                cached.source != "missing"
                and not cached.is_synthetic
                and "synthetic" not in cached_qual
                and "fixture" not in cached_src
                and "synthetic" not in cached_src
                and "fallback" not in cached_src
                and "replay" not in cached_src
                and cached.last_price is not None
                and cached.last_price > 0
            )
            if cached_is_auth and not is_stale:
                bar = None
                try:
                    bar = self._market_call("get_latest_bar", symbol)
                except Exception:
                    pass
                if bar is not None:
                    bar_src = (bar.source or "").lower()
                    bar_qual = (bar.quality or "").lower()
                    bar_is_syn = (
                        getattr(bar, "is_synthetic", False)
                        or "synthetic" in bar_qual
                        or "synthetic" in bar_src
                        or "fallback" in bar_src
                        or "fixture" in bar_src
                        or "replay" in bar_src
                    )
                    bar_time = bar.timestamp or bar.observed_at
                    cached_time = cached.bar_time or cached.observed_at
                    if not bar_is_syn and bar_time and cached_time and bar_time > cached_time:
                        age_b = max(0.0, (now - bar.observed_at).total_seconds()) if bar.observed_at else bar.delay_seconds
                        stale_b = (bar.is_stale and is_open) or (age_b > 172800.0)
                        session = "REGULAR" if is_open else "CLOSED"
                        quote = DurableQuoteSnapshot(
                            symbol=symbol,
                            market=mkt,
                            source=bar.source,
                            observed_at=bar.observed_at or now,
                            bar_time=bar.timestamp,
                            session=session,
                            regular_price=bar.close,
                            extended_price=None,
                            last_price=bar.close,
                            bid=None,
                            ask=None,
                            age_seconds=round(age_b, 2),
                            quality=bar.quality or "delayed",
                            is_stale=stale_b,
                            is_synthetic=bar_is_syn,
                            fabrication_guard=True,
                        )
                        self._durable_quotes[symbol] = quote
                        return quote
                if is_stale != cached.is_stale or cached.age_seconds != round(age, 2):
                    cached = cached.model_copy(update={"is_stale": is_stale, "age_seconds": round(age, 2)})
                    self._durable_quotes[symbol] = cached
                return cached

        bar = None
        try:
            bar = self._market_call("get_latest_bar", symbol)
        except Exception:
            pass

        if bar is None:
            if cached is not None:
                return cached
            return DurableQuoteSnapshot(
                symbol=symbol,
                market=mkt,
                source="missing",
                observed_at=now,
                bar_time=None,
                session="CLOSED",
                last_price=None,
                bid=None,
                ask=None,
                age_seconds=999999.0,
                quality="missing",
                is_stale=True,
                is_synthetic=False,
                fabrication_guard=True,
            )

        is_syn = (
            "synthetic" in (bar.quality or "").lower()
            or "synthetic" in (bar.source or "").lower()
            or "fallback" in (bar.source or "").lower()
        )
        age = max(0.0, (now - bar.observed_at).total_seconds()) if bar.observed_at else bar.delay_seconds
        stale = (bar.is_stale and is_open) or (age > 172800.0)
        session = "REGULAR" if is_open else "CLOSED"

        quote = DurableQuoteSnapshot(
            symbol=symbol,
            market=mkt,
            source=bar.source,
            observed_at=bar.observed_at or now,
            bar_time=bar.timestamp,
            session=session,
            regular_price=bar.close,
            extended_price=None,
            last_price=bar.close,
            bid=None,  # Never fabricated from daily close
            ask=None,  # Never fabricated from daily close
            age_seconds=round(age, 2),
            quality=bar.quality or "delayed",
            is_stale=stale,
            is_synthetic=is_syn,
            fabrication_guard=True,
        )
        self._durable_quotes[symbol] = quote
        return quote

    def generate_canonical_team_ops(self, now: Optional[datetime] = None, *, refresh_symbols: Optional[set[str]] = None) -> Dict[str, Any]:
        now = now or self._now()
        self.reload_settings_if_needed()

        all_symbols = set()
        for s in self.paper_orders.experiments.values():
            all_symbols.update(s.universe)

        all_fills: List[Fill] = []
        strategy_fills: Dict[str, List[Fill]] = {}
        strategy_names: Dict[str, str] = {}
        for b in (DecisionScope.SWING, DecisionScope.INTRADAY):
            port = self.portfolio_manager.get_portfolio(b)
            for f in port.fills:
                all_fills.append(f)
                all_symbols.add(f.symbol)

        for s_id, s_cfg in self.paper_orders.experiments.items():
            strategy_names[s_id] = s_cfg.strategy_name
            strategy_fills[s_id] = []
            for b in (DecisionScope.SWING, DecisionScope.INTRADAY):
                sp = self.portfolio_manager.get_strategy_portfolio(s_id, b)
                for f in sp.fills:
                    strategy_fills[s_id].append(f)
                    all_symbols.add(f.symbol)

        # Strategy ledgers are authoritative too: currency-isolated fills are not
        # necessarily mirrored into the aggregate compatibility ledgers.
        for fills in strategy_fills.values():
            all_fills.extend(fills)
        unique_fills_map: Dict[str, Fill] = {f.fill_id: f for f in all_fills}
        unique_fills = list(unique_fills_map.values())

        quotes: Dict[str, DurableQuoteSnapshot] = {}
        quote_deadline = time.monotonic() + 8.0
        held_symbols = {f.symbol for f in unique_fills}
        skipped_quotes = []
        for sym in sorted(all_symbols, key=lambda name: (name not in held_symbols, name)):
            if time.monotonic() >= quote_deadline:
                skipped_quotes.append(sym)
                if sym in held_symbols:
                    market = Market.TW if sym.endswith((".TW", ".TWO")) else Market.US
                    quotes[sym] = DurableQuoteSnapshot(
                        symbol=sym, market=market, source="missing", observed_at=now,
                        quality="missing", is_stale=True,
                    )
                continue
            # Focused decisions refresh target and held symbols, not the whole
            # discovery universe. Other quotes retain honest cache/missing labels.
            quotes[sym] = self.get_durable_quote(sym, refresh=(
                refresh_symbols is None or sym in refresh_symbols or sym in held_symbols
            ))

        positions, pos_warnings = TeamOpsSnapshotBuilder.consolidate_positions(
            fills=unique_fills,
            quotes=quotes,
            strategy_fills=strategy_fills,
            strategy_names=strategy_names,
        )

        usd_fx_info = self.resolve_canonical_valuation_fx("USD", now)
        fx_rates = {"TWD": 1.0}
        if usd_fx_info.get("status") == "AVAILABLE" and usd_fx_info.get("rate"):
            fx_rates["USD"] = float(usd_fx_info["rate"])

        # Use one native cash account per strategy; bucket ledgers can alias the
        # same unified account and must never be summed separately.
        account_initial: Dict[str, float] = {}
        account_cash: Dict[str, float] = {}
        account_currency: Dict[str, str] = {}
        for s_id, s_cfg in self.paper_orders.experiments.items():
            acct = self.portfolio_manager._strategy_cash_accounts.get(s_id)
            if acct is not None:
                account_initial[s_id] = float(acct.initial_cash)
                account_cash[s_id] = float(acct.cash)
                account_currency[s_id] = acct.currency.upper()
            else:
                # Non-unified strategy: sum its bucket ledgers exactly once each.
                ledgers = self.portfolio_manager._strategy_ledgers.get(s_id, {})
                account_initial[s_id] = sum(float(l.initial_cash) for l in ledgers.values())
                account_cash[s_id] = sum(float(l.cash) for l in ledgers.values())
                account_currency[s_id] = next(iter(ledgers.values())).currency.upper() if ledgers else s_cfg.base_currency.upper()

        active_currencies = set(account_currency.values()) | {f.currency.upper() for f in unique_fills}
        fx_missing_warnings: List[str] = []
        missing_fx = False
        for currency in active_currencies:
            if currency == "TWD":
                continue
            info = usd_fx_info if currency == "USD" else self.resolve_canonical_valuation_fx(currency, now)
            rate = info.get("rate") if info.get("status") == "AVAILABLE" else None
            if rate is None or float(rate) <= 0:
                missing_fx = True
                fx_missing_warnings.append(f"FX_UNAVAILABLE:{currency}/TWD; canonical NAV cannot be reported.")
            else:
                fx_rates[currency] = float(rate)

        total_initial_twd = 0.0
        total_cash_twd = 0.0
        for s_id in account_initial:
            currency = account_currency[s_id]
            rate = fx_rates.get(currency)
            if rate is None:
                continue
            total_initial_twd += account_initial[s_id] * rate
            total_cash_twd += account_cash[s_id] * rate
        override_initial = getattr(self, "team_initial_capital", None)
        if override_initial is not None and not self.paper_orders.experiments:
            total_initial_twd = float(override_initial)
            total_cash_twd = total_initial_twd
        elif not self.paper_orders.experiments:
            total_initial_twd = TEAM_INITIAL_CAPITAL_TWD
            total_cash_twd = total_initial_twd

        nav_status, total_equity_twd, total_unrealized_twd, total_pnl_twd, return_pct, nav_warnings = (
            TeamOpsSnapshotBuilder.evaluate_nav(
                cash_twd=total_cash_twd,
                initial_capital_twd=total_initial_twd,
                positions=positions,
                quotes=quotes,
                fx_rates=fx_rates,
                cash_fx_missing=missing_fx,
            )
        )
        if missing_fx:
            nav_status = "FX_UNAVAILABLE"
            total_equity_twd = total_unrealized_twd = total_pnl_twd = return_pct = None

        tw_open = intraday_market_open("2330.TW", now)
        us_open = intraday_market_open("AAPL", now)
        tw_regime = "TRENDING_BULL" if tw_open else "CLOSED"
        us_regime = "TRENDING_BULL" if us_open else "CLOSED"

        # A missing discovery/watchlist quote must not halt a fully priced
        # target and portfolio. Held exposures always remain mandatory; a
        # focused decision also requires its target. Unfocused desk snapshots
        # require the currently open execution universe, not the other market's
        # closed-session watchlist. Missing mandatory quotes still fail closed.
        risk_symbols = held_symbols | (
            set(refresh_symbols) if refresh_symbols is not None else
            {sym for sym in all_symbols if intraday_market_open(sym, now)}
        )
        risk_quotes = [quotes.get(sym) for sym in risk_symbols]
        data_quality = "FRESH"
        if any(p.current_price is None for p in positions if p.quantity > 1e-6) or any(q is None or q.source == "missing" for q in risk_quotes):
            data_quality = "MISSING"
        elif any(q.is_stale for q in risk_quotes):
            data_quality = "STALE"
        elif any(q.is_synthetic for q in risk_quotes):
            data_quality = "SYNTHETIC"

        total_pos_mv = sum(p.market_value * fx_rates[p.currency] for p in positions
                           if p.market_value is not None and p.currency in fx_rates)
        gross_exposure = (total_pos_mv / total_equity_twd) if (total_equity_twd and total_equity_twd > 0) else 0.0
        net_exposure = gross_exposure

        posture = compute_team_posture(
            tw_regime=tw_regime,
            us_regime=us_regime,
            data_quality=data_quality,
            gross_exposure=gross_exposure,
            net_exposure=net_exposure,
        )

        # Compute momentum and liquidity for desk playbook selection
        momentum = 0.0
        if quotes:
            changes = []
            for sym in list(quotes)[:4]:
                if refresh_symbols is not None and sym not in refresh_symbols:
                    continue
                if time.monotonic() >= quote_deadline:
                    break
                try:
                    bars = self._market_call("get_bars", sym, limit=2)
                    if bars and len(bars) >= 2 and bars[-2].close:
                        changes.append((bars[-1].close / bars[-2].close) - 1.0)
                except Exception:
                    pass
            if changes:
                momentum = sum(changes) / len(changes)

        liquidity = "HIGH" if data_quality == "FRESH" and len(quotes) >= 2 else "NORMAL"

        playbook = self.select_active_playbook(
            tw_regime=tw_regime,
            us_regime=us_regime,
            data_quality=data_quality,
            tw_open=tw_open,
            us_open=us_open,
            gross_exposure=gross_exposure,
            momentum=momentum,
            liquidity=liquidity,
            now=now,
        )

        roles = self.get_functional_desk_roles(
            playbook=playbook,
            tw_regime=tw_regime,
            us_regime=us_regime,
            data_quality=data_quality,
            is_open=tw_open or us_open,
            gross_exposure=gross_exposure,
            next_review_time=playbook["next_review_time"],
        )

        desk_info = {
            "desk_id": DYNAMIC_DESK_ID,
            "desk_name": DYNAMIC_DESK_NAME,
            "active_playbook": playbook,
            "roles": roles,
            "next_review_time": playbook["next_review_time"],
        }

        quote_summary = {
            "total_symbols": len(quotes),
            "stale_count": sum(1 for q in quotes.values() if q.is_stale),
            "synthetic_count": sum(1 for q in quotes.values() if q.is_synthetic),
            "fresh_count": sum(1 for q in quotes.values() if not q.is_stale and not q.is_synthetic),
            "max_age_seconds": max([q.age_seconds for q in quotes.values()], default=0.0),
            "quotes": {k: v.model_dump(mode="json") for k, v in quotes.items()},
            "status": "FAIL_CLOSED" if posture.fail_closed else ("OK" if data_quality == "FRESH" else "DEGRADED"),
        }

        benchmark_info = {
            "benchmark_name": "BLENDED_TW_US_BENCHMARK",
            "period": "30D",
            "currency": "TWD",
            "team_return_pct": return_pct,
            "alpha_pct": None,
            "updated_at": now.isoformat(),
            "name": "BLENDED_TW_US_BENCHMARK",
            "tw_proxy": "0050.TW",
            "us_proxy": "SPY",
            "benchmark_return_pct": None,
            "relative_return_pct": None,
        }

        current_dd = 0.0
        max_dd = 0.0
        if total_equity_twd is not None and total_equity_twd > 0:
            point = TeamEquitySnapshot(timestamp=now, equity_twd=total_equity_twd)
            if (
                not self._team_equity_history
                or self._team_equity_history[-1].timestamp != point.timestamp
                or self._team_equity_history[-1].equity_twd != point.equity_twd
            ):
                self._team_equity_history.append(point)
                self._append_jsonl("team_equity_snapshots.jsonl", point)
            equities = [item.equity_twd for item in self._team_equity_history if item.equity_twd > 0]
            peak = max(equities)
            current_dd = round(max(0.0, (peak - total_equity_twd) / peak * 100.0), 4)
            peak_run = equities[0]
            for eq in equities:
                peak_run = max(peak_run, eq)
                max_dd = max(max_dd, round((peak_run - eq) / peak_run * 100.0, 4))

        risk_info = {
            "gross_exposure": round(gross_exposure, 4) if total_equity_twd is not None else None,
            "net_exposure": round(net_exposure, 4) if total_equity_twd is not None else None,
            "current_drawdown_pct": current_dd,
            "max_drawdown_pct": max_dd,
            "kill_switch": self.paper_orders.kill_switch,
            "kill_switch_active": self.paper_orders.kill_switch,
        }

        all_warnings = list(pos_warnings) + list(nav_warnings) + list(fx_missing_warnings)
        if skipped_quotes:
            all_warnings.append(f"QUOTE_BUDGET_EXCEEDED: {len(skipped_quotes)} symbols not fetched; held symbols have explicit missing quotes.")
        if posture.fail_closed:
            all_warnings.append(f"POSTURE_FAIL_CLOSED: {posture.rationale}")

        disk_snap = load_canonical_snapshot(self.runtime_dir)
        if disk_snap and isinstance(disk_snap.get("version"), int):
            self._canonical_version = max(self._canonical_version, disk_snap["version"])
        self._canonical_version += 1

        all_orders_list: List[Order] = []
        for b in (DecisionScope.SWING, DecisionScope.INTRADAY):
            all_orders_list.extend(self.paper_orders.portfolio_manager.get_portfolio(b).orders)
        all_orders_list.sort(key=lambda o: o.created_at)
        recent_orders = []
        for o in all_orders_list[-20:]:
            od = o.model_dump(mode="json")
            od["price"] = o.limit_price or o.stop_price or getattr(o, "price", None)
            recent_orders.append(od)

        recent_fills = []
        for f in unique_fills[-20:]:
            fd = f.model_dump(mode="json")
            fd["price"] = f.fill_price
            recent_fills.append(fd)

        recent_activity = [d.model_dump(mode="json") for d in self._decisions[-20:]]

        holdings = []
        for p in positions:
            hd = p.model_dump(mode="json")
            hd["entry_price"] = p.average_entry_price
            holdings.append(hd)

        safety_info = {
            "paper_only": True,
            "broker_connected": False,
            "broker_state": "DISCONNECTED",
            "autonomous_capital_decisions": False,
            "autonomous_live_capital_decisions": False,
            "autonomous_paper_execution": True,
            "derivative_trading_supported": False,
            "kill_switch": self.paper_orders.kill_switch,
            "kill_switch_active": self.paper_orders.kill_switch,
        }

        portfolio_info = {
            "reporting_currency": "TWD",
            "nav_status": nav_status,
            "cash": round(total_cash_twd, 4) if not missing_fx else None,
            "equity": total_equity_twd,
            "nav": total_equity_twd if nav_status == "OK" else None,
            "initial_cash": round(total_initial_twd, 4) if not missing_fx else None,
            "initial_capital": round(total_initial_twd, 4) if not missing_fx else None,
            "realized_pnl": round(sum(p.realized_pnl * fx_rates[p.currency] for p in positions
                                      if p.currency in fx_rates), 4) if not missing_fx else None,
            "unrealized_pnl": total_unrealized_twd,
            "total_pnl": total_pnl_twd,
            "return_pct": return_pct,
            "as_of": now.isoformat(),
            "fx_rates": fx_rates,
            "fx_accounting": {
                "reporting_currency": "TWD",
                "rates": fx_rates,
                "usd_twd_rate": usd_fx_info.get("rate") if usd_fx_info.get("status") == "AVAILABLE" else None,
                "source": usd_fx_info.get("source", "missing"),
                "source_tier": usd_fx_info.get("source_tier", "missing"),
                "source_label": usd_fx_info.get("label", "missing"),
                "is_simulated": usd_fx_info.get("is_simulated", False),
                "observed_at": usd_fx_info.get("observed_at"),
                "source_timestamp": usd_fx_info.get("source_timestamp"),
                "source_url": usd_fx_info.get("source_url"),
                "provenance": usd_fx_info.get("source_tier"),
            },
        }

        sessions_info = {
            "tw": {"status": "OPEN" if tw_open else "CLOSED", "session": "REGULAR" if tw_open else "CLOSED", "timezone": "Asia/Taipei"},
            "us": {"status": "OPEN" if us_open else "CLOSED", "session": "REGULAR" if us_open else "CLOSED", "timezone": "America/New_York"},
            "TW": {"status": "OPEN" if tw_open else "CLOSED", "session": "REGULAR" if tw_open else "CLOSED", "timezone": "Asia/Taipei"},
            "US": {"status": "OPEN" if us_open else "CLOSED", "session": "REGULAR" if us_open else "CLOSED", "timezone": "America/New_York"},
        }

        snapshot = {
            "server_time": now.isoformat(),
            "timestamp": now.isoformat(),
            "version": self._canonical_version,
            "safety": safety_info,
            "sessions": sessions_info,
            "data_freshness": quote_summary,
            "quote_freshness": quote_summary,
            "portfolio": portfolio_info,
            "fx_rates": fx_rates,
            "holdings": holdings,
            "canonical_positions": [p.model_dump(mode="json") for p in positions],
            "posture": posture.model_dump(mode="json"),
            "regime_and_posture": posture.model_dump(mode="json"),
            "regime": {"TW": tw_regime, "US": us_regime},
            "risk": risk_info,
            "quotes": {k: v.model_dump(mode="json") for k, v in quotes.items()},
            "orders": recent_orders,
            "fills": recent_fills,
            "activity": recent_activity,
            "benchmark": benchmark_info,
            "desk": desk_info,
            "cash": round(total_cash_twd, 4) if not missing_fx else None,
            "equity": total_equity_twd,
            "initial_capital": round(total_initial_twd, 4) if not missing_fx else None,
            "realized_pnl": round(sum(p.realized_pnl * fx_rates[p.currency] for p in positions
                                      if p.currency in fx_rates), 4) if not missing_fx else None,
            "unrealized_pnl": total_unrealized_twd,
            "total_pnl": total_pnl_twd,
            "return_pct": return_pct,
            "nav_status": nav_status,
            "nav": total_equity_twd if nav_status == "OK" else None,
            "data_status": "FAIL_CLOSED" if posture.fail_closed else ("OK" if data_quality == "FRESH" else "DEGRADED"),
            "integrity_warnings": all_warnings,
        }

        save_canonical_snapshot(self.runtime_dir, snapshot)
        self._last_canonical_snapshot = snapshot
        return snapshot

    def get_canonical_team_ops(self, is_read_only: bool = False) -> Dict[str, Any]:
        if is_read_only:
            disk_snapshot = load_canonical_snapshot(self.runtime_dir)
            if disk_snapshot is not None:
                return disk_snapshot
        with self._lock:
            if is_read_only:
                disk_snapshot = load_canonical_snapshot(self.runtime_dir)
                if disk_snapshot is not None:
                    return disk_snapshot
                now = self._now()
                return {
                    "status": "unavailable",
                    "server_time": now.isoformat(),
                    "timestamp": now.isoformat(),
                    "version": 0,
                    "data_freshness": {"status": "UNAVAILABLE", "quotes": {}},
                    "safety": {
                        "paper_only": True,
                        "broker_connected": False,
                        "broker_state": "DISCONNECTED",
                        "autonomous_capital_decisions": False,
                        "autonomous_live_capital_decisions": False,
                        "autonomous_paper_execution": True,
                        "derivative_trading_supported": False,
                        "kill_switch": self.paper_orders.kill_switch,
                        "kill_switch_active": self.paper_orders.kill_switch,
                    },
                    "sessions": {
                        "tw": {"status": "UNKNOWN", "session": "UNKNOWN", "timezone": "Asia/Taipei"},
                        "us": {"status": "UNKNOWN", "session": "UNKNOWN", "timezone": "America/New_York"},
                    },
                    "portfolio": {
                        "reporting_currency": "TWD",
                        "nav_status": "SNAPSHOT_UNAVAILABLE",
                        "cash": None,
                        "equity": None,
                        "nav": None,
                        "initial_cash": None,
                        "initial_capital": None,
                        "realized_pnl": None,
                        "unrealized_pnl": None,
                        "total_pnl": None,
                        "return_pct": None,
                        "as_of": None,
                    },
                    "holdings": [],
                    "canonical_positions": [],
                    "posture": {},
                    "desk": {
                        "desk_id": DYNAMIC_DESK_ID,
                        "desk_name": DYNAMIC_DESK_NAME,
                        "active_playbook": {
                            "playbook_id": "SESSION_STANDBY",
                            "playbook_name": "休市待命 (Session Standby)",
                            "description": "唯讀端無磁碟快照，處於待命狀態",
                            "selection_rationale": "無可用的權威快照，依據唯讀安全規範保持待命。",
                            "selected_at": now.isoformat(),
                            "next_review_time": (now + timedelta(seconds=3600)).isoformat(),
                        },
                        "roles": [],
                        "next_review_time": (now + timedelta(seconds=3600)).isoformat(),
                    },
                    "risk": {
                        "gross_exposure": 0.0,
                        "net_exposure": 0.0,
                        "current_drawdown_pct": 0.0,
                        "max_drawdown_pct": 0.0,
                        "kill_switch": self.paper_orders.kill_switch,
                        "kill_switch_active": self.paper_orders.kill_switch,
                    },
                    "quotes": {},
                    "orders": [],
                    "fills": [],
                    "activity": [],
                    "benchmark": {
                        "benchmark_name": "BLENDED_TW_US_BENCHMARK",
                        "period": "30D",
                        "currency": "TWD",
                        "team_return_pct": None,
                        "alpha_pct": None,
                        "updated_at": now.isoformat(),
                    },
                    "nav_status": "SNAPSHOT_UNAVAILABLE",
                    "data_status": "UNAVAILABLE",
                    "integrity_warnings": ["SNAPSHOT_UNAVAILABLE: No canonical snapshot found on disk in read-only mode."],
                }
            return self.generate_canonical_team_ops()


    def start(self, strategy_id: str) -> Dict[str, Any]:
        if getattr(self, "is_read_only", False):
            raise ValueError("RUNNER_READONLY_INSTANCE: Runner cannot be started on a read-only instance")
        with self._lock:
            settings = self.paper_orders.experiment_for(strategy_id)
            if not settings.enabled:
                raise ValueError("EXPERIMENT_DISABLED")
            current = self._active.get(strategy_id)
            if current and current.get("running"):
                return self.status(strategy_id)
            self._active[strategy_id] = {
                "running": True,
                "processing": False,
                "started_at": self._now().isoformat(),
                "last_run_id": None,
                "next_due_at": self._now().isoformat(),
                "current_task": "啟動排程監控，等待市場決策時點",
                "task_started_at": None,
            }
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._loop, name="cio-paper-runner", daemon=True)
                self._thread.start()
            return self.status(strategy_id)

    def stop(self, strategy_id: str) -> Dict[str, Any]:
        with self._lock:
            item = self._active.setdefault(strategy_id, {})
            item["running"] = False
            item["stopped_at"] = self._now().isoformat()
            if not any(v.get("running") for v in self._active.values()):
                self._stop.set()
            return self.status(strategy_id)

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = self._now()
            with self._lock:
                active = list(self._active.items())
            for strategy_id, state in active:
                if not state.get("running"):
                    continue
                settings = self.paper_orders.experiment_for(strategy_id)
                if settings.expires_at and settings.expires_at <= now:
                    self.stop(strategy_id)
                    continue
                next_due_raw = state.get("next_due_at")
                next_due = datetime.fromisoformat(next_due_raw) if next_due_raw else now
                if next_due.tzinfo is None:
                    next_due = next_due.replace(tzinfo=timezone.utc)
                if now < next_due:
                    continue
                symbols, slots = self._scheduled_symbols(settings, now)
                if not symbols:
                    # Closed markets cause no quote fetch, no decision record, and no
                    # fake "cycle". Re-check the calendar gate in one minute.
                    state["next_due_at"] = (now + timedelta(seconds=60)).isoformat()
                    state["processing"] = False
                    state["current_task"] = "市場休市，等待下一個決策時點"
                    state["task_started_at"] = None
                    continue
                state["processing"] = True
                state["current_task"] = f"分析 {len(symbols)} 檔標的並執行策略週期"
                state["task_started_at"] = now.isoformat()
                try:
                    result = self.run_scheduled_cycle(strategy_id)
                    if result.get("run") is not None:
                        state["last_run_id"] = result["run"]["run_id"]
                        state["last_task_status"] = result["run"].get("status")
                        state["last_task_completed_at"] = result["run"].get("completed_at")
                finally:
                    state["processing"] = False
                    state["current_task"] = "等待下一個市場決策時點"
                    state["task_started_at"] = None
                # run_scheduled_cycle alone commits valid slots; source failures
                # remain retryable and may not be consumed here a second time.
                # Intraday uses the configured decision cadence. Swing is
                # once-per-session and the persisted slot prevents repeats.
                state["next_due_at"] = (now + timedelta(seconds=settings.cadence_seconds)).isoformat()
            self._stop.wait(1.0)

    def status(self, strategy_id: str) -> Dict[str, Any]:
        settings = self.paper_orders.experiment_for(strategy_id)
        latest = next((r for r in reversed(self._runs) if r.strategy_id == strategy_id), None)
        state = self._active.get(strategy_id, {})
        return {
            "strategy_id": strategy_id,
            "enabled": settings.enabled,
            "running": bool(state.get("running")),
            "processing": bool(state.get("processing")),
            "current_task": state.get("current_task") or ("排程未啟動" if not state.get("running") else "等待下一個市場決策時點"),
            "task_started_at": state.get("task_started_at"),
            "last_task_status": state.get("last_task_status"),
            "last_task_completed_at": state.get("last_task_completed_at"),
            "mode": settings.mode.value,
            "cadence_seconds": settings.cadence_seconds if settings.mode == DecisionScope.INTRADAY else None,
            "decision_schedule": "market_open_only" if settings.mode == DecisionScope.INTRADAY else "once_after_each_market_close",
            "next_due_at": state.get("next_due_at"),
            "expires_at": settings.expires_at.isoformat() if settings.expires_at else None,
            "last_run": latest.model_dump(mode="json") if latest else None,
            "paper_only": True,
            "broker_connected": False,
            "llm_policy": "intraday_veto_fail_closed;swing_review_only",
            "llm_may_change_quantity": False,
            "llm_may_place_order": False,
        }

    def history(self, strategy_id: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
        runs = [r for r in self._runs if strategy_id is None or r.strategy_id == strategy_id][-limit:]
        decisions = [d for d in self._decisions if strategy_id is None or d.strategy_id == strategy_id][-limit:]
        equity = [e for e in self._equity if strategy_id is None or e.strategy_id == strategy_id][-limit:]
        return {"runs": [r.model_dump(mode="json") for r in runs], "decisions": [d.model_dump(mode="json") for d in decisions], "equity_snapshots": [e.model_dump(mode="json") for e in equity], "paper_only": True, "broker_connected": False}

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            for state in self._active.values():
                state["running"] = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
