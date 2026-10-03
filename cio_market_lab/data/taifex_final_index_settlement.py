"""Retained official monthly TX/TXO index final settlement, not an executable quote.

Do not infer dates from third-Wednesday rules. Dates come from actual exchange
final tables. The 13:30 expiry cut-off is separately read from product rules;
no publication time or live contract registry is inferred here.
"""
from __future__ import annotations
from datetime import date, datetime, time
import hashlib, math, re
from pathlib import Path
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field
from cio_market_lab.engine.paper_derivatives import ContractSpec
from cio_market_lab.engine.expiry_cash_settlement import CashSettlementEvidence

URLS={'FUTURE':'https://www.taifex.com.tw/cht/5/futIndxFSP',
      'OPTION':'https://www.taifex.com.tw/cht/5/optIndxFSP'}
RULE_URLS={'FUTURE':'https://www.taifex.com.tw/cht/2/tX',
           'OPTION':'https://www.taifex.com.tw/cht/2/tXO'}
TZ=ZoneInfo('Asia/Taipei')


def _capture(path: Path) -> tuple[Path,bytes]:
    path=Path(path).resolve()
    try: path.relative_to(Path(__file__).resolve().parents[2]/'artifacts')
    except ValueError: raise ValueError('SOURCE_CAPTURE_SCOPE_INVALID')
    raw=path.read_bytes()
    if not 100<=len(raw)<=2500000: raise ValueError('SOURCE_CAPTURE_SIZE_INVALID')
    return path,raw


def _table(raw: bytes, marker: str):
    soup=BeautifulSoup(raw,'html.parser')
    candidates=[t for t in soup.select('table') if marker in re.sub(r'\s+','',t.get_text())]
    # Require a uniquely identified actual table, never a substring in a menu.
    candidates=[t for t in candidates if any(re.fullmatch(r'\d{4}/\d{2}/\d{2}',
        c.get_text(strip=True)) for c in t.select('td'))]
    if len(candidates)!=1: raise ValueError('FINAL_TABLE_SCHEMA_UNAVAILABLE')
    return candidates[0]


def decode_final_index_settlements(capture_path: Path, *, source_url: str,
        observed_at: datetime, instrument_type: str) -> dict:
    if source_url!=URLS.get(instrument_type) or observed_at.tzinfo is None:
        raise ValueError('UNTRUSTED_SOURCE_OR_CLOCK')
    path,raw=_capture(capture_path);digest=hashlib.sha256(raw).hexdigest()
    table=_table(raw,'TX/MTX/TMF' if instrument_type=='FUTURE' else 'TXO')
    accepted={};duplicates=set();rejections=[]
    for index,tr in enumerate(table.select('tr')):
        cells=[c.get_text(' ',strip=True) for c in tr.find_all(['td','th'],recursive=False)]
        if not cells or not re.fullmatch(r'\d{4}/\d{2}/\d{2}',cells[0]): continue
        try:
            if len(cells)<3 or not re.fullmatch(r'\d{6}',cells[1]): raise ValueError('MONTHLY_OUTRIGHT_ONLY')
            day=datetime.strptime(cells[0],'%Y/%m/%d').date()
            datetime.strptime(cells[1],'%Y%m')
            if day>observed_at.astimezone(TZ).date(): raise ValueError('FUTURE_SETTLEMENT_DATE')
            price=float(cells[2])
            if not math.isfinite(price) or price<=0: raise ValueError('FINAL_PRICE_UNAVAILABLE')
            month=cells[1];key=(month,day.isoformat())
            if key in accepted or key in duplicates:
                accepted.pop(key,None);duplicates.add(key);raise ValueError('FINAL_SETTLEMENT_DUPLICATE')
            accepted[key]={'exchange_contract':'TX' if instrument_type=='FUTURE' else 'TXO',
                'instrument_type':instrument_type,'contract_month':month,'settlement_date':day.isoformat(),
                'settlement_price':price,'observed_at':observed_at.isoformat(),'is_fixture':False,
                'source_url':source_url,'provenance':{'authority':'TAIFEX','kind':'FINAL_INDEX_SETTLEMENT',
                'capture_path':str(path),'response_sha256':digest,'table_row_index':index,
                'timestamp_semantics':'OFFICIAL_FINAL_DATE_ONLY_NO_PUBLICATION_TIME'}}
        except (ValueError,TypeError,OverflowError) as e:
            rejections.append({'row_index':index,'reason':str(e)})
    return {'status':'OFFICIAL_FINAL_TABLE_PARSED','settlements':list(accepted.values()),
            'rejections':rejections,'source_url':source_url,'response_sha256':digest,
            'observed_at':observed_at.isoformat(),'execution_enabled':False,'paper_only':True,
            'broker_connected':False,'is_fixture':False,'scope':'FINAL_INDEX_VALUE_NOT_EXECUTABLE_QUOTE'}


