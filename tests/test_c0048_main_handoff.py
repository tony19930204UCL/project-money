"""Offline PAPER regression. Fixture transport is not independent Main acceptance."""
import copy
import json
from datetime import timedelta

import pytest

from tests.test_daily_wait_formal_case import harness
from cio_market_lab.domain.models import DecisionScope
from cio_market_lab.engine.cio_packet import sign_cio_packet, validate_cio_packet
from cio_market_lab.engine.automation_policy import AutomationRiskPolicy, ExitAction
from cio_market_lab.engine.decision_learning import CIODecisionLearningStore


def evaluated_harness(tmp_path, verdict="WAIT"):
    runner, settings, now = harness(tmp_path)
    packet = runner.cio_executor.request_decision.return_value
    packet.conditions["daily_research_assessment"] = {
        "verdict": verdict, "rationale": "TEST_ONLY economics need uncertainty discount",
        "needed_evidence": ["TEST_ONLY official cash flow"],
        "price_trigger": "TEST_ONLY independently valued entry zone",
        "invalidation": "TEST_ONLY commercial failure",
        "review_at": (now + timedelta(hours=6)).isoformat(),
    }

    def signed_response(request):
        packet.predecision_snapshot = copy.deepcopy(request.predecision_snapshot)
        sign_cio_packet(packet, signer_id="fixture-test-signer")
        return packet

    runner.cio_executor.request_decision.side_effect = signed_response
    return runner, settings, now


def test_main_receives_economics_before_response(tmp_path):
    runner, settings, _ = evaluated_harness(tmp_path)
    runner._review_daily_unarmed_plan("TEST_ONLY", settings, "MSFT")
    request = runner.cio_executor.request_decision.call_args.args[0]
    opportunity = request.research_opportunities[0]
    assert opportunity["economics"]["thesis"]
    assert opportunity["official_source_lineage"]
    assert opportunity["economics"]["uncertainties"] == ["TEST_ONLY valuation"]
    assert not opportunity["execution_armed"] and opportunity["authorized_notional"] == 0
    assert request.predecision_snapshot["allowed_actions"] == ["NO_TRADE", "REJECT"]


@pytest.mark.parametrize("verdict,status", [
    ("WAIT", "MAIN_REVIEW_WAIT"), ("REJECT", "MAIN_REVIEW_REJECTED"),
    ("QUALIFIED_FOR_ENTRY_REVIEW", "MAIN_ENTRY_REVIEW_REQUIRED"),
])
def test_signed_review_persists_without_arming(tmp_path, verdict, status):
    runner, settings, _ = evaluated_harness(tmp_path, verdict)
    decision = runner._review_daily_unarmed_plan("TEST_ONLY", settings, "MSFT")
    assert decision["action"] == "NO_TRADE"
    rows = runner.daily_main_review_opportunities()
    assert len(rows) == 1 and rows[0]["status"] == status
    assert rows[0]["main_economics_evaluated"] is True
    assert rows[0]["execution_armed"] is False and rows[0]["authorized_notional"] == 0
    assert not runner.learning_store.get_record("TEST_ONLY_case").fill
    runner.learning_store = CIODecisionLearningStore(tmp_path / "learning")
    assert runner.daily_main_review_opportunities()[0]["source_case_id"] == "TEST_ONLY_case"


def test_missing_assessment_is_not_fabricated_qualification(tmp_path):
    runner, settings, _ = evaluated_harness(tmp_path)
    runner.cio_executor.request_decision.return_value.conditions.pop("daily_research_assessment")
    runner._review_daily_unarmed_plan("TEST_ONLY", settings, "MSFT")
    row = runner.daily_main_review_opportunities()[0]
    assert row["status"] == "MAIN_REVIEW_REQUIRED_NOT_EVALUATED"
    assert row["main_economics_evaluated"] is False


