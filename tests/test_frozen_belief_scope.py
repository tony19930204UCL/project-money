from datetime import datetime, timezone
from tests.test_cio_owned_desk import build_test_runner
from cio_market_lab.domain.models import CIODecisionPacket, CIOProvenance, DecisionScope, Market
from cio_market_lab.engine.cio_packet import sign_cio_packet
from cio_market_lab.engine.team_ops import DurableQuoteSnapshot


def test_frozen_no_trade_does_not_refetch_or_replace_its_decision_basis(tmp_path, monkeypatch):
    now = datetime(2026, 9, 30, 3, 0, tzinfo=timezone.utc)
    runner, _, _, adapter = build_test_runner(tmp_path, [now])
    frozen = {"canonical_portfolio": {"cash": 2378465.0, "nav": 2378465.0},
              "quotes": {"2330.TW": {"last_price": 1000.0}}}
    packet = CIODecisionPacket(case_id="freeze-no-trade", as_of=now,
        selected_instrument="2330.TW", action="NO_TRADE", quantity=0,
        holding_horizon=DecisionScope.CASH, thesis="No supported entry catalyst.",
        expiry=now.replace(hour=6), strategy_version="v1", is_fixture=True,
        predecision_snapshot=frozen,
        provenance=CIOProvenance(authority="MAIN_CIO", actor_role="CHIEF_INVESTMENT_OFFICER"))
    sign_cio_packet(packet, signer_id="main-cio-key")
    def forbidden(*args, **kwargs):
        raise AssertionError("post-model network revaluation must not rewrite frozen beliefs")
    monkeypatch.setattr(runner, "generate_canonical_team_ops", forbidden)
    decision = runner._execute_cio_packet("run-frozen", runner.paper_orders.experiments["dynamic-desk"],
        packet, adapter.get_latest_bar("2330.TW"), {})
    assert decision.terminal_status == "TERMINAL_NO_TRADE"
    rec = runner.learning_store.get_record(packet.case_id)
    assert rec.pre_decision_portfolio == frozen["canonical_portfolio"]
    assert rec.pre_decision_quotes == frozen["quotes"]


def test_target_missing_still_blocks_but_closed_discovery_quote_does_not(tmp_path, monkeypatch):
    now = datetime(2026, 9, 30, 17, 0, tzinfo=timezone.utc)
    runner, _, _, _ = build_test_runner(tmp_path, [now])
    settings = runner.paper_orders.experiments["dynamic-desk"]
    settings.universe = ["2330.TW", "MSFT"]
    monkeypatch.setattr(runner, "reload_settings_if_needed", lambda: None)
    def quote(symbol, refresh=True):
        return DurableQuoteSnapshot(symbol=symbol, market=Market.US if symbol=="MSFT" else Market.TW,
            source="authoritative" if symbol=="MSFT" else "missing", observed_at=now,
            regular_price=100.0 if symbol=="MSFT" else None,
            quality="good" if symbol=="MSFT" else "missing", is_stale=symbol!="MSFT")
    monkeypatch.setattr(runner, "get_durable_quote", quote)
    snap = runner.generate_canonical_team_ops(now, refresh_symbols={"MSFT"})
    assert snap["posture"]["fail_closed"] is False
    missing_target = runner.generate_canonical_team_ops(now, refresh_symbols={"2330.TW"})
    assert missing_target["posture"]["fail_closed"] is True


def test_cio_receives_real_bar_history_with_non_execution_authority(tmp_path):
    now = datetime(2026, 9, 24, 5, 0, tzinfo=timezone.utc)
    runner, _, _, _ = build_test_runner(tmp_path, [now])
    captured = []
    class Provider:
        last_receipt = None
        def request_decision(self, context):
            captured.append(context)
            raise RuntimeError("TEST_ONLY stop before any order")
    runner.set_cio_executor(Provider())
    runner.run_one_cycle("dynamic-desk", ["2330.TW"])
    assert len(captured) == 1
    snap = captured[0].predecision_snapshot
    expected = runner.market_adapter.get_bars("2330.TW", limit=20)
    assert snap["analysis_history"] == [bar.model_dump(mode="json") for bar in expected]
    assert len(snap["analysis_history"]) == 3
    assert snap["analysis_history_authority"] == "ANALYSIS_ONLY_NOT_EXECUTION_BOOK"
    assert snap["regime_evidence"]["verified_market_regime"] is False
