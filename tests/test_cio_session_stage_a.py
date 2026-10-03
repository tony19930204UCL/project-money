import pytest

from cio_market_lab.engine.cio_session import (
    CIOSessionHistory,
    FrozenDecisionContext,
    MaterialDeltaGate,
)


def _context():
    return FrozenDecisionContext(
        context_id="ctx-2026-09-30-2330", session_date="2026-09-30",
        official_source_lineage=[{"source": "fixture-official", "observed_at": "2026-09-30T00:00:00Z"}],
        thesis="fixture-only thesis", valuation_scenarios={"base": 100}, catalysts=["earnings"],
        entry_zone={"low": 90, "high": 95}, invalidation={"below": 85}, exposure_ceiling=0.02,
    )


def test_restart_reads_same_session_context_and_decision_history(tmp_path):
    first = CIOSessionHistory(tmp_path)
    first.freeze(_context())
    first.append({"observation_digest": "abc", "call_receipt": {"fixture": True}})
    restarted = CIOSessionHistory(tmp_path)
    assert restarted.session_id == first.session_id
    assert restarted.load_context().context_id == "ctx-2026-09-30-2330"
    assert restarted.history() == first.history()


def test_frozen_context_cannot_be_rewritten(tmp_path):
    store = CIOSessionHistory(tmp_path)
    store.freeze(_context())
    changed = _context().model_copy(update={"thesis": "changed"})
    with pytest.raises(ValueError, match="frozen context"):
        store.freeze(changed)


def test_idle_duplicate_invalidation_and_material_delta_gating(tmp_path):
    store = CIOSessionHistory(tmp_path)
    gate = MaterialDeltaGate(store)
    assert gate.evaluate({"quote": 101})["status"] == "IDLE"
    material = {"official_material_ids": ["filing-1"]}
    assert gate.evaluate(material)["should_call"] is True
    gate.record_call(material, {"fixture": True})
    assert gate.evaluate(material)["status"] == "DUPLICATE"
    assert gate.evaluate({"invalidation_triggered": True})["should_call"] is True
    assert gate.evaluate({"invalidation_triggered": True})["invalidation_requires_cio"] is True
    assert gate.evaluate(material, data_available=False)["status"] == "BLOCKED_DATA_UNAVAILABLE"
    assert len([r for r in store.history() if r.get("call_succeeded")]) == 1

def test_semantic_delta_ignores_noise_and_tracks_new_ids_and_failures(tmp_path):
    store = CIOSessionHistory(tmp_path, "stable-session")
    gate = MaterialDeltaGate(store)
    first = {"official_material_ids": ["doc-a"], "timestamp": "one", "price": 10}
    assert gate.evaluate(first)["should_call"]
    gate.record_call(first, {"success": True})
    assert not gate.evaluate({"official_material_ids": ["doc-a"], "timestamp": "two", "price": 11})["should_call"]
    assert gate.evaluate({"official_material_ids": ["doc-a", "doc-b"], "timestamp": "three"})["material_ids"] == ["doc-b"]
    failed = {"official_material_ids": ["doc-fail"]}
    gate.record_call(failed, {"success": False})
    assert gate.evaluate(failed)["should_call"]
    store.append({"session_id": "forged", "marker": 1})
    assert store.history()[-1]["session_id"] == "stable-session"
    other = CIOSessionHistory(tmp_path, "other")
    assert other.history() == []

def test_nested_frozen_context_is_immutable(tmp_path):
    context = _context()
    with pytest.raises(TypeError): context.valuation_scenarios["base"] = 1
    with pytest.raises(TypeError): context.official_source_lineage[0]["source"] = "changed"
    store = CIOSessionHistory(tmp_path)
    store.freeze(context)
    changed = FrozenDecisionContext(**{**_context().model_dump(), "valuation_scenarios": {"base": 101}})
    with pytest.raises(ValueError, match="frozen context"):
        store.freeze(changed)
