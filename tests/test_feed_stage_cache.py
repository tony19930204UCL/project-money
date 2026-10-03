"""Only same-stage analysis reads may be reused; execution reads are separate."""
import time
from datetime import datetime, timezone
from tests.test_cio_owned_desk import build_test_runner
import cio_market_lab.engine.autonomous_runner as module


def test_stage_reuses_analysis_but_not_execution_quote(tmp_path, monkeypatch):
    runner, _, _, _ = build_test_runner(tmp_path, [datetime(2026, 9, 30, 14, tzinfo=timezone.utc)])
    runner.market_adapter.offline_mode = False
    runner.market_adapter.is_fixture = False
    calls = []
    bars = [object()]
    def bounded(adapter, method, args, kwargs, deadline):
        calls.append((method, args[0]))
        return bars if method == 'get_bars' else object()
    monkeypatch.setattr(module, 'call_with_deadline', bounded)
    runner._feed_deadline = time.monotonic() + 8
    runner._stage_market_cache = {}
    assert runner._market_call('get_bars', 'MSFT', timeframe='1D', limit=32) is bars
    assert runner._market_call('get_latest_bar', 'MSFT') is bars[-1]
    runner._market_call('get_execution_quote', 'MSFT')
    assert calls == [('get_bars', 'MSFT'), ('get_execution_quote', 'MSFT')]
    # New execution stage after model reasoning must refresh; no cross-stage cache.
    runner._stage_market_cache = {}
    runner._market_call('get_latest_bar', 'MSFT')
    assert calls[-1] == ('get_latest_bar', 'MSFT')


def test_fixture_timeout_method_not_bypassed_by_analysis_cache(tmp_path, monkeypatch):
    runner, _, _, _ = build_test_runner(tmp_path, [datetime(2026, 9, 30, 14, tzinfo=timezone.utc)])
    runner.market_adapter.is_fixture = True
    runner._feed_deadline = time.monotonic() + 8
    runner._stage_market_cache = {}
    calls = []
    def bounded(adapter, method, args, kwargs, deadline):
        calls.append(method)
        return [object()] if method == 'get_bars' else object()
    monkeypatch.setattr(module, 'call_with_deadline', bounded)
    runner._market_call('get_bars', 'MSFT', timeframe='1D')
    runner._market_call('get_latest_bar', 'MSFT')
    assert calls == ['get_bars', 'get_latest_bar']
