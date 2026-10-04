"""Official facts -> authenticated daily CIO judgment -> trusted frozen packet.

This module is PAPER-only research formation, not an order generator. Source facts
are owned by OfficialResearchProducer; every price/valuation/thesis is explicitly
model judgment. No fixture, stale filing-index surrogate or caller-supplied model.
"""
from __future__ import annotations
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo

import requests
from pydantic import BaseModel, ConfigDict, Field, model_validator
from cio_market_lab.engine.cio_session import CIOSessionHistory
from cio_market_lab.engine.stage_d_observation import PersistedResearchPacketLoader
from cio_market_lab.research.official import OfficialResearchProducer


class DailyPlanJudgment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    thesis: str = Field(min_length=15, max_length=1600)
    valuation_scenarios: dict[str, Any]
    catalysts: list[str]
    buy_zone: dict[str, float] | None
    invalidation: str = Field(min_length=10)
    invalidation_condition: dict[str, Any] | None
    exposure_ceiling: float = Field(ge=0, le=0.02)
    stance: Literal['SCOUT_REVIEW', 'WAIT', 'REJECT']
    missing_evidence: list[str]
    review_trigger: str = Field(min_length=10)

    @model_validator(mode='after')
    def validate_numeric_rules(self):
        if self.buy_zone is None or self.invalidation_condition is None:
            if self.stance == 'SCOUT_REVIEW':
                raise ValueError('scout review requires numeric zone and failure condition')
            if not self.missing_evidence:
                raise ValueError('non-armed WAIT/REJECT must name missing evidence')
            return self
        lo, hi = self.buy_zone.get('low'), self.buy_zone.get('high')
        if lo is None or hi is None or not all(math.isfinite(x) and x > 0 for x in (lo, hi)) or lo > hi:
            raise ValueError('invalid positive ordered buy zone')
        c = self.invalidation_condition
        if c.get('field') != 'last_price' or c.get('operator') not in {'lt','lte','gt','gte'}:
            raise ValueError('explicit supported invalidation condition required')
        v = c.get('threshold')
        if isinstance(v, bool) or not isinstance(v, (int,float)) or not math.isfinite(v) or v <= 0:
            raise ValueError('invalid invalidation threshold')
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
    # Acquisition/refresh time and network metadata are not material evidence.
    semantic = {k: evidence.get(k) for k in ('symbol','market','source_url','published_at','verified_facts','title','raw_metadata')}
    return hashlib.sha256(json.dumps(semantic, sort_keys=True, default=str).encode()).hexdigest()


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
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    temp.replace(path)


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
        raw = parse_official_document(url, response.content, response.headers.get('Content-Type','')) if disclosure else response.json()
        digest = hashlib.sha256(response.content).hexdigest()
        if disclosure:
            wire = self.root / 'raw_official' / (digest + '.wire')
            if not wire.exists():
                wire.write_bytes(response.content)
        path = self.root / 'raw_official' / (digest + '.json')
        if not path.exists():
            atomic_json(path, {'source_url': url, 'observed_at': now.isoformat(), 'tls_verified': True,
                               'sha256_of_wire_bytes': digest, 'is_fixture': False, 'content': raw})
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
        evidence['excerpt'] = 'Canonical extracted official facts (not verbatim source quote):\n' + '\n'.join(evidence['verified_facts'])
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
        digest = semantic_research_digest(evidence)
        date = plan_session_date(symbol, now)
        key = f'{symbol}-{date}-{digest[:16]}-quote{int(reference_quote is not None)}'
        prior = self.root / 'authenticated_plans' / (key + '.json')
        if prior.exists() and (self.packet_root/(symbol+'.json')).exists():
            # Loader independently verifies source lineage and trusted receipt hash.
            packet = PersistedResearchPacketLoader(self.packet_root, trusted_manifest=self.manifest).load(symbol, now)
            if packet.get('research_digest') == digest and packet.get('plan_session_date') == date:
                return {'status':'CACHED_IMMUTABLE_PLAN', 'symbol':symbol, 'plan_path':str(prior), 'packet_path':str(self.packet_root/(symbol+'.json')), 'model_called':False}
        prompt = {'purpose':'DAILY_FROZEN_PAPER_RESEARCH_PLAN_NOT_ORDER', 'symbol':symbol, 'market':market,
                  'observed_at':now.isoformat(), 'market_session_date':date,
                  'official_evidence':evidence, 'reference_quote_for_valuation_only':reference_quote,
                  'position_ceiling_fraction':self.maximum_ceiling,
                  'prior_lessons':self.learning_store.retrieve_context_lessons(symbol=symbol, as_of=now, limit=5),
                  'matured_past_outcomes':self.learning_store.retrieve_past_outcomes(symbol=symbol, as_of=now, limit=5)}
        message = ('You are the authenticated Main CIO, forming the isolated PAPER daily frozen research plan. '
                   'Not a real broker order. Use ONLY supplied official facts. Valuation, thesis and numeric buy/invalidations '
                   'are explicitly uncertain model judgments, not source quotes. Identify missing evidence and do not invent '
                   'contracts, future catalysts, EPS, prices, growth or precise expected returns. No trade is required. '
                   'SCOUT_REVIEW requires commercial and financial support; missing essentials -> WAIT or REJECT. '
                   'Give independent fundamental/valuation reasoning, not VWAP/RSI ownership verdicts. '
                   'Exposure is a fraction of total isolated PAPER NAV, at most the supplied ceiling. '
                   'Supply explicit bear/base/bull assumptions, buy zone, failure condition and next source/price trigger. '
                   'When facts cannot support a numeric zone, use buy_zone=null and invalidation_condition=null, '
                   'exposure_ceiling=0 and name missing_evidence; do NOT invent a zone or zero-priced bounds. '
                   'Output pure concise JSON, no markdown, matching this schema:\n' + json.dumps(DailyPlanJudgment.model_json_schema()) +
                   '\nExact input:\n' + json.dumps(prompt, ensure_ascii=False, default=str))
        from cio_market_lab.integrations.hermes_chat import run_hermes_cli_chat
        from cio_market_lab.integrations.runtime_evidence import RuntimeEvidenceAdapter
        result = run_hermes_cli_chat(message, session_id=self.session_id, workspace_root=self.workspace_root,
                                    timeout_seconds=self.timeout_seconds, provider='openai-codex', model='gpt-6.1-sol', enforce_cio_pin=True)
        metadata = result.get('runtime_metadata') or {}
        response = result.get('response','')
        runtime = RuntimeEvidenceAdapter(pinned_provider='openai-codex', pinned_model='gpt-6.1-sol').verify_runtime_evidence(metadata=metadata, response_text=response,
                  exit_code=result.get('returncode',0), pinned_provider='openai-codex', pinned_model='gpt-6.1-sol', allow_fixture=False)
        if result.get('is_fixture') or runtime.is_fixture or not runtime.auth_verified or not runtime.is_success_response or result.get('failed') or result.get('error'):
            raise RuntimeError('unauthenticated/fixture/failed daily-plan receipt rejected')
        atomic_json(self.root/'authenticated_model_receipts'/(key+'.json'),
                    {'observed_at':self._now().isoformat(),'symbol':symbol,'runtime_metadata':metadata,
                     'response':response,'session_id':result.get('session_id'),'is_fixture':False,'purpose':'DAILY_RESEARCH_PLAN_NOT_ORDER'})
        plan = DailyPlanJudgment.model_validate_json(response)
        validate_plan_against_inputs(plan, evidence, maximum_ceiling=self.maximum_ceiling)
        actual_now = self._now()
        # A research formation crossing market-day boundaries must be retried, not misdated.
        if plan_session_date(symbol, actual_now) != date:
            raise RuntimeError('plan formation crossed market session date')
        local = actual_now.astimezone(ZoneInfo('Asia/Taipei' if market=='TW' else 'America/New_York'))
        formation_phase = 'PREOPEN' if (local.hour,local.minute) < ((9,0) if market=='TW' else (9,30)) else 'MID_OR_POST_SESSION_BOOTSTRAP'
        research_only = plan.buy_zone is None or plan.invalidation_condition is None or plan.exposure_ceiling == 0
        capture = self._evidence_capture(evidence)
        capture_text = capture.read_text()
        selectors = dict(evidence['raw_metadata'].get('raw_row') or {})
        if market == 'US':
            selectors = {'cik':evidence['raw_metadata']['cik']}
            for item in evidence['raw_metadata']['financial_baseline']:
                for field in ('val','end','form','accn'):
                    if item.get(field) is not None:
                        selectors[f'{item["concept"]}:{field}'] = (field,item[field])
        excerpts = []
        for field,value in selectors.items():
            if isinstance(value,tuple): field,value = value
            text = json.dumps({field:value},ensure_ascii=False)[1:-1]
            if text not in capture_text:
                raise RuntimeError('derived official fact missing from exact raw capture: '+field)
            if text not in excerpts: excerpts.append(text)
        packet = {**evidence, **plan.model_dump(mode='json'), 'observed_at':actual_now.isoformat(),
                  'source_excerpts':excerpts, 'research_only':research_only,
                  'verified':True, 'verification_scope':'OFFICIAL_FACTS_ONLY_THESIS_AND_VALUATION_ARE_MAIN_CIO_JUDGMENT',
                  'verified_facts':evidence['verified_facts'], 'is_fixture':False, 'research_digest':digest,
                  'plan_session_date':date, 'formation_phase':formation_phase, 'raw_excerpt':evidence.get('excerpt',''),
                  'official_baseline_path':str(self.root/'official_baselines'/(symbol+'.json'))}
        packet_hash = hashlib.sha256(json.dumps(packet,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        receipt = {'observed_at':actual_now.isoformat(),'symbol':symbol,'plan_session_date':date,'formation_phase':formation_phase,
                   'runtime_metadata':metadata,'resolved_session_id':result.get('session_id'), 'packet_sha256':packet_hash,
                   'model_called':True,'is_fixture':False,'plan':plan.model_dump(mode='json'),
                   'input_sha256':hashlib.sha256(json.dumps(prompt,sort_keys=True,default=str).encode()).hexdigest()}
        atomic_json(prior,receipt)
        manifest = json.loads(self.manifest.read_text()) if self.manifest.exists() else {'approved_packets':{}}
        manifest.setdefault('approved_packets',{})[packet_hash] = {
            'symbol':symbol,'approved_by':'MAIN_CIO','source_verified':True,'source_url':evidence['source_url'],
            'capture_file':str(capture.relative_to(self.root)),
            'capture_sha256':hashlib.sha256(capture.read_bytes()).hexdigest(),
            'receipt_path':str(prior),'observed_at':actual_now.isoformat()}
        # Publish packet after its approval. Atomic replacements never expose partial JSON.
        atomic_json(self.manifest,manifest)
        atomic_json(self.packet_root/(symbol+'.json'),packet)
        CIOSessionHistory(self.root/'formation_history',self.session_id).append({**receipt,'kind':'DAILY_OFFICIAL_RESEARCH_PLAN_FORMED'})
        return {'status':'AUTHENTICATED_RESEARCH_ONLY_PLAN' if research_only else 'AUTHENTICATED_PLAN_FORMED','symbol':symbol,'formation_phase':formation_phase,
                'plan_path':str(prior),'packet_path':str(self.packet_root/(symbol+'.json')),'model_called':True}