@pytest.mark.parametrize("defect", ["absent", "unverified", "wrong_case"])
def test_bad_receipt_cannot_persist_or_expose_handoff(tmp_path, defect):
    runner, settings, _ = evaluated_harness(tmp_path)
    if defect == "absent": runner.cio_executor.last_receipt = None
    if defect == "unverified": runner.cio_executor.last_receipt.readback_verified = False
    if defect == "wrong_case": runner.cio_executor.last_receipt.case_id = "DETACHED"
    decision = runner._review_daily_unarmed_plan("TEST_ONLY", settings, "MSFT")
    assert decision["terminal_status"] == "TERMINAL_RISK_BLOCK"
    assert not list(tmp_path.glob("daily_contexts/*/main_review.json"))
    assert not runner.learning_store.processed_case_ids


def test_retry_and_expiry_do_not_renew_deadline(tmp_path):
    runner, settings, now = evaluated_harness(tmp_path)
    runner._review_daily_unarmed_plan("TEST_ONLY", settings, "MSFT")
    path = next(tmp_path.glob("daily_contexts/*/main_review.json"))
    original = path.read_bytes()
    runner._now = lambda: now + timedelta(hours=1)
    runner._review_daily_unarmed_plan("TEST_ONLY_retry", settings, "MSFT")
    assert path.read_bytes() == original
    assert runner.cio_executor.request_decision.call_count == 1
    runner._now = lambda: now + timedelta(hours=25)
    assert runner.daily_main_review_opportunities() == []
    row = runner.daily_main_review_opportunities(include_expired=True)[0]
    assert row["review_state"] == "REVIEW_OVERDUE_NOT_AUTO_RENEWED"
    assert path.read_bytes() == original


@pytest.mark.parametrize("mutation", ["economics", "assessment", "case", "context", "armed", "notional", "fixture", "signature", "deadline", "horizon", "status", "lineage", "fixture_laundering", "strategy"])
def test_detached_or_escalated_rows_rejected(tmp_path, mutation):
    runner, settings, _ = evaluated_harness(tmp_path)
    runner._review_daily_unarmed_plan("TEST_ONLY", settings, "MSFT")
    path = next(tmp_path.glob("daily_contexts/*/main_review.json"))
    row = json.loads(path.read_text())
    if mutation == "economics": row["economics"]["thesis"] = "UNTRUSTED"
    if mutation == "assessment": row["main_assessment"]["verdict"] = "QUALIFIED_FOR_ENTRY_REVIEW"
    if mutation == "case": row["source_receipt"]["case_id"] = "DETACHED"
    if mutation == "context": row["frozen_context_sha256"] = "DETACHED"
    if mutation == "armed": row["execution_armed"] = True
    if mutation == "notional": row["authorized_notional"] = 9999
    if mutation == "fixture": runner.allow_fixture_quotes = False
    if mutation == "signature": runner.learning_store.get_record("TEST_ONLY_case").packet.provenance.signature = "BAD"
    if mutation == "deadline": row["valid_until"] = "2099-01-01T00:00:00+00:00"
    if mutation == "horizon": row["horizon"] = "long_term"
    if mutation == "status": row["status"] = "MAIN_ENTRY_REVIEW_REQUIRED"
    if mutation == "lineage": row["source_lineage"] = ["https://attacker.invalid/not-official"]
    if mutation == "strategy": row["strategy_id"] = "DETACHED_STRATEGY"
    if mutation == "fixture_laundering":
        row["is_fixture"] = False
        runner.allow_fixture_quotes = False
    path.write_text(json.dumps(row))
    assert runner.daily_main_review_opportunities(include_expired=True) == []


@pytest.mark.parametrize("mutation", ["assessment", "snapshot", "horizon"])
def test_signature_binds_assessment_snapshot_and_horizon(tmp_path, mutation):
    runner, settings, now = evaluated_harness(tmp_path)
    runner._review_daily_unarmed_plan("TEST_ONLY", settings, "MSFT")
    packet = runner.learning_store.get_record("TEST_ONLY_case").packet
    assert validate_cio_packet(packet, now, set())[0]
    if mutation == "assessment": packet.conditions["daily_research_assessment"]["verdict"] = "REJECT"
    if mutation == "snapshot": packet.predecision_snapshot["frozen_daily_context"]["thesis"] = "TAMPERED"
    if mutation == "horizon": packet.holding_horizon = DecisionScope.LONG_TERM
    assert not validate_cio_packet(packet, now, set())[0]


