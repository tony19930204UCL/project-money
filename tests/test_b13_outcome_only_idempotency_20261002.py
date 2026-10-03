"""Offline TEST_ONLY: exit receipt must not invent a second trading lesson."""
from datetime import datetime, timedelta, timezone
from cio_market_lab.domain.models import CIODecisionPacket
from cio_market_lab.engine.decision_learning import CIODecisionLearningStore

NOW = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)

def packet(case):
    return CIODecisionPacket(case_id=case, as_of=NOW, expiry=NOW + timedelta(hours=1),
        thesis='TEST_ONLY', selected_instrument='MSFT', action='SELL', quantity=1, is_fixture=True)

def test_explicit_empty_lessons_close_receipt_without_duplicate_lesson(tmp_path):
    store = CIODecisionLearningStore(tmp_path)
    store.record_decision(packet('TEST_ONLY_EXIT'), {}, {})
    outcome = {'realized_pnl': 10, 'as_of': NOW.isoformat()}
    assert store.record_outcome('TEST_ONLY_EXIT', outcome, lessons=[], as_of=NOW) is None
    assert store.get_record('TEST_ONLY_EXIT').status == 'CLOSED'
    assert len(store._lessons) == 0
    rebuilt = CIODecisionLearningStore(tmp_path)
    assert rebuilt.record_outcome('TEST_ONLY_EXIT', dict(outcome), lessons=[], as_of=NOW) is None
    assert len(rebuilt.get_record('TEST_ONLY_EXIT').outcomes) == 1
    assert rebuilt._lessons == []

def test_unspecified_lessons_preserve_structured_default(tmp_path):
    store = CIODecisionLearningStore(tmp_path)
    store.record_decision(packet('TEST_ONLY_DEFAULT'), {}, {})
    outcome = {'realized_pnl': 10, 'as_of': NOW.isoformat()}
    first = store.record_outcome('TEST_ONLY_DEFAULT', outcome, as_of=NOW)
    assert first is not None
    second = store.record_outcome('TEST_ONLY_DEFAULT', dict(outcome), as_of=NOW)
    assert second.lesson_id == first.lesson_id
    assert len(store._lessons) == 1


def test_explicit_empty_application_and_delta_do_not_inherit_packet_claim(tmp_path):
    store = CIODecisionLearningStore(tmp_path)
    p = packet('TEST_ONLY_EXPLICIT_NO_APPLICATION')
    p.conditions['applied_lesson_ids'] = ['TEST_ONLY_UNACKNOWLEDGED']
    p.conditions['decision_delta'] = {'claim': 'TEST_ONLY_NOT_ACKNOWLEDGED'}
    record = store.record_decision(p, {}, {}, applied_lesson_ids=[], decision_delta={})
    assert record.applied_lesson_ids == []
    assert record.decision_delta == {}
    rebuilt = CIODecisionLearningStore(tmp_path)
    assert rebuilt.get_record(p.case_id).applied_lesson_ids == []
    assert rebuilt.get_record(p.case_id).decision_delta == {}


def test_versioned_hypothesis_does_not_reuse_superseded_lesson_after_restart(tmp_path):
    store = CIODecisionLearningStore(tmp_path)
    p = packet('TEST_ONLY_OLD')
    store.record_decision(p, {}, {})
    store.record_fill(p.case_id, 'TEST_ONLY_ORDER', {'price': 100, 'quantity': 1}, {})
    store.record_outcome(p.case_id, {'realized_pnl': 10, 'as_of': NOW.isoformat()},
                         lessons=['TEST_ONLY superseded lesson'], as_of=NOW)
    assert len(store.retrieve_context_lessons('MSFT', NOW + timedelta(seconds=1))) == 1
    replacement = packet('TEST_ONLY_NEW')
    store.version_hypothesis(p.case_id, replacement, 'TEST_ONLY ownership change')
    assert store.retrieve_context_lessons('MSFT', NOW + timedelta(seconds=1)) == []
    rebuilt = CIODecisionLearningStore(tmp_path)
    assert rebuilt.retrieve_context_lessons('MSFT', NOW + timedelta(seconds=1)) == []
