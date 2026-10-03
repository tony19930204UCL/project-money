"""Explicit deterministic next-quote fixture step; never a live data source."""
from datetime import timedelta


def submit_after_new_fixture_quote(runner, clock, packet):
    pending = runner.submit_cio_packet(packet)
    if pending.action not in {'BUY_PENDING', 'SELL_PENDING'}:
        return pending
    assert packet.is_fixture and runner.allow_fixture_quotes
    adapter = runner.market_adapter
    original = adapter.get_latest_quote
    quote = original(packet.selected_instrument)
    assert quote is not None and not quote.is_stale
    assert quote.bid is not None and quote.ask is not None
    # Generate a separate, explicitly labeled test-only book observation AFTER
    # order creation. Do not backdate as_of/observed_at or relax source gates.
    clock[0] += timedelta(seconds=1)
    next_quote = quote.model_copy(update={
        'timestamp': clock[0], 'observed_at': clock[0], 'is_fixture': True,
        'quote_id': 'TEST_ONLY_NEXT_' + packet.case_id + '_' + clock[0].isoformat(),
    })
    adapter.get_latest_quote = lambda symbol: next_quote if symbol == packet.selected_instrument else original(symbol)
    try:
        decisions = runner.process_pending_orders()
    finally:
        adapter.get_latest_quote = original
    matches = [d for d in decisions if d.order_id == pending.order_id]
    assert len(matches) == 1, [(d.action, d.reason) for d in decisions]
    filled = matches[0]
    assert filled.action in {'BUY_FILLED', 'SELL_FILLED'}, filled.reason
    order = next(o for o in runner.paper_orders.all_orders() if o.order_id == pending.order_id)
    assert next_quote.timestamp > order.created_at
    assert next_quote.timestamp > packet.as_of
    return filled
