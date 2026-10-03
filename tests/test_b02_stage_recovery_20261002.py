"""B02 deterministic local failure/recovery; not vendor uptime proof."""
from datetime import datetime, timezone
import time
import pytest
from cio_market_lab.data.bounded_feed import FeedUnavailable, call_with_deadline
from tests.test_cio_owned_desk import build_test_runner
import cio_market_lab.engine.autonomous_runner as module

@pytest.mark.parametrize('method', ['get_bars', 'get_latest_bar'])
def test_expired_stage_cannot_return_cached_analysis(tmp_path, monkeypatch, method):
    runner, _, _, _ = build_test_runner(tmp_path, [datetime(2026, 10, 1, 6, tzinfo=timezone.utc)])
    runner.market_adapter.offline_mode = False
    runner.market_adapter.is_fixture = False
    runner._feed_deadline = time.monotonic() - 1
    runner._stage_market_cache = {('get_bars', '2330.TW', '{}'): [object()]}
    monkeypatch.setattr(module, 'call_with_deadline', lambda *a, **k:
        pytest.fail('expired stage must not start another worker'))
    with pytest.raises(FeedUnavailable, match='stage deadline exhausted'):
        runner._market_call(method, '2330.TW')

@pytest.mark.parametrize('symbol', ['2330.TW', 'AAPL'])
def test_worker_error_is_classified_then_new_stage_recovers(tmp_path, monkeypatch, symbol):
    runner, _, _, adapter = build_test_runner(tmp_path, [datetime(2026, 10, 1, 6, tzinfo=timezone.utc)])
    adapter.is_fixture = True
    def broken(symbol):
        raise ValueError('TEST_ONLY_PROVIDER_BAD_RESPONSE')
    monkeypatch.setattr(adapter, 'get_latest_quote', broken)
    with pytest.raises(FeedUnavailable, match='market adapter error: ValueError'):
        call_with_deadline(adapter, 'get_latest_quote', (symbol,), {}, time.monotonic() + 2)
    monkeypatch.setattr(adapter, 'get_latest_quote', lambda symbol: {'symbol': symbol, 'fixture': True})
    assert call_with_deadline(adapter, 'get_latest_quote', (symbol,), {}, time.monotonic() + 2) == {'symbol': symbol, 'fixture': True}

def test_error_result_not_cached_and_same_stage_retry_can_recover(tmp_path, monkeypatch):
    runner, _, _, adapter = build_test_runner(tmp_path, [datetime(2026, 10, 1, 6, tzinfo=timezone.utc)])
    adapter.is_fixture = True
    runner._feed_deadline = time.monotonic() + 5
    runner._stage_market_cache = {}
    calls = []
    def bounded(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise FeedUnavailable('FEED_UNAVAILABLE: TEST_ONLY')
        return ['TEST_ONLY_RECOVERY']
    monkeypatch.setattr(module, 'call_with_deadline', bounded)
    with pytest.raises(FeedUnavailable):
        runner._market_call('get_bars', 'AAPL')
    assert runner._stage_market_cache == {}
    assert runner._market_call('get_bars', 'AAPL') == ['TEST_ONLY_RECOVERY']
    assert runner._market_call('get_bars', 'AAPL') == ['TEST_ONLY_RECOVERY']
    assert len(calls) == 2
