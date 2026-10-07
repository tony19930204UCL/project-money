"""Portable source-only repair surface for Project Money Issue #16.

This module joins existing public-source, local inference, role, monitor and
durable session primitives. It never deploys, changes cron/provider settings,
touches private runtime data, or claims Main-owned live acceptance.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cio_market_lab.engine.cio_session import CIOSessionHistory
from cio_market_lab.engine.stage_d_observation import apply_verified_quote_edges
from cio_market_lab.engine.team_ops import FunctionalDeskRole
from cio_market_lab.integrations.hermes_chat import run_hermes_cli_chat
from cio_market_lab.integrations.runtime_evidence import RuntimeEvidenceAdapter
from cio_market_lab.research.browser import PublicResearchEvidence, validate_and_sanitize_evidence
from cio_market_lab.research.free_adapters import FreeSourceCoordinator


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _stable_hash(value: Any) -> str:
    raw=json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,default=str).encode()
    return hashlib.sha256(raw).hexdigest()


# ---------------- public outbound boundary ----------------

class PublicWorkerEvidence(BaseModel):
    model_config=ConfigDict(extra="forbid")
    research_id: str
    symbol: str
    source_url: str
    source_tier: str
    observed_at: datetime
    published_at: Optional[str]=None
    verification_status: str
    verified_facts: list[str]
    research_scope: str
    limitations: list[str]=Field(default_factory=list)


_SECRET_OR_LOCAL_VALUE=re.compile(
    r'(?i)(api[_ -]?key|bearer\\s+[A-Za-z0-9._-]+|password|secret|token|'
    r'(?:^|[\\s"\'=])/(?:home|users|var|private|mnt|tmp)/|'
    r'[A-Za-z]:\\(?:Users|Windows|Temp)\\)'
)
_FORBIDDEN_FIELD_PARTS=(
    "account","credential","secret","token","api_key","apikey","holding","order",
    "portfolio","position_quantity","quantity","private_path","runtime_path","client_id",
)


def _reject_private_content(value: Any, path: str="payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            low=str(key).lower()
            if any(part in low for part in _FORBIDDEN_FIELD_PARTS):
                raise ValueError(f"PUBLIC_OUTBOUND_FIELD_REJECTED:{path}.{key}")
            _reject_private_content(child,f"{path}.{key}")
    elif isinstance(value,(list,tuple)):
        for i,child in enumerate(value):
            _reject_private_content(child,f"{path}[{i}]")
    elif isinstance(value,str):
        lowered=value.lower()
        local_path_markers=("/home/","/users/","/var/","/private/","/mnt/","/tmp/")
        if _SECRET_OR_LOCAL_VALUE.search(value) or any(marker in lowered for marker in local_path_markers):
            raise ValueError(f"PUBLIC_OUTBOUND_VALUE_REJECTED:{path}")


def seal_public_evidence(record: Mapping[str,Any], *, now: datetime) -> dict[str,Any]:
    """Reuse canonical research sanitizer, then project through strict public allowlist."""
    ok,sanitized,reason=validate_and_sanitize_evidence(dict(record),now=_utc(now))
    if not ok or sanitized is None:
        raise ValueError(reason)
    allowed={
        "research_id":sanitized.research_id,
        "symbol":sanitized.symbol,
        "source_url":sanitized.source_url,
        "source_tier":sanitized.source_tier,
        "observed_at":sanitized.observed_at,
        "published_at":sanitized.published_at,
        "verification_status":sanitized.verification_status,
        "verified_facts":sanitized.verified_facts,
        "research_scope":sanitized.research_scope,
        "limitations":sanitized.limitations,
    }
    sealed=PublicWorkerEvidence.model_validate(allowed).model_dump(mode="json")
    _reject_private_content(sealed)
    return sealed


# ---------------- existing local inference transport + strict stage schemas ----------------

class DiscoveryOutput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    candidate_sources: list[str]=Field(min_length=1)
    discovery_summary: str=Field(min_length=1)
    missing_evidence: list[str]=Field(default_factory=list)

class CommercialOutput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    commercial_summary: str=Field(min_length=1)
    evidence_used: list[str]=Field(min_length=1)
    missing_evidence: list[str]=Field(default_factory=list)

class UnderwritingOutput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    underwriting_status: str=Field(pattern="^(PUBLIC_EVIDENCE_READY|INCOMPLETE|REJECT)$")
    thesis: str=Field(min_length=1)
    evidence_used: list[str]=Field(min_length=1)
    missing_evidence: list[str]=Field(default_factory=list)

class ChallengeOutput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    verdict: str=Field(pattern="^(PASS_PUBLIC_RESEARCH_ONLY|BLOCKED|REJECT)$")
    challenge_summary: str=Field(min_length=1)
    blockers: list[str]=Field(default_factory=list)
    next_action: str=Field(min_length=1)

STAGE_SCHEMAS={
    "discovery":DiscoveryOutput,
    "commercial":CommercialOutput,
    "underwriting":UnderwritingOutput,
    "challenge":ChallengeOutput,
}


class InferenceContract(BaseModel):
    model_config=ConfigDict(extra="forbid")
    provider: str=Field(min_length=1)
    model: str=Field(min_length=1)
    session_id: str=Field(min_length=1)
    workspace_root: str=Field(min_length=1)
    is_free_or_local_authorized: bool
    purpose: str=Field(min_length=1)




class StageRouteContract(BaseModel):
    """Explicit bounded primary/fallback route contract for one research stage."""
    model_config=ConfigDict(extra="forbid")
    stage: Literal["discovery","commercial","underwriting","challenge"]
    primary: InferenceContract
    fallback: list[InferenceContract]=Field(default_factory=list, max_length=2)

    def ordered(self) -> list[InferenceContract]:
        routes=[self.primary,*self.fallback]
        if not all(route.is_free_or_local_authorized for route in routes):
            raise RuntimeError("INFERENCE_ROUTE_NOT_FREE_OR_LOCAL_AUTHORIZED")
        identities=[(route.provider,route.model) for route in routes]
        if len(set(identities))!=len(identities):
            raise RuntimeError("INFERENCE_ROUTE_DUPLICATE_PROVIDER_MODEL")
        return routes


class AuthorizedStageInference:
    """Bounded free/local route selector over the existing Hermes transport.

    Every attempted route still passes HermesLocalInference's affirmative
    authenticated/non-fixture/returncode verification. Failed routes are
    retained for audit and never converted into success.
    """
    def __init__(
        self,
        route: StageRouteContract,
        *,
        transport: Callable[...,dict[str,Any]]=run_hermes_cli_chat,
        timeout_seconds: int=180,
    ):
        self.route=route
        self.transport=transport
        self.timeout_seconds=timeout_seconds
        self.last_route_attempts:list[dict[str,Any]]=[]

    def infer(self,stage:str,payload:Mapping[str,Any],schema:type[BaseModel])->tuple[str,dict[str,Any]]:
        if stage!=self.route.stage:
            raise RuntimeError("INFERENCE_STAGE_ROUTE_MISMATCH")
        self.last_route_attempts=[]
        last_error:Optional[Exception]=None
        for index,contract in enumerate(self.route.ordered()):
            try:
                identity,output=HermesLocalInference(
                    contract,transport=self.transport,timeout_seconds=self.timeout_seconds
                ).infer(stage,payload,schema)
                self.last_route_attempts.append({
                    "route":"PRIMARY" if index==0 else f"FALLBACK_{index}",
                    "provider":contract.provider,"model":contract.model,
                    "status":"AUTHENTICATED_NONFIXTURE_SUCCESS",
                    "model_identity":identity,
                })
                return identity,output
            except (RuntimeError,ValidationError,ValueError) as exc:
                last_error=exc
                self.last_route_attempts.append({
                    "route":"PRIMARY" if index==0 else f"FALLBACK_{index}",
                    "provider":contract.provider,"model":contract.model,
                    "status":"BLOCKED",
                    "reason":f"{type(exc).__name__}:{exc}",
                })
        raise RuntimeError(
            "ALL_AUTHORIZED_INFERENCE_ROUTES_BLOCKED:"
            + (f"{type(last_error).__name__}:{last_error}" if last_error else "NO_ROUTE")
        )

class HermesLocalInference:
    """Thin adapter over the repository's actual Hermes CLI + runtime verifier."""

    def __init__(
        self,
        contract: InferenceContract,
        *,
        transport: Callable[...,dict[str,Any]]=run_hermes_cli_chat,
        timeout_seconds: int=180,
    ):
        self.contract=contract
        self.transport=transport
        self.timeout_seconds=timeout_seconds

    def infer(self, stage: str, payload: Mapping[str,Any], schema: type[BaseModel]) -> tuple[str,dict[str,Any]]:
        if not self.contract.is_free_or_local_authorized:
            raise RuntimeError("INFERENCE_CONTRACT_NOT_FREE_OR_LOCAL_AUTHORIZED")
        prompt=(
            f"Project Money research-only stage={stage}. Use only supplied public evidence. "
            "No orders, holdings, accounts, credentials, private paths or capital actions. "
            "Return pure JSON matching this exact schema, with no extra keys:\n"
            + json.dumps(schema.model_json_schema(),ensure_ascii=False)
            + "\nInput:\n"+json.dumps(payload,ensure_ascii=False,default=str)
        )
        result=self.transport(
            prompt,
            session_id=self.contract.session_id,
            workspace_root=self.contract.workspace_root,
            timeout_seconds=self.timeout_seconds,
            provider=self.contract.provider,
            model=self.contract.model,
            enforce_cio_pin=False,
        )
        metadata=result.get("runtime_metadata")
        if not isinstance(metadata,Mapping):
            raise RuntimeError("INFERENCE_RUNTIME_METADATA_REQUIRED")
        required_runtime_flags={
            "auth_verified":True,
            "is_success_response":True,
            "is_fixture":False,
        }
        for key,expected in required_runtime_flags.items():
            if key not in metadata or metadata.get(key) is not expected:
                raise RuntimeError(f"INFERENCE_RUNTIME_EVIDENCE_INVALID:{key}")
        if "returncode" not in result or isinstance(result.get("returncode"),bool) or not isinstance(result.get("returncode"),int):
            raise RuntimeError("INFERENCE_RETURNCODE_REQUIRED")
        if result["returncode"]!=0:
            raise RuntimeError("INFERENCE_RETURNCODE_FAILED")
        if result.get("failed") or result.get("error") or result.get("is_fixture") is True:
            raise RuntimeError("INFERENCE_RUNTIME_UNAVAILABLE")
        response=result.get("response","")
        runtime=RuntimeEvidenceAdapter(
            pinned_provider=self.contract.provider,pinned_model=self.contract.model
        ).verify_runtime_evidence(
            metadata=metadata,response_text=response,exit_code=result["returncode"],
            pinned_provider=self.contract.provider,pinned_model=self.contract.model,allow_fixture=False,
        )
        if runtime.is_fixture:
            raise RuntimeError("INFERENCE_RUNTIME_UNAVAILABLE")
        parsed=schema.model_validate_json(response)
        return f"{runtime.resolved_provider}:{runtime.resolved_model}",parsed.model_dump(mode="json")


