"""Paper-only sourced corporate actions. Never creates orders or executable marks.

Committed effects are replayable. Cash dividends are gross native-currency amounts;
withholding, DRIP and fractional cash-in-lieu require separate evidenced events.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import threading
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator
from cio_market_lab.domain.events import EventEnvelope, EventType
from cio_market_lab.domain.models import DecisionScope, OrderSide, OrderStatus


class CorporateAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    action_id: str
    symbol: str
    currency: Literal['USD', 'TWD']
    kind: Literal['SPLIT', 'CASH_DIVIDEND']
    effective_at: datetime
    observed_at: datetime
    ratio: float | None = None
    amount_per_share: float | None = None
    payable_at: datetime | None = None
    is_fixture: bool = False
    provenance: dict[str, Any]

    @model_validator(mode='after')
    def validate_action(self):
        if not self.action_id or not self.symbol:
            raise ValueError('MISSING_ACTION_IDENTITY')
        for ts in [self.effective_at, self.observed_at, self.payable_at]:
            if ts is not None and (ts.tzinfo is None or ts.utcoffset() is None):
                raise ValueError('TIMEZONE_REQUIRED')
        if self.kind == 'SPLIT':
            if self.ratio is None or not math.isfinite(self.ratio) or self.ratio <= 0:
                raise ValueError('INVALID_SPLIT_RATIO')
            if self.amount_per_share is not None or self.payable_at is not None:
                raise ValueError('SPLIT_HAS_DIVIDEND_FIELDS')
        else:
            if self.ratio is not None or self.amount_per_share is None or not math.isfinite(self.amount_per_share) or self.amount_per_share <= 0:
                raise ValueError('INVALID_CASH_DIVIDEND')
            if self.payable_at is None or self.payable_at < self.effective_at:
                raise ValueError('INVALID_PAYABLE_DATE')
        return self


class PaperCorporateActions:
    def __init__(self, portfolio_manager, event_store, *, fixture_mode=False):
        self.pm = portfolio_manager
        self.store = event_store
        self.fixture_mode = fixture_mode
        if not hasattr(self.pm, '_corporate_effect_ids'):
            self.pm._corporate_effect_ids = set()
        if not hasattr(self.pm, '_corporate_cash_flows'):
            self.pm._corporate_cash_flows = {}
        if not hasattr(self.pm, '_corporate_action_lock'):
            self.pm._corporate_action_lock = threading.RLock()

    @staticmethod
    def _digest(action):
        # Defensive copy protects against later mutation of a nested provenance dict.
        text = json.dumps(action.model_dump(mode='json'), sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(text.encode()).hexdigest()

    def _source_gate(self, action):
        if action.is_fixture:
            if not self.fixture_mode:
                raise ValueError('FIXTURE_ACTION_FORBIDDEN')
            return
        p = action.provenance
        if p.get('verification_status') != 'VERIFIED' or not str(p.get('source_url', '')).startswith('https://'):
            raise ValueError('VERIFIED_SOURCE_REQUIRED')
        capture = Path(p.get('capture_path', ''))
        if not capture.is_file() or hashlib.sha256(capture.read_bytes()).hexdigest() != p.get('capture_sha256'):
            raise ValueError('SOURCE_CAPTURE_MISMATCH')
        facts = action.model_dump(mode='json', exclude={'provenance', 'is_fixture'})
        if p.get('verified_action') != facts:
            raise ValueError('SOURCE_FACTS_MISMATCH')

    def _events(self):
        cursor = 0
        while True:
            rows = self.store.get_events(event_type=EventType.CORPORATE_ACTION_APPLIED,
                                         since_id=cursor, limit=500, strict=True)
            if not rows:
                return
            for seq, event in rows:
                cursor = seq
                yield event

    @staticmethod
    def _key(action, strategy_id, bucket, stage):
        text = json.dumps([action.action_id, action.symbol, strategy_id, bucket.value, stage])
        return 'corporate:' + hashlib.sha256(text.encode()).hexdigest()

    def _entitled_quantity(self, ledger, action, strategy_id, bucket):
        # Ex-date cutoff: post-ex buys do not earn, post-ex sales do not lose rights.
        # Replay original fill quantities and pre-ex splits in timestamp order.
        history = [(f.timestamp, 1, f.fill_id, 'FILL', f) for f in ledger.fills
                   if f.symbol == action.symbol and f.timestamp < action.effective_at
                   and getattr(f, 'instrument_type', 'EQUITY') in {'EQUITY', 'ETF'}]
        for event in self._events():
            p = event.payload
            if p['stage'] == 'SPLIT' and p['strategy_id'] == strategy_id and p['bucket'] == bucket.value:
                prior = CorporateAction.model_validate(p['action'])
                if prior.symbol == action.symbol and prior.effective_at < action.effective_at:
                    history.append((prior.effective_at, 0, event.event_id, 'SPLIT', prior))
        qty = 0.0
        for _, _, _, kind, item in sorted(history, key=lambda h: h[:3]):
            if kind == 'SPLIT':
                qty *= item.ratio
            else:
                qty += item.quantity if item.side == OrderSide.BUY else -item.quantity
                if qty < -1e-8:
                    raise ValueError('INVALID_ENTITLEMENT_HISTORY')
        return max(qty, 0.0)

    def apply(self, action, *, strategy_id, bucket, now):
        if self.store.read_only:
            raise PermissionError('READ_ONLY_CORPORATE_ACTION')
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('TIMEZONE_REQUIRED')
        if now < action.effective_at or now < action.observed_at:
            raise ValueError('ACTION_NOT_EFFECTIVE_OR_NOT_OBSERVED')
        self._source_gate(action)
        bucket = DecisionScope(bucket)
        ledger = self.pm.get_strategy_ledger(strategy_id, bucket)
        if ledger.currency != action.currency:
            raise ValueError('CURRENCY_MISMATCH')
        with self.pm._corporate_action_lock:
            stages = ['SPLIT'] if action.kind == 'SPLIT' else ['ENTITLEMENT']
            if action.kind == 'CASH_DIVIDEND' and now >= action.payable_at:
                stages.append('PAYMENT')
            changed = False
            for stage in stages:
                key = self._key(action, strategy_id, bucket, stage)
                existing = self.store.get_by_event_id(key)
                digest = self._digest(action)
                if existing is not None:
                    if existing.payload['action_sha256'] != digest:
                        raise ValueError('CORPORATE_ACTION_IDENTITY_CONFLICT')
                    changed = self._apply_event(existing) or changed
                    continue
                payload = dict(action=action.model_dump(mode='json'), action_sha256=digest,
                               stage=stage, strategy_id=strategy_id, bucket=bucket.value,
                               accounting='GROSS_NATIVE_CURRENCY_NO_DRIP_NO_WITHHOLDING')
                if stage == 'ENTITLEMENT':
                    payload['eligible_quantity'] = self._entitled_quantity(ledger, action, strategy_id, bucket)
                elif stage == 'PAYMENT':
                    entitlement = self.store.get_by_event_id(self._key(action, strategy_id, bucket, 'ENTITLEMENT'))
                    payload['eligible_quantity'] = entitlement.payload['eligible_quantity']
                    payload['cash_delta'] = payload['eligible_quantity'] * action.amount_per_share
                else:
                    pos = ledger.positions.get(action.symbol)
                    if pos is not None and pos.instrument_type not in {'EQUITY', 'ETF'}:
                        raise ValueError('DERIVATIVE_ADJUSTMENT_REQUIRES_TERMS')
                    # Late split application across post-split fills is ambiguous: do not
                    # multiply new-share fills or invent adjusted exchange prices.
                    if any(f.symbol == action.symbol and f.timestamp >= action.effective_at for f in ledger.fills):
                        raise ValueError('LATE_SPLIT_REQUIRES_CHRONOLOGICAL_REPLAY')
                    payload['cancel_order_ids'] = [o.order_id for o in ledger.orders
                        if o.symbol == action.symbol and o.status in {OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED}]
                envelope = EventEnvelope(event_id=key, event_type=EventType.CORPORATE_ACTION_APPLIED,
                                         timestamp=now, aggregate_id=f'strategy:{strategy_id}', payload=payload)
                self.store.append_once(envelope)  # Commit before any in-memory financial effect.
                changed = self._apply_event(envelope) or changed
            return changed

    def _apply_event(self, envelope):
        if envelope.event_id in self.pm._corporate_effect_ids:
            return False
        p = envelope.payload
        action = CorporateAction.model_validate(p['action'])
        ledger = self.pm.get_strategy_ledger(p['strategy_id'], DecisionScope(p['bucket']))
        if ledger.currency != action.currency:
            raise ValueError('CURRENCY_MISMATCH_ON_REPLAY')
        stage = p['stage']
        if stage == 'SPLIT':
            pos = ledger.positions.get(action.symbol)
            old_qty = pos.quantity if pos else 0.0
            if pos is not None:
                pos.quantity *= action.ratio
                pos.average_entry_price /= action.ratio
                self._invalidate_mark(ledger, pos, action.symbol)
            for order in ledger.orders:
                if order.order_id in p['cancel_order_ids']:
                    order.status = OrderStatus.CANCELLED
                    order.rejection_reason = 'CORPORATE_ACTION_REQUIRES_NEW_ORDER'
            if action.currency == 'TWD':
                aggregate = self.pm.get_ledger(DecisionScope(p['bucket']))
                target = aggregate.positions.get(action.symbol)
                if target is not None and old_qty:
                    basis = target.quantity * target.average_entry_price
                    target.quantity += old_qty * (action.ratio - 1)
                    target.average_entry_price = basis / target.quantity if target.quantity else 0.0
                    self._invalidate_mark(aggregate, target, action.symbol)
                for order in aggregate.orders:
                    if order.order_id in p['cancel_order_ids']:
                        order.status = OrderStatus.CANCELLED
                        order.rejection_reason = 'CORPORATE_ACTION_REQUIRES_NEW_ORDER'
        elif stage == 'PAYMENT':
            ledger.cash += p['cash_delta']
            self.pm._corporate_cash_flows.setdefault(p['strategy_id'], {})[envelope.event_id] = p['cash_delta']
            ledger.realized_pnl += p['cash_delta']
            ledger._recalculate_equity()
            if action.currency == 'TWD':
                aggregate = self.pm.get_ledger(DecisionScope(p['bucket']))
                aggregate.cash += p['cash_delta']
                aggregate.realized_pnl += p['cash_delta']
                aggregate._recalculate_equity()
        self.pm._corporate_effect_ids.add(envelope.event_id)
        return True

    @staticmethod
    def _invalidate_mark(ledger, pos, symbol):
        pos.current_price = None
        pos.market_value = None
        pos.unrealized_pnl = None
        ledger._latest_prices.pop(symbol, None)
        ledger._recalculate_equity()

    def snapshot(self):
        """Atomic runner snapshot includes effects and financial state together."""
        if not self.pm._corporate_effect_ids:
            return {}
        def data(ledger):
            return dict(currency=ledger.currency, cash=ledger.cash,
                        realized_pnl=ledger.realized_pnl,
                        positions={s:p.model_dump(mode='json') for s,p in ledger.positions.items()},
                        latest_prices=dict(ledger._latest_prices))
        return dict(version=1, effect_ids=sorted(self.pm._corporate_effect_ids),
                    cash_flows=self.pm._corporate_cash_flows,
                    strategies={sid:{b.value:data(l) for b,l in ls.items()}
                                for sid,ls in self.pm._strategy_ledgers.items()},
                    aggregates={b.value:data(l) for b,l in self.pm._ledgers.items()})

    def restore(self, snapshot):
        if not snapshot:
            return
        if snapshot.get('version') != 1:
            raise ValueError('UNSUPPORTED_CORPORATE_SNAPSHOT')
        from cio_market_lab.domain.models import Position
        def restore_ledger(ledger, saved):
            if ledger.currency != saved['currency']:
                raise ValueError('CORPORATE_SNAPSHOT_CURRENCY_MISMATCH')
            ledger.cash = saved['cash']
            ledger.realized_pnl = saved['realized_pnl']
            ledger.positions = {s:Position.model_validate(p) for s,p in saved['positions'].items()}
            ledger._latest_prices = dict(saved['latest_prices'])
            ledger._recalculate_equity()
        for sid,ls in snapshot['strategies'].items():
            for b,saved in ls.items():
                restore_ledger(self.pm.get_strategy_ledger(sid,DecisionScope(b)),saved)
        for b,saved in snapshot['aggregates'].items():
            restore_ledger(self.pm.get_ledger(DecisionScope(b)),saved)
        self.pm._corporate_cash_flows = snapshot['cash_flows']
        self.pm._corporate_effect_ids = set(snapshot['effect_ids'])

    def replay(self):
        # Restore snapshot financial state and its applied IDs together before this.
        count = 0
        with self.pm._corporate_action_lock:
            for envelope in self._events():
                count += int(self._apply_event(envelope))
        return count
