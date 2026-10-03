"""Deterministic PAPER caller replay. Constructed bars are TEST_ONLY, not live BBO."""
from datetime import timedelta
import pytest
from tests.test_source_aligned_next_bar import NOW, make_bar, packet, runner_harness


def test_consumed_earlier_bar_cannot_starve_pending_order_after_restart(tmp_path, monkeypatch):
    clock = {'now': NOW}
    data = {'bar': make_bar(timestamp=NOW-timedelta(minutes=1), observed_at=NOW)}
    runner, pm, svc = runner_harness(tmp_path, clock, data)
    assert runner.submit_cio_packet(packet('TEST_ONLY_first', NOW), strategy_id='TEST_ONLY_native').action == 'BUY_PENDING'
    assert runner.submit_cio_packet(packet('TEST_ONLY_second', NOW), strategy_id='TEST_ONLY_native').action == 'BUY_PENDING'
    clock['now'] = NOW + timedelta(minutes=2)
    first_bar = make_bar(volume=2)
    data['bar'] = first_bar
    assert [d.action for d in runner.process_pending_orders()] == ['BUY_FILLED']
    assert sorted(o.remaining_quantity for o in svc.all_orders()) == [0, 2]
    second_bar = make_bar(timestamp=NOW+timedelta(minutes=3),
                          observed_at=NOW+timedelta(minutes=4), open=105,
                          high=106, low=104, close=105, volume=2)
    clock['now'] = NOW + timedelta(minutes=4)
    data['bar'] = second_bar
    restored, rpm, rsvc = runner_harness(tmp_path, clock, data)
    monkeypatch.setattr(restored.market_adapter, 'get_bars', lambda *a, **k: [first_bar, second_bar])
    decisions = restored.process_pending_orders()
    assert [d.action for d in decisions] == ['BUY_FILLED']
    ledger = rpm.get_strategy_ledger('TEST_ONLY_native', 'swing')
    assert ledger.positions['MSFT'].quantity == 4
    assert len(ledger.fills) == 2
    assert ledger.fills[-1].timestamp == second_bar.timestamp
    assert ledger.fills[-1].quote_verification == 'BAR_NEXT_OPEN_TEST_ONLY'
    assert ledger.fills[-1].consumed_quote is None
    assert ledger.cash == pytest.approx(1000 - 2*101.0505 - 1 - 2*105.0525 - 1)
    assert restored.process_pending_orders() == []
    assert all(o.remaining_quantity == 0 for o in rsvc.all_orders())
