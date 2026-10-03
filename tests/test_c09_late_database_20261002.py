"""Frozen deterministic paper caller/storage tests, not live acceptance."""
import asyncio,json
from datetime import timedelta
import pytest
from cio_market_lab.events.store import EventStore
from cio_market_lab.domain.events import EventEnvelope,EventType
from cio_market_lab.api.event_stream import event_frames,AUTHORITY
from tests.test_source_aligned_next_bar import NOW,packet
from tests.test_c08_quote_cutoff_20261002 import setup_book


def append_bar(store, n):
    payload={'symbol':'TEST_ONLY','source':'fixture://delayed_producer',
        'observed_at':NOW.isoformat(),'is_fixture':True,'quality':'TEST_ONLY',
        'sequence_fixture':n,'bid':None,'ask':None}
    return store.append(EventEnvelope(event_id=f'TEST_ONLY_delayed_{n}',
        event_type=EventType.BAR_OBSERVED,timestamp=NOW,
        aggregate_id='TEST_ONLY_stream',payload=payload))


def test_read_only_observer_discovers_later_committed_database(tmp_path):
    path=tmp_path/'later.sqlite'
    observer=EventStore(path,read_only=True)
    assert not path.exists()
    assert observer.get_events(strict=True)==[]
    producer=EventStore(path)
    sequence=append_bar(producer,1)
    rows=observer.get_events(strict=True)
    assert [s for s,e in rows]==[sequence]
    assert rows[0][1].payload['source']=='fixture://delayed_producer'
    assert rows[0][1].payload['bid'] is None
    with pytest.raises(PermissionError):
        append_bar(observer,2)
    assert producer.count()==1


def test_stream_resumes_across_new_producer_and_multiple_sqlite_batches(tmp_path):
    async def check():
        path=tmp_path/'stream.sqlite'
        observer=EventStore(path,read_only=True)
        producer=EventStore(path)
        ids=[append_bar(producer,i) for i in range(130)]
        frames=event_frames(observer,since_id=ids[4],poll_seconds=0.001)
        received=[]
        for i in ids[5:]:
            frame=await asyncio.wait_for(anext(frames),timeout=2)
            assert frame.startswith(f'id: {i}\n')
            rec=json.loads(next(line[6:] for line in frame.splitlines() if line.startswith('data: ')))
            assert rec['authority']==AUTHORITY
            assert rec['event']['payload']['quality']=='TEST_ONLY'
            assert rec['event']['payload']['observed_at']==NOW.isoformat()
            assert rec['event']['payload']['bid'] is None
            received.append(rec['sequence'])
        await frames.aclose()
        assert received==ids[5:]
        reopened=EventStore(path,read_only=True)
        reconnect=event_frames(reopened,since_id=received[-1],poll_seconds=0.001)
        new_id=append_bar(producer,131)
        frame=await asyncio.wait_for(anext(reconnect),timeout=2)
        assert frame.startswith(f'id: {new_id}\n')
        await reconnect.aclose()
        assert producer.count()==131
    asyncio.run(check())


def test_tw_native_odd_lot_partial_actual_caller_restart(tmp_path,monkeypatch):
    from cio_market_lab.engine.paper_orders import PaperExperimentSettings
    from cio_market_lab.engine.cio_packet import sign_cio_packet
    from tests.test_source_aligned_next_bar import runner_harness
    runner,pm,svc,us_cfg,clock,data,quote=setup_book(tmp_path,monkeypatch,session='ODD_LOT')
    quote.symbol='2330.TW'
    data['bar']=data['bar'].model_copy(update={'symbol':'2330.TW'})
    cfg=PaperExperimentSettings(strategy_id='TEST_ONLY_tw',market='TW',base_currency='TWD',
        reporting_currency='TWD',initial_cash=1000,universe=['2330.TW'],enabled=True,
        max_position_notional=500,paper_execution_model='QUOTE_BOOK')
    runner.configure(cfg)
    quote.timestamp=clock['now']-timedelta(seconds=30)
    p=packet('TEST_ONLY_tw_partial',clock['now'],paper_execution_model='QUOTE_BOOK',
        allow_partial_fills=True,allow_odd_lot=True)
    p.selected_instrument='2330.TW'
    p=sign_cio_packet(p,signer_id='fixture-test-signer')
    assert runner.submit_cio_packet(p,strategy_id='TEST_ONLY_tw').action=='BUY_PENDING'
    clock['now']+=timedelta(minutes=1)
    quote.timestamp=clock['now']-timedelta(seconds=1);quote.observed_at=clock['now']
    assert [d.action for d in runner.process_pending_orders()]==['BUY_PARTIALLY_FILLED']
    ledger=pm.get_strategy_ledger('TEST_ONLY_tw','swing')
    assert ledger.currency=='TWD' and ledger.positions['2330.TW'].quantity==1
    assert runner.process_pending_orders()==[]
    restored,rpm,rsvc=runner_harness(tmp_path,clock,data)
    restored.configure(cfg)
    monkeypatch.setattr(restored.market_adapter,'get_latest_quote',lambda symbol:quote)
    assert restored.process_pending_orders()==[]
    quote.quote_id='TEST_ONLY_tw_quote_2';quote.timestamp=clock['now']
    assert [d.action for d in restored.process_pending_orders()]==['BUY_FILLED']
    ledger=rpm.get_strategy_ledger('TEST_ONLY_tw','swing')
    assert ledger.currency=='TWD'
    assert ledger.positions['2330.TW'].quantity==2
    assert len(ledger.fills)==2 and all(f.currency=='TWD' for f in ledger.fills)
    assert all(f.consumed_quote.session=='ODD_LOT' for f in ledger.fills)
    assert ledger.cash==pytest.approx(1000+sum(f.cash_flow for f in ledger.fills))
    assert rpm.get_strategy_ledger('TEST_ONLY_native','swing').cash==1000
    assert restored.process_pending_orders()==[]
