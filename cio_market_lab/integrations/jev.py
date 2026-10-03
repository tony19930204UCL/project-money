"""Jev Decision Provider integration for CIO Market Lab.

Invokes local `jev choose` only with a prevalidated candidate allowlist and correct
`jev.action_choice_request_v1` schema. Validates that the returned `selected_id` is
strictly within the offered allowlist, supports timeout and safe fallback to
`reobserve` or `abstain`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

REQUEST_SCHEMA = "jev.action_choice_request_v1"
RESPONSE_SCHEMA = "jev.action_choice_v1"
MAX_CANDIDATES = 32
MAX_REGIONS = 100
MAX_HISTORY = 16

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class CandidateAction(BaseModel):
    id: str
    description: str

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if not _ID_PATTERN.fullmatch(v):
            raise ValueError(
                f"Candidate id '{v}' invalid; must match regex ^[A-Za-z0-9][A-Za-z0-9._:-]{{0,63}}$"
            )
        return v

    @field_validator("description")
    @classmethod
    def validate_description(cls, v: str) -> str:
        s = v.strip()
        if not s or len(s) > 600:
            raise ValueError("Candidate description must be 1 to 600 characters")
        return s


class JevChoiceRequest(BaseModel):
    schema_: str = Field(default=REQUEST_SCHEMA, alias="schema")
    goal: str
    observation_id: Optional[str] = ""
    regions: List[Dict[str, Any]] = Field(default_factory=list)
    history: List[Dict[str, Any]] = Field(default_factory=list)
    candidates: List[CandidateAction]

    @field_validator("schema_")
    @classmethod
    def validate_schema(cls, v: str) -> str:
        if v != REQUEST_SCHEMA:
            raise ValueError(f"schema must be {REQUEST_SCHEMA}")
        return v

    @field_validator("goal")
    @classmethod
    def validate_goal(cls, v: str) -> str:
        s = v.strip()
        if not s or len(s) > 2000:
            raise ValueError("goal must be 1 to 2000 characters")
        return s

    @field_validator("candidates")
    @classmethod
    def validate_candidates(cls, v: List[CandidateAction]) -> List[CandidateAction]:
        if not (2 <= len(v) <= MAX_CANDIDATES):
            raise ValueError(f"candidates must have between 2 and {MAX_CANDIDATES} items")
        ids = [c.id for c in v]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate ids must be unique")
        id_set = set(ids)
        if not ({"reobserve", "abstain"} <= id_set):
            raise ValueError("candidates must include 'reobserve' and 'abstain'")
        return v

    def model_dump_payload(self) -> Dict[str, Any]:
        data = {
            "schema": self.schema_,
            "goal": self.goal,
            "observation_id": self.observation_id or "",
            "regions": self.regions,
            "history": self.history,
            "candidates": [{"id": c.id, "description": c.description} for c in self.candidates],
        }
        return data


class JevChoiceResponse(BaseModel):
    schema_: str = Field(default=RESPONSE_SCHEMA, alias="schema")
    selected_id: str
    confidence: float = 0.0
    reason: str = ""
    observation_id: str = ""
    probabilities: Dict[str, float] = Field(default_factory=dict)
    is_fallback: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema_,
            "selected_id": self.selected_id,
            "confidence": self.confidence,
            "reason": self.reason,
            "observation_id": self.observation_id,
            "probabilities": self.probabilities,
            "is_fallback": self.is_fallback,
        }


class JevDecisionProvider:
    """Invokes local `jev choose` with candidate allowlist validation and safe fallback."""

    def __init__(
        self,
        jev_bin: Optional[str] = None,
        default_timeout: float = 5.0,
        default_fallback: str = "reobserve",
    ):
        self.jev_bin = jev_bin or os.environ.get("JEV_BIN", "jev")
        self.default_timeout = default_timeout
        self.default_fallback = default_fallback

    def choose(
        self,
        request: JevChoiceRequest | Mapping[str, Any],
        timeout: Optional[float] = None,
        fallback_id: Optional[str] = None,
    ) -> JevChoiceResponse:
        """Executes choice with input validation, allowlist enforcement, and safe fallback."""
        if not isinstance(request, JevChoiceRequest):
            req_dict = dict(request)
            if "schema" in req_dict and "schema_" not in req_dict:
                req_dict["schema_"] = req_dict.pop("schema")
            validated_request = JevChoiceRequest(**req_dict)
        else:
            validated_request = request

        allowlist: Set[str] = {c.id for c in validated_request.candidates}
        fb_id = fallback_id or self.default_fallback
        if fb_id not in allowlist:
            fb_id = "reobserve" if "reobserve" in allowlist else next(iter(allowlist))

        tout = timeout if timeout is not None else self.default_timeout
        obs_id = validated_request.observation_id or ""

        payload = validated_request.model_dump_payload()
        payload_bytes = json.dumps(payload).encode("utf-8")

        def make_fallback(reason: str) -> JevChoiceResponse:
            logger.warning("Jev choose fallback triggered: %s", reason)
            return JevChoiceResponse(
                schema_=RESPONSE_SCHEMA,
                selected_id=fb_id,
                confidence=0.0,
                reason=reason,
                observation_id=obs_id,
                probabilities={},
                is_fallback=True,
            )

        try:
            proc = subprocess.run(
                [self.jev_bin, "choose"],
                input=payload_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=tout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return make_fallback(f"Timeout expired after {tout}s")
        except FileNotFoundError:
            return make_fallback(f"Executable '{self.jev_bin}' not found on system")
        except Exception as ex:
            return make_fallback(f"Subprocess execution error: {type(ex).__name__}: {ex}")

        if proc.returncode != 0:
            stderr_snippet = proc.stderr.decode("utf-8", errors="replace").strip()[:200]
            return make_fallback(f"jev choose returned code {proc.returncode}: {stderr_snippet}")

        stdout_raw = proc.stdout.decode("utf-8", errors="replace").strip()
        if not stdout_raw:
            return make_fallback("Empty response from jev choose")

        try:
            res_json = json.loads(stdout_raw)
        except json.JSONDecodeError as err:
            return make_fallback(f"Invalid JSON from jev choose: {err}")

        if not isinstance(res_json, dict):
            return make_fallback("Response was not a JSON object")

        selected = res_json.get("selected_id")
        if not isinstance(selected, str) or not selected:
            return make_fallback("Missing or non-string selected_id in Jev response")

        # Crucial security & reliability check: allowlist enforcement
        if selected not in allowlist:
            return make_fallback(
                f"Selected action '{selected}' is not in prevalidated allowlist {sorted(allowlist)}"
            )

        confidence = 0.0
        try:
            confidence = float(res_json.get("confidence", 0.0))
        except (ValueError, TypeError):
            pass

        reason = str(res_json.get("reason", "chosen"))
        probabilities = res_json.get("probabilities")
        if not isinstance(probabilities, dict):
            probabilities = {}

        return JevChoiceResponse(
            schema_=RESPONSE_SCHEMA,
            selected_id=selected,
            confidence=round(confidence, 3),
            reason=reason,
            observation_id=str(res_json.get("observation_id", obs_id)),
            probabilities=probabilities,
            is_fallback=False,
        )
