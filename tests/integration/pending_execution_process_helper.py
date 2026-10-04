from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cio_market_lab.api.app import create_app
from cio_market_lab.domain.events import EventType
from cio_market_lab.domain.models import (
    Bar,
    CIODecisionPacket,
    DecisionScope,
    Quote,
)
from cio_market_lab.engine.cio_packet import sign_cio_packet
from cio_market_lab.engine.corporate_actions import CorporateAction, PaperCorporateActions
from cio_market_lab.engine.paper_orders import PaperExperimentSettings

US_STRATEGY = "TEST_ONLY_RESTART_US"
TW_STRATEGY = "TEST_ONLY_RESTART_TW"
US_CASE = "TEST_ONLY_RESTART_US_CASE"
TW_CASE = "TEST_ONLY_RESTART_TW_CASE"
ACTION_ID = "TEST_ONLY_RESTART_DIVIDEND"


class ControlledQuoteAdapter:
    source_name = "fixture://PR13_PROCESS"
    is_fixture = True
    offline_mode = True

    def __init__(self, control_path: Path):
        self.control_path = Path(control_path)
        self.last_fetch_mode = "TEST_ONLY_CONTROLLED"
        self.last_error = None

    def _control(self) -> dict:
        return json.loads(self.control_path.read_text(encoding="utf-8"))

    def now(self) -> datetime:
        return datetime.fromisoformat(self._control()["now"].replace("Z", "+00:00"))

    def get_bars(self, symbol, start=None, end=None, timeframe="1D", limit=80):
        now = self.now()
        return [
            Bar(
                symbol=symbol,
                timestamp=now - timedelta(minutes=5),
                observed_at=now,
                open=100.0,
                high=102.0,
                low=99.0,
                close=100.0,
                volume=1000,
                source=self.source_name,
                quality="TEST_ONLY",
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
        row = self._control().get("quotes", {}).get(symbol)
        if not row:
            return None
        now = self.now()
        session = row.get("session", "REGULAR")
        source = self.source_name
        return Quote(
            symbol=symbol,
            timestamp=now - timedelta(seconds=1),
            observed_at=now,
            bid=float(row.get("bid", 100.0)),
            ask=float(row.get("ask", 101.0)),
            bid_size=float(row["size"]),
            ask_size=float(row["size"]),
            last_price=float(row.get("last", 100.5)),
            last_size=float(row["size"]),
            source=source,
            quality="TEST_ONLY",
            session=session,
            quote_id=row["quote_id"],
            is_stale=False,
            is_synthetic=False,
            source_capabilities={
                "source": source,
                "two_sided_book": True,
                "size_backed": True,
                "exchange_session_attested": True,
                "entitlement_evidence_id": f"TEST_ONLY_{row['quote_id']}",
                "entitlement_status": "TEST_ONLY",
                "supported_sessions": [session],
                "extended_hours_book": session == "EXTENDED",
                "odd_lot_book": session == "ODD_LOT",
            },
        )

    def timeframe_metadata(self, timeframe):
        return {"timeframe": timeframe, "interval": "TEST_ONLY"}


def _offline() -> None:
    os.environ["CIO_MARKET_LAB_OFFLINE"] = "1"
    os.environ["HERMES_OFFLINE"] = "1"
    os.environ["CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS"] = "1"
    os.environ.pop("CIO_MATERIAL_GATE_ENABLED", None)


def _packet(case_id: str, symbol: str, now: datetime, *, odd_lot: bool = False):
    conditions = {
        "paper_execution_model": "QUOTE_BOOK",
        "allow_partial_fills": True,
    }
    if odd_lot:
        conditions["allow_odd_lot"] = True
    packet = CIODecisionPacket(
        case_id=case_id,
        as_of=now,
        expiry=now + timedelta(hours=2),
        thesis="TEST_ONLY cross-process pending execution acceptance",
        selected_instrument=symbol,
        action="BUY",
        holding_horizon=DecisionScope.SWING,
        quantity=2,
        strategy_version="TEST_ONLY_PROCESS_V1",
        conditions=conditions,
        is_fixture=True,
    )
    return sign_cio_packet(packet, signer_id="fixture-test-signer")


def _build(workspace_root: Path, runtime_dir: Path, control_path: Path):
    _offline()
    adapter = ControlledQuoteAdapter(control_path)
    app = create_app(
        workspace_root=workspace_root,
        runtime_dir=runtime_dir,
        fixture_mode=False,
        is_read_only=False,
        market_adapter=adapter,
    )
    state = app.state.app_state
    state.runner.allow_fixture_quotes = True
    state.runner._now_fn = adapter.now
    state.paper_orders._now_fn = adapter.now
    state.runner.corporate_actions = PaperCorporateActions(
        state.portfolio_manager,
        state.event_store,
        fixture_mode=True,
    )
    return app, state, adapter


def _snapshot(state, decisions=None, corporate_changed=None) -> dict:
    strategies = {}
    for strategy_id in (US_STRATEGY, TW_STRATEGY):
        ledger = state.portfolio_manager.get_strategy_ledger(
            strategy_id, DecisionScope.SWING
        )
        strategies[strategy_id] = {
            "currency": ledger.currency,
            "cash": ledger.cash,
            "equity": ledger.equity,
            "realized_pnl": ledger.realized_pnl,
            "positions": {
                symbol: position.model_dump(mode="json")
                for symbol, position in ledger.positions.items()
            },
            "orders": [
                order.model_dump(mode="json") for order in ledger.orders
            ],
            "fills": [
                fill.model_dump(mode="json") for fill in ledger.fills
            ],
        }
    corporate_events = state.event_store.get_events(
        event_type=EventType.CORPORATE_ACTION_APPLIED,
        limit=1000,
        strict=True,
    )
    return {
        "decisions": [
            decision.model_dump(mode="json") for decision in (decisions or [])
        ],
        "corporate_changed": corporate_changed,
        "event_count": state.event_store.count(),
        "corporate_event_ids": [event.event_id for _, event in corporate_events],
        "strategies": strategies,
    }


def _seed(workspace_root: Path, runtime_dir: Path, control_path: Path) -> dict:
    app, state, adapter = _build(workspace_root, runtime_dir, control_path)
    try:
        state.runner.configure(PaperExperimentSettings(
            strategy_id=US_STRATEGY,
            enabled=True,
            market="US",
            base_currency="USD",
            reporting_currency="USD",
            initial_cash=10_000,
            max_position_notional=5_000,
            universe=["MSFT"],
            paper_execution_model="QUOTE_BOOK",
        ))
        state.runner.configure(PaperExperimentSettings(
            strategy_id=TW_STRATEGY,
            enabled=True,
            market="TW",
            base_currency="TWD",
            reporting_currency="TWD",
            initial_cash=100_000,
            max_position_notional=50_000,
            universe=["2330.TW"],
            paper_execution_model="QUOTE_BOOK",
        ))
        now = adapter.now()
        us = state.runner.submit_cio_packet(
            _packet(US_CASE, "MSFT", now),
            strategy_id=US_STRATEGY,
        )
        tw = state.runner.submit_cio_packet(
            _packet(TW_CASE, "2330.TW", now, odd_lot=True),
            strategy_id=TW_STRATEGY,
        )
        if us.action != "BUY_PENDING" or tw.action != "BUY_PENDING":
            raise AssertionError(
                f"seed must create pending orders through real admission: {us.action}, {tw.action}"
            )
        state.runner._persist_portfolios()
        return _snapshot(state)
    finally:
        state.runner.shutdown()


def _advance(
    workspace_root: Path,
    runtime_dir: Path,
    control_path: Path,
    *,
    apply_action: bool,
    break_pending: bool,
    break_dedup: bool,
) -> dict:
    app, state, adapter = _build(workspace_root, runtime_dir, control_path)
    try:
        if break_pending:
            def blocked_pending():
                raise RuntimeError("TEST_ONLY_PENDING_EXECUTION_BLOCKED")
            state.runner.process_pending_orders = blocked_pending
        if break_dedup:
            def raw_capacity(quote, side):
                return float(quote.ask_size if side.value == "BUY" else quote.bid_size)
            state.runner._available_observed_book_size = raw_capacity

        decisions = state.runner.process_pending_orders()
        corporate_changed = None
        if apply_action:
            control = json.loads(control_path.read_text(encoding="utf-8"))
            action_at = datetime.fromisoformat(
                control["action_at"].replace("Z", "+00:00")
            )
            action = CorporateAction(
                action_id=ACTION_ID,
                symbol="2330.TW",
                currency="TWD",
                kind="CASH_DIVIDEND",
                effective_at=action_at,
                observed_at=action_at,
                amount_per_share=2,
                payable_at=action_at,
                is_fixture=True,
                provenance={"TEST_ONLY": True, "source": "PR13_PROCESS"},
            )
            corporate_changed = state.runner.apply_corporate_action(
                action,
                strategy_id=TW_STRATEGY,
                bucket=DecisionScope.SWING,
                now=adapter.now(),
            )
        state.runner._persist_portfolios()
        return _snapshot(state, decisions, corporate_changed)
    finally:
        state.runner.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("seed", "advance", "inspect"))
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--apply-action", action="store_true")
    parser.add_argument("--break-pending", action="store_true")
    parser.add_argument("--break-dedup", action="store_true")
    args = parser.parse_args()

    if args.command == "seed":
        payload = _seed(args.workspace_root, args.runtime_dir, args.control)
    elif args.command == "advance":
        payload = _advance(
            args.workspace_root,
            args.runtime_dir,
            args.control,
            apply_action=args.apply_action,
            break_pending=args.break_pending,
            break_dedup=args.break_dedup,
        )
    else:
        app, state, _adapter = _build(
            args.workspace_root, args.runtime_dir, args.control
        )
        try:
            payload = _snapshot(state)
        finally:
            state.runner.shutdown()
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
