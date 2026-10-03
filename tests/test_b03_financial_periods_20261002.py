"""TEST_ONLY SEC-shaped rows; no live official acquisition claim."""
import pytest
from cio_market_lab.research.financial_periods import classify_flow_period, aligned_cash_flow_derivations

@pytest.mark.parametrize('start,end,expected', [
    ('2026-01-01','2026-03-31','quarterly'),
    ('2026-01-01','2026-06-30','half_year'),
    ('2026-01-01','2026-09-30','nine_month'),
    ('2025-07-01','2026-06-30','annual'),
    (None,'2026-06-30','instant'),
    ('2026-09-30','2026-01-01','invalid'),
    ('bad','2026-01-01','invalid'),
])
def test_period_is_from_duration_not_filing_label(start,end,expected):
    assert classify_flow_period(start,end) == expected

def rows():
    base=dict(unit='USD',start='2026-01-01',end='2026-06-30',filed='2026-07-29',form='10-Q',accn='TEST_ONLY')
    return [dict(base,concept='NetCashProvidedByUsedInOperatingActivities',val=100),
            dict(base,concept='PaymentsToAcquirePropertyPlantAndEquipment',val=35)]

def test_aligned_fcf_is_explicitly_derived_not_company_reported():
    result=aligned_cash_flow_derivations(rows())
    assert len(result)==1
    assert result[0]['value_decimal']=='65'
    assert result[0]['period_kind']=='half_year'
    assert result[0]['classification']=='DERIVED_NOT_COMPANY_REPORTED'
    assert result[0]['formula']=='operating_cash_flow - property_plant_equipment_payments'

@pytest.mark.parametrize('field,value', [('unit','EUR'),('start','2026-04-01'),('end','2026-09-30'),
    ('accn','OTHER'),('accn',''),('form','10-K'),('filed','2026-07-30'),('val',float('nan')),('val',float('inf'))])
def test_do_not_mix_period_currency_filing_or_nonfinite_values(field,value):
    data=rows();data[1][field]=value
    assert aligned_cash_flow_derivations(data)==[]
