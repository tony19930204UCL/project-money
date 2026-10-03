"""Decision Learning and Lessons Retrieval Engine.

Persists pre-decision beliefs, fills/costs, subsequent outcomes, attribution,
rejected opportunities, and lessons.
Builds retrieval of prior lessons into the next decision context.
Changes to hypotheses are versioned, never hindsight rewriting.
This is decision learning, not claimed model-weight training.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import uuid

from pydantic import BaseModel, Field

from cio_market_lab.domain.events import EventEnvelope, EventType
from cio_market_lab.domain.models import CIODecisionPacket, CIOLessonRecord
from cio_market_lab.events.store import EventStore


class CIODecisionRecord(BaseModel):
    """Complete lifecycle record of an externally supplied CIO decision."""
    case_id: str
    packet: CIODecisionPacket
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    pre_decision_portfolio: Dict[str, Any] = Field(default_factory=dict)
    pre_decision_quotes: Dict[str, Any] = Field(default_factory=dict)
    order_id: Optional[str] = None
    fill: Optional[Dict[str, Any]] = None
    costs: Optional[Dict[str, float]] = None
    outcome: Optional[Dict[str, Any]] = None
    outcomes: List[Dict[str, Any]] = Field(default_factory=list)
    residual_quantity: Optional[float] = None
    residual_entry_cost: Optional[float] = None
    attribution: Optional[Dict[str, Any]] = None
    rejected_opportunities: List[Dict[str, Any]] = Field(default_factory=list)
    lessons: List[str] = Field(default_factory=list)
    hypothesis_version: int = 1
    superseded_by: Optional[str] = None
    status: str = "ACTIVE"  # ACTIVE, FILLED, PARTIALLY_CLOSED, CLOSED, REJECTED, EXPIRED
    strategy_version: str = "dynamic-desk-cio-20260927"
    prediction_vs_outcome: Optional[Dict[str, Any]] = None
    applied_lesson_ids: List[str] = Field(default_factory=list)
    decision_delta: Dict[str, Any] = Field(default_factory=dict)
    predecision_snapshot: Dict[str, Any] = Field(default_factory=dict)
    predecision_version: str = "v1"
    validation_status: str = "RECORDED"  # RECORDED, VALIDATED_IMPROVEMENT


class CIODecisionLearningStore:
    """Manages persistence and retrieval of decision beliefs, outcomes, and lessons."""

    def __init__(self, runtime_dir: Path, event_store: Optional[EventStore] = None, read_only: bool = False) -> None:
        self.runtime_dir = runtime_dir
        self.read_only = read_only
        if not self.read_only:
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.event_store = event_store
        self._records: Dict[str, CIODecisionRecord] = {}
        self._lessons: List[CIOLessonRecord] = []
        self._processed_case_ids: Set[str] = set()
        self._log_file = self.runtime_dir / "cio_learning.jsonl"
        self._lessons_file = self.runtime_dir / "cio_lessons.jsonl"
        self._load()

    def _load(self) -> None:
        if self._log_file.exists():
            try:
                with self._log_file.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        data = json.loads(line)
                        rec = CIODecisionRecord.model_validate(data)
                        self._records[rec.case_id] = rec
                        self._processed_case_ids.add(rec.case_id)
            except Exception:
                pass

        if self._lessons_file.exists():
            try:
                with self._lessons_file.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        data = json.loads(line)
                        lesson = CIOLessonRecord.model_validate(data)
                        self._lessons.append(lesson)
            except Exception:
                pass

    @property
    def processed_case_ids(self) -> Set[str]:
        return set(self._processed_case_ids)

    def get_record(self, case_id: str) -> Optional[CIODecisionRecord]:
        """Retrieve decision record by case_id."""
        return self._records.get(case_id)

    def record_decision(
        self,
        packet: CIODecisionPacket,
        pre_decision_portfolio: Dict[str, Any],
        pre_decision_quotes: Dict[str, Any],
        applied_lesson_ids: Optional[List[str]] = None,
        decision_delta: Optional[Dict[str, Any]] = None,
    ) -> CIODecisionRecord:
        """Persist pre-decision beliefs before execution."""
        # Explicit empty overrides mean no acknowledged application/delta.
        # Truthiness fallback would manufacture a use claim from packet metadata.
        applied_lessons = (applied_lesson_ids if applied_lesson_ids is not None else
                           getattr(packet, "applied_lesson_ids", None) or packet.conditions.get("applied_lesson_ids", []))
        dec_delta = (decision_delta if decision_delta is not None else
                     getattr(packet, "decision_delta", None) or packet.conditions.get("decision_delta", {}))
        pre_snap = getattr(packet, "predecision_snapshot", None) or {
            "portfolio": pre_decision_portfolio,
            "quotes": pre_decision_quotes,
        }
        pre_ver = getattr(packet, "predecision_version", "v1")
        strat_ver = getattr(packet, "strategy_version", "dynamic-desk-cio-20260927")

        rec = CIODecisionRecord(
            case_id=packet.case_id,
            packet=packet,
            pre_decision_portfolio=pre_decision_portfolio,
            pre_decision_quotes=pre_decision_quotes,
            predecision_snapshot=pre_snap,
            predecision_version=pre_ver,
            strategy_version=strat_ver,
            applied_lesson_ids=list(applied_lessons),
            decision_delta=dict(dec_delta),
            rejected_opportunities=list(packet.alternatives_considered),
            hypothesis_version=1,
            status="ACTIVE",
            validation_status="RECORDED",
        )
        self._records[packet.case_id] = rec
        self._processed_case_ids.add(packet.case_id)
        self._append_jsonl(self._log_file, rec)

        if self.event_store is not None:
            self.event_store.append(
                EventEnvelope(
                    event_type=EventType.CIO_DECISION_RECORDED,
                    aggregate_id=f"cio:{packet.case_id}",
                    payload=rec.model_dump(mode="json"),
                )
            )
        return rec

    def record_fill(
        self,
        case_id: str,
        order_id: str,
        fill_dict: Dict[str, Any],
        costs_dict: Dict[str, float],
    ) -> Optional[CIODecisionRecord]:
        """Record fill and transaction costs for a decision."""
        rec = self._records.get(case_id)
        if not rec:
            return None
        rec.order_id = order_id
        rec.fill = fill_dict
        rec.costs = costs_dict
        rec.status = "FILLED"
        self._persist_all()
        return rec

    def record_outcome(
        self,
        case_id: str,
        outcome_dict: Dict[str, Any],
        attribution_dict: Optional[Dict[str, Any]] = None,
        lessons: Optional[List[str]] = None,
        is_partial: bool = False,
        prediction_vs_outcome: Optional[Dict[str, Any]] = None,
        applied_lesson_ids: Optional[List[str]] = None,
        decision_delta: Optional[Dict[str, Any]] = None,
        strategy_version: Optional[str] = None,
        validation_status: str = "RECORDED",
        as_of: Optional[datetime] = None,
    ) -> Optional[CIOLessonRecord]:
        """Record subsequent outcome, attribution, and extracted lessons."""
        rec = self._records.get(case_id)
        if not rec:
            return None
        # Idempotency check: if outcome already recorded, return existing lesson
        if rec.status == "CLOSED" and rec.outcome == outcome_dict:
            existing = next((l for l in self._lessons if l.case_id == case_id), None)
            return existing
        if outcome_dict in rec.outcomes:
            existing = next((l for l in self._lessons if l.case_id == case_id), None)
            return existing

        rec.outcomes.append(outcome_dict)
        rec.outcome = outcome_dict
        rec.attribution = attribution_dict or {}
        rec.residual_quantity = outcome_dict.get("residual_quantity")
        rec.residual_entry_cost = outcome_dict.get("costs", {}).get("residual_entry_costs")

        pred = {
            "case_id": rec.case_id,
            "thesis": rec.packet.thesis,
            "catalyst": getattr(rec.packet, "catalyst", None),
            "invalidation": getattr(rec.packet, "invalidation", None),
            "action": rec.packet.action,
            "horizon": getattr(rec.packet.holding_horizon, "value", str(rec.packet.holding_horizon)),
            "quantity": rec.packet.quantity,
            "confidence": rec.packet.confidence,
            "strategy_version": rec.strategy_version,
        }
        realized_pnl = outcome_dict.get("realized_pnl", 0.0)
        p_vs_o = prediction_vs_outcome or {
            "prediction": pred,
            "outcome": {
                "realized_pnl": realized_pnl,
                "exit_price": outcome_dict.get("exit_price"),
                "holding_duration_seconds": outcome_dict.get("holding_duration_seconds"),
                "exit_reason": outcome_dict.get("exit_reason"),
            },
            "pnl_sign": "POSITIVE" if realized_pnl > 0 else ("NEGATIVE" if realized_pnl < 0 else "FLAT"),
            "prediction_accurate": (realized_pnl > 0 and rec.packet.action == "BUY") or (realized_pnl < 0 and rec.packet.action == "SELL"),
        }
        rec.prediction_vs_outcome = p_vs_o
        if applied_lesson_ids is not None:
            rec.applied_lesson_ids = list(applied_lesson_ids)
        if decision_delta is not None:
            rec.decision_delta = dict(decision_delta)
        if strategy_version is not None:
            rec.strategy_version = strategy_version
        rec.validation_status = validation_status

        if is_partial:
            rec.status = "PARTIALLY_CLOSED"
            self._persist_all()
            if self.event_store is not None:
                self.event_store.append(
                    EventEnvelope(
                        event_type=EventType.CIO_OUTCOME_EVALUATED,
                        aggregate_id=f"cio:{case_id}",
                        payload={"outcome": outcome_dict, "attribution": rec.attribution, "is_partial": True},
                    )
                )
            return None

        rec.status = "CLOSED"
        new_lessons = lessons or []
        for l in new_lessons:
            if l not in rec.lessons:
                rec.lessons.append(l)

        # An explicit empty list is an outcome-only receipt (e.g. the SELL
        # case paired with the original BUY hypothesis). Do not invent another
        # generic lesson or duplicate attribution in subsequent CIO context.
        if lessons == []:
            self._persist_all()
            if self.event_store is not None:
                self.event_store.append(EventEnvelope(
                    event_type=EventType.CIO_OUTCOME_EVALUATED,
                    aggregate_id=f"cio:{case_id}",
                    payload={"outcome": outcome_dict, "attribution": rec.attribution,
                             "outcome_only": True},
                ))
            return None

        # Form structured lesson record
        # Derive lesson timestamp from decision timeline, not wall clock
        lesson_created_at = as_of
        if lesson_created_at is None:
            # Fall back to the outcome's closed_at timestamp if available
            raw_closed = outcome_dict.get("closed_at") or outcome_dict.get("as_of") or outcome_dict.get("timestamp")
            if raw_closed:
                try:
                    lesson_created_at = datetime.fromisoformat(str(raw_closed))
                    if lesson_created_at.tzinfo is None:
                        lesson_created_at = lesson_created_at.replace(tzinfo=timezone.utc)
                except Exception:
                    lesson_created_at = None
        if lesson_created_at is None:
            lesson_created_at = getattr(rec.packet, "as_of", None) or rec.created_at
        if lesson_created_at is None:
            lesson_created_at = datetime.now(timezone.utc)
        if lesson_created_at.tzinfo is None:
            lesson_created_at = lesson_created_at.replace(tzinfo=timezone.utc)

        if "as_of" not in outcome_dict:
            outcome_dict["as_of"] = lesson_created_at.isoformat()

        lesson_obj = CIOLessonRecord(
            lesson_id=f"lesson-{uuid.uuid4()}",
            case_id=case_id,
            symbol=rec.packet.selected_instrument,
            created_at=lesson_created_at,
            hypothesis_version=rec.hypothesis_version,
            pre_decision_thesis=rec.packet.thesis,
            catalyst=getattr(rec.packet, "catalyst", None),
            invalidation=getattr(rec.packet, "invalidation", None),
            fill_details=rec.fill,
            outcome_details=outcome_dict,
            attribution=rec.attribution or {},
            rejected_alternatives=rec.rejected_opportunities,
            takeaway="; ".join(rec.lessons) if rec.lessons else "Outcome recorded.",
            strategy_version=rec.strategy_version,
            prediction_vs_outcome=p_vs_o,
            applied_lesson_ids=list(rec.applied_lesson_ids),
            decision_delta=dict(rec.decision_delta),
            validation_status=validation_status,
        )
        self._lessons.append(lesson_obj)
        self._append_jsonl(self._lessons_file, lesson_obj)
        self._persist_all()

        if self.event_store is not None:
            self.event_store.append(
                EventEnvelope(
                    event_type=EventType.CIO_OUTCOME_EVALUATED,
                    aggregate_id=f"cio:{case_id}",
                    payload={"outcome": outcome_dict, "attribution": rec.attribution},
                )
            )
            self.event_store.append(
                EventEnvelope(
                    event_type=EventType.CIO_LESSON_RECORDED,
                    aggregate_id=f"cio:{case_id}",
                    payload=lesson_obj.model_dump(mode="json"),
                )
            )
        return lesson_obj

    def version_hypothesis(
        self,
        parent_case_id: str,
        new_packet: CIODecisionPacket,
        reason: str,
    ) -> CIODecisionRecord:
        """Version an investment hypothesis without rewriting past records."""
        parent = self._records.get(parent_case_id)
        parent_ver = parent.hypothesis_version if parent else 1
        new_ver = parent_ver + 1

        if parent:
            parent.superseded_by = new_packet.case_id
            self._persist_all()

        rec = CIODecisionRecord(
            case_id=new_packet.case_id,
            packet=new_packet,
            hypothesis_version=new_ver,
            status="ACTIVE",
            lessons=[f"Versioned from {parent_case_id} (v{parent_ver}->v{new_ver}): {reason}"],
        )
        self._records[new_packet.case_id] = rec
        self._processed_case_ids.add(new_packet.case_id)
        self._append_jsonl(self._log_file, rec)
        return rec

    def retrieve_context_lessons(
        self,
        symbol: Optional[str] = None,
        as_of: Optional[datetime] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Retrieve prior lessons for inclusion in the next CIO decision context.

        Filters strictly for verified real outcomes (must have actual fill and realized outcome details),
        excludes superseded records, and enforces as_of boundary to prevent future leakage.
        Distinguishes recorded outcomes from validated improvements without automatic PnL promotion.
        """
        as_of_utc = as_of if as_of and as_of.tzinfo else (as_of.replace(tzinfo=timezone.utc) if as_of else None)
        valid_lessons = []
        for l in self._lessons:
            rec = self._records.get(l.case_id)
            non_action = bool(rec and rec.status == "CLOSED" and rec.packet.action.upper() in {"HOLD", "REJECT", "NO_TRADE"}
                              and l.outcome_details and l.outcome_details.get("no_trade_pnl") is True
                              and l.outcome_details.get("no_fill") is True and l.outcome_details.get("realized_pnl") == 0)
            if (not l.fill_details and not non_action) or not l.outcome_details or "realized_pnl" not in l.outcome_details:
                continue
            if l.superseded_by is not None or (rec is not None and rec.superseded_by is not None):
                continue
            if as_of_utc is not None:
                created = l.created_at if l.created_at.tzinfo else l.created_at.replace(tzinfo=timezone.utc)
                # Enforce strict temporal eligibility: lessons are consumed ONLY by decisions timestamped strictly AFTER the outcome
                if created >= as_of_utc:
                    continue  # Exclude future and concurrent lessons to prevent future leakage
            valid_lessons.append(l)

        matches: List[CIOLessonRecord] = []
        if symbol:
            sym_clean = symbol.upper()
            matches = [l for l in valid_lessons if l.symbol.upper() == sym_clean]
        for l in reversed(valid_lessons):
            if l not in matches and len(matches) < limit:
                matches.append(l)
        return [m.model_dump(mode="json") for m in matches[-limit:]]

    def retrieve_past_outcomes(
        self,
        symbol: Optional[str] = None,
        as_of: Optional[datetime] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Retrieve later outcomes: executed fills or explicitly zero-PnL non-actions.
        
        Enforces as_of boundary so future outcomes stay strictly excluded.
        """
        as_of_utc = as_of if as_of and as_of.tzinfo else (as_of.replace(tzinfo=timezone.utc) if as_of else None)
        outcomes = []
        for rec in self._records.values():
            if rec.outcome and (rec.fill or (rec.status == "CLOSED" and rec.packet.action.upper() in {"HOLD", "REJECT", "NO_TRADE"}
                                             and rec.outcome.get("no_trade_pnl") is True and rec.outcome.get("no_fill") is True
                                             and rec.outcome.get("realized_pnl") == 0)):
                if as_of_utc is not None:
                    raw_ts = (
                        rec.outcome.get("as_of")
                        or rec.outcome.get("closed_at")
                        or rec.outcome.get("timestamp")
                    )
                    if raw_ts:
                        try:
                            o_dt = datetime.fromisoformat(str(raw_ts))
                            o_utc = o_dt if o_dt.tzinfo else o_dt.replace(tzinfo=timezone.utc)
                        except Exception:
                            o_utc = rec.created_at if rec.created_at.tzinfo else rec.created_at.replace(tzinfo=timezone.utc)
                    else:
                        o_utc = rec.created_at if rec.created_at.tzinfo else rec.created_at.replace(tzinfo=timezone.utc)
                    if o_utc >= as_of_utc:
                        continue
                if symbol is None or rec.packet.selected_instrument.upper() == symbol.upper():
                    outcomes.append({
                        "case_id": rec.case_id,
                        "symbol": rec.packet.selected_instrument,
                        "thesis": rec.packet.thesis,
                        "outcome": rec.outcome,
                        "attribution": rec.attribution,
                        "costs": rec.costs,
                        "fill": rec.fill,
                    })
        return outcomes[-limit:]

    def retrieve_rejected_opportunities(
        self,
        as_of: Optional[datetime] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Retrieve recent rejected opportunities strictly up to as_of timestamp."""
        as_of_utc = as_of if as_of and as_of.tzinfo else (as_of.replace(tzinfo=timezone.utc) if as_of else None)
        rejected = []
        for rec in self._records.values():
            if as_of_utc is not None:
                p_ts = rec.packet.as_of or rec.created_at
                p_utc = p_ts if p_ts.tzinfo else p_ts.replace(tzinfo=timezone.utc)
                if p_utc >= as_of_utc:
                    continue
            for opp in rec.rejected_opportunities:
                rejected.append({
                    "case_id": rec.case_id,
                    "opportunity": opp,
                })
        return rejected[-limit:]

    def _append_jsonl(self, path: Path, item: BaseModel) -> None:
        if self.read_only:
            return
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item.model_dump(mode="json"), sort_keys=True) + "\n")

    def _persist_all(self) -> None:
        if self.read_only:
            return
        # Atomic rewrite of learning store
        tmp = self._log_file.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for rec in self._records.values():
                fh.write(json.dumps(rec.model_dump(mode="json"), sort_keys=True) + "\n")
        tmp.replace(self._log_file)
