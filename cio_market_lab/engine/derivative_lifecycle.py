"""Isolated paper derivative caller and persistent risk-to-execution lifecycle.

No market feed or broker is created here. Quotes must be supplied explicitly by a
contract-aware caller; a spot bar or underlying price is never an executable quote.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import NAMESPACE_URL, uuid5

from cio_market_lab.domain.events import EventEnvelope, EventType
from cio_market_lab.domain.models import DecisionScope, OrderSide
from cio_market_lab.engine.expiry_cash_settlement import CashSettlementEvidence, build_cash_expiry_event
from cio_market_lab.engine.paper_derivatives import (
    AccountingDelta, ContractSpec, DerivativeInstrumentType, DerivativePosition,
    DerivativeQuote, NormalizedAccountingEvent, PaperDerivativesEngine,
    apply_derivative_event_to_portfolio,
)


class PaperDerivativeLifecycle:
    def __init__(self, portfolio_manager: Any, paper_orders: Any, *, fixture_mode: bool = False):
        self.portfolios = portfolio_manager
        self.orders = paper_orders
        self.store = paper_orders.event_store
        self.engine = PaperDerivativesEngine(production_mode=not fixture_mode)
        self.fixture_mode = fixture_mode

    def _position(self, strategy_id: str, bucket: DecisionScope, symbol: str) -> Optional[DerivativePosition]:
        pos = self.portfolios.get_strategy_ledger(strategy_id, bucket).positions.get(symbol)
        if pos is None or not pos.quantity or "derivative_position" not in pos.assumptions:
            return None
        return DerivativePosition.model_validate(pos.assumptions["derivative_position"])

    def _record(self, event: NormalizedAccountingEvent, strategy_id: str,
                bucket: DecisionScope, spec: Optional[ContractSpec] = None) -> bool:
        if spec is not None:
            event.payload["contract_spec"] = spec.model_dump(mode="json")
        return apply_derivative_event_to_portfolio(
            event, self.portfolios, strategy_id, bucket, self.store,
        )

    def replay(self) -> int:
        """Reconcile committed event-store entries after snapshot recovery."""
        count, cursor = 0, 0
        while True:
            batch = self.store.get_events(event_type=EventType.POSITION_UPDATED,
                                          since_id=cursor, limit=500)
            if not batch:
                break
            for cursor, envelope in batch:
                p = envelope.payload
                if not p.get("paper_derivative"):
                    continue
                event = NormalizedAccountingEvent(
                    event_id=envelope.event_id, event_type=p["normalized_event_type"],
                    aggregate_id=envelope.aggregate_id, timestamp=envelope.timestamp,
                    delta=AccountingDelta.model_validate(p["delta"]),
                    idempotency_key=p["idempotency_key"], payload=p,
                )
                count += apply_derivative_event_to_portfolio(
                    event, self.portfolios, p["strategy_id"], DecisionScope(p["bucket"]), replay=True,
                )
        return count

    def execute(self, *, strategy_id: str, bucket: DecisionScope, spec: ContractSpec,
                quote: DerivativeQuote, side: OrderSide, quantity: float, order_id: str,
                now: datetime, risk_exit: bool = False) -> Any:
        if not self.fixture_mode:
            raise ValueError("DERIVATIVES_UNAVAILABLE_PENDING_ADAPTER_ACCEPTANCE")
        if (not self.orders.experiments[strategy_id].enabled or self.orders.kill_switch) and not risk_exit:
            raise ValueError("DERIVATIVE_ENTRY_DISABLED_OR_KILL_SWITCH")
        settings = self.orders.experiments[strategy_id]
        if bucket not in settings.allowed_buckets or spec.symbol not in settings.universe:
            raise ValueError("DERIVATIVE_SCOPE_NOT_ALLOWED")
        if spec.instrument_type not in {DerivativeInstrumentType.OPTION, DerivativeInstrumentType.FUTURE}:
            raise ValueError("UNSUPPORTED_DERIVATIVE_CONTRACT")
        if spec.expiry is None or (spec.instrument_type == DerivativeInstrumentType.OPTION and
                                   (spec.option_right is None or spec.strike is None or spec.strike <= 0)):
            raise ValueError("EXPIRY_OR_EXERCISE_SPEC_MISSING")
        if quote.symbol != spec.symbol or quote.bid is None or quote.ask is None or not quote.provenance.get("authority"):
            raise ValueError("NO_EXECUTABLE_CONTRACT_QUOTE")
        if quote.is_fixture != self.fixture_mode:
            raise ValueError("FIXTURE_MODE_MISMATCH")
        if self.fixture_mode and not quote.is_fixture:
            raise ValueError("FIXTURE_LABEL_REQUIRED")
        if spec.currency != settings.base_currency:
            raise ValueError("UNSUPPORTED_DERIVATIVE_CURRENCY_CONVERSION")
        ledger = self.portfolios.get_strategy_ledger(strategy_id, bucket, settings.initial_cash)
        current = self._position(strategy_id, bucket, spec.symbol)
        old_spec = ledger.positions.get(spec.symbol)
        if current and (old_spec is None or old_spec.assumptions.get("contract_spec") != spec.model_dump(mode="json")):
            raise ValueError("CONTRACT_SPEC_CHANGED_WITH_OPEN_POSITION")
        increasing = not current or (current.quantity >= 0 and side == OrderSide.BUY) or (current.quantity < 0 and side == OrderSide.SELL)
        if risk_exit and increasing:
            raise ValueError("RISK_EXIT_CANNOT_INCREASE_EXPOSURE")
        if increasing:
            px = quote.ask if side == OrderSide.BUY else quote.bid
            if px is None or px <= 0:
                raise ValueError("NO_EXECUTABLE_CONTRACT_QUOTE")
            notional = (abs(current.quantity) if current else 0) * px * spec.multiplier + quantity * px * spec.multiplier
            limits = self.orders.risk_limits
            if (quantity * px * spec.multiplier > limits.max_order_notional or
                notional > min(limits.max_position_notional, settings.max_position_notional)):
                raise ValueError("DERIVATIVE_NOTIONAL_LIMIT")
            open_positions = sum(bool(p.quantity) for p in ledger.positions.values())
            if not current and open_positions >= min(limits.max_open_positions, settings.max_open_positions):
                raise ValueError("DERIVATIVE_OPEN_POSITION_LIMIT")
            if ledger.realized_pnl <= -min(limits.max_daily_loss, settings.max_daily_loss, limits.max_strategy_loss):
                raise ValueError("DERIVATIVE_LOSS_LIMIT")
            if ledger.equity is None:
                raise ValueError("NAV_UNAVAILABLE")
        # Strategy buckets share one cash account: reserve margin from both.
        committed_margin = sum(
            float(p.assumptions["derivative_position"].get("margin_locked", 0.0))
            for scoped_bucket in (DecisionScope.SWING, DecisionScope.INTRADAY)
            for p in self.portfolios.get_strategy_ledger(strategy_id, scoped_bucket).positions.values()
            if p.quantity and "derivative_position" in p.assumptions
        )
        key = f"exec:{order_id}:{spec.symbol}:{side.value}:{quantity}"
        scoped = f"{strategy_id}:{bucket.value}:{key}"
        event_id = str(uuid5(NAMESPACE_URL, f"paper-derivative:{scoped}"))
        if self.store.get_by_event_id(event_id):
            # Reconcile crash between event commit and portfolio snapshot without a second fill.
            self.replay()
            raise ValueError("DUPLICATE_IDEMPOTENT_REQUEST")
        result = self.engine.attempt_execution(
            order_id=order_id, spec=spec, quote=quote, side=side, quantity=quantity,
            available_cash=ledger.cash - committed_margin if increasing else ledger.cash,
            existing_position=current, as_of=now, allow_deficit_close=risk_exit,
        )
        if result.success:
            if risk_exit:
                result.event.payload.update({"risk_exit": True, "execution_status": "PAPER_RISK_CLOSE_FILLED",
                                             "quote_source": quote.source, "quote_provenance": quote.provenance,
                                             "paper_only": True, "broker_connected": False})
            self._record(result.event, strategy_id, bucket, spec)
        return result

    def settle_futures_daily(self, *, strategy_id: str, bucket: DecisionScope, symbol: str,
                             settlement_price: float, settlement_date: str, quote: DerivativeQuote,
                             now: datetime) -> bool:
        """Caller-supplied official settlement evidence; no inferred exchange rules."""
        if not self.fixture_mode:
            raise ValueError("DERIVATIVES_UNAVAILABLE_PENDING_ADAPTER_ACCEPTANCE")
        pos = self._position(strategy_id, bucket, symbol)
        if pos is None or pos.instrument_type != DerivativeInstrumentType.FUTURE:
            raise ValueError("NO_OPEN_FUTURE")
        spec = ContractSpec.model_validate(
            self.portfolios.get_strategy_ledger(strategy_id, bucket).positions[symbol].assumptions["contract_spec"]
        )
        if (quote.symbol != symbol or quote.is_fixture != self.fixture_mode or
            not quote.provenance.get("authority") or
            not self.engine.validate_quote(quote, as_of=now)[0] or
            settlement_price <= 0 or not self.engine.validate_tick_size(settlement_price, spec.tick_size)):
            raise ValueError("SETTLEMENT_SOURCE_UNAVAILABLE")
        key = f"settle:{pos.position_id}:{settlement_date}"
        scoped = f"{strategy_id}:{bucket.value}:{key}"
        if self.store.get_by_event_id(str(uuid5(NAMESPACE_URL, f"paper-derivative:{scoped}"))):
            return False
        result = self.engine.settle_daily_variation(pos, spec, settlement_price, settlement_date, as_of=now)
        result.event.idempotency_key = key
        result.event.delta.idempotency_key = key
        result.event.payload.update({"position": pos.model_dump(mode="json"),
                                     "settlement_provenance": quote.provenance,
                                     "paper_only": True})
        return self._record(result.event, strategy_id, bucket, spec)

    def settle_futures_daily_evidence(self, *, strategy_id: str, bucket: DecisionScope,
                                      symbol: str, evidence: Any, now: datetime) -> bool:
        """Replay a dated settlement without manufacturing a fresh executable quote.

        Source contract/month binds explicitly to the isolated registry identifier
        CONTRACT:YYYYMM. This does not map MIS symbols or activate derivatives.
        The original official capture can drive a fixture position without being
        relabeled fixture market data. Corrections need explicit reconciliation.
        """
        from cio_market_lab.data.taifex_daily_settlement import DailyFuturesSettlementEvidence, TAIPEI
        if not self.fixture_mode:
            raise ValueError("DERIVATIVES_UNAVAILABLE_PENDING_ADAPTER_ACCEPTANCE")
        proof=DailyFuturesSettlementEvidence.model_validate(evidence)
        proof.validate_source(now)
        if symbol != f"{proof.exchange_contract}:{proof.contract_month}":
            raise ValueError("SOURCE_CONTRACT_MAPPING_REQUIRED")
        pos=self._position(strategy_id,bucket,symbol)
        if pos is None or pos.instrument_type!=DerivativeInstrumentType.FUTURE:
            raise ValueError("NO_OPEN_FUTURE")
        spec=ContractSpec.model_validate(self.portfolios.get_strategy_ledger(strategy_id,bucket).positions[symbol].assumptions['contract_spec'])
        if (pos.opened_at.tzinfo is None or proof.settlement_date<=pos.opened_at.astimezone(TAIPEI).date() or
            spec.expiry is None or spec.expiry.tzinfo is None or
            proof.settlement_date>=spec.expiry.astimezone(TAIPEI).date() or
            not self.engine.validate_tick_size(proof.settlement_price,spec.tick_size)):
            raise ValueError("SETTLEMENT_CHRONOLOGY_OR_TICK_UNAVAILABLE")
        day=proof.settlement_date.isoformat()
        key=f"settle:{pos.position_id}:{day}"
        scoped=f"{strategy_id}:{bucket.value}:{key}"
        existing=self.store.get_by_event_id(str(uuid5(NAMESPACE_URL,f"paper-derivative:{scoped}")))
        if existing:
            if existing.payload.get('settlement_price')!=proof.settlement_price:
                raise ValueError('SETTLEMENT_CORRECTION_REQUIRES_RECONCILIATION')
            self.replay()
            return False
        proof.validate_capture()
        # Do not apply an older day after a later committed settlement.
        cursor=0
        while True:
            entries=self.store.get_events(event_type=EventType.POSITION_UPDATED,since_id=cursor,limit=500)
            if not entries: break
            for cursor,envelope in entries:
                payload=envelope.payload
                previous=payload.get('delta',{}).get('metadata',{}).get('settlement_date')
                if (payload.get('strategy_id')==strategy_id and payload.get('bucket')==bucket.value and
                    envelope.aggregate_id==symbol and previous and previous>=day):
                    raise ValueError('SETTLEMENT_OUT_OF_ORDER_REQUIRES_RECONCILIATION')
        result=self.engine.settle_daily_variation(pos,spec,proof.settlement_price,day,as_of=now)
        result.event.idempotency_key=key;result.event.delta.idempotency_key=key
        result.event.payload.update({'position':pos.model_dump(mode='json'),
            'settlement_evidence':proof.model_dump(mode='json'),
            'settlement_provenance':proof.provenance,'source_is_fixture':proof.is_fixture,
            'position_context':'EXPLICIT_FIXTURE_REPLAY','paper_only':True,'broker_connected':False})
        return self._record(result.event,strategy_id,bucket,spec)

    def settle_expiry_cash(self, *, strategy_id: str, bucket: DecisionScope,
                           symbol: str, evidence: Any, now: datetime) -> bool:
        """Commit explicit final cash settlement through the canonical event ledger.

        Adapter readiness is NOT inferred from caller evidence. Physical delivery
        and unspecified settlement stay unresolved. The first committed price wins
        across retries/restarts; an expired contract cannot generate another fill.
        """
        if not self.fixture_mode:
            raise ValueError("DERIVATIVES_UNAVAILABLE_PENDING_ADAPTER_ACCEPTANCE")
        ledger = self.portfolios.get_strategy_ledger(strategy_id, bucket)
        canonical = ledger.positions.get(symbol)
        if canonical is None or "derivative_position" not in canonical.assumptions:
            raise ValueError("NO_OPEN_DERIVATIVE")
        spec = ContractSpec.model_validate(canonical.assumptions["contract_spec"])
        pos = DerivativePosition.model_validate(canonical.assumptions["derivative_position"])
        if spec.expiry is None:
            raise ValueError("SETTLEMENT_CLOCK_OR_EXPIRY_UNAVAILABLE")
        key = f"expiry_cash:{pos.position_id}:{spec.expiry.isoformat()}"
        scoped = f"{strategy_id}:{bucket.value}:{key}"
        if self.store.get_by_event_id(str(uuid5(NAMESPACE_URL, f"paper-derivative:{scoped}"))):
            self.replay()
            return False
        proof = CashSettlementEvidence.model_validate(evidence)
        event = build_cash_expiry_event(pos, spec, proof, now, fixture_mode=self.fixture_mode)
        return self._record(event, strategy_id, bucket, spec)

    def settle_expiry_exchange_evidence(self, *, strategy_id: str, bucket: DecisionScope,
                                        symbol: str, evidence: Any, contract_rules: dict,
                                        now: datetime) -> bool:
        """Real retained index settlement applied only to an isolated fixture position.

        Source and position contexts are separate. This cannot activate production
        derivatives, create an entry, certify BBO or invent a contract expiry.
        """
        from cio_market_lab.data.taifex_final_index_settlement import FinalIndexSettlementEvidence
        if not self.fixture_mode:
            raise ValueError("DERIVATIVES_UNAVAILABLE_PENDING_ADAPTER_ACCEPTANCE")
        ledger=self.portfolios.get_strategy_ledger(strategy_id,bucket)
        canonical=ledger.positions.get(symbol)
        if canonical is None or 'derivative_position' not in canonical.assumptions:
            raise ValueError('NO_OPEN_DERIVATIVE')
        spec=ContractSpec.model_validate(canonical.assumptions['contract_spec'])
        pos=DerivativePosition.model_validate(canonical.assumptions['derivative_position'])
        proof=FinalIndexSettlementEvidence.model_validate(evidence)
        cash_proof=proof.cash_evidence(spec,contract_rules,now)
        key=f'expiry_cash:{pos.position_id}:{spec.expiry.isoformat()}'
        scoped=f'{strategy_id}:{bucket.value}:{key}'
        prior=self.store.get_by_event_id(str(uuid5(NAMESPACE_URL,f'paper-derivative:{scoped}')))
        if prior:
            old=prior.payload.get('settlement_evidence',{}).get('settlement_price')
            if old!=proof.settlement_price:
                raise ValueError('FINAL_SETTLEMENT_CORRECTION_REQUIRES_RECONCILIATION')
            self.replay()
            return False
        # Calculator mode identifies the MARKET SOURCE here, not the position.
        # The lifecycle fixture-only gate above remains the activation boundary.
        event=build_cash_expiry_event(pos,spec,cash_proof,now,fixture_mode=False)
        event.payload.update({'position_context':'EXPLICIT_FIXTURE_REPLAY',
            'is_fixture':True,'source_is_fixture':False,'position_is_fixture':True,
            'production_adapter_accepted':False,'contract_rule_provenance':contract_rules['provenance']})
        return self._record(event,strategy_id,bucket,spec)

    def review(self, *, strategy_id: str, bucket: DecisionScope, symbol: str,
               now: datetime, quote: Optional[DerivativeQuote] = None,
               settlement: Optional[Any] = None) -> str:
        if settlement is not None:
            applied = self.settle_expiry_cash(strategy_id=strategy_id, bucket=bucket,
                                             symbol=symbol, evidence=settlement, now=now)
            return "PAPER_CASH_SETTLED" if applied else "PAPER_CASH_SETTLEMENT_ALREADY_APPLIED"
        ledger = self.portfolios.get_strategy_ledger(strategy_id, bucket)
        pos = self._position(strategy_id, bucket, symbol)
        canonical = ledger.positions.get(symbol)
        if pos is None or canonical is None:
            return "NO_POSITION"
        spec = ContractSpec.model_validate(canonical.assumptions["contract_spec"])
        expiry = spec.expiry if spec.expiry is None or spec.expiry.tzinfo else spec.expiry.replace(tzinfo=timezone.utc)
        valid = (not expiry or now < expiry) and quote is not None and quote.symbol == symbol and quote.is_fixture == self.fixture_mode and bool(quote.provenance.get("authority")) and quote.bid is not None and quote.ask is not None and quote.bid > 0 and quote.ask > 0 and self.engine.validate_quote(quote, as_of=now)[0]
        if valid:
            marked = self.engine.mark_to_market(pos, spec, quote, as_of=now)
            mark = NormalizedAccountingEvent(
                event_type="DERIVATIVE_MARK", aggregate_id=symbol, timestamp=now,
                delta=AccountingDelta(idempotency_key=f"mark:{pos.position_id}:{quote.timestamp.isoformat()}",
                                      aggregate_id=symbol, event_type="DERIVATIVE_MARK", timestamp=now),
                payload={"position": marked.model_dump(mode="json"), "quote_source": quote.source,
                         "quote_provenance": quote.provenance},
                idempotency_key=f"mark:{pos.position_id}:{quote.timestamp.isoformat()}",
            )
            self._record(mark, strategy_id, bucket, spec)
            pos = marked
        else:
            # Missing mark is not zero: the NAV is explicitly unavailable.
            key = f"mark_unavailable:{pos.position_id}:{pos.last_updated_at.isoformat()}"
            pos = pos.model_copy(update={"current_price": None, "market_value": None,
                                         "unrealized_pnl": None})
            unavailable = NormalizedAccountingEvent(
                event_type="DERIVATIVE_MARK_UNAVAILABLE", aggregate_id=symbol, timestamp=now,
                delta=AccountingDelta(idempotency_key=key, aggregate_id=symbol,
                                      event_type="DERIVATIVE_MARK_UNAVAILABLE", timestamp=now),
                idempotency_key=key,
                payload={"position": pos.model_dump(mode="json"), "execution_status": "UNRESOLVED_EXECUTABLE_QUOTE"},
            )
            self._record(unavailable, strategy_id, bucket, spec)
        maintenance = (abs(pos.quantity) * spec.maintenance_margin_per_contract if spec.maintenance_margin_per_contract
                       else abs(pos.quantity) * (pos.current_price or pos.average_entry_price) * spec.multiplier * spec.maintenance_margin_rate)
        risk = self.engine.check_position_risk(pos, spec, quote if valid else None,
                                               ledger.equity if ledger.equity is not None else ledger.cash,
                                               maintenance, as_of=now)
        if not risk.events:
            return "MARKED" if valid else "UNRESOLVED_PRICE"
        reason = "UNRESOLVED_EXPIRY_DELIVERY_UNSUPPORTED" if expiry and now >= expiry else (
            "PENDING_EXECUTION" if valid else "UNRESOLVED_EXECUTABLE_QUOTE")
        for event in risk.events:
            event.idempotency_key = f"risk:{event.event_type}:{pos.position_id}"
            event.delta.idempotency_key = event.idempotency_key
            event.payload.update({"execution_status": reason, "paper_only": True,
                                  "broker_connected": False})
            self._record(event, strategy_id, bucket, spec)
        if reason == "PENDING_EXECUTION":
            key = f"risk_pending:{pos.position_id}:{quote.timestamp.isoformat()}"
            self._record(NormalizedAccountingEvent(
                event_type="DERIVATIVE_RISK_EXECUTION_PENDING", aggregate_id=symbol, timestamp=now,
                delta=AccountingDelta(idempotency_key=key, aggregate_id=symbol,
                                      event_type="DERIVATIVE_RISK_EXECUTION_PENDING", timestamp=now),
                idempotency_key=key,
                payload={"execution_status": reason, "quote_source": quote.source,
                         "quote_provenance": quote.provenance, "risk_events": [e.event_type for e in risk.events],
                         "paper_only": True, "broker_connected": False},
            ), strategy_id, bucket, spec)
        if reason == "UNRESOLVED_EXPIRY_DELIVERY_UNSUPPORTED":
            key = f"expiry_overdue:{pos.position_id}"
            overdue = NormalizedAccountingEvent(
                event_type="EXPIRY_OVERDUE_UNRESOLVED", aggregate_id=symbol, timestamp=now,
                delta=AccountingDelta(idempotency_key=key, aggregate_id=symbol,
                                      event_type="EXPIRY_OVERDUE_UNRESOLVED", timestamp=now),
                idempotency_key=key,
                payload={"execution_status": reason, "paper_only": True,
                         "broker_connected": False, "expiry": expiry.isoformat(),
                         "unresolved_quantity": pos.quantity, "cash_settlement": False},
            )
            self._record(overdue, strategy_id, bucket, spec)
        if reason != "PENDING_EXECUTION":
            return reason
        side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
        try:
            result = self.execute(strategy_id=strategy_id, bucket=bucket, spec=spec, quote=quote,
                                  side=side, quantity=abs(pos.quantity),
                                  order_id=f"risk-close-{pos.position_id}", now=now, risk_exit=True)
        except ValueError:
            return "UNRESOLVED_EXECUTION_REJECTED"
        if not result.success:
            return "UNRESOLVED_EXECUTION_REJECTED"
        return "PAPER_RISK_CLOSE_FILLED"

    def review_account_margin(self, *, strategy_id: str, quotes: dict, review_id: str, now: datetime) -> dict:
        """Mark all scopes then reduce actual shared-account maintenance deficiency."""
        from cio_market_lab.engine.account_margin_review import review_account_margin
        return review_account_margin(self, strategy_id=strategy_id, quotes=quotes,
                                     review_id=review_id, now=now)
