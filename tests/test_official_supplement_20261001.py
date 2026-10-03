from datetime import datetime, timezone
from cio_market_lab.research.official import OfficialResearchProducer, SEC_TICKERS_URL


def test_capex_and_comparable_annual_survive_latest_quarter():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rows = [
        dict(start='2025-07-01', end='2026-06-30', filed='2026-07-29', form='10-K', val=100, accn='new'),
        dict(start='2024-07-01', end='2025-06-30', filed='2026-07-29', form='10-K', val=80, accn='new'),
        dict(start='2026-01-01', end='2026-03-31', filed='2026-04-29', form='10-Q', val=25, accn='q'),
    ]
    data = dict(cik=789019, entityName='Microsoft', facts={'us-gaap': {
        name: {'units': {'USD': rows}}
        for name in ('Revenues', 'PaymentsToAcquirePropertyPlantAndEquipment', 'OperatingLeaseLiabilityCurrent')
    }})
    def fetch(url):
        return {'0': {'ticker': 'MSFT', 'cik_str': 789019}} if url == SEC_TICKERS_URL else data
    class Reader:
        def add_evidence(self, row, now=None):
            self.row = row
            return True, 'accepted'
    reader = Reader()
    result = OfficialResearchProducer(fetch_json=fetch).acquire(['MSFT'], reader, now)
    assert not result['gaps']
    baseline = reader.row['raw_metadata']['financial_baseline']
    capex = [r for r in baseline if r['concept'] == 'PaymentsToAcquirePropertyPlantAndEquipment']
    assert {r['start'] for r in capex} == {'2024-07-01', '2025-07-01', '2026-01-01'}
    assert all(r['accn'] and r['filed'] and r['unit'] == 'USD' for r in capex)
    assert any(r['concept'] == 'OperatingLeaseLiabilityCurrent' for r in baseline)
