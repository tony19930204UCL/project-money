"""Strict disk-backed read-only visibility; never creates or evaluates cases."""
import json
from datetime import datetime, timezone
from pathlib import Path

def _read_rows(path):
    if not path.exists():
        return []
    rows=[]
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row=json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Expected an object")
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{path.name}:{number}: incomplete or invalid record") from exc
        rows.append(row)
    return rows

def _utc(value):
    result=datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Observation deadline requires a timezone")
    return result.astimezone(timezone.utc)

def read_learning_snapshot(runtime_dir, *, as_of=None, symbols=None, limit=200):
    runtime_dir=Path(runtime_dir)
    now=as_of or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("as_of requires a timezone")
    source=runtime_dir/"cio_learning.jsonl"
    rows=_read_rows(source)
    # Historical versions are append-only: only the last version counts.
    unique={}
    for row in rows:
        if not row.get("case_id") or not isinstance(row.get("packet"), dict):
            raise ValueError("Learning record is missing case_id or packet")
        unique[row["case_id"]]=row
    allowed=set(symbols or [])
    cases=[]
    for row in unique.values():
        packet=row["packet"]
        symbol=packet.get("selected_instrument")
        if allowed and symbol not in allowed:
            continue
        conditions=packet.get("conditions") or {}
        deadline=conditions.get("observation_deadline")
        outcome=row.get("outcome")
        state=row.get("status", "UNKNOWN")
        if state=="CLOSED":
            evaluation="CLOSED_WITH_RECORDED_OUTCOME" if outcome is not None else "CLOSED_WITHOUT_OUTCOME"
        elif deadline:
            evaluation="DUE_AWAITING_ELIGIBLE_EVIDENCE" if now>=_utc(deadline) else "AWAITING_DEADLINE"
        else:
            evaluation="NO_EVALUATION_DEADLINE"
        cases.append({
            "case_id":row["case_id"], "symbol":symbol, "action":packet.get("action"),
            "status":state, "created_at":row.get("created_at"),
            "observation_deadline":deadline, "evaluation_status":evaluation,
            "reason":packet.get("reason") or packet.get("rationale") or packet.get("thesis"),
            "execution_armed":bool(conditions.get("execution_armed", packet.get("execution_armed", False))),
            "analysis_reference":conditions.get("analysis_reference"),
            "packet_provenance":row.get("packet_provenance") or {key:value for key,value in (packet.get("provenance") or {}).items() if key in ("authority", "actor_role", "source", "receipt_id", "signer_id", "verified_by_worker")},
            "applied_lesson_ids":row.get("applied_lesson_ids", []),
            "lesson_used":bool(row.get("applied_lesson_ids", [])),
            "decision_delta":row.get("decision_delta", {}),
            "lessons":row.get("lessons", []), "outcome":outcome,
            "no_trade_pnl":outcome.get("realized_pnl") if packet.get("action")=="NO_TRADE" and isinstance(outcome,dict) else None,
        })
    cases.sort(key=lambda case:(case.get("created_at") or "",case["case_id"]),reverse=True)
    counts={}
    for case in cases:
        status=case["evaluation_status"]
        counts[status]=counts.get(status,0)+1
    return {"as_of":now.isoformat(),"source_status":"CURRENT_DISK_READBACK" if source.exists() else "NOT_OBSERVED",
            "source_file":str(source),"raw_record_count":len(rows),"total_case_count":len(cases),
            "returned_case_count":min(len(cases),limit),"has_more":len(cases)>limit,
            "evaluation_counts":counts,"cases":cases[:limit],
            "lessons":_read_rows(runtime_dir/"cio_lessons.jsonl"),
            "read_only":True,"paper_only":True,"broker_connected":False,
            "completion_claim_allowed":False}
