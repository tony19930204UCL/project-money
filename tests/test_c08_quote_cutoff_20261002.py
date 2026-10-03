"""Frozen TEST_ONLY caller acceptance, never live quote evidence."""
from datetime import timedelta
import pytest
from cio_market_lab.domain.models import Quote
from tests.test_source_aligned_next_bar import NOW, make_bar, make_order, packet, runner_harness


def setup_book(tmp_path, monkeypatch, session='REGULAR', size=1):
    clock = {'now': NOW+timedelta(minutes=2)}
    data = {'bar': make_bar(timestamp=NOW-timedelta(minutes=1), observed_at=NOW)}
    runner, pm, svc = runner_harness(tmp_path, clock, data)
    cfg = svc.experiment_for('TEST_ONLY_native').model_copy(update={'paper_execution_model':'QUOTE_BOOK'})
    runner.configure(cfg)
    quote = Quote(symbol='MSFT', timestamp=NOW+timedelta(seconds=10),
        observed_at=clock['now'], last_price=101, bid=100, ask=101,
        bid_size=size, ask_size=size, quote_id='TEST_ONLY_q1',
        source='fixture://c08_cutoff', quality='TEST_ONLY', session=session,
        source_capabilities={'source':'fixture://c08_cutoff', 'two_sided_book':True,
            'size_backed':True, 'exchange_session_attested':True,
            'entitlement_status':'TEST_ONLY', 'entitlement_evidence_id':'TEST_ONLY_eid',
            'supported_sessions':[session], 'extended_hours_book':session=='EXTENDED',
            'odd_lot_book':session=='ODD_LOT'})
    monkeypatch.setattr(runner.market_adapter, 'get_latest_quote', lambda symbol: quote)
    return runner, pm, svc, cfg, clock, data, quote


@pytest.mark.parametrize('cutoff_kind', ['decision', 'order', 'entry'])
@pytest.mark.parametrize('delta', [0, -1])
def test_quote_cannot_execute_before_or_at_authorization_cutoff(tmp_path, monkeypatch, cutoff_kind, delta):
    runner, pm, svc, cfg, clock, data, quote = setup_book(tmp_path, monkeypatch, size=2)
    cutoff = NOW+timedelta(seconds=30)
    quote.timestamp = cutoff+timedelta(seconds=delta)
    order = make_order(created_at=cutoff if cutoff_kind=='order' else NOW)
    p = packet('TEST_ONLY_cutoff', cutoff if cutoff_kind=='decision' else NOW,
               paper_execution_model='QUOTE_BOOK')
    result = runner._resolve_cio_execution(cfg, p, data['bar'], order,
        entry_time=cutoff if cutoff_kind=='entry' else None)
    assert result is None
    assert not pm.get_strategy_ledger('TEST_ONLY_native','swing').fills


@pytest.mark.parametrize('session', ['REGULAR', 'EXTENDED', 'ODD_LOT'])
def test_positive_declared_session_partial_size_and_cutoff(tmp_path, monkeypatch, session):
    runner, pm, svc, cfg, clock, data, quote = setup_book(tmp_path, monkeypatch, session=session)
    tw = session=='ODD_LOT'
    symbol = '2330.TW' if tw else 'MSFT'
    data['bar'] = data['bar'].model_copy(update={'symbol':symbol})
    quote.symbol = symbol
    p = packet('TEST_ONLY_positive', NOW, paper_execution_model='QUOTE_BOOK',
               allow_partial_fills=True, allow_extended_hours=session=='EXTENDED', allow_odd_lot=tw)
    p.selected_instrument = symbol
    order = make_order(symbol=symbol, market='TW' if tw else 'US', currency='TWD' if tw else 'USD')
    result = runner._resolve_cio_execution(cfg, p, data['bar'], order)
    assert result is not None
    assert result.assumptions['executed_quantity']==1
    assert result.assumptions['remaining_quantity_before_fill']==2
    assert result.quote_evidence.session==session
    assert result.quote_evidence.source_quote_id=='TEST_ONLY_q1'
    assert result.timestamp==quote.timestamp
    assert result.verification=='BOOK_BOUND_TEST_ONLY'
    assert result.effective_price==pytest.approx(101.0505)


@pytest.mark.parametrize('session', ['REGULAR', 'EXTENDED'])
def test_full_pending_caller_partial_restart_and_new_quote_completion(tmp_path, monkeypatch, session):
    runner, pm, svc, cfg, clock, data, quote = setup_book(tmp_path, monkeypatch, session=session)
    quote.timestamp = NOW+timedelta(seconds=30)
    quote.observed_at = clock['now']
    p = packet('TEST_ONLY_partial', clock['now'], paper_execution_model='QUOTE_BOOK',
               allow_partial_fills=True, allow_extended_hours=session=='EXTENDED')
    assert runner.submit_cio_packet(p,strategy_id='TEST_ONLY_native').action=='BUY_PENDING'
    assert pm.get_strategy_ledger('TEST_ONLY_native','swing').cash==1000
    clock['now']=NOW+timedelta(minutes=3)
    quote.timestamp=clock['now']-timedelta(seconds=1)
    quote.observed_at=clock['now']
    decisions=runner.process_pending_orders()
    assert [d.action for d in decisions]==['BUY_PARTIALLY_FILLED']
    ledger=pm.get_strategy_ledger('TEST_ONLY_native','swing')
    assert ledger.positions['MSFT'].quantity==1
    assert svc.all_orders()[0].remaining_quantity==1
    assert len(ledger.fills)==1
    assert runner.process_pending_orders()==[]
    restored, rpm, rsvc=runner_harness(tmp_path,clock,data)
    restored.configure(cfg)
    monkeypatch.setattr(restored.market_adapter,'get_latest_quote',lambda symbol:quote)
    assert restored.process_pending_orders()==[]
    quote.quote_id='TEST_ONLY_q2'
    quote.timestamp=clock['now']
    decisions=restored.process_pending_orders()
    assert [d.action for d in decisions]==['BUY_FILLED']
    ledger=rpm.get_strategy_ledger('TEST_ONLY_native','swing')
    assert ledger.positions['MSFT'].quantity==2
    assert len(ledger.fills)==2
    assert ledger.cash==pytest.approx(1000-2*101.0505-2)
    assert rsvc.all_orders()[0].remaining_quantity==0
    assert restored.process_pending_orders()==[]
