"""Bounded TAIFEX contract quote intake; never a broker or trading authority.

CDate/CTime are kept literally. HTTP receipt time cannot make a stale last
trade into a fresh book. After-midnight session-date interpretation remains
unverified and is NOT repaired by guessing a rollover or a current price.
Contract rules, settlement, expiry, fees and CIO approval are separate seams.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from cio_market_lab.engine.paper_derivatives import ContractSpec, DerivativeQuote, DerivativeInstrumentType

BASE='https://mis.taifex.com.tw/futures/api/'
ENDPOINTS={'TXF':'getQuoteList','TXO':'getQuoteListOption'}
TAIPEI=ZoneInfo('Asia/Taipei')
MAX_BODY_BYTES=8*1024*1024


def _price(value: Any) -> float | None:
    if value is None or str(value).strip()=='': return None
    n=float(str(value).replace(',',''))
    if not math.isfinite(n) or n<0: raise ValueError('INVALID_PRICE')
    return n


def _source_url(url: str) -> None:
    p=urlsplit(url)
    if p.scheme!='https' or p.hostname!='mis.taifex.com.tw' or p.path not in {
        '/futures/api/getQuoteList','/futures/api/getQuoteListOption'} or p.port not in (None,443) or p.username or p.password:
        raise ValueError('UNTRUSTED_SOURCE_URL')


def decode_quote_snapshot(response: dict, *, source_url: str, market_type: str,
                          observed_at: datetime, is_fixture: bool=False,
                          response_sha256: str | None=None) -> dict:
    _source_url(source_url)
    if observed_at.tzinfo is None: raise ValueError('OBSERVED_AT_TIMEZONE_REQUIRED')
    if market_type not in ('0','1'): raise ValueError('UNSUPPORTED_MARKET_TYPE')
    if str(response.get('RtCode'))!='0' or not isinstance(response.get('RtData'),dict):
        raise ValueError('TAIFEX_SOURCE_RESPONSE_ERROR')
    rows=response['RtData'].get('QuoteList')
    if not isinstance(rows,list) or len(rows)>5000: raise ValueError('TAIFEX_SOURCE_RESPONSE_ROWS_INVALID')
    sha=response_sha256 or hashlib.sha256(json.dumps(response,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    quotes=[];rejections=[];seen=set()
    for index,row in enumerate(rows):
        symbol=row.get('SymbolID','') if isinstance(row,dict) else ''
        try:
            if not symbol or not isinstance(symbol,str): raise ValueError('CONTRACT_SYMBOL_REQUIRED')
            if symbol in seen: raise ValueError('DUPLICATE_SYMBOL')
            seen.add(symbol)
            if row.get('Status') not in ('',None): raise ValueError('CONTRACT_STATUS_NOT_CONTINUOUS')
            rawdate=str(row.get('CDate',''));rawtime=str(row.get('CTime',''))
            if len(rawdate)!=8 or len(rawtime)!=6 or not (rawdate+rawtime).isdigit():
                raise ValueError('EXCHANGE_TIMESTAMP_REQUIRED')
            ts=datetime.strptime(rawdate+rawtime,'%Y%m%d%H%M%S').replace(tzinfo=TAIPEI)
            bid=_price(row.get('CBidPrice1'));ask=_price(row.get('CAskPrice1'))
            if bid is None or ask is None or bid<=0 or ask<=0 or bid>ask:
                raise ValueError('NO_EXECUTABLE_TWO_SIDED_BOOK')
            bid_size=_price(row.get('CBidSize1'));ask_size=_price(row.get('CAskSize1'))
            if not bid_size or not ask_size: raise ValueError('NO_EXECUTABLE_DISPLAYED_SIZE')
            last=_price(row.get('CLastPrice'))
            if last==0: last=None
            age=(observed_at-ts).total_seconds()
            quote=DerivativeQuote(symbol=symbol,timestamp=ts,observed_at=observed_at,
                bid=bid,ask=ask,last_price=last,is_stale=age < -5 or age>300,
                is_fixture=is_fixture,source='taifex_mis_contract_book',provenance={
                'authority':'TAIFEX' if not is_fixture else 'EXPLICIT_OFFLINE_FIXTURE',
                'source_url':source_url,'source_tier':'official_exchange','market_type':market_type,
                'response_sha256':sha,'raw_exchange_date':rawdate,'raw_exchange_time':rawtime,
                'timestamp_rule':'RAW_CDATE_CTIME_ASIA_TAIPEI_NO_INFERRED_ROLLOVER',
                'timestamp_semantics':'LAST_TRADE_NOT_CERTIFIED_BOOK_UPDATE',
                'bid_size':bid_size,'ask_size':ask_size,'contract_right':row.get('CP'),
                'strike':row.get('StrikePrice'),'raw_status':row.get('Status'),
                'observed_at':observed_at.isoformat(),'row_index':index})
            quotes.append(quote.model_dump(mode='json'))
        except (ValueError,TypeError,OverflowError) as exc:
            rejections.append({'row_index':index,'symbol':symbol,'reason':str(exc)[:180]})
    return {'schema_version':1,'status':'SOURCE_SNAPSHOT_PARSED','observed_at':observed_at.isoformat(),
        'source_url':source_url,'source_tier':'official_exchange','market_type':market_type,
        'response_sha256':sha,'raw_row_count':len(rows),'quote_count':len(quotes),
        'fresh_quote_count':sum(not q['is_stale'] for q in quotes),
        'quotes':quotes,'rejections':rejections,'is_fixture':is_fixture,
        'paper_only':True,'broker_connected':False,'execution_enabled':False,
        'scope':'CONTRACT_QUOTE_INTAKE_ONLY_NOT_DERIVATIVE_ACTIVATION',
        'remaining_gaps':['CERTIFIED_BOOK_TIMESTAMP_SEMANTICS','EXPIRY_AND_CONTRACT_REGISTRY',
            'SOURCE_ALIGNED_FEES_MARGIN_SETTLEMENT','MAIN_CIO_TRADE_APPROVAL','FULL_DERIVATIVE_LIFECYCLE_ACCEPTANCE']}


def bind_test_only_quote_to_contract(quote_data: dict, contract_spec: dict, *, as_of: datetime) -> DerivativeQuote:
    """Bind one decoded TEST_ONLY TAIFEX contract quote to an explicit registry spec.

    This is fixture-only source/caller glue. It validates exact target metadata and
    never upgrades the quote or contract to production/live authorization.
    """
    quote=DerivativeQuote.model_validate(quote_data)
    spec=ContractSpec.model_validate(contract_spec)
    if as_of.tzinfo is None:
        raise ValueError('AS_OF_TIMEZONE_REQUIRED')
    if not quote.is_fixture:
        raise ValueError('TEST_ONLY_QUOTE_REQUIRED')
    if spec.expiry is None or spec.expiry.tzinfo is None or as_of>=spec.expiry:
        raise ValueError('CONTRACT_EXPIRED_OR_EXPIRY_UNAVAILABLE')
    if quote.symbol!=spec.symbol:
        raise ValueError('CONTRACT_TARGET_MISMATCH')
    meta=dict(quote.provenance or {})
    if spec.instrument_type==DerivativeInstrumentType.OPTION:
        raw_strike=meta.get('strike')
        try:
            strike=float(raw_strike)
        except (TypeError,ValueError,OverflowError):
            raise ValueError('CONTRACT_TARGET_MISMATCH')
        if spec.strike is None or not math.isfinite(strike) or strike!=float(spec.strike):
            raise ValueError('CONTRACT_TARGET_MISMATCH')
        raw_right=str(meta.get('contract_right') or '').strip().upper()
        expected=str(spec.option_right.value).upper() if spec.option_right else ''
        aliases={'CALL':{'CALL','C'},'PUT':{'PUT','P'}}
        if expected not in aliases or raw_right not in aliases[expected]:
            raise ValueError('CONTRACT_TARGET_MISMATCH')
    meta.update({
        'contract_authorized':True,
        'session_open':True,
        'expiry':spec.expiry.isoformat(),
        'multiplier':spec.multiplier,
        'tick_size':spec.tick_size,
        'currency':spec.currency,
        'initial_margin_per_contract':spec.initial_margin_per_contract,
        'maintenance_margin_per_contract':spec.maintenance_margin_per_contract,
        'registry_binding':'EXPLICIT_TEST_ONLY_CONTRACT_SPEC',
    })
    if spec.option_right is not None:
        meta['contract_right']=spec.option_right.value
    if spec.strike is not None:
        meta['strike']=spec.strike
    return quote.model_copy(update={'provenance':meta})


class TaifexQuoteAdapter:
    def __init__(self, *, timeout_seconds: float=12):
        if not 0<timeout_seconds<=30: raise ValueError('BOUNDED_TIMEOUT_REQUIRED')
        self.timeout_seconds=timeout_seconds

    def fetch(self, product: str, *, market_type: str='1', expiry_month: str='') -> dict:
        if product not in ENDPOINTS: raise ValueError('UNSUPPORTED_TAIFEX_PRODUCT')
        if market_type not in ('0','1'): raise ValueError('UNSUPPORTED_MARKET_TYPE')
        selection_authority = 'EXPLICIT_QUERY_MONTH_NOT_CERTIFIED_EXPIRY'
        if product == 'TXO' and not expiry_month:
            # This selects a bounded query only. Returned exchange rows, not
            # this calendar hint, are the authority for observed contracts.
            expiry_month = datetime.now(TAIPEI).strftime('%Y%m')
            selection_authority = 'CALENDAR_QUERY_HINT_NOT_CERTIFIED_EXPIRY'
        if expiry_month:
            if len(expiry_month) != 6 or not expiry_month.isdigit():
                raise ValueError('EXPIRY_MONTH_FORMAT')
            try:
                datetime.strptime(expiry_month, '%Y%m')
            except ValueError as exc:
                raise ValueError('EXPIRY_MONTH_FORMAT') from exc
        endpoint=ENDPOINTS[product];url=BASE+endpoint
        payload={'MarketType':market_type,'SymbolType':'O' if product=='TXO' else 'F',
            'KindID':'1','CID':product,'ExpireMonth':expiry_month,'RowSize':'100',
            'PageNo':'1','SortColumn':'','AscDesc':'A'}
        req=Request(url,data=json.dumps(payload).encode(),headers={
            'User-Agent':'Mozilla/5.0','Content-Type':'application/json','Referer':'https://mis.taifex.com.tw/futures/'})
        with urlopen(req,timeout=self.timeout_seconds) as response:
            _source_url(response.geturl())
            raw=response.read(MAX_BODY_BYTES+1)
        if len(raw)>MAX_BODY_BYTES: raise ValueError('SOURCE_RESPONSE_TOO_LARGE')
        now=datetime.now(timezone.utc)
        result=decode_quote_snapshot(json.loads(raw),source_url=url,market_type=market_type,
            observed_at=now,response_sha256=hashlib.sha256(raw).hexdigest())
        result['request_payload']=payload
        result['expiry_selection_authority']=selection_authority
        result['coverage_scope']='ONE_EXCHANGE_RESPONSE_NO_COMPLETE_CHAIN_CLAIM'
        return result

    def refresh_inventory(self, output_path: Path, *, market_type: str='1', expiry_month: str='') -> dict:
        snapshots=[];failures=[]
        for product in ('TXF','TXO'):
            try:snapshots.append({'product':product,**self.fetch(product,market_type=market_type,
                expiry_month=expiry_month if product=='TXO' else '')})
            except Exception as exc:failures.append({'product':product,'reason':type(exc).__name__+': '+str(exc)[:400]})
        result={'schema_version':1,'status':'SOURCE_INVENTORY_OBSERVED' if snapshots else 'SOURCE_FAILED_CLOSED',
            'observed_at':datetime.now(timezone.utc).isoformat(),'snapshots':snapshots,'failures':failures,
            'quote_count':sum(s['quote_count'] for s in snapshots),
            'fresh_quote_count':sum(s['fresh_quote_count'] for s in snapshots),
            'paper_only':True,'broker_connected':False,'execution_enabled':False,
            'scope':'CONTRACT_QUOTE_INTAKE_ONLY_NOT_DERIVATIVE_ACTIVATION'}
        output_path=Path(output_path);output_path.parent.mkdir(parents=True,exist_ok=True)
        temp=output_path.with_suffix('.tmp');temp.write_text(json.dumps(result,ensure_ascii=False,indent=2));temp.replace(output_path)
        return result


def load_quote_inventory(path: Path, *, now: datetime | None=None) -> dict:
    now=now or datetime.now(timezone.utc)
    try:result=json.loads(Path(path).read_text())
    except (OSError,ValueError):
        return {'status':'SOURCE_NOT_OBSERVED','execution_enabled':False,'paper_only':True,'broker_connected':False,'quotes':[]}
    result['execution_enabled']=False;result['paper_only']=True;result['broker_connected']=False
    result['readback_at']=now.isoformat()
    snapshots=result.get('snapshots',[result])
    for snapshot in snapshots:
        for quote in snapshot.get('quotes',[]):
            try:
                timestamp=datetime.fromisoformat(quote['timestamp'].replace('Z','+00:00'))
                age=(now-timestamp).total_seconds()
                quote['is_stale']=bool(quote.get('is_stale')) or age < -5 or age>300
            except (ValueError,TypeError,KeyError):quote['is_stale']=True
        snapshot['fresh_quote_count']=sum(not q['is_stale'] for q in snapshot.get('quotes',[]))
    result['fresh_quote_count']=sum(s['fresh_quote_count'] for s in snapshots)
    return result
