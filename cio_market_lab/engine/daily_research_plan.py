"""Official facts -> bounded, deduplicated script-only PAPER research request.

Source facts are owned by OfficialResearchProducer. New formation deliberately
does not invoke any model or publish authenticated CIO approvals. Existing trusted
packets are read-only cache inputs, not permission to restart paid inference.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo

import requests
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from cio_market_lab.engine.stage_d_observation import PersistedResearchPacketLoader
from cio_market_lab.research.official import OfficialResearchProducer


class BuyZone(BaseModel):
    model_config = ConfigDict(extra='forbid')
    low: float = Field(description='Lower positive finite price bound for the PAPER review zone.')
    high: float = Field(description='Upper positive finite price bound for the PAPER review zone.')

    @model_validator(mode='after')
    def validate_bounds(self):
        if not all(math.isfinite(x) and x > 0 for x in (self.low, self.high)) or self.low > self.high:
            raise ValueError('invalid positive ordered buy zone')
        return self


class PriceInvalidationCondition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field: Literal['last_price'] = Field(description='Only supported numeric execution field.')
    operator: Literal['lt','lte','gt','gte'] = Field(description='Supported last-price comparison operator.')
    threshold: float = Field(description='Positive finite last-price threshold.')

    @field_validator('threshold', mode='before')
    @classmethod
    def reject_non_numeric_raw_threshold(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError('invalid invalidation threshold type')
        return value

    @model_validator(mode='after')
    def validate_threshold(self):
        if not math.isfinite(self.threshold) or self.threshold <= 0:
            raise ValueError('invalid invalidation threshold')
        return self


class DailyPlanJudgment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    thesis: str = Field(min_length=15, max_length=1600)
    valuation_scenarios: dict[str, Any]
    catalysts: list[str]
    buy_zone: BuyZone | None = Field(
        description='Use null for WAIT/REJECT when evidence cannot support numeric bounds; SCOUT_REVIEW requires {low, high}.'
    )
    invalidation: str = Field(
        min_length=10,
        description='Fundamental invalidation prose. Do not encode this prose as the numeric execution condition.'
    )
    invalidation_condition: PriceInvalidationCondition | None = Field(
        description='Use null for WAIT/REJECT when no supported numeric last-price condition is justified.'
    )
    exposure_ceiling: float = Field(ge=0, le=0.02)
    stance: Literal['SCOUT_REVIEW', 'WAIT', 'REJECT']
    missing_evidence: list[str]
    review_trigger: str = Field(min_length=10)

    @model_validator(mode='after')
    def validate_numeric_rules(self):
        if self.buy_zone is None or self.invalidation_condition is None:
            if self.stance == 'SCOUT_REVIEW':
                raise ValueError('scout review requires numeric zone and failure condition')
            if self.exposure_ceiling != 0:
                raise ValueError('non-armed WAIT/REJECT requires zero exposure')
            if not self.missing_evidence:
                raise ValueError('non-armed WAIT/REJECT must name missing evidence')
            return self
        if not {'bear','base','bull'} <= set(self.valuation_scenarios):
            raise ValueError('three named valuation scenarios required')
        return self


def is_research_review_formation(result: dict[str, Any]) -> bool:
    """Exact authenticated producer contract, not an unverified stance label.

    Cached plans still undergo the runner's verified research_only/context gate.
    Armed plans belong exclusively to the session-valid execution path.
    """
    return result.get('status') in {'AUTHENTICATED_RESEARCH_ONLY_PLAN', 'CACHED_IMMUTABLE_PLAN'}


def plan_session_date(symbol: str, now: datetime) -> str:
    return now.astimezone(ZoneInfo('Asia/Taipei' if symbol.upper().endswith(('.TW','.TWO')) else 'America/New_York')).date().isoformat()


def semantic_research_digest(evidence: dict[str, Any]) -> str:
    from .paper_research_trigger import semantic_evidence_identity
    return semantic_evidence_identity(evidence)[1]


def normalize_reference_quote(reference_quote: dict[str, Any] | None, *, now: datetime) -> dict[str, Any] | None:
    if reference_quote is None:
        return None
    if not isinstance(reference_quote, dict):
        raise ValueError('reference quote must be a mapping')
    quote=dict(reference_quote)
    quote.setdefault('observed_at', now.astimezone(timezone.utc).isoformat())
    quote['timestamp_semantics']={
        'source_timestamp_or_date':'market/source time identifies the trading observation; date-only official EOD data does not imply an intraday timestamp',
        'observed_at':'collector observation time; it does not replace or advance the source trading date',
    }
    return quote


def semantic_reference_quote_digest(reference_quote: dict[str, Any] | None) -> str:
    if reference_quote is None:
        return 'none'
    semantic={k: reference_quote.get(k) for k in (
        'symbol','price','close','last_price','source','timestamp','source_timestamp','source_date','date'
    )}
    return hashlib.sha256(json.dumps(semantic,sort_keys=True,default=str).encode()).hexdigest()


def validate_plan_against_inputs(plan: DailyPlanJudgment, evidence: dict[str, Any], *, maximum_ceiling: float):
    if not evidence.get('verified_facts'):
        raise ValueError('no verified official facts')
    meta = evidence.get('raw_metadata') or {}
    if str(evidence.get('market','')).upper() == 'TW':
        if not meta.get('raw_row'):
            raise ValueError('TW current official monthly revenue row missing')
    elif not meta.get('financial_baseline'):
        raise ValueError('SEC filing index alone is not operating financial baseline')
    if plan.exposure_ceiling > maximum_ceiling:
        raise ValueError('plan exceeds independently supplied PAPER position ceiling')


def atomic_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '-')
    temp = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


class _OfficialCollector:
    def __init__(self, reader):
        self.rows, self.reader = [], reader
    def add_evidence(self, row, now=None):
        ok, reason = self.reader.add_evidence(row, now=now)
        if ok: self.rows.append(row)
        return ok, reason


class DailyResearchPlanProducer:
    def __init__(self, *, root: Path, packet_root: Path, session_id: str, workspace_root: str,
                 learning_store: Any, reader: Any = None, maximum_ceiling: float = 0.02, timeout_seconds: int = 240,
                 now_fn: Callable[[], datetime] | None = None):
        self.root, self.packet_root = Path(root), Path(packet_root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'raw_official').mkdir(parents=True, exist_ok=True)
        self.packet_root.mkdir(parents=True, exist_ok=True)
        self.manifest = self.root / 'trusted_main_cio_approvals.json'
        self.session_id, self.workspace_root = session_id, workspace_root
        self.learning_store, self.maximum_ceiling = learning_store, maximum_ceiling
        self.timeout_seconds = timeout_seconds
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.network = requests.Session()
        self.captures_by_url = {}
        from cio_market_lab.research.browser import PublicResearchInboxReader
        self.collector = _OfficialCollector(reader or PublicResearchInboxReader(self.root/'official_intake'))
        self.official = OfficialResearchProducer(fetch_json=self._capture_official)

    def _now(self) -> datetime:
        value = self._now_fn()
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def _capture_official(self, url):
        now = self._now()
        from cio_market_lab.research.official import USER_AGENT
        from urllib.parse import urlparse
        from cio_market_lab.research.official_documents import ALLOWED_HOSTS, parse_official_document, MAX_BYTES
        disclosure = urlparse(url).hostname in ALLOWED_HOSTS
        headers={'User-Agent': 'Googlebot' if urlparse(url).hostname == 'investor.tsmc.com' else USER_AGENT,
                 'Accept':'application/json,text/html,application/pdf'}
        # Same-URL bounded retry; no access/rate-limit bypass or alternate facts.
        for attempt in range(2):
            try:
                response = self.network.get(url, headers=headers, timeout=(8,25))
                if response.status_code in {502, 503, 504} and attempt == 0:
                    continue
                response.raise_for_status()
                break
            except (requests.Timeout, requests.ConnectionError):
                if attempt:
                    raise
        if len(response.content) > MAX_BYTES:
            raise ValueError('OFFICIAL_DOCUMENT_OVERSIZE')
        digest = hashlib.sha256(response.content).hexdigest()
        schema = 'official-document-enriched-v1' if disclosure else 'official-json-v1'
        capture_identity = hashlib.sha256(
            json.dumps({'source_url':url,'wire_sha256':digest,'schema':schema},sort_keys=True).encode()
        ).hexdigest()
        path = self.root / 'raw_official' / (capture_identity + '.json')
        if path.exists():
            persisted=json.loads(path.read_text())
            if (
                persisted.get('source_url') != url
                or persisted.get('sha256_of_wire_bytes') != digest
                or persisted.get('capture_schema') != schema
                or persisted.get('is_fixture')
                or persisted.get('tls_verified') is not True
            ):
                raise RuntimeError('OFFICIAL_CAPTURE_IDENTITY_CONFLICT')
            self.captures_by_url[url]=path
            return persisted.get('content')

        raw = parse_official_document(url, response.content, response.headers.get('Content-Type','')) if disclosure else response.json()
        if disclosure and isinstance(raw,list):
            enriched=[]
            for row in raw:
                current=dict(row)
                current.update({
                    'source_url':url,
                    'document_sha256':digest,
                    'observed_at':now.isoformat(),
                    'body_provenance':'EXTRACTED_FROM_CAPTURED_WIRE_BYTES',
                })
                enriched.append(current)
            raw=enriched
        if disclosure:
            # Preserve the established content-addressed wire path for exact-byte
            # lineage/readers. JSON capture identity is stricter (URL+wire+schema).
            wire = self.root / 'raw_official' / (digest + '.wire')
            if not wire.exists():
                wire.write_bytes(response.content)
        capture={'source_url': url, 'observed_at': now.isoformat(), 'tls_verified': True,
                 'sha256_of_wire_bytes': digest, 'capture_schema':schema,
                 'is_fixture': False, 'content': raw}
        atomic_json(path, capture)
        # Backward-compatible content-addressed snapshot for existing audit
        # readers. It is immutable and never used as the canonical identity;
        # captures_by_url always points at URL+wire+schema identity above.
        legacy_path=self.root/'raw_official'/(digest+'.json')
        if not legacy_path.exists():
            atomic_json(legacy_path, {**capture,'canonical_capture_identity':capture_identity})
        self.captures_by_url[url] = path
        return raw

    def _evidence_capture(self, evidence):
        primary = self.captures_by_url.get(evidence['source_url'])
        if primary is None or not primary.is_file():
            raise RuntimeError('exact official raw capture lineage unavailable')
        supplements = evidence.get('raw_metadata', {}).get('supplemental_source_rows', [])
        if not supplements:
            return primary
        bundle = json.loads(primary.read_text())
        bundle['supplemental_official_captures'] = []
        for supplemental in supplements:
            path = self.captures_by_url.get(supplemental['source_url'])
            if path is None or not path.is_file():
                raise RuntimeError('supplemental source capture unavailable')
            document = json.loads(path.read_text())
            if document.get('source_url') != supplemental['source_url'] or document.get('is_fixture') or document.get('tls_verified') is not True:
                raise RuntimeError('supplemental capture provenance rejected')
            if supplemental['raw_row'] not in document.get('content', []):
                raise RuntimeError('supplemental source row/capture mismatch')
            bundle['supplemental_official_captures'].append(document)
        digest = hashlib.sha256(json.dumps(bundle, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = self.root/'raw_official'/('bundle-'+digest+'.json')
        if not path.exists():
            atomic_json(path, bundle)
        return path

    def refresh(self, symbol: str, *, now: datetime, reference_quote: dict[str, Any] | None = None):
        market = 'TW' if symbol.upper().endswith(('.TW','.TWO')) else 'US'
        self.collector.rows.clear()
        intake = self.official.acquire([symbol], self.collector, now)
        candidates = [row for row in self.collector.rows if row.get('symbol') == symbol]
        if not candidates:
            raise RuntimeError('official source intake unavailable: ' + str(intake))
        evidence = dict(candidates[-1])
        evidence['market'] = market
        # Validate/summarize before disk writes or old caches. Do not rebuild the
        # huge duplicated excerpt; the original evidence remains read-only input.
        semantic_research_digest(evidence)
        # Validate official operating inputs before cache/inference/publication.
        # A filing index is discovery only, never an operating financial baseline.
        if not evidence.get('verified_facts'):
            raise ValueError('no verified official facts')
        meta = evidence.get('raw_metadata') or {}
        if market == 'US' and not meta.get('financial_baseline'):
            raise ValueError('SEC filing index alone is not operating financial baseline')
        if market == 'TW' and not meta.get('raw_row'):
            raise ValueError('TW current official monthly revenue row missing')
        atomic_json(self.root / 'official_baselines' / (symbol + '.json'), evidence)
        blocked_documents=list(meta.get('blocked_official_documents') or [])
        if blocked_documents:
            # Keep the independently verified intake on disk, but never promote
            # a research plan while a required linked official statement is blocked.
            blocked_urls=','.join(str(row.get('source_url') or '') for row in blocked_documents)
            raise RuntimeError('OFFICIAL_DISCLOSURE_DOCUMENT_BLOCKED:'+blocked_urls)
        digest = semantic_research_digest(evidence)
        quote_context=normalize_reference_quote(reference_quote,now=now)
        quote_digest=semantic_reference_quote_digest(quote_context)
        date = plan_session_date(symbol, now)
        key = f'{symbol}-{date}-{digest[:16]}-quote-{quote_digest[:16]}'
        prior = self.root / 'authenticated_plans' / (key + '.json')
        if prior.exists() and (self.packet_root/(symbol+'.json')).exists():
            # Loader independently verifies source lineage and trusted receipt hash.
            packet = PersistedResearchPacketLoader(self.packet_root, trusted_manifest=self.manifest).load(symbol, now)
            if packet.get('research_digest') == digest and packet.get('plan_session_date') == date:
                return {'status':'CACHED_IMMUTABLE_PLAN', 'symbol':symbol, 'plan_path':str(prior), 'packet_path':str(self.packet_root/(symbol+'.json')), 'model_called':False}
        from .paper_research_trigger import admit_script_request
        return admit_script_request(
            self.root, symbol=symbol, evidence=evidence, quote=quote_context,
            lessons=lambda: self.learning_store.retrieve_context_lessons(symbol=symbol, as_of=now, limit=5),
            outcomes=lambda: self.learning_store.retrieve_past_outcomes(symbol=symbol, as_of=now, limit=5),
            ceiling=self.maximum_ceiling,
        )
