"""TEST_ONLY deterministic regression. No live source/model acceptance."""
from datetime import datetime, timedelta, timezone
import pytest
from cio_market_lab.engine.stage_d_observation import apply_verified_quote_edges, build_canonical_observation
from cio_market_lab.engine.cio_session import CIOSessionHistory, MaterialDeltaGate
from cio_market_lab.integrations.hermes_chat import HermesCIODecisionExecutor
from tests.test_stage_d_observation import packet

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)

def observation():
    value = packet(invalidation_condition={"field": "last_price", "operator": "lt", "threshold": 90})
    return build_canonical_observation(value, "NVDA", "TEST_ONLY")

def quote(price=102, **kw):
    return {"symbol": "NVDA", "source": "fixture://TEST_ONLY", "test_only": True,
            "quality": "good", "observed_at": NOW.isoformat(), "bar_time": NOW.isoformat(),
            "last_price": price, **kw}

def edge(obs, price=102, **kw):
    return apply_verified_quote_edges(obs, quote(price, **kw), NOW, allow_fixture=True)

def test_edges_dedup_restart_and_reentry(tmp_path):
    obs, ctx = observation()
    store = CIOSessionHistory(tmp_path, "TEST_ONLY"); store.freeze(ctx)
    gate = MaterialDeltaGate(store)
    outside = edge(obs, 110)
    assert gate.evaluate(outside)["should_call"]
    gate.record_call(outside, {"success": True, "is_fixture": True})
    inside = edge(obs)
    assert gate.evaluate(inside)["edge_state"]["entry_triggered"] is True
    gate.record_call(inside, {"success": True, "is_fixture": True})
    assert gate.evaluate(inside)["should_call"] is False
    restarted = MaterialDeltaGate(CIOSessionHistory(tmp_path, "TEST_ONLY"))
    assert restarted.evaluate(inside)["should_call"] is False
    restarted.evaluate(outside)
    assert restarted.evaluate(inside)["should_call"] is True
    restarted.record_call(inside, {"success": True, "is_fixture": True})
    invalid = edge(obs, 89)
    assert restarted.evaluate(invalid)["invalidation_requires_cio"] is True
    restarted.record_call(invalid, {"success": True, "is_fixture": True})
    assert restarted.evaluate(invalid)["should_call"] is False

@pytest.mark.parametrize("change", [
    {"observed_at": (NOW-timedelta(minutes=6)).isoformat()},
    {"bar_time": (NOW-timedelta(minutes=6)).isoformat()},
    {"observed_at": (NOW+timedelta(seconds=1)).isoformat()},
    {"bar_time": None}, {"symbol": "AAPL"}, {"quality": "missing"},
    {"is_stale": True}, {"is_synthetic": True}, {"verified": False},
    {"last_price": float("nan")}, {"last_price": -1}, {"last_price": True},
])
def test_bad_quotes_do_not_create_edges(change):
    obs, _ = observation()
    result = apply_verified_quote_edges(obs, quote(**change), NOW, allow_fixture=True)
    assert result["quote_edge_status"] == "BLOCKED_QUOTE_UNAVAILABLE"
    assert "entry_triggered" not in result
    assert "invalidation_triggered" not in result

def test_fixture_and_freeform_are_not_live_signals():
    obs, _ = observation()
    assert apply_verified_quote_edges(obs, quote(), NOW)["quote_edge_status"] == "BLOCKED_QUOTE_UNAVAILABLE"
    obs["invalidation_condition"] = None
    result = edge(obs, 89)
    assert result["invalidation_edge"]["triggered"] is None
    assert "invalidation_triggered" not in result

def test_invalidation_rule_changes_semantic_context():
    a = packet(invalidation_condition={"field":"last_price", "operator":"lt", "threshold":90})
    b = {**a, "invalidation_condition":{**a["invalidation_condition"], "threshold":91}}
    assert build_canonical_observation(a,"NVDA","test")[1].context_id != build_canonical_observation(b,"NVDA","test")[1].context_id

def test_real_public_last_sale_can_trigger_review_not_imply_fill():
    obs, _ = observation()
    result = apply_verified_quote_edges(obs, quote(source="cnbc_nasdaq_last_sale", quality="public_reported_last_sale", test_only=False), NOW)
    assert result["quote_edge_status"] == "VALIDATED_ADAPTER_QUOTE"
    assert result["entry_triggered"] is True
    assert "fill" not in result
    untrusted = apply_verified_quote_edges(obs, quote(source="unknown_feed", quality="public_reported_last_sale", test_only=False), NOW)
    assert untrusted["quote_edge_status"] == "BLOCKED_QUOTE_UNAVAILABLE"


def test_legacy_production_model_override_cannot_escalate(monkeypatch):
    monkeypatch.setenv("CIO_PROVIDER_ID", "openai-codex")
    with pytest.raises(ValueError, match="LEGACY_MODEL_OVERRIDE_BLOCKED"):
        HermesCIODecisionExecutor(model_id="gpt-6-astra", allow_fixture=False)
    with pytest.raises(ValueError, match="LEGACY_MODEL_OVERRIDE_BLOCKED"):
        HermesCIODecisionExecutor(model_id="gpt-6-sol", allow_fixture=False)
    default = HermesCIODecisionExecutor(allow_fixture=False)
    assert default.model_id == "gpt-6.1-sol"
    escalated = HermesCIODecisionExecutor(escalation_reason="thesis_conflict", allow_fixture=False)
    assert escalated.model_id == "gpt-6-astra"
    with pytest.raises(ValueError):
        HermesCIODecisionExecutor(escalation_reason="quota_retry", allow_fixture=False)

def test_canonical_runner_invokes_edge_helper_and_blocks_stale(tmp_path):
    from tests.test_runtime_continuity import build_harness
    from tests.test_cio_gate_canonical import CountingExecutor
    from cio_market_lab.engine.team_ops import DurableQuoteSnapshot
    now = datetime(2026,9,28,2,tzinfo=timezone.utc)
    runner, _, _, adapter = build_harness(tmp_path, [now])
    adapter.set_bar("2330.TW", now, 995, 1005, 990, 1000)
    executor = CountingExecutor(now)
    runner.cio_executor = executor; runner.material_gate_enabled = True
    value = packet(symbol="2330.TW", market="TW", observed_at=now.isoformat(), buy_zone={"low":990,"high":1010})
    obs, ctx = build_canonical_observation(value, "2330.TW", "TEST_ONLY")
    runner.frozen_decision_context = ctx
    runner.material_observation_provider = lambda **kw: obs
    snapshot = DurableQuoteSnapshot(symbol="2330.TW", market="TW", source="fixture://TEST_ONLY", observed_at=now,
        bar_time=now, quality="good", is_stale=False, is_synthetic=False, last_price=1000, session="REGULAR")
    runner.get_durable_quote = lambda symbol, *, refresh=True: snapshot
    def call():
        return runner._run_cio_decision_path("TEST_ONLY", runner.paper_orders.experiment_for("dynamic-desk"), "2330.TW", adapter.get_latest_bar("2330.TW"), {})
    first=call(); assert len(executor.requests)==1, first.model_dump(mode="json")
    assert first.inputs["quote_edges"]["entry_triggered"] is True
    call(); assert len(executor.requests)==1
    snapshot = snapshot.model_copy(update={"bar_time": now-timedelta(minutes=6)})
    stale=call(); assert len(executor.requests)==1
    assert stale.inputs["material_delta_gate"]["status"] == "BLOCKED_QUOTE_UNAVAILABLE"