def test_missing_assessment_still_binds_frozen_source_and_horizon(tmp_path):
    runner, settings, now = evaluated_harness(tmp_path)
    runner.cio_executor.request_decision.return_value.conditions.pop("daily_research_assessment")
    runner._review_daily_unarmed_plan("TEST_ONLY", settings, "MSFT")
    packet = runner.learning_store.get_record("TEST_ONLY_case").packet
    assert packet.predecision_snapshot["decision_type"] == "DAILY_RESEARCH_REVIEW"
    assert validate_cio_packet(packet, now, set())[0]
    packet.holding_horizon = DecisionScope.LONG_TERM
    assert not validate_cio_packet(packet, now, set())[0]


def test_live_response_without_signed_snapshot_is_blocked(tmp_path):
    runner, settings, _ = evaluated_harness(tmp_path)
    packet = runner.cio_executor.request_decision.return_value
    packet.is_fixture = False
    packet.conditions.pop("daily_research_assessment")
    runner.cio_executor.request_decision.side_effect = None
    sign_cio_packet(packet, signer_id="main-cio")
    result = runner._review_daily_unarmed_plan("TEST_ONLY", settings, "MSFT")
    assert result["terminal_status"] == "TERMINAL_RISK_BLOCK"
    assert "DAILY_PLAN_LIVE_SIGNED_SNAPSHOT_REQUIRED" in result["inputs"]["validation_errors"]
    assert not runner.learning_store.processed_case_ids


@pytest.mark.parametrize("horizon,duration", [(DecisionScope.INTRADAY, timedelta(hours=6)), (DecisionScope.SWING, timedelta(days=7)), (DecisionScope.LONG_TERM, timedelta(days=30))])
def test_observation_clock_is_not_order_expiry(tmp_path, horizon, duration):
    runner, settings, now = evaluated_harness(tmp_path)
    packet = runner.cio_executor.request_decision.return_value
    packet.holding_horizon = horizon
    runner._review_daily_unarmed_plan("TEST_ONLY", settings, "MSFT")
    record = runner.learning_store.get_record(packet.case_id)
    assert record.packet.conditions["observation_deadline"] == (now + duration).isoformat()
    assert record.packet.conditions["thesis_review_horizon"] == horizon.value
    assert record.packet.conditions["entry_order_expiry_liquidates_position"] is False
    assert runner.daily_main_review_opportunities()[0]["horizon"] == horizon.value


def test_horizon_risk_policy_preserves_downside_and_separates_high_water(tmp_path):
    _, _, now = harness(tmp_path)
    policy = AutomationRiskPolicy()
    args = dict(symbol="MSFT", quantity=2, average_entry_price=100, now=now)
    assert policy.evaluate(bucket=DecisionScope.INTRADAY, current_price=120, **args).reason == "TAKE_PROFIT"
    assert policy.evaluate(bucket=DecisionScope.LONG_TERM, current_price=120, **args).action == ExitAction.HOLD
    assert policy.evaluate(bucket=DecisionScope.SWING, current_price=101, **args).action == ExitAction.HOLD
    assert policy.evaluate(bucket=DecisionScope.LONG_TERM, current_price=80, **args).reason == "LONG_TERM_CONFIGURED_RISK_STOP"
    for reason in ("thesis_broken", "portfolio_risk_breach", "explicit_main_exit"):
        assert policy.evaluate(bucket=DecisionScope.LONG_TERM, current_price=120, **args, **{reason: True}).action == ExitAction.EXIT
    args["now"] = now + timedelta(days=1)
    assert policy.evaluate(bucket=DecisionScope.SWING, current_price=101, **args).action == ExitAction.HOLD
