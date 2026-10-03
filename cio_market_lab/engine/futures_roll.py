"""Explicit two-leg futures roll through the canonical isolated PAPER ledger.

Caller-supplied contract expiries form the calendar; no exchange date is invented.
Roll basis is a cash-equivalent comparison, NOT earned carry or realized profit.
This implements fixture accounting/recovery only and cannot activate derivatives.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
import fcntl
import hashlib
import json
import math
import threading

from cio_market_lab.domain.events import EventEnvelope, EventType
from cio_market_lab.domain.models import DecisionScope, OrderSide
from cio_market_lab.engine.paper_derivatives import ContractSpec, DerivativeInstrumentType, DerivativeQuote


class PaperFuturesRollService:
    def __init__(self, lifecycle):
        self.lifecycle = lifecycle
        self.store = lifecycle.store
        self._thread_lock = threading.RLock()

    @contextmanager
    def _exclusive(self):
        # Cooperating callers on the same persistent event store cannot race legs.
        # In-memory callers must share this service instance.
        with self._thread_lock:
            if self.store.db_path == ':memory:':
                yield
            else:
                path = Path(self.store.db_path).with_suffix('.futures-roll.lock')
                with path.open('a+') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(lock, fcntl.LOCK_UN)

    def roll(self, *, strategy_id: str, bucket: DecisionScope, old_spec: ContractSpec,
             new_spec: ContractSpec, close_quote: DerivativeQuote, open_quote: DerivativeQuote,
             quantity: float, roll_id: str, now: datetime) -> dict:
        """Persist request, close, then open; crash may leave CLOSE_ONLY_PENDING_ENTRY.

        Retry preserves committed prices and cash; fresh quotes can be supplied for
        an uncommitted leg. A completed roll never recalculates either fill.
        Each leg keeps all experiment, currency, notional, margin and quote guards.
        """
        with self._exclusive():
            return self._roll(strategy_id=strategy_id, bucket=bucket, old_spec=old_spec,
                              new_spec=new_spec, close_quote=close_quote, open_quote=open_quote,
                              quantity=quantity, roll_id=roll_id, now=now)

    def _roll(self, *, strategy_id, bucket, old_spec, new_spec, close_quote, open_quote,
              quantity, roll_id, now):
        svc = self.lifecycle
        if not svc.fixture_mode:
            raise ValueError('DERIVATIVES_UNAVAILABLE_PENDING_ADAPTER_ACCEPTANCE')
        q = float(quantity)
        if not math.isfinite(q) or q <= 0 or not q.is_integer() or not roll_id:
            raise ValueError('ROLL_INTEGER_QUANTITY_AND_ID_REQUIRED')
        if now.tzinfo is None:
            raise ValueError('ROLL_EXPLICIT_CLOCK_REQUIRED')
        if (old_spec.instrument_type != DerivativeInstrumentType.FUTURE or
            new_spec.instrument_type != DerivativeInstrumentType.FUTURE or
            old_spec.symbol == new_spec.symbol or
            old_spec.underlying_symbol != new_spec.underlying_symbol or
            old_spec.multiplier != new_spec.multiplier or
            old_spec.currency != new_spec.currency or
            old_spec.expiry is None or new_spec.expiry is None or
            old_spec.expiry.tzinfo is None or new_spec.expiry.tzinfo is None or
            new_spec.expiry <= old_spec.expiry):
            raise ValueError('ROLL_COMPATIBLE_EXPLICIT_FUTURE_SERIES_REQUIRED')
        request = {'strategy_id': strategy_id, 'bucket': bucket.value, 'roll_id': roll_id,
                   'old_spec': old_spec.model_dump(mode='json'),
                   'new_spec': new_spec.model_dump(mode='json'), 'quantity': q}
        request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        identity = f'paper-roll:{strategy_id}:{bucket.value}:{roll_id}'
        intent_id = str(uuid5(NAMESPACE_URL, identity + ':intent'))
        done_id = str(uuid5(NAMESPACE_URL, identity + ':completed'))
        existing = self.store.get_by_event_id(intent_id)
        if existing and existing.payload['request_hash'] != request_hash:
            raise ValueError('ROLL_REQUEST_CONFLICT')
        done = self.store.get_by_event_id(done_id)
        if done:
            svc.replay()
            return done.payload['receipt']
        settings = svc.orders.experiments[strategy_id]
        if not settings.enabled or svc.orders.kill_switch:
            raise ValueError('ROLL_ENTRY_DISABLED')
        if (bucket not in settings.allowed_buckets or
            any(s.symbol not in settings.universe for s in (old_spec, new_spec)) or
            new_spec.currency != settings.base_currency):
            raise ValueError('ROLL_SCOPE_OR_CURRENCY_NOT_ALLOWED')
        # Crash between commit and snapshot is reconciled BEFORE any leg lookup.
        svc.replay()
        if existing:
            direction = existing.payload['direction']
        else:
            pos = svc._position(strategy_id, bucket, old_spec.symbol)
            if pos is None or abs(pos.quantity) < q:
                raise ValueError('ROLL_SOURCE_POSITION_REQUIRED')
            if svc._position(strategy_id, bucket, new_spec.symbol):
                raise ValueError('ROLL_TARGET_ALREADY_OPEN')
            direction = 1 if pos.quantity > 0 else -1
        close_side = OrderSide.SELL if direction == 1 else OrderSide.BUY
        open_side = OrderSide.BUY if direction == 1 else OrderSide.SELL

        def committed(leg, spec, side):
            key = f'exec:roll:{roll_id}:{leg}:{spec.symbol}:{side.value}:{q}'
            return self.store.get_by_event_id(str(uuid5(
                NAMESPACE_URL, f'paper-derivative:{strategy_id}:{bucket.value}:{key}')))

        close_event = committed('close', old_spec, close_side)
        open_event = committed('open', new_spec, open_side)
        if open_event and not close_event:
            raise ValueError('ROLL_EVENT_SEQUENCE_CONFLICT')
        # Validate BOTH quotes before a new close; a missing next-series quote
        # cannot silently transform a requested roll into a one-legged exit.
        for spec, quote, event in [(old_spec, close_quote, close_event),
                                   (new_spec, open_quote, open_event)]:
            if event:
                continue
            if (quote.symbol != spec.symbol or not quote.is_fixture or
                not quote.provenance.get('authority') or
                quote.timestamp.tzinfo is None or quote.observed_at.tzinfo is None or
                quote.timestamp > now or quote.observed_at > now or
                quote.timestamp > quote.observed_at or
                any(v is None or not math.isfinite(v) or v <= 0 for v in (quote.bid, quote.ask)) or
                quote.bid > quote.ask or
                any(not svc.engine.validate_tick_size(v, spec.tick_size) for v in (quote.bid, quote.ask)) or
                not svc.engine.validate_quote(quote, as_of=now)[0] or now >= spec.expiry):
                raise ValueError('ROLL_EXECUTABLE_CONTRACT_QUOTE_REQUIRED')
        if existing is None:
            self.store.append_once(EventEnvelope(event_id=intent_id,
                event_type=EventType.POSITION_UPDATED, timestamp=now, aggregate_id=roll_id,
                payload={'paper_derivative_roll': True, 'request': request,
                         'request_hash': request_hash, 'direction': direction,
                         'paper_only': True, 'is_fixture': True, 'broker_connected': False}))
        if close_event is None:
            closed = svc.execute(strategy_id=strategy_id, bucket=bucket, spec=old_spec,
                quote=close_quote, side=close_side, quantity=q,
                order_id=f'roll:{roll_id}:close', now=now)
            if not closed.success:
                return self._pending('ROLL_CLOSE_REJECTED', closed.rejection_reason, request)
            close_event = committed('close', old_spec, close_side)
        if open_event is None:
            opened = svc.execute(strategy_id=strategy_id, bucket=bucket, spec=new_spec,
                quote=open_quote, side=open_side, quantity=q,
                order_id=f'roll:{roll_id}:open', now=now)
            if not opened.success:
                return self._pending('CLOSE_ONLY_PENDING_ENTRY', opened.rejection_reason, request)
            open_event = committed('open', new_spec, open_side)
        close_px = close_event.payload['delta']['metadata']['price']
        open_px = open_event.payload['delta']['metadata']['price']
        receipt = {'status': 'PAPER_ROLL_COMPLETED', 'roll_id': roll_id,
                   'request_hash': request_hash, 'quantity': q, 'direction': direction,
                   'old_symbol': old_spec.symbol, 'new_symbol': new_spec.symbol,
                   'expiry_calendar': {'old_expiry': old_spec.expiry.isoformat(),
                                       'new_expiry': new_spec.expiry.isoformat(),
                                       'authority': 'EXPLICIT_CALLER_SPECS_NOT_EXCHANGE_CERTIFICATION'},
                   'close_event_id': close_event.event_id, 'open_event_id': open_event.event_id,
                   'close_price': close_px, 'open_price': open_px,
                   'roll_basis_points': direction * (open_px - close_px),
                   'roll_basis_cash_equivalent': round(direction*(open_px-close_px)*q*old_spec.multiplier,4),
                   'basis_is_realized_pnl': False, 'paper_only': True, 'is_fixture': True,
                   'broker_connected': False, 'live_activation': False,
                   'completed_at': open_event.timestamp.isoformat()}
        self.store.append_once(EventEnvelope(event_id=done_id,
            event_type=EventType.POSITION_UPDATED, timestamp=now, aggregate_id=roll_id,
            payload={'paper_derivative_roll': True, 'receipt': receipt}))
        return receipt

    @staticmethod
    def _pending(status, reason, request):
        return {'status': status, 'reason': reason, 'request': request,
                'paper_only': True, 'is_fixture': True, 'broker_connected': False,
                'live_activation': False}
