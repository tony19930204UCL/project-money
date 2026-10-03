"""TEST_ONLY source-shaped evidence; real producer code, no live SEC claim."""
from datetime import datetime, timezone
from cio_market_lab.research.official import OfficialResearchProducer, SEC_TICKERS_URL


def acquire(ocf_values):
    base = dict(start='2026-01-01', end='2026-06-30', filed='2026-07-29',
                form='10-Q', accn='TEST_ONLY_same_filing')
    facts = {'Revenues': {'units': {'USD': [dict(base, val=250)]}},
             'NetCashProvidedByUsedInOperatingActivities': {'units': {'USD': [dict(base, val=v) for v in ocf_values]}},
             'PaymentsToAcquirePropertyPlantAndEquipment': {'units': {'USD': [dict(base, val=35)]}}}
    data = dict(cik=789019, entityName='TEST_ONLY', facts={'us-gaap': facts})
    def fetch(url):
        return {'0': {'ticker': 'MSFT', 'cik_str': 789019}} if url == SEC_TICKERS_URL else data
    class Reader:
        def add_evidence(self, row, now=None):
            self.row = row
            return True, 'accepted'
    reader = Reader()
    result = OfficialResearchProducer(fetch_json=fetch).acquire(
        ['MSFT'], reader, datetime(2026, 10, 2, tzinfo=timezone.utc))
    assert result['accepted']
    return reader.row['raw_metadata']


def test_producer_joins_same_filing_and_preserves_duration():
    metadata = acquire([100])
    derived = metadata['financial_derivations']
    assert len(derived) == 1
    assert derived[0]['value_decimal'] == '65'
    assert derived[0]['period_kind'] == 'half_year'
    assert derived[0]['classification'] == 'DERIVED_NOT_COMPANY_REPORTED'


def test_conflicting_source_rows_are_not_hidden_by_period_dedup():
    for values in ([100, 120], [120, 100]):
        metadata = acquire(values)
        assert metadata['financial_derivations'] == []
        assert not any(r['concept'] == 'NetCashProvidedByUsedInOperatingActivities'
                       for r in metadata['financial_baseline'])
        assert any(g['reason'] == 'AMBIGUOUS_SAME_FILING_FACT'
                   for g in metadata['financial_baseline_gaps'])


def test_numerically_equal_duplicates_are_not_ambiguous():
    metadata = acquire([100, 100.0])
    assert len(metadata['financial_derivations']) == 1
    assert metadata['financial_derivations'][0]['value_decimal'] == '65'
