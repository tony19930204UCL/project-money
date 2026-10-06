from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cio_market_lab.research.issue16_acceptance import (
    DeliveryReceiptConsumer,
    MonitorContract,
    PublicOnlyResearchWorkflowAdapter,
    ReceiptAwarePositionConsumer,
    assert_public_worker_export,
    original_role_acceptance,
)


NOW = datetime(2026, 10, 6, 7, 30, tzinfo=timezone.utc)


class FakeCoordinator:
    def __init__(self, *, empty=False, private=False):
        self.empty = empty
        self.private = private

    def refresh_symbol(self, symbol, now, reader=None):
        if self.private:
            return {
                "symbol": symbol,
                "official_facts": {
                    "record": {
                        "source_url": "https://www.sec.gov/Archives/edgar/data/1/test",
                        "observed_at": now.isoformat(),
                        "holdings": [{"symbol": symbol, "quantity": 99}],
                    }
                },
                "gaps": [],
            }
        if self.empty:
            return {
                "symbol": symbol,
                "official_facts": {"status": "SOURCE_UNAVAILABLE", "record": None},
                "secondary_finviz": {"record": None},
                "secondary_stock_analysis": {"record": None},
                "peer_market_cap": {"record": None},
                "gaps": [{"symbol": symbol, "reason": "SOURCE_UNAVAILABLE"}],
            }
        return {
            "symbol": symbol,
            "official_facts": {
                "status": "SUCCESS",
                "record": {
                    "source_url": "https://www.sec.gov/Archives/edgar/data/789019/test",
                    "observed_at": now.isoformat(),
                    "verified_facts": ["Official public filing fact"],
                    "metrics": {"Revenue": "100"},
                },
            },
            "secondary_finviz": {
                "record": {
                    "source_url": "https://finviz.com/quote.ashx?t=MSFT",
                    "observed_at": now.isoformat(),
                    "metrics": {"Market Cap": "1T"},
                }
            },
            "secondary_stock_analysis": {"record": None},
            "peer_market_cap": {"record": None},
            "gaps": [],
            "cross_checks": {"discrepancies": []},
        }


def test_original_nine_role_identity_is_independent_from_schedule_enablement():
    roles = original_role_acceptance([
        {"role": "tw_research", "enabled": False},
        {"role": "us_research", "enabled": True},
    ])
    assert set(roles) == {
        "main_cio",
        "tw_research",
        "us_research",
        "underwriting",
        "allocation",
        "source_audio",
        "industry_mapping",
        "red_team",
        "blindside",
    }
    assert all(row["identity_status"] == "CONTRACT_VALID" for row in roles.values())
    assert roles["tw_research"]["owner"] == "TW_RESEARCH"
    assert roles["tw_research"]["schedule_status"] == "SCHEDULE_DISABLED"
    assert roles["underwriting"]["schedule_status"] == "SCHEDULE_NOT_CONFIGURED"
    assert roles["main_cio"]["private_context_allowed"] is True
    assert roles["tw_research"]["private_context_allowed"] is False


def test_public_worker_export_rejects_private_holdings_orders_and_credentials():
    for payload in (
        {"holdings": [{"symbol": "VTI"}]},
        {"nested": {"orders": [{"id": "x"}]}},
        {"nested": {"api_credentials": "secret"}},
        {"account_path": "/private/runtime/account.json"},
    ):
        with pytest.raises(ValueError, match="PRIVATE_EXPORT_REJECTED"):
            assert_public_worker_export(payload)

    assert_public_worker_export({
        "symbol": "MSFT",
        "source_url": "https://www.sec.gov/test",
        "verified_facts": ["public fact"],
    })


def test_public_workflow_runs_all_bounded_stages_with_provenance_and_distinct_challenge():
    adapter = PublicOnlyResearchWorkflowAdapter(coordinator=FakeCoordinator())
    result = adapter.run("MSFT", now=NOW)

    assert result["status"] == "COMPLETED_PUBLIC_RESEARCH_CANDIDATE"
    assert result["live_acceptance_claimed"] is False
    assert result["owner"] == "MAIN_CIO"
    assert result["exact_next_action"] == "MAIN_CIO_RUN_REAL_HOST_PUBLIC_INPUT_ACCEPTANCE"
    assert result["challenge_model_distinct"] is True

    stages = [row["stage"] for row in result["attempts"]]
    assert stages == ["fetch", "discovery", "commercial", "underwriting", "challenge"]
    assert result["attempts"][0]["source_count"] == 2
    assert all(p["source_url"].startswith("https://") for p in result["source_provenance"])
    assert all(len(p["content_sha256"]) == 64 for p in result["source_provenance"])

    model_ids = {
        row["stage"]: row.get("model_identity")
        for row in result["attempts"]
        if row["stage"] != "fetch"
    }
    assert model_ids["underwriting"] == "LOCAL_UNDERWRITING_RULES_V1"
    assert model_ids["challenge"] == "LOCAL_CHALLENGE_RULES_V2"
    assert result["attempts"][-1]["output"]["verdict"] == "PASS_PUBLIC_RESEARCH_ONLY"


