"""FastAPI application shell for CIO Market Lab.

Serves the REST API for market overviews, strategy management, paper portfolios,
diagnostics, research intake, and bridges to Jev and Hermes chat.
Mounts the static plugin UI.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import uuid
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from urllib.parse import urlsplit
from urllib.request import urlopen
from cio_market_lab.domain.models import (
    Bar,
    CIODecisionContextRequest,
    CIODecisionPacket,
    DecisionScope,
    Market,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PaperPortfolio,
    Signal,
    StrategyStatus,
)
from cio_market_lab.domain.events import EventEnvelope, EventType
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.engine.capabilities import get_derivative_capabilities_report
from cio_market_lab.engine.paper_orders import (
    OrderOrigin,
    PaperExperimentSettings,
    PaperOrderRequest,
    PaperOrderService,
    RiskLimits,
)
from cio_market_lab.engine.team_ops import (
    TEAM_INITIAL_CAPITAL_TWD,
    to_public_team_ops_snapshot,
)
from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner
from cio_market_lab.engine.market_schedule import intraday_market_open
from cio_market_lab.data.yahoo import YahooAdapter
from cio_market_lab.api.market_routes import router as market_data_router
from cio_market_lab.api.shioaji_facade import router as shioaji_facade_router
from cio_market_lab.events.store import EventStore
from cio_market_lab.integrations.hermes_chat import (
    DEFAULT_PINNED_MODEL_ID,
    DEFAULT_PINNED_PROVIDER_ID,
    PAPER_DISCLAIMER,
    HermesChatDraft,
    HermesCIODecisionExecutor,
    build_chat_draft,
    get_hermes_runtime_contract,
    run_hermes_cli_chat,
)
from cio_market_lab.integrations.jev import (
    JevChoiceRequest,
    JevChoiceResponse,
    JevDecisionProvider,
)
from cio_market_lab.research.browser import (
    FakeBrowserResearchAdapter,
    ResearchItem,
)
from cio_market_lab.strategies.registry import StrategyRegistry


PROJECT_MONEY_MANDATE: Dict[str, Any] = {
    "project": "Project Money",
    "status": "ACTIVE_PAPER_EXPERIMENT",
    "objective": "terminal paper NAV growth over existing month",
    "evaluation_window": {"starts_on": "2026-09-26", "ends_on": "2026-10-26", "duration_days": 30},
    "client_role": "OBSERVER_ONLY",
    "decision_owner": "MAIN_CIO",
    "research_scope": ["COMPANIES", "EQUITIES", "ETFS", "MARKET_REGIME"],
    "execution_scope": {
        "paper_only": True,
        "markets": ["TW", "US"],
        "supported_instruments": ["CASH_EQUITY", "SPOT_ETF", "LONG_PREMIUM_OPTION"],
        "unsupported_capabilities": [
            "FUTURES",
            "UNCOVERED_SHORT_OPTIONS",
            "OPTION_ASSIGNMENT",
            "MARGIN_TRADING",
            "SHORT_SELLING",
            "SYNTHETIC_DERIVATIVES",
        ],
        "intraday_permitted": True,
        "swing_permitted": True,
        "real_money_permitted": False,
    },
    "team_integration": {
        "workers": "research_only",
        "worker_role": "engineering_worker_validate_account_simulate_only",
        "main_cio": "final paper capital decision",
        "current_execution_engine": "CIO-owned autonomous paper laboratory; Python validates, accounts, and simulates only",
        "llm_evaluator_authority": "veto_only_when_configured",
    },
    "live_promotion": {
        "automatic": False,
        "requires_new_client_authorization": True,
        "minimum_evidence": [
            "audited canonical ledger and NAV",
            "zero same-bar or look-ahead fills",
            "controlled maximum drawdown",
            "positive risk-adjusted return across multiple regimes",
            "reproducible execution and complete decision audit trail",
        ],
    },
}


class StrategyActionRequest(BaseModel):
    authority: str = "Main CIO"


class StrategyVersionRequest(BaseModel):
    code_hash: str
    authority: str = "Main CIO"


class ChatDraftRequest(BaseModel):
    symbol: str
    user_prompt: str
    run_id: Optional[str] = None
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    visible_metrics: Optional[Dict[str, Any]] = None
    evidence_paths: Optional[List[str]] = None
    extra_context: Optional[Dict[str, Any]] = None


class ChatRunRequest(BaseModel):
    message: str
    session_id: str = "cio-market-lab"


class ResearchIntakeRequest(BaseModel):
    # Support raw document intake
    url: Optional[str] = None
    title: Optional[str] = None
    claims: List[str] = Field(default_factory=list)
    related_symbols: List[str] = Field(default_factory=list)
    hypothesis: Optional[str] = None
    source_mode: str = "manual_intake"
    status: Optional[str] = None
    provenance: Dict[str, Any] = Field(default_factory=dict)

    # Support structured public research evidence (public_research_schema.json)
    research_id: Optional[str] = None
    symbol: Optional[str] = None
    source_url: Optional[str] = None
    source_tier: Optional[str] = None
    observed_at: Optional[Union[str, datetime]] = None
    published_at: Optional[str] = None
    is_fixture: Optional[bool] = None
    verification_status: Optional[str] = None
    verified_facts: List[str] = Field(default_factory=list)
    research_scope: Optional[str] = None
    limitations: List[str] = Field(default_factory=list)
    raw_metadata: Dict[str, Any] = Field(default_factory=dict)


def normalize_local_origin(orig: str) -> Optional[str]:
    try:
        trimmed = orig.strip()
        if not trimmed or trimmed == "*":
            return None
        parsed = urlsplit(trimmed)
        if parsed.scheme not in ("http", "https"):
            return None
        if "@" in parsed.netloc or parsed.username or parsed.password:
            return None
        if parsed.query or parsed.fragment:
            return None
        if parsed.path not in ("", "/"):
            return None
        hostname = (parsed.hostname or "").lower()
        if hostname not in ("localhost", "127.0.0.1", "::1"):
            return None
        port_part = f":{parsed.port}" if parsed.port is not None else ""
        host_part = f"[{hostname}]" if ":" in hostname else hostname
        return f"{parsed.scheme}://{host_part}{port_part}"
    except Exception:
        return None


from cio_market_lab.api.security import is_owner_port, assert_owner_port, is_read_only_role
from cio_market_lab.data.market_data import CompositeMarketDataAdapter
from cio_market_lab.engine.continuation import ContinuationLockAcquisitionError


# Global or app-level state container
class AppState:
    def __init__(
        self,
        workspace_root: Path,
        runtime_dir: Optional[Union[str, Path]] = None,
        is_read_only: Optional[bool] = None,
        market_adapter: Optional[MarketDataAdapter] = None,
        event_store: Optional[EventStore] = None,
        fixture_mode: Optional[bool] = None,
    ):
        self.workspace_root = Path(workspace_root).resolve()
        self.strategies_dir = self.workspace_root / "strategies"

        # Role resolution
        if is_read_only is not None:
            self.is_read_only = is_read_only
        else:
            self.is_read_only = is_read_only_role()

        # Canonical runtime directory resolution upfront
        default_root = Path(__file__).resolve().parent.parent.parent
        if runtime_dir is not None:
            self.runtime_dir = Path(runtime_dir).resolve()
        elif self.workspace_root != default_root.resolve():
            self.runtime_dir = (self.workspace_root / "data" / "runtime").resolve()
        elif os.environ.get("CIO_MARKET_LAB_RUNTIME_DIR"):
            self.runtime_dir = Path(os.environ["CIO_MARKET_LAB_RUNTIME_DIR"]).resolve()
        else:
            self.runtime_dir = (self.workspace_root / "data" / "runtime").resolve()

        if not self.is_read_only:
            self.runtime_dir.mkdir(parents=True, exist_ok=True)

        from cio_market_lab.strategies.durable_registry import DurableStrategyRegistry
        self.registry = DurableStrategyRegistry(
            self.strategies_dir, self.runtime_dir / 'strategy_registry',
            read_only=self.is_read_only,
        )

        self.portfolio_manager = PortfolioManager(
            initial_cash_swing=TEAM_INITIAL_CAPITAL_TWD,
            initial_cash_intraday=TEAM_INITIAL_CAPITAL_TWD,
        )

        # Canonical persistent EventStore on SQLite (events.db)
        if event_store is not None:
            self.event_store = event_store
        else:
            events_db_path = self.runtime_dir / "events.db"
            self.event_store = EventStore(events_db_path, read_only=self.is_read_only)

        # Runtime-native paper account metadata is explicit; never infer cash or FX.
        account_path = self.runtime_dir / "paper_account.json"
        account = json.loads(account_path.read_text()) if account_path.exists() else {}
        account_cash = float(account.get("initial_cash", TEAM_INITIAL_CAPITAL_TWD))
        account_currency = account.get("currency", "TWD")
        if account_cash < 0 or not __import__('math').isfinite(account_cash) or account_currency not in {"TWD", "USD"}:
            raise ValueError("INVALID_PAPER_ACCOUNT_CONFIGURATION")
        for bucket in (DecisionScope.SWING, DecisionScope.INTRADAY):
            restored = self.event_store.reconstruct_portfolio(
                bucket, initial_cash=account_cash, currency=account_currency
            )
            self.portfolio_manager.restore_portfolio(restored)

        self.paper_orders = PaperOrderService(self.portfolio_manager, self.event_store)
        self.jev_provider = JevDecisionProvider()

        # Canonical CompositeMarketDataAdapter (TW: MIS/Yahoo, US: CNBC sale/Yahoo bars)
        if market_adapter is not None:
            self.market_adapter = market_adapter
        else:
            offline_mode = os.environ.get("CIO_MARKET_LAB_OFFLINE", "0") == "1"
            self.market_adapter = CompositeMarketDataAdapter(offline_mode=offline_mode)

        # Register startup CIO decision executor with explicit configurable provider and model
        hermes_workspace = os.getenv("CIO_HERMES_WORKSPACE")
        hermes_session = os.getenv("CIO_HERMES_SESSION_ID", "cio-market-lab")
        hermes_timeout = int(os.getenv("CIO_HERMES_TIMEOUT_SECONDS", "240"))
        cio_provider = os.getenv("CIO_PROVIDER_ID")
        cio_model = os.getenv("CIO_MODEL_ID")
        self.cio_executor = HermesCIODecisionExecutor(
            session_id=hermes_session,
            workspace_root=hermes_workspace,
            timeout_seconds=hermes_timeout,
            provider_id=cio_provider if cio_provider is not None else DEFAULT_PINNED_PROVIDER_ID,
            model_id=cio_model if cio_model is not None else DEFAULT_PINNED_MODEL_ID,
        )
        material_provider = None
        frozen_context = None
        material_enabled = False
        if not self.is_read_only and os.environ.get("CIO_MATERIAL_GATE_ENABLED") == "1":
            packet_root = Path(os.environ.get("CIO_STAGE_D_PACKET_ROOT", str(self.runtime_dir / "official_packets")))
            trusted_manifest = os.environ.get("CIO_STAGE_D_TRUSTED_MANIFEST")
            from cio_market_lab.engine.stage_d_observation import make_packet_observation_provider
            material_provider = make_packet_observation_provider(
                packet_root, trusted_manifest=Path(trusted_manifest) if trusted_manifest else None
            )
            material_enabled = True
        self.runner = AutonomousPaperRunner(
            root=self.workspace_root,
            portfolio_manager=self.portfolio_manager,
            paper_orders=self.paper_orders,
            market_adapter=self.market_adapter,
            cio_executor=self.cio_executor,
            runtime_dir=self.runtime_dir,
            is_read_only=self.is_read_only,
            material_observation_provider=material_provider,
            material_gate_enabled=material_enabled,
            cio_session_id=os.environ.get("CIO_HERMES_SESSION_ID", "project-money-main-cio"),
            frozen_decision_context=frozen_context,
        )
        # Explicit reporting-only native books. Never infer accounts from directories.
        books_path = self.runtime_dir / "paper_books.json"
        if books_path.exists():
            from cio_market_lab.engine.paper_orders import PaperExperimentSettings
            seen_books, seen_ids = set(), set()
            for book in json.loads(books_path.read_text())["books"]:
                sid = book["strategy_id"]
                directory = (self.runtime_dir / book["runtime_dir"]).resolve()
                if not directory.is_relative_to(self.runtime_dir.resolve()) or directory in seen_books or sid in seen_ids:
                    raise ValueError("INVALID_OR_DUPLICATE_PAPER_BOOK")
                seen_books.add(directory)
                seen_ids.add(sid)
                metadata = json.loads((directory / "paper_account.json").read_text())
                currency, capital = metadata["currency"], float(metadata["initial_cash"])
                settings = PaperExperimentSettings(strategy_id=sid, universe=book["universe"],
                    base_currency=currency, reporting_currency="TWD", initial_cash=capital,
                    enabled=False)
                # External books cannot be executed by this merged reporting runtime.
                if sid in self.paper_orders.experiments:
                    raise ValueError("PAPER_BOOK_STRATEGY_COLLISION")
                source_store = EventStore(directory / "events.db", read_only=True)
                portfolio = source_store.reconstruct_portfolio(DecisionScope.SWING,
                    initial_cash=capital, currency=currency)
                self.portfolio_manager.register_strategy(sid, capital, currency=currency)
                ledger = self.portfolio_manager.get_strategy_ledger(sid, DecisionScope.SWING)
                ledger.cash = portfolio.cash
                ledger.equity = portfolio.equity
                ledger.realized_pnl = portfolio.realized_pnl
                ledger.unrealized_pnl = portfolio.unrealized_pnl
                ledger.positions = dict(portfolio.positions)
                ledger.orders = list(portfolio.orders)
                ledger.fills = list(portfolio.fills)
                self.portfolio_manager._strategy_cash_accounts[sid]._applied_fill_ids = {f.fill_id for f in portfolio.fills}
                self.paper_orders.experiments[sid] = settings
        self.runner.set_cio_executor(self.cio_executor)
        self.research_adapter = FakeBrowserResearchAdapter()
        self.signals_log: List[Dict[str, Any]] = []
        self.experiments_log: List[Dict[str, Any]] = []

        if fixture_mode is not None:
            self.fixture_mode = fixture_mode
        else:
            self.fixture_mode = (
                os.environ.get("CIO_FIXTURE_MODE") == "1"
                or os.environ.get("CIO_ENABLE_FIXTURES") == "1"
                or os.environ.get("CIO_ENABLE_DEMO_FIXTURES") == "1"
            )

        self._initialize_seed_data()

    def market_snapshot(self, symbol: str, name: str) -> Dict[str, Any]:
        bars = self.market_adapter.get_bars(symbol)
        if not bars:
            return {
                "symbol": symbol, "name": name, "last": None,
                "change_pct": None, "volume": None, "is_stale": True,
                "source": "unavailable", "quality": "missing",
            }
        latest = bars[-1]
        previous = bars[-2] if len(bars) > 1 else latest
        change_pct = ((latest.close / previous.close) - 1.0) * 100.0 if previous.close else 0.0
        return {
            "symbol": symbol,
            "name": name,
            "last": round(latest.close, 4),
            "change_pct": round(change_pct, 4),
            "volume": latest.volume,
            "is_stale": latest.is_stale,
            "source": latest.source,
            "quality": latest.quality,
            "market_timestamp": latest.timestamp.isoformat(),
            "observed_at": latest.observed_at.isoformat(),
            "delay_seconds": latest.delay_seconds,
        }

    def _initialize_seed_data(self) -> None:
        # Discover available strategies in strategies/
        try:
            self.registry.discover_all()
        except Exception:
            pass

        # In default production, do NOT inject unmarked demo signals or completed experiments.
        # Only inject in explicitly marked fixture mode, and mark objects with is_fixture=True.
        # Read-only instance must neither seed nor persist data on startup.
        if self.is_read_only or not self.fixture_mode:
            return

        # Seed sample signals and simulated paper orders
        now = datetime.now(timezone.utc)
        sig1 = {
            "signal_id": "sig-seed-001",
            "strategy_id": "volatility_contraction",
            "version": "1.0.0",
            "symbol": "2330.TW",
            "market": "TW",
            "decision_scope": "swing",
            "side": "BUY",
            "generated_at": now.isoformat(),
            "reason_codes": ["VCP_CONTRACTION_MET", "RVOL_SURGE"],
            "evidence": {"compression_ratio": 0.62, "rvol": 1.45},
            "entry_model": "NEXT_OPEN",
            "max_simulated_risk": 0.02,
            "is_fixture": True,
        }
        sig2 = {
            "signal_id": "sig-seed-002",
            "strategy_id": "opening_range_breakout",
            "version": "1.0.0",
            "symbol": "AAPL",
            "market": "US",
            "decision_scope": "intraday",
            "side": "BUY",
            "generated_at": now.isoformat(),
            "reason_codes": ["ORB_HIGH_BREAKOUT", "RVOL_CONFIRMATION"],
            "evidence": {"orb_high": 232.50, "breakout_volume_ratio": 1.82},
            "entry_model": "CONFIRMED_BREAKOUT",
            "max_simulated_risk": 0.015,
            "is_fixture": True,
        }
        self.signals_log.extend([sig1, sig2])

        # Seed walk-forward / replay experiment
        self.experiments_log.append({
            "experiment_id": "exp-wf-20260901",
            "name": "VCP + ORB Walk-Forward Replay",
            "mode": "rolling_window_walk_forward",
            "symbols": ["2330.TW", "AAPL", "NVDA"],
            "status": "COMPLETED",
            "net_expectancy": 0.0185,
            "max_drawdown": 0.042,
            "hit_rate": 0.615,
            "turnover": 4.2,
            "cost_slippage_stress_tested": True,
            "completed_at": now.isoformat(),
            "is_fixture": True,
        })


def create_app(
    workspace_root: Optional[Path] = None,
    runtime_dir: Optional[Union[str, Path]] = None,
    is_read_only: Optional[bool] = None,
    market_adapter: Optional[MarketDataAdapter] = None,
    event_store: Optional[EventStore] = None,
    fixture_mode: Optional[bool] = None,
) -> FastAPI:
    root = workspace_root or Path(__file__).resolve().parent.parent.parent
    state = AppState(
        workspace_root=root,
        runtime_dir=runtime_dir,
        is_read_only=is_read_only,
        market_adapter=market_adapter,
        event_store=event_store,
        fixture_mode=fixture_mode,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Resume enabled, unexpired paper experiments after a service restart.
        # This never connects to a broker; it only restores local simulation threads.
        now = datetime.now(timezone.utc)
        if not state.is_read_only:
            for settings in state.paper_orders.list_experiments():
                if not settings.enabled:
                    continue
                if settings.expires_at and settings.expires_at <= now:
                    continue
                try:
                    state.runner.start(settings.strategy_id)
                except (ValueError, RuntimeError):
                    # Status/history expose a failed restart without preventing the UI
                    # and diagnostics endpoints from starting.
                    continue
        try:
            yield
        finally:
            if not state.is_read_only:
                state.runner.shutdown()

    app = FastAPI(
        title="CIO Market Lab Engine",
        version="0.1.0",
        description="Local simulation engine, strategy lab, and paper portfolio host for Hermes Desktop.",
        lifespan=lifespan,
    )

    # Attach state to app
    app.state.app_state = state
    app.state.is_read_only = state.is_read_only

    local_origins = [
        "http://localhost",
        "http://127.0.0.1",
        "http://[::1]",
        "http://localhost:8765",
        "http://127.0.0.1:8765",
        "http://[::1]:8765",
        "http://localhost:21322",
        "http://127.0.0.1:21322",
        "http://[::1]:21322",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://[::1]:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://[::1]:5173",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://[::1]:8080",
    ]
    extra_origins = os.environ.get("CIO_CORS_ORIGINS", "")
    if extra_origins:
        for orig in extra_origins.split(","):
            norm = normalize_local_origin(orig)
            if norm and norm not in local_origins:
                local_origins.append(norm)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=local_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Keep the expanded market-data surface in a separate router so it can be
    # mounted by other hosts without importing the full application shell.
    app.include_router(market_data_router, prefix="/api/markets")
    # Singular alias matches the existing API naming convention and keeps the
    # expanded router reusable for hosts that already use /api/market.
    app.include_router(market_data_router, prefix="/api/market")
    app.include_router(shioaji_facade_router)
    from cio_market_lab.api.replay_routes import router as replay_lab_router
    app.state.replay_workspace_root = root
    app.include_router(replay_lab_router)
    from cio_market_lab.api.event_stream import router as event_stream_router
    app.include_router(event_stream_router)

    @app.get('/lab/replay', include_in_schema=False)
    def replay_lab_page():
        from fastapi.responses import FileResponse
        page = root / 'plugin' / 'ui' / 'replay.html'
        if not page.exists():
            raise HTTPException(404, 'REPLAY_UI_NOT_INSTALLED')
        return FileResponse(page)

    # --- Health Endpoints ---
    @app.get("/health", tags=["System"])
    @app.get("/api/health", tags=["System"])
    def get_health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "version": "0.1.0",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "simulation_only",
            "paper_only": True,
            "broker_connected": False,
            "autonomous_capital_decisions": False,
            "autonomous_live_capital_decisions": False,
            "autonomous_paper_execution": True,
            "capabilities": {
                "supported_instruments": ["CASH_EQUITY", "SPOT_ETF"],
                "unsupported_capabilities": [
                    "FUTURES",
                    "UNCOVERED_SHORT_OPTIONS",
                    "OPTION_ASSIGNMENT",
                    "MARGIN_TRADING",
                    "SHORT_SELLING",
                    "SYNTHETIC_DERIVATIVES",
                    "LONG_PREMIUM_OPTIONS",
                ],
                "derivative_trading_supported": False,
                "option_scope": "UNAVAILABLE_IMPLEMENTED_COMPONENTS_NOT_ACTIVATED",
                "intraday_paper_trading_supported": True,
            },
            "disclaimer": PAPER_DISCLAIMER,
        }

    # --- Overview Cockpit ---
    @app.get("/api/overview", tags=["Cockpit"])
    def get_overview() -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        st = app.state.app_state
        strats = st.registry.list_all()
        swing_port = st.portfolio_manager.get_portfolio(DecisionScope.SWING)
        intra_port = st.portfolio_manager.get_portfolio(DecisionScope.INTRADAY)
        tw_sample = st.market_snapshot("2330.TW", "TSMC")
        us_sample = st.market_snapshot("NVDA", "NVIDIA")

        return {
            "timestamp": now.isoformat(),
            "mode": "simulation_only",
            "disclaimer": PAPER_DISCLAIMER,
            "market_regimes": {
                "TW": {
                    "exchange": "TWSE",
                    "status": "CLOSED_REPLAY_MODE",
                    "freshness": "15m delayed bootstrap",
                    "sample_ticker": "2330.TW",
                    "last_price": tw_sample["last"],
                    "change_pct": tw_sample["change_pct"],
                    "source": tw_sample["source"],
                    "market_timestamp": tw_sample.get("market_timestamp"),
                },
                "US": {
                    "exchange": "NASDAQ/NYSE",
                    "status": "SIMULATION_REPLAY_MODE",
                    "freshness": "15m delayed bootstrap",
                    "sample_ticker": "NVDA",
                    "last_price": us_sample["last"],
                    "change_pct": us_sample["change_pct"],
                    "source": us_sample["source"],
                    "market_timestamp": us_sample.get("market_timestamp"),
                },
            },
            "strategies_summary": {
                "total": len(strats),
                "candidate": sum(1 for s in strats if s.status == StrategyStatus.CANDIDATE),
                "paper_active": sum(1 for s in strats if s.status == StrategyStatus.PAPER_ACTIVE),
                "paused": sum(1 for s in strats if s.status == StrategyStatus.PAUSED),
            },
            "portfolios_summary": {
                "swing": {
                    "equity": swing_port.equity,
                    "cash": swing_port.cash,
                    "nav_status": swing_port.nav_status,
                    "nav_reason": (
                        "One or more open positions lack a current market mark."
                        if swing_port.equity is None
                        else None
                    ),
                    "realized_pnl": swing_port.realized_pnl,
                    "open_positions": len(swing_port.positions),
                    "open_orders": len(swing_port.orders),
                },
                "intraday": {
                    "equity": intra_port.equity,
                    "cash": intra_port.cash,
                    "nav_status": intra_port.nav_status,
                    "nav_reason": (
                        "One or more open positions lack a current market mark."
                        if intra_port.equity is None
                        else None
                    ),
                    "realized_pnl": intra_port.realized_pnl,
                    "open_positions": len(intra_port.positions),
                    "open_orders": len(intra_port.orders),
                },
            },
            "recent_signals_count": len(st.signals_log),
            "research_inbox_count": (
                len(st.runner.research_reader.list_inbox())
                if hasattr(st.runner, "research_reader") and st.runner.research_reader is not None and st.runner.research_reader.list_inbox()
                else len(st.research_adapter.list_inbox())
            ),
            "safety_guards": {
                "kill_switch": st.paper_orders.kill_switch,
                "stale_data_rejection": True,
                "broker_credentials": "NONE_CONFIGURED",
            },
        }

    # --- Watchlists ---
    @app.get("/api/watchlists", tags=["Markets"])
    def get_watchlists() -> Dict[str, Any]:
        st = app.state.app_state
        tw_symbols = [("2330.TW", "TSMC"), ("2454.TW", "MediaTek"),
                      ("2317.TW", "Hon Hai"), ("3231.TW", "Wistron")]
        us_symbols = [("NVDA", "NVIDIA"), ("AAPL", "Apple"),
                      ("MSFT", "Microsoft"), ("TSLA", "Tesla")]
        return {
            "TW": [st.market_snapshot(symbol, name) for symbol, name in tw_symbols],
            "US": [st.market_snapshot(symbol, name) for symbol, name in us_symbols],
            "disclaimer": PAPER_DISCLAIMER,
            "provider_mode": st.market_adapter.last_fetch_mode,
            "provider_error": st.market_adapter.last_error,
        }

    @app.get("/api/market/bars/{symbol}", tags=["Markets"])
    def get_market_bars(
        symbol: str,
        timeframe: str = Query(default="1D"),
        limit: int = Query(default=80, ge=2, le=500),
    ) -> Dict[str, Any]:
        st = app.state.app_state
        try:
            from cio_market_lab.data.tw_official import resolve_tw_symbol
            normalized = resolve_tw_symbol(symbol) if symbol[:1].isdigit() else symbol
            bars = st.market_adapter.get_bars(normalized, timeframe=timeframe, limit=limit)
            timeframe_metadata = st.market_adapter.timeframe_metadata(timeframe)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {
            "symbol": normalized,
            **timeframe_metadata,
            "count": len(bars),
            "provider_mode": st.market_adapter.last_fetch_mode,
            "provider_error": st.market_adapter.last_error,
            "data_status": "ok" if bars else "unavailable",
            "bars": [bar.model_dump(mode="json") for bar in bars],
        }

    # --- Strategy Lab Endpoints ---
    @app.get("/api/strategies", tags=["Strategies"])
    def list_strategies() -> List[Dict[str, Any]]:
        st = app.state.app_state
        strats = st.registry.list_all()
        return [s.model_dump() for s in strats]

    @app.post("/api/strategies/{strategy_id}/activate", tags=["Strategies"])
    def activate_strategy(strategy_id: str, request: Request, req: StrategyActionRequest = StrategyActionRequest()) -> Dict[str, Any]:
        assert_owner_port(request, "Strategy mutations")
        st = app.state.app_state
        try:
            reg = st.registry.activate_strategy(strategy_id, authority=req.authority)
            return {"status": "success", "strategy": reg.model_dump()}
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/strategies/{strategy_id}/hot-swap", tags=["Strategies"])
    def hot_swap_strategy(strategy_id: str, request: Request, req: StrategyVersionRequest) -> Dict[str, Any]:
        assert_owner_port(request, "Strategy hot-swap")
        st = app.state.app_state
        try:
            reg = st.registry.hot_swap_strategy(strategy_id, req.code_hash, authority=req.authority)
            return {"status": "success", "strategy": reg.model_dump()}
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/strategies/{strategy_id}/rollback", tags=["Strategies"])
    def rollback_strategy(strategy_id: str, request: Request, req: StrategyVersionRequest) -> Dict[str, Any]:
        assert_owner_port(request, "Strategy rollback")
        st = app.state.app_state
        try:
            reg = st.registry.rollback_strategy(strategy_id, req.code_hash, authority=req.authority)
            return {"status": "success", "strategy": reg.model_dump()}
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/strategies/{strategy_id}/pause", tags=["Strategies"])
    def pause_strategy(strategy_id: str, request: Request, req: StrategyActionRequest = StrategyActionRequest()) -> Dict[str, Any]:
        assert_owner_port(request, "Strategy mutations")
        st = app.state.app_state
        try:
            reg = st.registry.pause_strategy(strategy_id, authority=req.authority)
            return {"status": "success", "strategy": reg.model_dump()}
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Strategy '{strategy_id}' not found")
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    # --- Paper Portfolios ---
    @app.get("/api/portfolios", tags=["Portfolio"])
    @app.get("/api/portfolio", tags=["Portfolio"])
    def get_portfolios() -> Dict[str, Any]:
        st = app.state.app_state
        swing = st.portfolio_manager.get_portfolio(DecisionScope.SWING)
        intraday = st.portfolio_manager.get_portfolio(DecisionScope.INTRADAY)
        return {
            "mode": "simulation_only",
            "paper_only": True,
            "disclaimer": PAPER_DISCLAIMER,
            "assumptions": {
                "tax_rate_tw": 0.003,
                "fee_rate_tw": 0.001425,
                "fee_us_per_share": 0.005,
                "slippage_bps": 5.0,
                "reject_stale_data": True,
            },
            "swing": swing.model_dump(),
            "intraday": intraday.model_dump(),
            "strategy_ledgers": {
                strategy_id: {
                    bucket.value: portfolio.model_dump(mode="json")
                    for bucket, portfolio in st.portfolio_manager.get_all_strategy_portfolios(strategy_id).items()
                }
                for strategy_id in st.portfolio_manager.strategy_ids()
            },
        }

    # --- Signals & Fills ---
    @app.get("/api/signals", tags=["Simulation"])
    def list_signals() -> List[Dict[str, Any]]:
        st = app.state.app_state
        return st.signals_log

    @app.get("/api/fills", tags=["Simulation"])
    def list_fills() -> List[Dict[str, Any]]:
        st = app.state.app_state
        swing = st.portfolio_manager.get_portfolio(DecisionScope.SWING)
        intraday = st.portfolio_manager.get_portfolio(DecisionScope.INTRADAY)
        all_fills = [f.model_dump() for f in swing.fills] + [f.model_dump() for f in intraday.fills]
        return all_fills

    @app.get("/api/paper/candidate-result", tags=["Paper Trade"])
    def paper_candidate_result() -> Dict[str, Any]:
        """Read the exact persisted source-only candidate launcher artifact."""
        from cio_market_lab.engine.candidate_launcher import read_candidate_result
        return read_candidate_result(app.state.app_state.runtime_dir)

    @app.get("/api/paper/readback", tags=["Paper Trade"])
    def paper_readback() -> Dict[str, Any]:
        """Read-only canonical paper orders/fills across native and strategy ledgers."""
        st = app.state.app_state
        orders = [order.model_dump(mode="json") for order in st.paper_orders.all_orders()]
        fills: List[Dict[str, Any]] = []
        seen_fill_ids: set[str] = set()
        for bucket in (DecisionScope.SWING, DecisionScope.INTRADAY):
            portfolio = st.portfolio_manager.get_portfolio(bucket)
            for fill in portfolio.fills:
                if fill.fill_id not in seen_fill_ids:
                    seen_fill_ids.add(fill.fill_id)
                    fills.append(fill.model_dump(mode="json"))
        for strategy_id in st.portfolio_manager.strategy_ids():
            for portfolio in st.portfolio_manager.get_all_strategy_portfolios(strategy_id).values():
                for fill in portfolio.fills:
                    if fill.fill_id not in seen_fill_ids:
                        seen_fill_ids.add(fill.fill_id)
                        row = fill.model_dump(mode="json")
                        row["strategy_id"] = strategy_id
                        fills.append(row)
        portfolios = {
            "global": {
                bucket.value: st.portfolio_manager.get_portfolio(bucket).model_dump(mode="json")
                for bucket in (DecisionScope.SWING, DecisionScope.INTRADAY)
            },
            "strategies": {
                strategy_id: {
                    bucket.value: portfolio.model_dump(mode="json")
                    for bucket, portfolio in st.portfolio_manager.get_all_strategy_portfolios(strategy_id).items()
                }
                for strategy_id in st.portfolio_manager.strategy_ids()
            },
        }
        return {
            "orders": orders,
            "fills": fills,
            "portfolios": portfolios,
            "paper_only": True,
            "broker_connected": False,
        }

    # --- Guarded local paper orders ---
    @app.get("/api/paper/orders", tags=["Paper Trade"])
    def list_paper_orders() -> List[Dict[str, Any]]:
        return [order.model_dump(mode="json") for order in app.state.app_state.paper_orders.all_orders()]

    @app.post("/api/paper/orders/preview", tags=["Paper Trade"])
    def preview_paper_order(req: PaperOrderRequest) -> Dict[str, Any]:
        return app.state.app_state.paper_orders.preview(req)

    @app.post("/api/paper/orders", tags=["Paper Trade"])
    def submit_paper_order(req: PaperOrderRequest, request: Request) -> Dict[str, Any]:
        assert_owner_port(request, "Order mutations")
        if (
            os.getenv("CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS") != "1"
            and not intraday_market_open(req.symbol, datetime.now(timezone.utc))
        ):
            raise HTTPException(
                status_code=409,
                detail="MARKET_CLOSED: weekend/closed-session quotes may value NAV but cannot create paper orders.",
            )
        try:
            lock = getattr(app.state.app_state.runner, "writer_lock", None)
            if lock is not None:
                with lock:
                    order = app.state.app_state.paper_orders.submit(req)
            else:
                order = app.state.app_state.paper_orders.submit(req)
            return {"status": "accepted", "order": order.model_dump(mode="json"), "paper_only": True}
        except ContinuationLockAcquisitionError as exc:
            raise HTTPException(status_code=423, detail=f"RUNTIME_LOCKED: {exc}")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/paper/orders/{order_id}/cancel", tags=["Paper Trade"])
    def cancel_paper_order(order_id: str, request: Request) -> Dict[str, Any]:
        assert_owner_port(request, "Order mutations")
        try:
            lock = getattr(app.state.app_state.runner, "writer_lock", None)
            if lock is not None:
                with lock:
                    order = app.state.app_state.paper_orders.cancel(order_id)
            else:
                order = app.state.app_state.paper_orders.cancel(order_id)
            return {"status": "cancelled", "order": order.model_dump(mode="json"), "paper_only": True}
        except ContinuationLockAcquisitionError as exc:
            raise HTTPException(status_code=423, detail=f"RUNTIME_LOCKED: {exc}")
        except KeyError:
            raise HTTPException(status_code=404, detail="paper order not found")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/paper/orders/{order_id}/cancel-replace", tags=["Paper Trade"])
    def cancel_replace_paper_order(order_id: str, req: PaperOrderRequest, request: Request) -> Dict[str, Any]:
        assert_owner_port(request, "Order mutations")
        if (
            os.getenv("CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS") != "1"
            and not intraday_market_open(req.symbol, datetime.now(timezone.utc))
        ):
            raise HTTPException(
                status_code=409,
                detail="MARKET_CLOSED: cancel is allowed, but replacement order creation is blocked outside an open session.",
            )
        try:
            lock = getattr(app.state.app_state.runner, "writer_lock", None)
            if lock is not None:
                with lock:
                    order = app.state.app_state.paper_orders.cancel_replace(order_id, req)
            else:
                order = app.state.app_state.paper_orders.cancel_replace(order_id, req)
            return {"status": "replaced", "order": order.model_dump(mode="json"), "paper_only": True}
        except ContinuationLockAcquisitionError as exc:
            raise HTTPException(status_code=423, detail=f"RUNTIME_LOCKED: {exc}")
        except KeyError:
            raise HTTPException(status_code=404, detail="paper order not found")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/api/paper/kill-switch", tags=["Risk"])
    def get_kill_switch() -> Dict[str, Any]:
        return app.state.app_state.paper_orders.kill_switch_state()

    @app.post("/api/paper/kill-switch", tags=["Risk"])
    def set_kill_switch(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
        assert_owner_port(request, "Kill switch mutations")
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=422, detail="enabled must be a boolean")
        try:
            lock = getattr(app.state.app_state.runner, "writer_lock", None)
            if lock is not None:
                with lock:
                    return app.state.app_state.paper_orders.set_kill_switch(enabled, str(payload.get("reason", "")))
            return app.state.app_state.paper_orders.set_kill_switch(enabled, str(payload.get("reason", "")))
        except ContinuationLockAcquisitionError as exc:
            raise HTTPException(status_code=423, detail=f"RUNTIME_LOCKED: {exc}")

    @app.get("/api/paper/risk-limits", tags=["Risk"])
    def get_risk_limits() -> Dict[str, Any]:
        return app.state.app_state.paper_orders.risk_limits.model_dump(mode="json")

    @app.put("/api/paper/risk-limits", tags=["Risk"])
    def put_risk_limits(limits: RiskLimits, request: Request) -> Dict[str, Any]:
        assert_owner_port(request, "Risk limit mutations")
        st = app.state.app_state
        try:
            lock = getattr(st.runner, "writer_lock", None)
            if lock is not None:
                with lock:
                    st.paper_orders.risk_limits = limits
                    st.event_store.append(EventEnvelope(
                        event_type=EventType.RISK_LIMIT_UPDATED,
                        aggregate_id="risk:limits",
                        payload=limits.model_dump(mode="json"),
                    ))
            else:
                st.paper_orders.risk_limits = limits
                st.event_store.append(EventEnvelope(
                    event_type=EventType.RISK_LIMIT_UPDATED,
                    aggregate_id="risk:limits",
                    payload=limits.model_dump(mode="json"),
                ))
            return limits.model_dump(mode="json")
        except ContinuationLockAcquisitionError as exc:
            raise HTTPException(status_code=423, detail=f"RUNTIME_LOCKED: {exc}")

    @app.get("/api/paper/experiments", tags=["Risk"])
    def list_paper_experiments() -> List[Dict[str, Any]]:
        app.state.app_state.runner.reload_settings_if_needed()
        return [item.model_dump(mode="json") for item in app.state.app_state.paper_orders.list_experiments()]

    @app.get("/api/paper/automation/policy", tags=["Risk"])
    def paper_automation_policy() -> Dict[str, Any]:
        state = app.state.app_state
        return {
            "paper_only": True,
            "broker_connected": False,
            "autonomous_capital_decisions": False,
            "autonomous_live_capital_decisions": False,
            "autonomous_paper_execution": True,
            "derivative_trading_supported": False,
            "intraday": {
                "execution": "deterministic",
                "llm_authority": "veto_only",
                "llm_fail_closed": True,
                "forced_flatten": True,
            },
            "swing": {
                "execution": "deterministic",
                "llm_authority": "event_or_close_review_only",
                "may_change_quantity": False,
                "may_place_order": False,
            },
            "llm_evaluator_configured": state.runner.llm_policy.evaluator is not None,
            "exit_rules": state.runner.exit_policy.rules.model_dump(mode="json"),
        }

    @app.get("/api/paper/mandate", tags=["Risk"])
    def project_money_mandate() -> Dict[str, Any]:
        return PROJECT_MONEY_MANDATE

    @app.get("/api/paper/experiments/{strategy_id}", tags=["Risk"])
    def get_paper_experiment(strategy_id: str) -> Dict[str, Any]:
        app.state.app_state.runner.reload_settings_if_needed()
        return app.state.app_state.paper_orders.experiment_for(strategy_id).model_dump(mode="json")

    @app.put("/api/paper/experiments/{strategy_id}", tags=["Risk"])
    def put_paper_experiment(strategy_id: str, settings: PaperExperimentSettings, request: Request) -> Dict[str, Any]:
        assert_owner_port(request, "Runner mutations")
        if settings.strategy_id != strategy_id:
            raise HTTPException(status_code=422, detail="strategy_id path/body mismatch")
        try:
            item = app.state.app_state.runner.configure(settings)
            return item.model_dump(mode="json")
        except ContinuationLockAcquisitionError as exc:
            raise HTTPException(status_code=423, detail=f"RUNTIME_LOCKED: {exc}")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))

    @app.post("/api/paper/experiments/{strategy_id}/start", tags=["Experiments"])
    def start_paper_experiment(strategy_id: str, request: Request) -> Dict[str, Any]:
        assert_owner_port(request, "Runner mutations")
        try:
            return app.state.app_state.runner.start(strategy_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/paper/experiments/{strategy_id}/stop", tags=["Experiments"])
    def stop_paper_experiment(strategy_id: str, request: Request) -> Dict[str, Any]:
        assert_owner_port(request, "Runner mutations")
        return app.state.app_state.runner.stop(strategy_id)

    @app.get("/api/paper/experiments/{strategy_id}/status", tags=["Experiments"])
    def paper_experiment_status(strategy_id: str, request: Request) -> Dict[str, Any]:
        if not is_owner_port(request):
            try:
                with urlopen(
                    f"http://127.0.0.1:21322/api/paper/experiments/{strategy_id}/status",
                    timeout=1.0,
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception:
                pass
        return app.state.app_state.runner.status(strategy_id)

    @app.post("/api/paper/experiments/{strategy_id}/run-one-cycle", tags=["Experiments"])
    def paper_experiment_run_one_cycle(strategy_id: str, request: Request) -> Dict[str, Any]:
        assert_owner_port(request, "Runner mutations")
        try:
            return app.state.app_state.runner.run_one_cycle(strategy_id)
        except ContinuationLockAcquisitionError as exc:
            raise HTTPException(status_code=423, detail=f"RUNTIME_LOCKED: {exc}")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/api/paper/experiments/{strategy_id}/history", tags=["Experiments"])
    def paper_experiment_history(strategy_id: str, limit: int = Query(default=100, ge=1, le=1000)) -> Dict[str, Any]:
        return app.state.app_state.runner.history(strategy_id, limit)

    @app.get("/api/paper/experiment-history", tags=["Experiments"])
    def paper_experiment_history_all(limit: int = Query(default=100, ge=1, le=1000)) -> Dict[str, Any]:
        return app.state.app_state.runner.history(None, limit)

    @app.get("/api/paper/team-ops", tags=["Risk", "TeamOps"])
    def get_team_ops(request: Request) -> Dict[str, Any]:
        """Authoritative canonical Team Ops snapshot with unified Team NAV, consolidated positions, and posture."""
        st = app.state.app_state
        # GET is a bounded snapshot read, not a NAV recomputation on the owner.
        snap = st.runner.get_canonical_team_ops(is_read_only=True)
        return to_public_team_ops_snapshot(snap)

    @app.get("/api/paper/competition", tags=["Experiments"])
    def paper_competition() -> Dict[str, Any]:
        return app.state.app_state.runner.competition_summary()


    # --- Experiments ---
    @app.get("/api/experiments", tags=["Experiments"])
    def list_experiments() -> List[Dict[str, Any]]:
        st = app.state.app_state
        return st.experiments_log

    # --- Diagnostics ---
    @app.get("/api/diagnostics", tags=["Diagnostics"])
    def get_diagnostics() -> Dict[str, Any]:
        st = app.state.app_state
        capability_report = get_derivative_capabilities_report().model_dump(mode="json")
        available_instruments = [
            name for name, detail in capability_report["capabilities"].items()
            if detail["status"] == "AVAILABLE"
        ]
        option_detail = capability_report["capabilities"]["LONG_PREMIUM_OPTIONS"]
        option_scope = option_detail["status"]
        if option_detail["status"] != "AVAILABLE" and option_detail["implemented_components"]:
            option_scope = "UNAVAILABLE_IMPLEMENTED_COMPONENTS_NOT_ACTIVATED"
        return {
            "system_time": datetime.now(timezone.utc).isoformat(),
            "event_store_events_count": st.event_store.count(),
            "strategies_registered": len(st.registry.list_all()),
            "adapters": {
                "replay": {"status": "READY", "mode": "deterministic_fixture"},
                "composite": {"status": "INITIALIZED", "mode": st.market_adapter.last_fetch_mode,
                              "last_error": st.market_adapter.last_error},
                "yahoo": {"status": "INITIALIZED", "mode": getattr(st.market_adapter, "last_fetch_mode", "unknown"),
                           "last_error": getattr(st.market_adapter, "last_error", None)},
                "browser_research": {"status": "READY", "mode": "fake_test_adapter"},
            },
            "safety_invariants": {
                "broker_integration_permitted": False,
                "broker_credentials_configured": False,
                "autonomous_capital_decision_authority": False,
                "autonomous_paper_execution_enabled": True,
                "derivative_trading_permitted": False,
                "supported_instruments": available_instruments,
                "option_scope": option_scope,
                "stale_data_rejection_enabled": True,
                "ledgers_isolated": True,
            },
            "derivative_capabilities": capability_report,
            "memory_usage_mb": 42.5,
            "paper_disclaimer": PAPER_DISCLAIMER,
        }

    # --- Jev Decision Provider Endpoint ---
    @app.post("/api/integrations/jev/choose", tags=["Integrations"])
    def jev_choose(
        req: JevChoiceRequest,
        timeout: Optional[float] = Query(default=None),
        fallback_id: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        st = app.state.app_state
        resp = st.jev_provider.choose(req, timeout=timeout, fallback_id=fallback_id)
        return resp.to_dict()

    # --- Hermes Chat Draft Bridge ---
    @app.post("/api/integrations/hermes/chat-draft", tags=["Integrations"])
    def create_chat_draft(req: ChatDraftRequest) -> Dict[str, Any]:
        draft = build_chat_draft(
            symbol=req.symbol,
            user_prompt=req.user_prompt,
            run_id=req.run_id,
            strategy_id=req.strategy_id,
            strategy_version=req.strategy_version,
            visible_metrics=req.visible_metrics,
            evidence_paths=req.evidence_paths,
            extra_context=req.extra_context,
        )
        # Returns inspectable draft. Never sends automatically.
        return draft.to_inspectable_dict()

    @app.post("/api/chat", tags=["Integrations"])
    def run_chat(req: ChatRunRequest, request: Request) -> Dict[str, Any]:
        """Send one turn to this machine's existing Hermes Agent session."""
        assert_owner_port(request, "Chat and mutations")
        try:
            return run_hermes_cli_chat(
                req.message,
                session_id=req.session_id,
                workspace_root=os.getenv("CIO_HERMES_WORKSPACE"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    # --- Research Inbox Endpoints ---
    @app.get("/api/research/inbox", tags=["Research"])
    def list_research_inbox() -> List[Dict[str, Any]]:
        st = app.state.app_state
        if hasattr(st.runner, "research_reader"):
            items = st.runner.research_reader.list_inbox()
            if items:
                return items
        items = st.research_adapter.list_inbox()
        return [i.model_dump() for i in items]

    @app.get("/api/research/rejected", tags=["Research"])
    def list_rejected_research() -> List[Dict[str, Any]]:
        st = app.state.app_state
        if hasattr(st.runner, "research_reader"):
            return st.runner.research_reader.list_rejected()
        return []

    @app.post("/api/research/intake", tags=["Research"])
    def intake_research(req: ResearchIntakeRequest, request: Request) -> Dict[str, Any]:
        assert_owner_port(request, "Research intake")
        st = app.state.app_state
        req_data = req.model_dump(mode="json", exclude_none=True)

        is_structured = bool(req.research_id or req.symbol or req.source_url or req.verified_facts)
        url = req.source_url or req.url or ""
        title = req.title or req.research_id or f"Research for {req.symbol or 'unknown'}"
        claims = req.verified_facts if req.verified_facts else req.claims
        related_symbols = [req.symbol] if req.symbol else req.related_symbols
        status = req.verification_status or req.status or "unverified"
        provenance = dict(req.provenance)
        if req.source_tier:
            provenance["source_tier"] = req.source_tier
        if req.raw_metadata:
            provenance.update(req.raw_metadata)

        item = ResearchItem(
            id=req.research_id or f"res-{uuid.uuid4().hex[:8]}",
            url=url,
            title=title,
            claims=claims,
            related_symbols=related_symbols,
            hypothesis=req.hypothesis,
            source_mode=req.source_mode,
            status=status,
            provenance=provenance,
        )
        st.research_adapter.intake_item(item)

        verified = False
        reason = "NO_READER"
        if hasattr(st.runner, "research_reader") and st.runner.research_reader is not None:
            evidence_input = req_data if is_structured else item
            verified, reason = st.runner.research_reader.add_evidence(evidence_input, now=st.runner._now())

        return {
            "status": "success",
            "item": item.model_dump(mode="json"),
            "verified": verified,
            "verification_reason": reason,
        }

    # --- CIO-Owned Paper Desk Endpoints ---
    @app.post("/api/paper/cio/decision-packets", tags=["CIO Paper Desk"])
    def submit_cio_decision_packet(packet: CIODecisionPacket, request: Request) -> Dict[str, Any]:
        """Submit an externally supplied CIO decision packet."""
        assert_owner_port(request, "CIO packet submission")
        st = app.state.app_state
        try:
            decision = st.runner.submit_cio_packet(packet)
            return {
                "status": "ACCEPTED" if decision.action != "NO_TRADE" or "REJECTED" not in decision.reason else "REJECTED",
                "decision": decision.model_dump(mode="json"),
                "paper_only": True,
                "broker_connected": False,
                "decision_owner": "MAIN_CIO",
                "worker_role": "ENGINEERING_WORKER_ONLY",
            }
        except ContinuationLockAcquisitionError as exc:
            raise HTTPException(status_code=423, detail=f"RUNTIME_LOCKED: {exc}")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))

    @app.get("/api/paper/cio/learning-context", tags=["CIO Paper Desk"])
    def get_cio_learning_context(symbols: Optional[str] = Query(default=None)) -> Dict[str, Any]:
        """Retrieve current decision context with retrieved prior lessons and outcomes."""
        st = app.state.app_state
        sym_list = [s.strip() for s in symbols.split(",")] if symbols else None
        req = st.runner.build_decision_context_request(sym_list, read_only=True)
        return req.model_dump(mode="json")

    @app.get("/api/paper/cio/learning-cases", tags=["CIO Paper Desk"])
    def get_cio_learning_cases(
        symbols: Optional[str] = Query(default=None),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> Dict[str, Any]:
        """Fresh disk readback including unexpired cases; no evaluation side effects."""
        from cio_market_lab.engine.learning_readback import read_learning_snapshot
        runtime_dir = app.state.app_state.runner.learning_store.runtime_dir
        selected = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
        try:
            return read_learning_snapshot(runtime_dir, symbols=selected, limit=limit)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=503, detail="Learning store readback unavailable") from exc

    @app.get("/lab/learning", include_in_schema=False)
    def learning_readback_page() -> FileResponse:
        return FileResponse(root / "plugin" / "ui" / "learning.html")

    @app.get("/api/paper/cio/lessons", tags=["CIO Paper Desk"])
    def get_cio_lessons(
        symbols: Optional[str] = Query(default=None),
        limit: int = Query(default=10),
    ) -> Dict[str, Any]:
        """Retrieve recorded decision lessons and past outcomes from the learning store."""
        st = app.state.app_state
        sym = symbols.split(",")[0].strip() if symbols else None
        lessons = st.runner.learning_store.retrieve_context_lessons(symbol=sym, limit=limit)
        outcomes = st.runner.learning_store.retrieve_past_outcomes(symbol=sym, limit=limit)
        return {
            "symbol": sym,
            "lessons": lessons,
            "outcomes": outcomes,
            "total_lessons_count": len(st.runner.learning_store._lessons),
        }

    @app.get("/api/paper/cio/runtime-contract", tags=["CIO Paper Desk"])
    def get_cio_runtime_contract() -> Dict[str, Any]:
        """Return the precise supported runtime contract for CIO execution."""
        contract = get_hermes_runtime_contract()
        data = contract.model_dump(mode="json")
        st = app.state.app_state
        data["executor_registered"] = st.runner.cio_executor is not None
        data["executor_available"] = (
            getattr(st.runner.cio_executor, "is_available", lambda: False)()
            if st.runner.cio_executor is not None
            else False
        )
        data["last_receipt"] = (
            st.runner.last_receipt.model_dump(mode="json")
            if getattr(st.runner, "last_receipt", None) is not None
            else None
        )
        data["supported_horizons"] = ["intraday", "swing", "long_term", "cash"]
        data["research_verification_required"] = True
        data["research_schema_ref"] = "cio_market_lab/research/public_research_schema.json"
        return data

    @app.get("/api/paper/capabilities", tags=["CIO Paper Desk"])
    def get_derivative_capabilities() -> Dict[str, Any]:
        """Return explicit derivative capabilities and capability gaps."""
        report = get_derivative_capabilities_report()
        return report.model_dump(mode="json")

    @app.get("/api/paper/derivative-quotes", tags=["CIO Paper Desk"])
    def get_derivative_quotes() -> Dict[str, Any]:
        """Read-only exact-target exchange intake; never execution approval."""
        from cio_market_lab.data.taifex_derivatives import load_quote_inventory
        return load_quote_inventory(app.state.app_state.runtime_dir / "taifex_quote_inventory.json")

    # --- Mount Static Dashboard UI ---
    imported_ui_dir = root / "vendor" / "shioaji-pro-app" / "dist"
    legacy_ui_dir = root / "plugin" / "ui"
    # Preserve the original static asset contract for backend regression tests
    # and bookmarked asset URLs while the imported SPA owns the root route.
    if legacy_ui_dir.exists():
        app.mount("/static", StaticFiles(directory=str(legacy_ui_dir)), name="legacy-static")
    ui_dir = imported_ui_dir if imported_ui_dir.exists() else root / "plugin" / "ui"
    if ui_dir.exists():
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")

    return app


# Module-level app instance
app = create_app()
