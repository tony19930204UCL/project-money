from datetime import datetime, timezone
from tests.test_cio_owned_desk import build_test_runner
from cio_market_lab.engine.autonomous_runner import DYNAMIC_DESK_ID
from cio_market_lab.engine.paper_orders import PaperExperimentSettings


def test_focused_context_does_not_refresh_unrelated_universe(tmp_path, monkeypatch):
    clock = [datetime(2026, 9, 29, 14, tzinfo=timezone.utc)]
    runner, _, _, _ = build_test_runner(tmp_path, clock)
    runner.configure(PaperExperimentSettings(strategy_id=DYNAMIC_DESK_ID,
        enabled=True, universe=['0050.TW', '2330.TW'], mode='intraday', base_currency='TWD'))
    calls = []
    original = runner._market_call
    def tracked(method, symbol, **kwargs):
        calls.append((method, symbol))
        return original(method, symbol, **kwargs)
    monkeypatch.setattr(runner, '_market_call', tracked)
    context = runner.build_decision_context_request(['AAPL'])
    assert context is not None
    assert ('get_latest_bar', 'AAPL') in calls
    assert not any(symbol in {'2330.TW', '0050.TW'} for _, symbol in calls), calls


def test_cache_only_quote_does_not_invent_fresh_evidence(tmp_path, monkeypatch):
    clock = [datetime(2026, 9, 29, 14, tzinfo=timezone.utc)]
    runner, _, _, _ = build_test_runner(tmp_path, clock)
    def forbidden(*args, **kwargs):
        raise AssertionError('cache-only must not call network')
    monkeypatch.setattr(runner, '_market_call', forbidden)
    quote = runner.get_durable_quote('MSFT', refresh=False)
    assert quote.source == 'missing'
    assert quote.is_stale
    assert quote.last_price is None