class PublicOnlyResearchWorkflowAdapter:
    def __init__(
        self,
        coordinator: Optional[FreeSourceCoordinator]=None,
        *,
        stage_inference: Optional[Mapping[str,HermesLocalInference]]=None,
        max_serialized_bytes: int=250_000,
    ):
        self.coordinator=coordinator or FreeSourceCoordinator()
        self.stage_inference=dict(stage_inference or {})
        self.max_serialized_bytes=max_serialized_bytes

    def _records(self,bundle:Mapping[str,Any],now:datetime)->list[dict[str,Any]]:
        rows=[]
        for name in ("official_facts","secondary_finviz","secondary_stock_analysis","peer_market_cap"):
            part=bundle.get(name) or {}
            record=part.get("record") if isinstance(part,Mapping) else None
            if not isinstance(record,Mapping):
                continue
            # Inspect the original adapter record before projection so secret/private
            # extras cannot be silently dropped and then transported.
            _reject_private_content(record,f"source_record.{name}")
            # Existing source adapters may expose richer records. Only the canonical
            # sanitized public evidence projection is allowed to leave this boundary.
            candidate={
                "research_id":record.get("research_id") or f"{name}-{bundle.get('symbol','unknown')}-{_stable_hash(record)[:12]}",
                "symbol":record.get("symbol") or bundle.get("symbol"),
                "source_url":record.get("source_url") or record.get("url"),
                "source_tier":record.get("source_tier") or ("official_filing" if name=="official_facts" else "secondary_cross_check"),
                "observed_at":record.get("observed_at") or now.isoformat(),
                "published_at":record.get("published_at"),
                "is_fixture":record.get("is_fixture",False),
                "verification_status":record.get("verification_status","verified"),
                "verified_facts":record.get("verified_facts") or [
                    json.dumps(record.get("metrics") or {},sort_keys=True,ensure_ascii=False)
                ],
                "research_scope":record.get("research_scope","historical_company_facts_not_catalyst"),
                "limitations":record.get("limitations") or [],
                "raw_metadata":record.get("raw_metadata") or {},
            }
            rows.append(seal_public_evidence(candidate,now=now))
        return rows

    def _run_stage(self,stage:str,payload:Mapping[str,Any],now:datetime)->dict[str,Any]:
        engine=self.stage_inference.get(stage)
        if engine is None:
            return {
                "stage":stage,"status":"BLOCKED","schema_valid":False,
                "observed_at":_utc(now).isoformat(),"reason":"INFERENCE_PREREQUISITE_MISSING",
            }
        try:
            _reject_private_content(payload)
            model_identity,output=engine.infer(stage,payload,STAGE_SCHEMAS[stage])
            # Validate a second time at the final outbound boundary.
            validated=STAGE_SCHEMAS[stage].model_validate(output).model_dump(mode="json")
            _reject_private_content(validated)
            row={
                "stage":stage,"status":"COMPLETED","schema_valid":True,
                "observed_at":_utc(now).isoformat(),"model_identity":model_identity,
                "input_sha256":_stable_hash(payload),"output_sha256":_stable_hash(validated),
                "output":validated,
            }
            route_attempts=getattr(engine,"last_route_attempts",None)
            if isinstance(route_attempts,list):
                row["route_attempts"]=json.loads(json.dumps(route_attempts))
            return row
        except (ValidationError,ValueError,RuntimeError) as exc:
            return {
                "stage":stage,"status":"BLOCKED","schema_valid":False,
                "observed_at":_utc(now).isoformat(),"reason":f"{type(exc).__name__}:{exc}",
            }

    def run(self,symbol:str,*,now:datetime,reader:Any=None)->dict[str,Any]:
        observed=_utc(now)
        try:
            bundle=self.coordinator.refresh_symbol(symbol,observed,reader=reader)
            raw=json.dumps(bundle,default=str,ensure_ascii=False).encode()
            if len(raw)>self.max_serialized_bytes:
                raise ValueError("PUBLIC_RESEARCH_BUNDLE_OVERSIZE")
            records=self._records(bundle,observed)
        except Exception as exc:
            return self._blocked(symbol,[{"stage":"fetch","status":"BLOCKED","reason":f"{type(exc).__name__}:{exc}"}],
                                 "RESTORE_APPROVED_PUBLIC_SOURCE_PREREQUISITE")
        official=any(r["source_tier"] in {"official_filing","regulatory_filing","official_exchange"} for r in records)
        fetch={
            "stage":"fetch","status":"COMPLETED" if records else "BLOCKED",
            "official_source_present":official,"source_count":len(records),
            "provenance":[{"source_url":r["source_url"],"observed_at":r["observed_at"],
                           "content_sha256":_stable_hash(r)} for r in records],
            "gaps":list(bundle.get("gaps") or []),
        }
        attempts=[fetch]
        if not records or not official:
            return self._blocked(symbol,attempts,"SUPPLY_GENUINELY_FRESH_OFFICIAL_PUBLIC_INPUT")

        payload={"symbol":symbol,"public_evidence":records,"gaps":list(bundle.get("gaps") or [])}
        prior={}
        model_ids={}
        for stage in ("discovery","commercial","underwriting","challenge"):
            stage_payload={**payload,**prior}
            row=self._run_stage(stage,stage_payload,observed)
            attempts.append(row)
            if row["status"]!="COMPLETED":
                return self._blocked(symbol,attempts,f"RESTORE_{stage.upper()}_INFERENCE_PREREQUISITE")
            prior[stage]=row["output"]
            model_ids[stage]=row["model_identity"]
        if model_ids["challenge"]==model_ids["underwriting"]:
            return self._blocked(symbol,attempts,"CONFIGURE_INDEPENDENT_HETEROGENEOUS_CHALLENGE_MODEL")
        underwriting_status=prior["underwriting"]["underwriting_status"]
        verdict=prior["challenge"]["verdict"]
        if underwriting_status=="INCOMPLETE":
            status="BLOCKED"
            exact_next_action="RESOLVE_UNDERWRITING_MISSING_EVIDENCE"
        elif underwriting_status=="REJECT":
            status="BLOCKED"
            exact_next_action="UNDERWRITING_REJECTED_RESEARCH_CANDIDATE"
        elif underwriting_status=="PUBLIC_EVIDENCE_READY" and verdict=="PASS_PUBLIC_RESEARCH_ONLY":
            status="COMPLETED_PUBLIC_RESEARCH_CANDIDATE"
            exact_next_action="MAIN_CIO_RUN_REAL_HOST_PUBLIC_INPUT_ACCEPTANCE"
        else:
            status="BLOCKED"
            exact_next_action=prior["challenge"]["next_action"]
        return {
            "status":status,"symbol":symbol,"attempts":attempts,
            "underwriting_status":underwriting_status,
            "challenge_verdict":verdict,
            "challenge_model_distinct":True,"live_acceptance_claimed":False,
            "owner":"MAIN_CIO",
            "exact_next_action":exact_next_action,
        }

    @staticmethod
    def _blocked(symbol:str,attempts:list[dict[str,Any]],action:str)->dict[str,Any]:
        return {"status":"BLOCKED","symbol":symbol,"attempts":attempts,"owner":"MAIN_CIO",
                "exact_next_action":action,"live_acceptance_claimed":False}