def test_missing_public_source_stays_blocked_without_fixture_substitution():
    adapter = PublicOnlyResearchWorkflowAdapter(coordinator=FakeCoordinator(empty=True))
    result = adapter.run("MSFT", now=NOW)

    assert result["status"] == "BLOCKED"
    assert result["live_acceptance_claimed"] is False
    assert result["exact_next_action"] == "SUPPLY_GENUINELY_FRESH_PUBLIC_INPUT"
    assert result["attempts"] == [{
        "stage": "fetch",
        "status": "BLOCKED",
        "observed_at": NOW.isoformat(),
        "source_count": 0,
        "provenance": [],
        "gaps": [{"symbol": "MSFT", "reason": "SOURCE_UNAVAILABLE"}],
    }]


def test_private_marker_is_rejected_before_research_stage_transport():
    adapter = PublicOnlyResearchWorkflowAdapter(coordinator=FakeCoordinator(private=True))
    result = adapter.run("MSFT", now=NOW)

    assert result["status"] == "BLOCKED"
    assert result["attempts"][0]["stage"] == "fetch"
    assert "PRIVATE_EXPORT_REJECTED" in result["attempts"][0]["reason"]


def test_position_consumer_covers_vti_and_keeps_identity_stable_across_poll_time():
    consumer = ReceiptAwarePositionConsumer()
    contracts = [
        MonitorContract(
            contract_id="vti-drawdown",
            symbol="VTI",
            field="drawdown",
            operator=">=",
            threshold=0.05,
            max_age_seconds=1800,
        ),
        MonitorContract(
            contract_id="msft-risk",
            symbol="MSFT",
            field="risk_score",
            operator=">",
            threshold=0.8,
            max_age_seconds=60,
        ),
        MonitorContract(
            contract_id="aapl-gap",
            symbol="AAPL",
            field="risk_score",
            operator=">",
            threshold=0.7,
            max_age_seconds=60,
        ),
    ]
    observations = {
        "VTI": {
            "drawdown": 0.06,
            "observed_at": (NOW - timedelta(minutes=5)).isoformat(),
            "receipt_id": "receipt-vti",
        },
        "MSFT": {
            "risk_score": 0.7,
            "observed_at": (NOW - timedelta(minutes=5)).isoformat(),
            "receipt_id": "receipt-msft",
        },
    }

    first = consumer.evaluate(contracts, observations, now=NOW)
    second = consumer.evaluate(contracts, observations, now=NOW + timedelta(seconds=10))

    assert first["vti_contract_covered"] is True
    assert first["private_positions_exported"] is False
    by_symbol = {r["symbol"]: r for r in first["results"]}
    assert by_symbol["VTI"]["classification"] == "FRESH"
    assert by_symbol["VTI"]["triggered"] is True
    assert by_symbol["MSFT"]["classification"] == "STALE"
    assert by_symbol["MSFT"]["triggered"] is None
    assert by_symbol["AAPL"]["classification"] == "UNKNOWN"
    assert by_symbol["AAPL"]["triggered"] is None

    assert [r["stable_identity"] for r in first["results"]] == [
        r["stable_identity"] for r in second["results"]
    ]


def test_delivery_ack_requires_exact_linkage_and_nonempty_platform_message_id():
    expected = {
        "execution_hash": "exec-1",
        "body_hash": "body-1",
        "job_id": "job-1",
        "platform": "telegram",
        "target": "main-cio-thread",
        "thread_id": "thread-7",
    }
    consumer = DeliveryReceiptConsumer()

    missing = consumer.consume(expected, None)
    assert missing == {
        "status": "UNKNOWN",
        "acknowledged": False,
        "replay_permitted": False,
        "reason": "RECEIPT_MISSING",
    }

    delivered_only = consumer.consume(expected, {
        **expected,
        "transport_status": "DELIVERED",
        "delivered": True,
        "platform_message_id": "",
    })
    assert delivered_only["status"] == "UNKNOWN"
    assert delivered_only["reason"] == "DELIVERED_WITHOUT_LINKED_PLATFORM_PROOF"
    assert delivered_only["replay_permitted"] is False

    unknown_transport = consumer.consume(expected, {
        **expected,
        "transport_status": "UNKNOWN",
        "platform_message_id": "msg-x",
    })
    assert unknown_transport["status"] == "UNKNOWN"
    assert unknown_transport["replay_permitted"] is False

    mismatch = consumer.consume(expected, {
        **expected,
        "body_hash": "wrong",
        "transport_status": "ACKNOWLEDGED",
        "platform_message_id": "msg-1",
    })
    assert mismatch["status"] == "UNKNOWN"
    assert "body_hash" in mismatch["reason"]

    receipt = {
        **expected,
        "transport_status": "ACKNOWLEDGED",
        "delivered": True,
        "platform_message_id": "msg-1",
    }
    accepted = consumer.consume(expected, receipt)
    assert accepted["status"] == "ACKNOWLEDGED"
    assert accepted["acknowledged"] is True
    assert accepted["replay_permitted"] is False

    duplicate = consumer.consume(expected, receipt)
    assert duplicate["status"] == "ACKNOWLEDGED_DUPLICATE"
    assert duplicate["receipt_identity"] == accepted["receipt_identity"]
    assert duplicate["replay_permitted"] is False
