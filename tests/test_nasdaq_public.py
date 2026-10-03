from copy import deepcopy
from datetime import datetime, timezone
import pytest
from cio_market_lab.data.nasdaq_public import parse_public_book

@pytest.fixture
def payload():
    return {'data': {'symbol': 'MSFT', 'marketStatus': 'Open', 'primaryData': {
        'isRealTime': True, 'bidPrice': '$518.65', 'askPrice': '$518.70',
        'bidSize': '4', 'askSize': '100', 'lastSalePrice': '$518.68',
        'lastTradeTimestamp': 'Sep 30, 2026 11:56 AM ET'}}}

NOW = datetime(2026, 9, 30, 15, 56, 40, tzinfo=timezone.utc)

def test_public_book_is_real_raw_depth_not_paid_entitlement(payload):
    q = parse_public_book('MSFT', payload, NOW)
    assert q.bid_size == 4 and q.ask_size == 100
    assert q.timestamp == NOW.replace(second=0)
    assert q.source_capabilities['book_exchange_timestamp_available'] is False
    assert q.source_capabilities['entitlement_scope'] == 'unauthenticated_public_page_only_not_paid_feed'
    assert not q.is_synthetic and q.bid < q.ask

@pytest.mark.parametrize('key,value', [('bidPrice', '$600'), ('askPrice', 'NaN'), ('bidSize', '0'), ('askSize', '-1'), ('isRealTime', False), ('lastTradeTimestamp', 'Sep 30, 2026 11:53 AM ET'), ('lastTradeTimestamp', 'Sep 30, 2026 11:57 AM ET'), ('lastTradeTimestamp', 'Sep 30, 2026 11:56 AM')])
def test_reject_unusable_book(payload, key, value):
    payload['data']['primaryData'][key] = value
    with pytest.raises(ValueError):
        parse_public_book('MSFT', payload, NOW)

def test_reject_wrong_symbol_or_closed_market(payload):
    with pytest.raises(ValueError):
        parse_public_book('NVDA', payload, NOW)
    payload['data']['marketStatus'] = 'Closed'
    with pytest.raises(ValueError):
        parse_public_book('MSFT', payload, NOW)