def decode_index_contract_rules(capture_path: Path, *, source_url: str, instrument_type: str) -> dict:
    if source_url!=RULE_URLS.get(instrument_type): raise ValueError('UNTRUSTED_RULE_SOURCE')
    path,raw=_capture(capture_path);soup=BeautifulSoup(raw,'html.parser')
    rows={}
    for tr in soup.select('tr'):
        cells=[c.get_text(' ',strip=True) for c in tr.find_all(['td','th'],recursive=False)]
        if len(cells)==2: rows[re.sub(r'\s+','',cells[0])]=cells[1]
    code='TX' if instrument_type=='FUTURE' else 'TXO'
    if rows.get('英文代碼')!=code: raise ValueError('CONTRACT_RULE_SCHEMA_UNAVAILABLE')
    m=re.search(r'最後交易日.{0,150}?下午\s*(\d{1,2})\s*:\s*(\d{2})',rows.get('交易時間',''),re.S)
    if not m: raise ValueError('EXACT_EXPIRY_END_TIME_UNAVAILABLE')
    hour=int(m[1])+12;minute=int(m[2]);end=time(hour,minute).strftime('%H:%M')
    unit=rows.get('契約價值','') if instrument_type=='FUTURE' else rows.get('契約乘數','')
    unit_match=re.search(r'新臺幣\s*(\d+)\s*元',unit)
    if not unit_match or '現金' not in rows.get('交割方式',''):raise ValueError('CONTRACT_VALUE_OR_CASH_RULE_UNAVAILABLE')
    proof={'authority':'TAIFEX','capture_path':str(path),'response_sha256':hashlib.sha256(raw).hexdigest(),
           'source_url':source_url,'kind':'OFFICIAL_PRODUCT_RULES'}
    result={'instrument_type':instrument_type,'exchange_contract':code,'multiplier':int(unit_match[1]),
            'expiry_end_time':end,'cash_settled':True,'provenance':proof}
    if instrument_type=='FUTURE':
        tick=re.search(r'指數\s*(\d+)\s*點',rows.get('最小升降單位',''))
        if not tick:raise ValueError('FUTURE_TICK_RULE_UNAVAILABLE')
        result['tick_size']=int(tick[1])
    else:
        ticks=rows.get('權利金報價單位','')
        # Freeze this supported source schema; any exchange rule change blocks,
        # rather than silently retaining an old tick ladder.
        patterns=[r'未滿10點[：:]\s*0\.1點',r'10點以上，未滿50點[：:]\s*0\.5點',
                  r'50點以上，未滿500點[：:]\s*1點',r'500點以上，未滿1,000點[：:]\s*5點',
                  r'1,000點以上[：:]\s*10點']
        if not all(re.search(p,ticks) for p in patterns) or '歐式' not in rows.get('履約型態',''):
            raise ValueError('OPTION_TICK_OR_STYLE_RULE_UNAVAILABLE')
        result.update({'premium_tick_bands':[[0,0.1],[10,0.5],[50,1],[500,5],[1000,10]],
                       'exercise_style':'EUROPEAN'})
    return result


class FinalIndexSettlementEvidence(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    exchange_contract: str
    instrument_type: str
    contract_month: str
    settlement_date: date
    settlement_price: float=Field(gt=0)
    observed_at: datetime
    is_fixture: bool
    source_url: str
    provenance: dict

    def validate_capture(self) -> None:
        if self.is_fixture or self.provenance.get('authority')!='TAIFEX':
            raise ValueError('REAL_FINAL_CAPTURE_REQUIRED')
        path,raw=_capture(self.provenance.get('capture_path',''))
        if hashlib.sha256(raw).hexdigest()!=self.provenance.get('response_sha256'):
            raise ValueError('FINAL_CAPTURE_HASH_MISMATCH')
        decoded=decode_final_index_settlements(path,source_url=self.source_url,
            observed_at=self.observed_at,instrument_type=self.instrument_type)
        same=next((r for r in decoded['settlements'] if r['contract_month']==self.contract_month and
            r['settlement_date']==self.settlement_date.isoformat()),None)
        if same is None or self.model_dump(mode='json')!=type(self).model_validate(same).model_dump(mode='json'):
            raise ValueError('FINAL_CAPTURE_ROW_MISMATCH')

    def cash_evidence(self, spec: ContractSpec, rules: dict, now: datetime) -> CashSettlementEvidence:
        self.validate_capture()
        p=rules.get('provenance',{})
        verified=decode_index_contract_rules(p.get('capture_path',''),source_url=p.get('source_url',''),
                                            instrument_type=self.instrument_type)
        if verified!=rules: raise ValueError('CONTRACT_RULE_CAPTURE_MISMATCH')
        if now.tzinfo is None or self.observed_at.tzinfo is None or self.observed_at>now:
            raise ValueError('FINAL_OBSERVATION_CLOCK_UNAVAILABLE')
        if (spec.instrument_type.value!=self.instrument_type or spec.multiplier!=rules['multiplier'] or
            spec.underlying_symbol!='TAIEX'):
            raise ValueError('FINAL_SOURCE_CONTRACT_MAPPING_REQUIRED')
        symbol=self.exchange_contract+':'+self.contract_month
        if self.instrument_type=='OPTION':
            if spec.strike is None or spec.strike<=0 or spec.option_right is None:
                raise ValueError('OPTION_EXERCISE_SPEC_UNAVAILABLE')
            symbol+=':'+('C' if spec.option_right.value=='CALL' else 'P')+':'+format(spec.strike,'g')
        if spec.symbol!=symbol:raise ValueError('FINAL_SOURCE_CONTRACT_MAPPING_REQUIRED')
        hour,minute=map(int,rules['expiry_end_time'].split(':'))
        expiry=datetime.combine(self.settlement_date,time(hour,minute),tzinfo=TZ)
        if spec.expiry is None or spec.expiry.tzinfo is None or spec.expiry!=expiry:
            raise ValueError('EXACT_OFFICIAL_EXPIRY_REQUIRED')
        return CashSettlementEvidence(contract_symbol=spec.symbol,underlying_symbol=spec.underlying_symbol,
            expiry=expiry,settlement_type='CASH',settlement_price=self.settlement_price,timestamp=expiry,
            observed_at=self.observed_at,is_fixture=False,source=self.source_url,
            provenance={**self.provenance,'kind':'FINAL_SETTLEMENT','product_rule_provenance':p,
                'timestamp_semantics':'CONTRACT_EXPIRY_FROM_OFFICIAL_DATE_AND_RULE_NOT_PUBLICATION_TIMESTAMP'})