# ---------------- original research-engine callback bridge ----------------

class OriginalResearchEngineCallbackBridge:
    """Wire the portable public-only adapter into the original engine callback seam.

    The host keeps ownership of the original engine. This bridge supplies only
    its live fetch/generate/challenge callbacks and a deterministic verdict
    reducer. It does not create a second research engine.
    """
    GENERATE_STAGES=("discovery","commercial","underwriting")

    def __init__(
        self,
        coordinator: Optional[FreeSourceCoordinator]=None,
        *,
        stage_inference: Mapping[str,Any],
        max_serialized_bytes: int=250_000,
    ):
        self.workflow=PublicOnlyResearchWorkflowAdapter(
            coordinator=coordinator,
            stage_inference=stage_inference,
            max_serialized_bytes=max_serialized_bytes,
        )

    def fetch(self, *, symbol:str, now:datetime, reader:Any=None)->dict[str,Any]:
        observed=_utc(now)
        try:
            bundle=self.workflow.coordinator.refresh_symbol(symbol,observed,reader=reader)
            raw=json.dumps(bundle,default=str,ensure_ascii=False).encode()
            if len(raw)>self.workflow.max_serialized_bytes:
                raise ValueError("PUBLIC_RESEARCH_BUNDLE_OVERSIZE")
            records=self.workflow._records(bundle,observed)
        except Exception as exc:
            raise RuntimeError(f"FETCH_CALLBACK_BLOCKED:{type(exc).__name__}:{exc}") from exc
        official=any(r["source_tier"] in {"official_filing","regulatory_filing","official_exchange"} for r in records)
        if not records:
            raise RuntimeError("FETCH_CALLBACK_BLOCKED:NO_PUBLIC_RECORDS")
        if not official:
            raise RuntimeError("FETCH_CALLBACK_BLOCKED:NO_GENUINE_OFFICIAL_SOURCE")
        payload={
            "symbol":symbol,
            "public_evidence":records,
            "gaps":list(bundle.get("gaps") or []),
            "observed_at":observed.isoformat(),
            "provenance":[{
                "source_url":row["source_url"],
                "observed_at":row["observed_at"],
                "content_sha256":_stable_hash(row),
            } for row in records],
        }
        _reject_private_content(payload)
        return payload

    def generate(self, *, stage:str, payload:Mapping[str,Any], now:datetime)->dict[str,Any]:
        if stage not in self.GENERATE_STAGES:
            raise RuntimeError("ORIGINAL_ENGINE_GENERATE_STAGE_UNSUPPORTED")
        row=self.workflow._run_stage(stage,payload,_utc(now))
        if row.get("status")!="COMPLETED":
            raise RuntimeError(f"{stage.upper()}_CALLBACK_BLOCKED:{row.get('reason','UNKNOWN')}")
        return row

    def challenge(self, *, payload:Mapping[str,Any], now:datetime)->dict[str,Any]:
        row=self.workflow._run_stage("challenge",payload,_utc(now))
        if row.get("status")!="COMPLETED":
            raise RuntimeError(f"CHALLENGE_CALLBACK_BLOCKED:{row.get('reason','UNKNOWN')}")
        return row

    @staticmethod
    def verdict(*, underwriting:Mapping[str,Any], challenge:Mapping[str,Any])->dict[str,Any]:
        uw_identity=str(underwriting.get("model_identity") or "")
        challenge_identity=str(challenge.get("model_identity") or "")
        if not uw_identity or not challenge_identity:
            return {"status":"BLOCKED","reason":"VERIFIED_MODEL_IDENTITY_REQUIRED"}
        if uw_identity==challenge_identity:
            return {"status":"BLOCKED","reason":"HETEROGENEOUS_CHALLENGE_MODEL_REQUIRED"}
        uw_output=underwriting.get("output")
        ch_output=challenge.get("output")
        try:
            uw=UnderwritingOutput.model_validate(uw_output)
            ch=ChallengeOutput.model_validate(ch_output)
        except ValidationError as exc:
            return {"status":"BLOCKED","reason":f"STAGE_OUTPUT_SCHEMA_INVALID:{exc}"}
        if uw.underwriting_status=="INCOMPLETE":
            return {"status":"BLOCKED","underwriting_status":"INCOMPLETE",
                    "challenge_verdict":ch.verdict,"reason":"UNDERWRITING_INCOMPLETE"}
        if uw.underwriting_status=="REJECT":
            return {"status":"BLOCKED","underwriting_status":"REJECT",
                    "challenge_verdict":ch.verdict,"reason":"UNDERWRITING_REJECT"}
        if ch.verdict!="PASS_PUBLIC_RESEARCH_ONLY":
            return {"status":"BLOCKED","underwriting_status":uw.underwriting_status,
                    "challenge_verdict":ch.verdict,"reason":"CHALLENGE_DID_NOT_PASS"}
        return {
            "status":"COMPLETED_PUBLIC_RESEARCH_CANDIDATE",
            "underwriting_status":uw.underwriting_status,
            "challenge_verdict":ch.verdict,
            "challenge_model_distinct":True,
            "live_acceptance_claimed":False,
        }

    def callbacks(self)->dict[str,Callable[...,Any]]:
        return {
            "fetch":self.fetch,
            "generate":self.generate,
            "challenge":self.challenge,
            "verdict":self.verdict,
        }

    def run_original_entrypoint(
        self,
        entrypoint:Callable[...,Any],
        *,
        symbol:str,
        now:datetime,
        reader:Any=None,
    )->Any:
        if not callable(entrypoint):
            return {
                "status":"BLOCKED",
                "owner":"MAIN_CIO",
                "exact_next_action":"SUPPLY_ORIGINAL_RESEARCH_ENGINE_ENTRYPOINT",
                "live_acceptance_claimed":False,
            }
        return entrypoint(
            symbol=symbol,
            now=_utc(now),
            reader=reader,
            **self.callbacks(),
        )


