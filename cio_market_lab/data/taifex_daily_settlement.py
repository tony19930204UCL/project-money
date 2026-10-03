"""Official daily settlement intake, never BBO or final-expiry evidence.

The endpoint provides a DATE, not a certified book/publication timestamp. Keep
that fact literal. Only earlier-date regular-session monthly outright contracts
are accepted; do not infer expiry, a closing timestamp, fees or contract specs.
"""
from __future__ import annotations
from datetime import date, datetime
import hashlib, json, math, re
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field

SOURCE_URL='https://openapi.taifex.com.tw/v1/DailyMarketReportFut'
TAIPEI=ZoneInfo('Asia/Taipei')
MAX_BYTES=8*1024*1024
KIND='DAILY_SETTLEMENT_NOT_FINAL_EXPIRY'

class DailyFuturesSettlementEvidence(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    exchange_contract: str
    contract_month: str
    settlement_date: date
    settlement_price: float=Field(gt=0)
    observed_at: datetime
    is_fixture: bool
    source_url: str
    provenance: dict

    def validate_capture(self) -> None:
        if self.is_fixture: return
        raw_path=self.provenance.get('capture_path')
        if not raw_path: raise ValueError('DAILY_SETTLEMENT_CAPTURE_REQUIRED')
        path=Path(raw_path).resolve()
        try: path.relative_to(Path(__file__).resolve().parents[2]/'artifacts')
        except ValueError: raise ValueError('DAILY_SETTLEMENT_CAPTURE_SCOPE_INVALID')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=self.provenance['response_sha256']:
            raise ValueError('DAILY_SETTLEMENT_CAPTURE_HASH_MISMATCH')
        rows=json.loads(raw);index=self.provenance.get('row_index')
        if not isinstance(index,int) or isinstance(index,bool) or not 0<=index<len(rows):
            raise ValueError('DAILY_SETTLEMENT_CAPTURE_ROW_INVALID')
        row=rows[index]
        if (row.get('Contract')!=self.exchange_contract or row.get('ContractMonth(Week)')!=self.contract_month or
            row.get('Date')!=self.settlement_date.strftime('%Y%m%d') or
            row.get('TradingSession')!='一般' or row.get('TradingHalt') not in ('',None) or
            float(row.get('SettlementPrice'))!=self.settlement_price):
            raise ValueError('DAILY_SETTLEMENT_CAPTURE_ROW_MISMATCH')

    def validate_source(self, now: datetime) -> None:
        if (now.tzinfo is None or self.observed_at.tzinfo is None or
            self.observed_at>now or self.settlement_date>=self.observed_at.astimezone(TAIPEI).date() or
            self.source_url!=SOURCE_URL or not re.fullmatch(r'[A-Z0-9]{1,12}',self.exchange_contract) or
            not re.fullmatch(r'\d{6}',self.contract_month) or
            self.provenance.get('kind')!=KIND or
            self.provenance.get('authority')!=('EXPLICIT_OFFLINE_FIXTURE' if self.is_fixture else 'TAIFEX') or
            self.provenance.get('trading_session')!='一般' or
            not re.fullmatch(r'[a-f0-9]{64}',str(self.provenance.get('response_sha256','')))):
            raise ValueError('DAILY_SETTLEMENT_SOURCE_UNAVAILABLE')
        datetime.strptime(self.contract_month,'%Y%m')


def decode_daily_futures_settlements(rows: list, *, source_url: str,
        observed_at: datetime, response_sha256: str, is_fixture: bool=False,
        capture_path: Path | None=None) -> dict:
    if source_url!=SOURCE_URL or observed_at.tzinfo is None:
        raise ValueError('UNTRUSTED_SOURCE_OR_CLOCK')
    if not re.fullmatch(r'[a-f0-9]{64}',response_sha256): raise ValueError('SOURCE_DIGEST_REQUIRED')
    if not isinstance(rows,list) or len(rows)>20000: raise ValueError('SOURCE_ROWS_INVALID')
    accepted={};duplicates=set();rejections=[]
    for index,row in enumerate(rows):
        key=None
        try:
            if not isinstance(row,dict): raise ValueError('ROW_INVALID')
            contract=row.get('Contract');month=row.get('ContractMonth(Week)');rawdate=row.get('Date')
            if not isinstance(rawdate,str) or not re.fullmatch(r'\d{8}',rawdate): raise ValueError('DATE_INVALID')
            day=datetime.strptime(rawdate,'%Y%m%d').date()
            if not isinstance(month,str) or not re.fullmatch(r'\d{6}',month): raise ValueError('MONTHLY_OUTRIGHT_ONLY')
            datetime.strptime(month,'%Y%m')
            if row.get('TradingSession')!='一般' or row.get('TradingHalt') not in ('',None): raise ValueError('REGULAR_UNHALTED_SESSION_REQUIRED')
            value=row.get('SettlementPrice');price=float(value)
            if not math.isfinite(price) or price<=0: raise ValueError('PRICE_INVALID')
            proof=DailyFuturesSettlementEvidence(exchange_contract=contract,contract_month=month,
                settlement_date=day,settlement_price=price,observed_at=observed_at,is_fixture=is_fixture,
                source_url=source_url,provenance={'authority':'EXPLICIT_OFFLINE_FIXTURE' if is_fixture else 'TAIFEX',
                'kind':KIND,'response_sha256':response_sha256,'row_index':index,'trading_session':'一般',
                'capture_path':str(capture_path.resolve()) if capture_path else None,
                'raw_date':rawdate,'timestamp_semantics':'SESSION_DATE_ONLY_NO_INFERRED_TIME'})
            proof.validate_source(observed_at)
            key=(contract,month,day.isoformat())
            if key in accepted or key in duplicates:
                duplicates.add(key);accepted.pop(key,None);raise ValueError('DUPLICATE_CONTRACT_SESSION')
            accepted[key]=proof.model_dump(mode='json')
        except (ValueError,TypeError,OverflowError) as exc:
            rejections.append({'row_index':index,'reason':str(exc)[:180]})
    return {'status':'DAILY_SETTLEMENT_SOURCE_PARSED','source_url':source_url,
        'observed_at':observed_at.isoformat(),'response_sha256':response_sha256,
        'raw_row_count':len(rows),'settlements':list(accepted.values()),'rejections':rejections,
        'paper_only':True,'broker_connected':False,'execution_enabled':False,
        'scope':KIND,'is_fixture':is_fixture}


class TaifexDailySettlementAdapter:
    def __init__(self,timeout_seconds: float=12):
        if not 0<timeout_seconds<=30: raise ValueError('BOUNDED_TIMEOUT_REQUIRED')
        self.timeout_seconds=timeout_seconds

    def fetch(self, capture_path: Path) -> dict:
        from datetime import timezone
        with urlopen(Request(SOURCE_URL,headers={'User-Agent':'Mozilla/5.0'}),timeout=self.timeout_seconds) as res:
            if res.geturl()!=SOURCE_URL: raise ValueError('SOURCE_REDIRECT_UNEXPECTED')
            raw=res.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES: raise ValueError('SOURCE_RESPONSE_TOO_LARGE')
        observed_at=datetime.now(timezone.utc)
        digest=hashlib.sha256(raw).hexdigest()
        capture_path=Path(capture_path);capture_path.parent.mkdir(parents=True,exist_ok=True)
        temporary=capture_path.with_suffix('.tmp');temporary.write_bytes(raw);temporary.replace(capture_path)
        result=decode_daily_futures_settlements(json.loads(raw),source_url=SOURCE_URL,
            observed_at=observed_at,response_sha256=digest,capture_path=capture_path)
        result['capture_path']=str(capture_path)
        return result
