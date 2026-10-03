"""TEST_ONLY executor integration, not live-model acceptance."""
from datetime import datetime, timezone, timedelta
from tests.test_runtime_continuity import build_harness
from tests.test_cio_session_stage_a import _context
from cio_market_lab.domain.models import CIODecisionPacket, CIOProvenance, DecisionScope
from cio_market_lab.engine.cio_packet import sign_cio_packet

class CountingExecutor:
    def __init__(self, now):
        self.now = now
        self.requests = []
        self.fail = False
    def request_decision(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("TEST_ONLY failure")
        packet = CIODecisionPacket(case_id=f"fixture-{len(self.requests)}", as_of=self.now,
            evidence=["fixture://TEST_ONLY"], thesis="TEST_ONLY no trade", selected_instrument="2330.TW",
            action="NO_TRADE", holding_horizon=DecisionScope.SWING, quantity=0,
            expiry=self.now+timedelta(hours=1), confidence=0.5, strategy_version="fixture-v1",
            provenance=CIOProvenance(authority="MAIN_CIO", signer_id="main-cio-key", source="external_packet"), is_fixture=True)
        sign_cio_packet(packet, signer_id="main-cio-key")
        return packet

def test_canonical_material_counts_restart_context_and_retry(tmp_path):
    now = datetime(2026,9,28,2,0,tzinfo=timezone.utc)
    clock = [now]
    obs = {"official_material_ids": ["doc-a"]}
    executor = CountingExecutor(now)
    def make():
        runner, _, _, adapter = build_harness(tmp_path, clock)
        adapter.set_bar("2330.TW", now, 995,1005,990,1000)
        runner.cio_executor = executor
        runner.material_gate_enabled = True
        runner.frozen_decision_context = _context()
        runner.material_observation_provider = lambda **kw: dict(obs)
        return runner, adapter
    runner, adapter = make()
    def call():
        settings = runner.paper_orders.experiment_for("dynamic-desk")
        return runner._run_cio_decision_path("fixture-run", settings, "2330.TW", adapter.get_latest_bar("2330.TW"), {})
    call()
    assert len(executor.requests) == 1
    lesson = executor.requests[0].prior_lessons[-1]
    assert lesson["session_id"] == "project-money-main-cio"
    assert lesson["frozen_context"]["thesis"] == "fixture-only thesis"
    learned_packet = executor.request_decision(executor.requests[0])
    executor.requests.pop()  # Setup only, not a runner invocation.
    learned_packet.case_id = "observed-nonaction"
    runner.learning_store.record_decision(learned_packet, {}, {})
    runner.learning_store.record_outcome(learned_packet.case_id,
        {"no_trade_pnl": True, "no_fill": True, "realized_pnl": 0,
         "as_of": (now-timedelta(seconds=1)).isoformat(), "source": "fixture://TEST_ONLY-observed"},
        lessons=["TEST_ONLY observed non-action"], as_of=now-timedelta(seconds=1))
    obs["timestamp"] = "noise"
    call()
    assert len(executor.requests) == 1
    runner, adapter = make()
    call()
    assert len(executor.requests) == 1
    obs["official_material_ids"].append("doc-b")
    call()
    assert len(executor.requests) == 2
    assert executor.requests[-1].prior_lessons[-1]["persisted_history"]
    assert executor.requests[-1].prior_lessons[-1]["observed_lessons"][0]["case_id"] == "observed-nonaction"
    assert executor.requests[-1].prior_lessons[-1]["observed_outcomes"]
    obs.clear(); obs["invalidation_triggered"] = True
    call(); call()
    assert len(executor.requests) == 3
    obs.clear(); obs["quote"] = 1000
    call()
    assert len(executor.requests) == 3
    obs.clear(); obs["invalidation_triggered"] = True
    call()
    assert len(executor.requests) == 4
    obs.clear(); obs["official_material_ids"] = ["retry-doc"]
    executor.fail = True
    call()
    assert len(executor.requests) == 5
    executor.fail = False
    call(); call()
    assert len(executor.requests) == 6
