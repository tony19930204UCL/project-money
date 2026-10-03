from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple, Union

from cio_market_lab.domain.events import EventEnvelope, EventType
from cio_market_lab.domain.models import (
    Bar,
    DecisionScope,
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PaperPortfolio,
    Position,
)


class EventStore:
    """SQLite append-only event store for audit, replay, and portfolio reconstruction."""

    def __init__(self, db_path: Union[str, Path] = ":memory:", read_only: bool = False):
        self.db_path = str(db_path)
        self.read_only = read_only
        self._mem_conn: Optional[sqlite3.Connection] = None
        if self.db_path == ":memory:":
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row
            # Read-only protects disk and append; ephemeral views need schema.
            self._init_db()
        elif self.read_only:
            target = Path(self.db_path).resolve()
            if not target.exists():
                self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._mem_conn.row_factory = sqlite3.Row
                self._init_db()
        else:
            self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        # An observer may start before its producer. The empty in-memory
        # fallback is temporary: once a committed on-disk store appears,
        # open it read-only instead of freezing the observer at startup.
        # Keep the fallback alive for any in-flight readers; never write or
        # initialize the producer database from this read-only path.
        if self.read_only and self.db_path != ":memory:":
            target = Path(self.db_path).resolve()
            if target.exists():
                conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                return conn
        if self._mem_conn is not None:
            return self._mem_conn
        if self.read_only:
            target = Path(self.db_path).resolve()
            if not target.exists():
                self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._mem_conn.row_factory = sqlite3.Row
                self._init_db()
                return self._mem_conn
            conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            return conn
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_aggregate ON events(aggregate_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp)"
            )
            conn.commit()

    def append(self, event: EventEnvelope) -> int:
        """Appends an event immutably to the event store."""
        if self.read_only:
            raise PermissionError("Cannot append event to read-only EventStore")
        now_str = datetime.now(timezone.utc).isoformat()
        ts_str = event.timestamp.isoformat()
        payload_str = json.dumps(event.payload, default=str)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO events (event_id, event_type, timestamp, aggregate_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.event_type.value,
                    ts_str,
                    event.aggregate_id,
                    payload_str,
                    now_str,
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def append_once(self, event: EventEnvelope) -> bool:
        """Atomically insert an immutable event or reject a conflicting identity."""
        if self.read_only:
            raise PermissionError("Cannot append event to read-only EventStore")
        payload = json.dumps(event.payload, sort_keys=True, default=str)
        with self._get_connection() as conn:
            # Serialize the check/insert with other SQLite writers. An event's
            # occurrence time is part of its immutable identity, not metadata.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT event_type, aggregate_id, payload_json, timestamp FROM events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if row is not None:
                if (row["event_type"], row["aggregate_id"], json.loads(row["payload_json"]),
                        datetime.fromisoformat(row["timestamp"])) != (
                    event.event_type.value, event.aggregate_id, event.payload, event.timestamp,
                ):
                    raise ValueError("CONFLICTING_EVENT_ID")
                return False
            conn.execute(
                "INSERT INTO events (event_id, event_type, timestamp, aggregate_id, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event.event_id, event.event_type.value, event.timestamp.isoformat(),
                 event.aggregate_id, payload, datetime.now(timezone.utc).isoformat()),
            )
            return True

    def get_by_event_id(self, event_id: str) -> Optional[EventEnvelope]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT event_id, event_type, timestamp, aggregate_id, payload_json "
                "FROM events WHERE event_id = ?", (event_id,),
            ).fetchone()
        if row is None:
            return None
        return EventEnvelope(event_id=row["event_id"], event_type=row["event_type"],
                             timestamp=datetime.fromisoformat(row["timestamp"]),
                             aggregate_id=row["aggregate_id"], payload=json.loads(row["payload_json"]))

    def get_events(
        self,
        event_type: Optional[EventType] = None,
        aggregate_id: Optional[str] = None,
        since_id: int = 0,
        limit: int = 1000,
        strict: bool = False,
    ) -> List[Tuple[int, EventEnvelope]]:
        """Retrieves events matching filters in ascending sequence."""
        query = "SELECT id, event_id, event_type, timestamp, aggregate_id, payload_json FROM events WHERE id > ?"
        params: list = [since_id]

        if event_type:
            query += " AND event_type = ?"
            params.append(event_type.value)

        if aggregate_id:
            query += " AND aggregate_id = ?"
            params.append(aggregate_id)

        query += " ORDER BY id ASC LIMIT ?"
        params.append(limit)

        results = []
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, params)
                for row in cursor.fetchall():
                    envelope = EventEnvelope(
                        event_id=row["event_id"],
                        event_type=EventType(row["event_type"]),
                        timestamp=datetime.fromisoformat(row["timestamp"]),
                        aggregate_id=row["aggregate_id"],
                        payload=json.loads(row["payload_json"]),
                    )
                    results.append((row["id"], envelope))
        except sqlite3.OperationalError:
            if strict:
                raise
            return []
        return results

    def archive_snapshot(self, directory, *, python_executable, batch_size=4096):
        """Publish an exact, immutable Parquet snapshot; keep SQLite hot rows."""
        from cio_market_lab.events.parquet_archive import ParquetEventArchive
        return ParquetEventArchive(directory, python_executable=python_executable).snapshot(self, batch_size=batch_size)

    def restore_archive(self, directory, *, python_executable):
        """Recover verified cold events atomically and idempotently."""
        from cio_market_lab.events.parquet_archive import ParquetEventArchive
        return ParquetEventArchive(directory, python_executable=python_executable).restore(self)

    def count(self) -> int:
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM events")
                return cursor.fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    def reconstruct_portfolio(
        self,
        bucket: DecisionScope,
        initial_cash: float = 1_000_000.0,
        currency: str = "TWD",
        strategy_id: Optional[str] = None,
    ) -> PaperPortfolio:
        """Deterministically reconstructs paper portfolio state by replaying all events."""
        # Lazy import avoids EventStore -> engine.__init__ -> simulation -> EventStore.
        from cio_market_lab.engine.portfolio import is_slippage_embedded

        bucket = DecisionScope(bucket)
        currency = currency.upper()
        if currency not in {"TWD", "USD"}:
            raise ValueError("UNSUPPORTED_ACCOUNT_CURRENCY")

        portfolio = PaperPortfolio(
            currency=currency,
            bucket=bucket,
            cash=initial_cash,
            initial_cash=initial_cash,
            equity=initial_cash,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            positions={},
            orders=[],
            fills=[],
        )

        all_events = self.get_events(limit=1000000)

        latest_prices: dict[str, float] = {}
        order_strategy_map: dict[str, Optional[str]] = {}
        strategy_positions: dict[str, dict[str, Position]] = {}
        applied_fill_ids: set[str] = set()
        applied_corporate_events: set[str] = set()

        for _, envelope in all_events:
            p = envelope.payload

            # Track latest bar prices for mark-to-market
            if envelope.event_type == EventType.BAR_OBSERVED:
                sym = p.get("symbol")
                close_px = p.get("close")
                if sym and close_px is not None:
                    latest_prices[sym] = float(close_px)
                    for pos_map in strategy_positions.values():
                        if sym in pos_map:
                            pos = pos_map[sym]
                            mult = getattr(pos, "multiplier", 1.0) or 1.0
                            pos.current_price = float(close_px)
                            pos.market_value = pos.quantity * pos.current_price * mult
                            pos.unrealized_pnl = pos.market_value - (
                                pos.quantity * pos.average_entry_price * mult
                            )

            elif envelope.event_type == EventType.ORDER_CREATED:
                if p.get("bucket") == bucket.value:
                    strat = p.get("strategy_id")
                    order_id = p.get("order_id")
                    if order_id:
                        order_strategy_map[order_id] = strat
                    if strategy_id is not None and strat != strategy_id:
                        continue
                    order = Order(**p)
                    portfolio.orders.append(order)

            elif envelope.event_type == EventType.ORDER_REJECTED:
                order_id = p.get("order_id")
                reason = p.get("reason")
                for o in portfolio.orders:
                    if o.order_id == order_id:
                        o.status = OrderStatus.REJECTED
                        o.rejection_reason = reason
                        break

            elif envelope.event_type == EventType.ORDER_CANCELLED:
                order_id = p.get("order_id")
                reason = p.get("reason")
                for o in portfolio.orders:
                    if o.order_id == order_id:
                        o.status = OrderStatus.CANCELLED
                        o.rejection_reason = reason
                        break

            elif envelope.event_type == EventType.ORDER_REPLACED:
                old_id = p.get("old_order_id")
                for o in portfolio.orders:
                    if o.order_id == old_id:
                        o.status = OrderStatus.CANCELLED
                        o.rejection_reason = "CANCEL_REPLACE"
                        break

            elif envelope.event_type == EventType.CORPORATE_ACTION_APPLIED:
                event_bucket = p.get("bucket")
                if event_bucket != bucket.value:
                    continue

                event_strategy = p.get("strategy_id")
                if strategy_id is not None and event_strategy != strategy_id:
                    continue

                act_data = p.get("action", {})
                act_currency = act_data.get("currency")
                if act_currency != currency:
                    raise ValueError(
                        f"CURRENCY_MISMATCH: corporate action currency {act_currency} does not match {currency}"
                    )

                stage = p.get("stage")

                if stage == "SPLIT":
                    ratio = act_data.get("ratio")
                    if ratio is None or not math.isfinite(ratio) or ratio <= 0:
                        raise ValueError("INVALID_SPLIT_RATIO")
                    sym = act_data.get("symbol")
                    if not sym:
                        raise ValueError("MISSING_ACTION_IDENTITY")

                    if envelope.event_id in applied_corporate_events:
                        continue
                    applied_corporate_events.add(envelope.event_id)

                    # Invalidate mark price for this symbol immediately
                    latest_prices.pop(sym, None)

                    strat_keys = [event_strategy] if event_strategy else list(strategy_positions.keys())
                    for sk in strat_keys:
                        pos = strategy_positions.get(sk, {}).get(sym)
                        if pos is not None:
                            pos.quantity *= float(ratio)
                            if pos.quantity > 0:
                                pos.average_entry_price /= float(ratio)
                            pos.current_price = None
                            pos.market_value = None
                            pos.unrealized_pnl = None

                    cancel_ids = set(p.get("cancel_order_ids") or [])
                    for o in portfolio.orders:
                        if o.order_id in cancel_ids or (
                            o.symbol == sym
                            and o.status in {OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED}
                        ):
                            o.status = OrderStatus.CANCELLED
                            o.rejection_reason = "CORPORATE_ACTION_REQUIRES_NEW_ORDER"

                elif stage == "PAYMENT":
                    cash_delta = p.get("cash_delta")
                    if cash_delta is None or not math.isfinite(cash_delta) or cash_delta < 0:
                        raise ValueError("INVALID_CASH_DIVIDEND")

                    if envelope.event_id in applied_corporate_events:
                        continue
                    applied_corporate_events.add(envelope.event_id)

                    portfolio.cash += float(cash_delta)
                    portfolio.realized_pnl += float(cash_delta)

            elif envelope.event_type == EventType.ORDER_FILLED:
                if p.get("bucket") == bucket.value:
                    fill = Fill(**p)

                    # Determine strategy for this fill
                    fill_strat = (
                        fill.provenance.get("strategy_id")
                        if isinstance(getattr(fill, "provenance", None), dict)
                        else None
                    )
                    if not fill_strat:
                        fill_strat = order_strategy_map.get(fill.order_id)
                    if not fill_strat:
                        fill_strat = p.get("strategy_id")

                    if strategy_id is not None and fill_strat != strategy_id:
                        continue

                    if fill.currency != currency:
                        raise ValueError(
                            "CURRENCY_MISMATCH: aggregate reconstruction requires a native account"
                        )

                    if fill.fill_id in applied_fill_ids:
                        continue
                    applied_fill_ids.add(fill.fill_id)

                    portfolio.fills.append(fill)

                    # Update associated order
                    matched_order = next((o for o in portfolio.orders if o.order_id == fill.order_id), None)
                    cumulative_quantity = sum(f.quantity for f in portfolio.fills if f.order_id == fill.order_id)
                    if matched_order is not None:
                        matched_order.status = (
                            OrderStatus.FILLED
                            if math.isclose(cumulative_quantity, matched_order.quantity, rel_tol=0, abs_tol=1e-9)
                            else OrderStatus.PARTIALLY_FILLED
                        )

                    sym = fill.symbol
                    strat_key = fill_strat or "__default__"
                    strat_pos_map = strategy_positions.setdefault(strat_key, {})
                    pos = strat_pos_map.get(
                        sym, Position(symbol=sym, bucket=bucket, currency=currency)
                    )

                    # Synchronize contract attributes
                    if getattr(fill, "instrument_type", None):
                        pos.instrument_type = fill.instrument_type
                    if getattr(fill, "underlying_symbol", None):
                        pos.underlying_symbol = fill.underlying_symbol
                    if getattr(fill, "option_right", None):
                        pos.option_right = fill.option_right
                    if getattr(fill, "strike", None) is not None:
                        pos.strike = fill.strike
                    if getattr(fill, "expiry", None) is not None:
                        pos.expiry = fill.expiry
                    if getattr(fill, "provenance", None):
                        pos.provenance = fill.provenance

                    mult = getattr(fill, "multiplier", 1.0) or 1.0
                    pos.multiplier = mult
                    cost_basis = fill.quantity * fill.fill_price * mult
                    slippage_embedded = is_slippage_embedded(fill)
                    explicit_costs = fill.fee + fill.tax
                    cash_costs = explicit_costs if slippage_embedded else (explicit_costs + fill.slippage)

                    if fill.side == OrderSide.BUY:
                        portfolio.cash -= (cost_basis + cash_costs)
                        new_qty = pos.quantity + fill.quantity
                        if new_qty > 0:
                            pos.average_entry_price = (
                                (pos.average_entry_price * pos.quantity * mult) + cost_basis
                            ) / (new_qty * mult)
                        pos.quantity = new_qty
                    elif fill.side == OrderSide.SELL:
                        portfolio.cash += (cost_basis - cash_costs)
                        costs_to_deduct = explicit_costs if slippage_embedded else (explicit_costs + fill.slippage)
                        realized = (
                            fill.fill_price - pos.average_entry_price
                        ) * fill.quantity * mult - costs_to_deduct
                        pos.realized_pnl += realized
                        portfolio.realized_pnl += realized
                        pos.quantity -= fill.quantity
                        if pos.quantity <= 1e-9:
                            pos.quantity = 0.0
                            pos.average_entry_price = 0.0

                    curr_px = latest_prices.get(sym)
                    if curr_px is not None:
                        pos.current_price = curr_px
                        pos.market_value = pos.quantity * curr_px * mult
                        pos.unrealized_pnl = (
                            pos.market_value - (pos.quantity * pos.average_entry_price * mult)
                        )
                    else:
                        pos.current_price = None
                        pos.market_value = None
                        pos.unrealized_pnl = None
                    strat_pos_map[sym] = pos

        # Construct portfolio.positions
        if strategy_id is not None:
            portfolio.positions = {
                sym: pos.model_copy()
                for sym, pos in strategy_positions.get(strategy_id, {}).items()
            }
            for sym, pos in portfolio.positions.items():
                mult = getattr(pos, "multiplier", 1.0) or 1.0
                curr_px = latest_prices.get(sym)
                if curr_px is not None:
                    pos.current_price = curr_px
                    pos.market_value = pos.quantity * curr_px * mult
                    pos.unrealized_pnl = (
                        pos.market_value - (pos.quantity * pos.average_entry_price * mult)
                    )
                else:
                    pos.current_price = None
                    pos.market_value = None
                    pos.unrealized_pnl = None
        else:
            all_symbols: set[str] = set()
            for s_map in strategy_positions.values():
                all_symbols.update(s_map.keys())

            for sym in all_symbols:
                total_qty = 0.0
                total_basis = 0.0
                total_realized = 0.0
                mult = 1.0
                sample_pos: Optional[Position] = None

                for s_map in strategy_positions.values():
                    if sym in s_map:
                        spos = s_map[sym]
                        sample_pos = spos
                        smult = getattr(spos, "multiplier", 1.0) or 1.0
                        mult = smult
                        total_qty += spos.quantity
                        total_basis += spos.quantity * spos.average_entry_price * smult
                        total_realized += spos.realized_pnl

                if total_qty > 1e-9:
                    avg_entry = total_basis / (total_qty * mult)
                else:
                    total_qty = 0.0
                    avg_entry = 0.0

                curr_px = latest_prices.get(sym)
                if curr_px is not None:
                    mv = total_qty * curr_px * mult
                    unrealized = mv - (total_qty * avg_entry * mult)
                else:
                    curr_px = None
                    mv = None
                    unrealized = None

                pos_kwargs = dict(
                    symbol=sym,
                    bucket=bucket,
                    currency=currency,
                    quantity=total_qty,
                    average_entry_price=avg_entry,
                    current_price=curr_px,
                    market_value=mv,
                    unrealized_pnl=unrealized,
                    realized_pnl=total_realized,
                    multiplier=mult,
                )
                if sample_pos is not None:
                    pos_kwargs["instrument_type"] = sample_pos.instrument_type
                    pos_kwargs["underlying_symbol"] = sample_pos.underlying_symbol
                    pos_kwargs["option_right"] = sample_pos.option_right
                    pos_kwargs["strike"] = sample_pos.strike
                    pos_kwargs["expiry"] = sample_pos.expiry
                    pos_kwargs["assumptions"] = dict(sample_pos.assumptions)
                    pos_kwargs["provenance"] = dict(sample_pos.provenance)

                portfolio.positions[sym] = Position(**pos_kwargs)

        # Recalculate portfolio equity and total unrealized pnl
        has_missing_mark = any(
            pos.quantity > 0 and (pos.current_price is None or pos.market_value is None)
            for pos in portfolio.positions.values()
        )
        if has_missing_mark:
            portfolio.unrealized_pnl = None
            portfolio.equity = None
            portfolio.nav_status = "NAV_UNAVAILABLE"
        else:
            total_market_value = sum(
                (pos.market_value or 0.0) for pos in portfolio.positions.values()
            )
            portfolio.unrealized_pnl = sum(
                (pos.unrealized_pnl or 0.0) for pos in portfolio.positions.values()
            )
            portfolio.equity = portfolio.cash + total_market_value
            portfolio.nav_status = "OK"

        return portfolio
