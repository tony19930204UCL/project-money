"""Restart-stable CIO context and deterministic call gate (engineering candidate)."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field


class FrozenDecisionContext(BaseModel):
    """Immutable, dated preopen snapshot; amendments require a new context id."""
    model_config = ConfigDict(frozen=True)
    context_id: str
    session_date: str
    official_source_lineage: list[dict[str, Any]]
    thesis: str
    valuation_scenarios: dict[str, Any]
    catalysts: list[str]
    entry_zone: dict[str, Any]
    invalidation: dict[str, Any]
    exposure_ceiling: float

    def __init__(self, **data: Any) -> None:
        from copy import deepcopy
        super().__init__(**data)
        object.__setattr__(self, "_frozen_payload", {
            "official_source_lineage": tuple(MappingProxyType(deepcopy(x)) for x in data["official_source_lineage"]),
            "valuation_scenarios": MappingProxyType(deepcopy(data["valuation_scenarios"])),
            "catalysts": tuple(data["catalysts"]),
            "entry_zone": MappingProxyType(deepcopy(data["entry_zone"])),
            "invalidation": MappingProxyType(deepcopy(data["invalidation"])),
        })

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = super().model_dump(*args, **kwargs)
        result.update({k: _thaw(v) for k, v in self._frozen_payload.items()})
        return result

    def __getattribute__(self, name: str) -> Any:
        if name in {"official_source_lineage", "valuation_scenarios", "catalysts", "entry_zone", "invalidation"}:
            payload = super().__getattribute__("_frozen_payload")
            return payload[name]
        value = super().__getattribute__(name)
        return value


def _thaw(value: Any) -> Any:
    if isinstance(value, MappingProxyType): return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple): return [_thaw(v) for v in value]
    if isinstance(value, list): return [_thaw(v) for v in value]
    if isinstance(value, dict): return {k: _thaw(v) for k, v in value.items()}
    return value


class CIOSessionHistory:
    """Durable session identity, context and append-only invocation history."""
    def __init__(self, root: Path, session_id: str = "project-money-main-cio") -> None:
        if not session_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in session_id):
            raise ValueError("invalid session_id")
        self.root = Path(root)
        self.session_id = session_id
        self.root = self.root / session_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.context_file = self.root / "frozen_context.json"
        self.history_file = self.root / "session_history.jsonl"

    def freeze(self, context: FrozenDecisionContext) -> FrozenDecisionContext:
        if self.context_file.exists():
            prior = self.load_context()
            if prior.model_dump(mode="json") != context.model_dump(mode="json"):
                raise ValueError("frozen context already exists; create a new session context")
            return prior
        self._atomic_write(self.context_file, context.model_dump(mode="json"))
        return context

    def load_context(self) -> FrozenDecisionContext:
        return FrozenDecisionContext.model_validate_json(self.context_file.read_text(encoding="utf-8"))

    def append(self, item: dict[str, Any]) -> None:
        row = {**item, "session_id": self.session_id}
        with self.history_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
            f.flush(); os.fsync(f.fileno())

    def history(self) -> list[dict[str, Any]]:
        if not self.history_file.exists():
            return []
        return [json.loads(line) for line in self.history_file.read_text(encoding="utf-8").splitlines() if line]

    @staticmethod
    def _atomic_write(path: Path, value: dict[str, Any]) -> None:
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(value, f, sort_keys=True, ensure_ascii=False); f.flush(); os.fsync(f.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp): os.unlink(temp)


class MaterialDeltaGate:
    """Trigger only on new material evidence/levels/risk changes; dedupe and fail closed."""
    MATERIAL_FIELDS = {"official_material_ids", "entry_triggered", "invalidation_triggered", "reserve_changed", "risk_changed"}

    def __init__(self, history: CIOSessionHistory) -> None:
        self.history_store = history

    def evaluate(self, observation: dict[str, Any], *, data_available: bool = True) -> dict[str, Any]:
        if not data_available:
            return {"status": "BLOCKED_DATA_UNAVAILABLE", "should_call": False, "reason": "fail_closed"}
        prior = self.history_store.history()
        seen = {x for row in prior if row.get("call_succeeded") for x in row.get("material_ids", [])}
        scope = f"{observation.get('symbol', '')}|{observation.get('session_id', '')}|"
        ids = {scope + str(x) for x in (observation.get("official_material_ids") or [])} - seen
        edge_keys = ("entry_triggered", "invalidation_triggered", "reserve_changed", "risk_changed")
        edge_state = {k: bool(observation.get(k, False)) for k in edge_keys}
        prior_edges = [row.get("edge_state", {}) for row in prior if (row.get("call_succeeded") or row.get("observation_only")) and row.get("scope", "||") == scope]
        previous = prior_edges[-1] if prior_edges else {}
        edge = any(value and not previous.get(key, False) for key, value in edge_state.items())
        material = bool(ids or edge)
        if not material:
            self.history_store.append({"observation_only": True, "scope": scope, "edge_state": edge_state})
            return {"status": "DUPLICATE" if observation.get("official_material_ids") else "IDLE", "should_call": False, "reason": "no_material_delta"}
        return {"status": "TRIGGERED", "should_call": True, "material_ids": sorted(x[len(scope):] for x in ids), "edge_state": edge_state, "invalidation_requires_cio": bool(observation.get("invalidation_triggered"))}

    def record_call(self, observation: dict[str, Any], receipt: dict[str, Any]) -> None:
        success = bool(receipt.get("success", True))
        scope = f"{observation.get('symbol', '')}|{observation.get('session_id', '')}|"
        self.history_store.append({"scope": scope, "material_ids": [scope + str(x) for x in (observation.get("official_material_ids") or [])], "edge_state": {k: bool(observation.get(k, False)) for k in ("entry_triggered", "invalidation_triggered", "reserve_changed", "risk_changed")}, "observation": observation, "call_receipt": receipt, "call_succeeded": success})