# ---------------- role bindings: validate actual caller-supplied existing contracts ----------------

EXPECTED_ORIGINAL_FUNCTIONS=(
    "main_cio","tw_research","us_research","underwriting","allocation",
    "source_audio","industry_mapping","red_team","blindside",
)


def original_role_acceptance(
    ownership_bindings: Mapping[str,Mapping[str,Any]],
    schedule_jobs: Optional[Iterable[Mapping[str,Any]]]=None,
)->dict[str,dict[str,Any]]:
    jobs=list(schedule_jobs or [])
    result={}
    for function in EXPECTED_ORIGINAL_FUNCTIONS:
        binding=ownership_bindings.get(function)
        if not isinstance(binding,Mapping):
            result[function]={"identity_status":"BLOCKED_BINDING_MISSING","schedule_status":"UNKNOWN",
                              "privacy_evidence":"MISSING","downstream_evidence":"MISSING","live_output_evidence":"MISSING"}
            continue
        try:
            desk_role=FunctionalDeskRole.model_validate(binding.get("desk_role"))
            owner=str(binding.get("owner") or "").strip()
            if not owner or str(binding.get("function") or "")!=function:
                raise ValueError("OWNERSHIP_BINDING_MISMATCH")
            if function!="main_cio" and binding.get("public_worker_export_only") is not True:
                raise ValueError("WORKER_PUBLIC_EXPORT_BOUNDARY_MISSING")
            if function=="main_cio" and binding.get("private_context_allowed") is not True:
                raise ValueError("MAIN_CIO_PRIVATE_CONTEXT_OWNERSHIP_MISSING")
            identity="CONTRACT_VALID"
        except Exception as exc:
            result[function]={"identity_status":"BLOCKED_INVALID_BINDING","reason":f"{type(exc).__name__}:{exc}",
                              "schedule_status":"UNKNOWN","privacy_evidence":"MISSING",
                              "downstream_evidence":"MISSING","live_output_evidence":"MISSING"}
            continue
        matching=[j for j in jobs if str(j.get("role_id",""))==desk_role.role_id]
        sched="DUPLICATE_SCHEDULE_BINDING" if len(matching)>1 else (
            "SCHEDULE_NOT_CONFIGURED" if not matching else ("SCHEDULE_ENABLED" if matching[0].get("enabled") is True else "SCHEDULE_DISABLED")
        )
        result[function]={
            "identity_status":identity,"owner":owner,"role_id":desk_role.role_id,
            "schedule_status":sched,
            "privacy_evidence":"PASS" if binding.get("privacy_evidence") is True else "MISSING",
            "downstream_evidence":"PASS" if binding.get("downstream_evidence") is True else "MISSING",
            "live_output_evidence":"PASS" if binding.get("live_output_evidence") is True else "MISSING",
        }
    return result


