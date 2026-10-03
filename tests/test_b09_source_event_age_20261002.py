"""Deterministic TEST_ONLY regressions; never live-source/fill acceptance."""
from datetime import datetime, timedelta, timezone
import pytest
from cio_market_lab.domain.models import Bar, Order
from cio_market_lab.engine.execution import ExecutionCostConfig
from cio_market_lab.engine.source_aligned_execution import resolve_next_bar_open

NOW = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)

@pytest.mark.parametrize('age,expected', [(299, True), (300, True), (301, False), (86400, False)])
def test_event_age_cannot_be_hidden_by_recent_retrieval(age, expected):
    event = NOW - timedelta(seconds=age)
    decision = event - timedelta(seconds=1)
    bar = Bar(symbol='MSFT', timestamp=event, observed_at=NOW,
              open=101, high=103, low=99, close=102, volume=1000,
              source='TEST_ONLY', quality='TEST_ONLY', is_fixture=True,
              delay_seconds=0, is_stale=False)
    order = Order(order_id=f'TEST_ONLY_{age}', symbol='MSFT', market='US', currency='USD',
                  bucket='swing', side='BUY', order_type='MARKET', quantity=2,
                  origin='MAIN_CIO', reason='TEST_ONLY', created_at=decision)
    before = bar.model_dump(mode='json')
    result = resolve_next_bar_open(bar, order, ExecutionCostConfig(), now=NOW,
                                  decision_at=decision, allow_fixture=True, max_age_seconds=300)
    assert (result is not None) is expected
    assert bar.model_dump(mode='json') == before
    if result:
        assert result.quote_evidence is None
        assert result.assumptions['live_execution_acceptance'] is False
