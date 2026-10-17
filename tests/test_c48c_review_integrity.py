"""Offline PAPER regressions for the independent C48b findings."""
import json
from datetime import timedelta

import pytest

from cio_market_lab.engine.cio_packet import sign_cio_packet, validate_cio_packet
from tests.test_c0048_main_handoff import evaluated_harness


@pytest.mark.parametrize("field", ["as_of", "expiry"])
def test_signed_packet_timestamps_cannot_be_changed(tmp_path, field):
    runner, _, now = evaluated_harness(tmp_path)
    packet = runner.cio_executor.request_decision.return_value
    sign_cio_packet(packet, signer_id="fixture-test-signer")
    assert validate_cio_packet(packet, now, set()) == (True, "VALID")
    setattr(packet, field, getattr(packet, field) + timedelta(minutes=1))
    valid, reason = validate_cio_packet(packet, now, set())
    assert not valid
    assert "signature" in reason


def test_coordinated_timestamp_tamper_cannot_readmit_expired_review(tmp_path):
    runner, settings, now = evaluated_harness(tmp_path)
    runner._review_daily_unarmed_plan("TEST_ONLY", settings, "MSFT")
    packet = runner.learning_store.get_record("TEST_ONLY_case").packet
    signature = packet.provenance.signature
    runner._now = lambda: now + timedelta(hours=25)
    assert runner.daily_main_review_opportunities() == []
    packet.as_of += timedelta(days=1)
    packet.expiry += timedelta(days=1)
    path = next(tmp_path.glob("daily_contexts/*/main_review.json"))
    review = json.loads(path.read_text())
    review["valid_until"] = (packet.as_of + timedelta(hours=24)).isoformat()
    path.write_text(json.dumps(review))
    assert packet.provenance.signature == signature
    assert not validate_cio_packet(packet, runner._now(), set())[0]
    assert runner.daily_main_review_opportunities() == []
    assert runner.daily_main_review_opportunities(include_expired=True) == []


def test_authentically_signed_expired_review_is_rejected(tmp_path):
    runner, settings, now = evaluated_harness(tmp_path)
    runner._review_daily_unarmed_plan("TEST_ONLY", settings, "MSFT")
    packet = runner.learning_store.get_record("TEST_ONLY_case").packet
    # A long packet expiry must not extend the daily review's own deadline.
    packet.expiry = now + timedelta(days=3)
    sign_cio_packet(packet, signer_id="fixture-test-signer")
    assert validate_cio_packet(packet, now, set())[0]
    valid, reason = validate_cio_packet(packet, now + timedelta(hours=24), set())
    assert not valid
    assert "REVIEW_EXPIRED" in reason
    runner._now = lambda: now + timedelta(hours=24)
    assert runner.daily_main_review_opportunities() == []


@pytest.mark.parametrize("missing", ["all", "needed_evidence", "price_trigger", "invalidation", "review_at"])
def test_incomplete_qualification_is_not_economics_evaluated(tmp_path, missing):
    runner, settings, now = evaluated_harness(tmp_path, "QUALIFIED_FOR_ENTRY_REVIEW")
    packet = runner.cio_executor.request_decision.return_value
    assessment = {
        "verdict": "QUALIFIED_FOR_ENTRY_REVIEW", "rationale": "Missing inputs.",
        "needed_evidence": ["TEST_ONLY official operating cash flow"],
        "price_trigger": "TEST_ONLY price within independently valued entry zone",
        "invalidation": "TEST_ONLY negative commercial validation",
        "review_at": (now + timedelta(hours=6)).isoformat(),
    }
    if missing == "all":
        assessment = {key: assessment[key] for key in ("verdict", "rationale")}
    else:
        assessment.pop(missing)
    packet.conditions["daily_research_assessment"] = assessment
    runner._review_daily_unarmed_plan("TEST_ONLY", settings, "MSFT")
    row = runner.daily_main_review_opportunities()[0]
    assert row["main_economics_evaluated"] is False
    assert row["status"] == "MAIN_REVIEW_REQUIRED_NOT_EVALUATED"
    assert row["execution_armed"] is False
    assert row["authorized_notional"] == 0


@pytest.mark.parametrize("field,bad", [
    ("needed_evidence", []), ("needed_evidence", [""]),
    ("price_trigger", "  "), ("invalidation", None),
    ("review_at", "not-a-date"), ("review_at", "2026-10-01T18:00:00"),
])
def test_empty_or_malformed_qualification_fields_are_not_evaluated(tmp_path, field, bad):
    runner, _, now = evaluated_harness(tmp_path)
    assessment = {
        "verdict": "QUALIFIED_FOR_ENTRY_REVIEW", "rationale": "TEST_ONLY valuation needs confirmation",
        "needed_evidence": ["TEST_ONLY cash flow"], "price_trigger": "TEST_ONLY entry zone",
        "invalidation": "TEST_ONLY commercial failure",
        "review_at": (now + timedelta(hours=6)).isoformat(),
    }
    assessment[field] = bad
    assert runner._daily_main_assessment_status(assessment) == ("MAIN_REVIEW_REQUIRED_NOT_EVALUATED", False)