# ---------------- monitor: reuse canonical quote-edge evaluator ----------------

class ReceiptAwarePositionConsumer:
    """Adapter over existing canonical Stage-D trigger evaluator.

    Caller supplies the already-existing observation contract. No invented holding
    quantity or synthetic VTI contract is created here.
    """
    def evaluate(
        self,
        observations: Mapping[str,Mapping[str,Any]],
        quotes: Mapping[str,Any],
        *,
        now: datetime,
        max_age_seconds: float=300,
        allow_fixture: bool=False,
    )->dict[str,Any]:
        results=[]
        for symbol,observation in observations.items():
            quote=quotes.get(symbol)
            stable_identity=_stable_hash({
                "symbol":symbol,
                "session_id":observation.get("session_id"),
                "condition_id":observation.get("condition_id"),
                "condition_version":observation.get("condition_version"),
                "observation_identity":observation.get("observation_identity"),
                "official_material_ids":observation.get("official_material_ids") or [],
                "buy_zone":observation.get("buy_zone"),
                "invalidation_condition":observation.get("invalidation_condition"),
            })
            if quote is None:
                results.append({"symbol":symbol,"stable_identity":stable_identity,
                                "classification":"UNKNOWN","triggered":None,"reason":"QUOTE_MISSING"})
                continue
            current_observation=dict(observation)
            # Historical edge objects are audit history, not evidence about the
            # current quote. Canonical evaluation must start from an edge-clean
            # observation so a rejected quote cannot inherit an old trigger.
            current_observation.pop("entry_edge",None)
            current_observation.pop("invalidation_edge",None)
            evaluated=apply_verified_quote_edges(
                current_observation,quote,_utc(now),
                max_age_seconds=max_age_seconds,allow_fixture=allow_fixture,
            )
            quote_status=evaluated.get("quote_edge_status")
            if quote_status=="BLOCKED_QUOTE_UNAVAILABLE":
                results.append({
                    "symbol":symbol,"stable_identity":stable_identity,
                    "classification":"UNKNOWN","triggered":None,
                    "reason":evaluated.get("quote_edge_reason") or "BLOCKED_QUOTE_UNAVAILABLE",
                    "quote_edge_status":quote_status,
                })
                continue
            if quote_status=="RESEARCH_ONLY_NO_PRICE_TRIGGER":
                results.append({
                    "symbol":symbol,"stable_identity":stable_identity,
                    "classification":"RESEARCH_ONLY_NO_PRICE_TRIGGER","triggered":False,
                    "quote_edge_status":quote_status,
                })
                continue
            if quote_status not in {"VALIDATED_ADAPTER_QUOTE","TEST_ONLY"}:
                results.append({
                    "symbol":symbol,"stable_identity":stable_identity,
                    "classification":"UNKNOWN","triggered":None,
                    "reason":"QUOTE_EDGE_STATUS_UNAVAILABLE",
                    "quote_edge_status":quote_status,
                })
                continue
            entry_triggered=evaluated.get("entry_triggered")
            invalidation_triggered=evaluated.get("invalidation_triggered")
            known=entry_triggered is not None or invalidation_triggered is not None
            triggered=bool(entry_triggered or invalidation_triggered) if known else None
            results.append({
                "symbol":symbol,"stable_identity":stable_identity,
                "classification":"FRESH" if known else "UNKNOWN",
                "triggered":triggered,
                "quote_edge_status":quote_status,
                "entry_edge":evaluated.get("entry_edge") or {},
                "invalidation_edge":evaluated.get("invalidation_edge") or {},
            })
        return {"status":"EVALUATED","vti_contract_covered":"VTI" in observations,
                "results":results,"private_positions_exported":False}


# ---------------- durable receipt consumer over existing CIOSessionHistory ----------------


class LocalPositionReceiptBridge:
    """Join caller-supplied local monitor contracts to existing trigger + receipt consumers.

    Private holdings remain caller-local. Only sanitized trigger classifications
    and receipt linkage outcomes leave this bridge. Pending replay uses the same
    CIOSessionHistory as DeliveryReceiptConsumer; no second ACK store exists.
    """
    def __init__(
        self,
        position_consumer: ReceiptAwarePositionConsumer,
        receipt_consumer: "DeliveryReceiptConsumer",
    ):
        self.position_consumer=position_consumer
        self.receipt_consumer=receipt_consumer
        self.history=receipt_consumer.history

    @staticmethod
    def _contract_observation(contract:Mapping[str,Any])->tuple[str,dict[str,Any]]:
        contract_id=str(contract.get("contract_id") or "").strip()
        condition_id=str(contract.get("condition_id") or "").strip()
        condition_version=str(contract.get("condition_version") or "").strip()
        observation=contract.get("observation")
        if not contract_id or not condition_id or not condition_version or not isinstance(observation,Mapping):
            raise ValueError("LOCAL_MONITOR_CONTRACT_INCOMPLETE")
        symbol=str(observation.get("symbol") or contract.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("LOCAL_MONITOR_SYMBOL_REQUIRED")
        semantic={
            "contract_id":contract_id,
            "condition_id":condition_id,
            "condition_version":condition_version,
            "symbol":symbol,
            "session_id":observation.get("session_id"),
            "official_material_ids":observation.get("official_material_ids") or [],
            "buy_zone":observation.get("buy_zone"),
            "invalidation_condition":observation.get("invalidation_condition"),
            "research_only":observation.get("research_only",False),
        }
        obs={
            **dict(observation),
            "symbol":symbol,
            "condition_id":condition_id,
            "condition_version":condition_version,
            "observation_identity":_stable_hash(semantic),
        }
        return contract_id,obs

    def evaluate_contracts(
        self,
        contracts:Iterable[Mapping[str,Any]],
        quotes:Mapping[str,Any],
        *,
        now:datetime,
        max_age_seconds:float=300,
    )->dict[str,Any]:
        rows=[]
        vti=False
        for contract in contracts:
            try:
                contract_id,observation=self._contract_observation(contract)
            except ValueError as exc:
                rows.append({"classification":"UNKNOWN","triggered":None,"reason":str(exc)})
                continue
            symbol=observation["symbol"]
            vti=vti or symbol=="VTI"
            evaluated=self.position_consumer.evaluate(
                {symbol:observation},quotes,now=now,max_age_seconds=max_age_seconds,allow_fixture=False
            )["results"][0]
            rows.append({
                "contract_id":contract_id,
                "symbol":symbol,
                "condition_id":observation["condition_id"],
                "condition_version":observation["condition_version"],
                "observation_identity":observation["observation_identity"],
                "stable_identity":evaluated["stable_identity"],
                "classification":evaluated["classification"],
                "triggered":evaluated["triggered"],
                **({"reason":evaluated["reason"]} if evaluated.get("reason") else {}),
                **({"quote_edge_status":evaluated["quote_edge_status"]} if evaluated.get("quote_edge_status") else {}),
            })
        return {
            "status":"EVALUATED",
            "vti_contract_covered":vti,
            "results":rows,
            "private_positions_exported":False,
        }

    @staticmethod
    def _expected_linkage_hash(expected:Mapping[str,Any])->str:
        missing=[k for k in DeliveryReceiptConsumer.REQUIRED_LINKS if not expected.get(k)]
        if missing:
            raise ValueError("PENDING_DELIVERY_LINKAGE_REQUIRED:"+",".join(sorted(missing)))
        return _stable_hash({k:expected.get(k) for k in DeliveryReceiptConsumer.REQUIRED_LINKS})

    def record_pending(self, *, contract_id:str, expected:Mapping[str,Any], observation_identity:str)->dict[str,Any]:
        linkage_hash=self._expected_linkage_hash(expected)
        prior=self.pending_replay()
        if any(row.get("linkage_hash")==linkage_hash for row in prior):
            return {"status":"PENDING_DUPLICATE","linkage_hash":linkage_hash}
        self.history.append({
            "kind":"PENDING_DELIVERY",
            "contract_id":contract_id,
            "observation_identity":observation_identity,
            "linkage_hash":linkage_hash,
            "expected":{k:expected.get(k) for k in DeliveryReceiptConsumer.REQUIRED_LINKS},
        })
        return {"status":"PENDING_RECORDED","linkage_hash":linkage_hash}

    def consume_receipt(self, expected:Mapping[str,Any], receipt:Optional[Mapping[str,Any]])->dict[str,Any]:
        return self.receipt_consumer.consume(expected,receipt)

    def pending_replay(self)->list[dict[str,Any]]:
        rows=self.history.history()
        acknowledged={
            _stable_hash({k:(row.get("linked") or {}).get(k) for k in DeliveryReceiptConsumer.REQUIRED_LINKS})
            for row in rows if row.get("kind")=="PLATFORM_ACK" and isinstance(row.get("linked"),Mapping)
        }
        pending={}
        for row in rows:
            if row.get("kind")!="PENDING_DELIVERY":
                continue
            linkage_hash=row.get("linkage_hash")
            if linkage_hash and linkage_hash not in acknowledged:
                pending[linkage_hash]={
                    "contract_id":row.get("contract_id"),
                    "observation_identity":row.get("observation_identity"),
                    "linkage_hash":linkage_hash,
                    "expected":row.get("expected"),
                }
        return list(pending.values())


class DeliveryReceiptConsumer:
    REQUIRED_LINKS=("execution_hash","body_hash","job_id","platform","target","thread_id")
    ACK_STATUSES={"ACKNOWLEDGED","DELIVERED_ACKNOWLEDGED"}

    def __init__(self, history: CIOSessionHistory):
        self.history=history

    def consume(self,expected:Mapping[str,Any],receipt:Optional[Mapping[str,Any]])->dict[str,Any]:
        if not receipt:
            return self._unknown("RECEIPT_MISSING")
        # Fixture evidence is never acknowledgement evidence. Reject it before
        # transport/linkage checks, identity construction, history reads or writes.
        if receipt.get("is_fixture") is True:
            return self._unknown("FIXTURE_RECEIPT_REJECTED")
        transport=str(receipt.get("transport_status") or "").upper()
        if transport not in self.ACK_STATUSES:
            return self._unknown("UNSUPPORTED_OR_FAILED_TRANSPORT_STATUS")
        if receipt.get("delivered") is not True:
            return self._unknown("CONTRADICTORY_DELIVERY_STATE")
        message_id=str(receipt.get("platform_message_id") or "").strip()
        if not message_id:
            return self._unknown("PLATFORM_MESSAGE_ID_MISSING")
        mismatches=[k for k in self.REQUIRED_LINKS if not receipt.get(k) or receipt.get(k)!=expected.get(k)]
        if mismatches:
            return self._unknown("RECEIPT_LINKAGE_MISMATCH:"+",".join(sorted(mismatches)))
        identity=_stable_hash({k:receipt.get(k) for k in (*self.REQUIRED_LINKS,"platform_message_id")})
        prior=[row for row in self.history.history() if row.get("kind")=="PLATFORM_ACK" and row.get("receipt_identity")==identity]
        if prior:
            return {"status":"ACKNOWLEDGED_DUPLICATE","acknowledged":True,"replay_permitted":False,
                    "receipt_identity":identity,"platform_message_id":message_id}
        self.history.append({"kind":"PLATFORM_ACK","receipt_identity":identity,
                             "platform_message_id":message_id,
                             "linked":{k:receipt.get(k) for k in self.REQUIRED_LINKS}})
        return {"status":"ACKNOWLEDGED","acknowledged":True,"replay_permitted":False,
                "receipt_identity":identity,"platform_message_id":message_id}

    @staticmethod
    def _unknown(reason:str)->dict[str,Any]:
        return {"status":"UNKNOWN","acknowledged":False,"replay_permitted":False,"reason":reason}
