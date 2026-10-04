from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from cio_market_lab.api.app import create_app
from cio_market_lab.engine.daily_research_plan import DailyResearchPlanProducer
from cio_market_lab.engine.decision_learning import CIODecisionLearningStore
from cio_market_lab.engine.learning_readback import read_learning_snapshot
from cio_market_lab.engine.paper_orders import PaperExperimentSettings
from cio_market_lab.engine.stage_d_observation import (
    PersistedResearchPacketLoader,
    build_canonical_observation,
    make_packet_observation_provider,
)
from cio_market_lab.research.browser import PublicResearchInboxReader
from cio_market_lab.research.official import (
    SEC_TICKERS_URL,
    TW_BALANCE_URL,
    TW_FINANCIAL_URL,
    TW_REVENUE_URL,
)

from tests.integration.research_learning_chain_helper import (
    ChainMarketAdapter,
    TestOnlyDailyExecutor,
)

ROOT = Path(__file__).resolve().parents[2]
HELPER = Path(__file__).with_name("research_learning_chain_helper.py")
T0 = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, data, status_code=200, content_type="application/json"):
        self.data = data
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self.content = (
            json.dumps(data, ensure_ascii=False, sort_keys=True).encode()
            if content_type == "application/json"
            else bytes(data)
        )

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"TEST_ONLY_HTTP_{self.status_code}")

    def json(self):
        return self.data


def _official_data(url: str):
    if url == TW_REVENUE_URL:
        return [{
            "公司代號": "2330",
            "公司名稱": "台積電 TEST_ONLY",
            "出表日期": "1150908",
            "資料年月": "11508",
            "營業收入-當月營收": "1000000",
            "單位": "仟元",
        }]
    if url == TW_FINANCIAL_URL:
        return [{
            "公司代號": "2330",
            "公司名稱": "台積電 TEST_ONLY",
            "出表日期": "1150814",
            "資料年月": "115Q2",
            "年度": "115",
            "季別": "2",
            "營業收入": "2000000",
            "本期淨利": "300000",
            "單位": "仟元",
        }]
    if url == TW_BALANCE_URL:
        return [{
            "公司代號": "2330",
            "出表日期": "1150814",
            "年度": "115",
            "季別": "2",
            "資產總額": "9000000",
            "單位": "仟元",
        }]
    if url == SEC_TICKERS_URL:
        return {"0": {"ticker": "MSFT", "cik_str": 789019}}
    if "companyfacts/CIK0000789019.json" in url:
        fact = {
            "val": 65000000000,
            "start": "2026-04-01",
            "end": "2026-06-30",
            "filed": "2026-07-30",
            "form": "10-Q",
            "fy": 2026,
            "fp": "Q2",
            "accn": "0000789019-26-000001",
        }
        return {
            "cik": 789019,
            "entityName": "Microsoft TEST_ONLY",
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {"USD": [fact]}
                    }
                }
            },
        }
    raise KeyError(url)


def _install_offline_model(monkeypatch):
    plan = {
        "thesis": "TEST_ONLY official-shaped research remains incomplete for an action",
        "valuation_scenarios": {},
        "catalysts": [],
        "buy_zone": None,
        "invalidation": "TEST_ONLY wait for refreshed official valuation evidence",
        "invalidation_condition": None,
        "exposure_ceiling": 0,
        "stance": "WAIT",
        "missing_evidence": ["TEST_ONLY valuation evidence"],
        "review_trigger": "TEST_ONLY next official disclosure",
    }

    def fake_chat(*args, **kwargs):
        return {
            "response": json.dumps(plan),
            "runtime_metadata": {"TEST_ONLY": True},
            "session_id": "TEST_ONLY_DAILY_PLAN_MODEL",
            "returncode": 0,
            "is_fixture": False,
            "failed": False,
            "error": None,
        }

    monkeypatch.setattr(
        "cio_market_lab.integrations.hermes_chat.run_hermes_cli_chat",
        fake_chat,
    )
    monkeypatch.setattr(
        "cio_market_lab.integrations.runtime_evidence.RuntimeEvidenceAdapter.verify_runtime_evidence",
        lambda self, **kwargs: SimpleNamespace(
            is_fixture=False,
            auth_verified=True,
            is_success_response=True,
        ),
    )


def _producer(tmp_path: Path, monkeypatch, clock, learning_store):
    _install_offline_model(monkeypatch)
    reader = PublicResearchInboxReader(tmp_path / "runtime" / "research_inbox")
    producer = DailyResearchPlanProducer(
        root=tmp_path / "research",
        packet_root=tmp_path / "packets",
        session_id="TEST_ONLY_CHAIN_SESSION",
        workspace_root=str(tmp_path),
        learning_store=learning_store,
        reader=reader,
        now_fn=lambda: clock[0],
    )

    def fake_get(url, *args, **kwargs):
        try:
            return FakeResponse(_official_data(url))
        except KeyError:
            return FakeResponse(b"TEST_ONLY optional disclosure unavailable", 404, "text/html")

    monkeypatch.setattr(producer.network, "get", fake_get)
    return producer, reader


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_official_shaped_producer_freeze_and_input_gaps(tmp_path, monkeypatch):
    clock = [T0]
    store = CIODecisionLearningStore(tmp_path / "runtime")
    producer, reader = _producer(tmp_path, monkeypatch, clock, store)

    tw = producer.refresh("2330.TW", now=clock[0])
    us = producer.refresh("MSFT", now=clock[0])
    assert tw["status"] == "AUTHENTICATED_RESEARCH_ONLY_PLAN"
    assert us["status"] == "AUTHENTICATED_RESEARCH_ONLY_PLAN"

    tw_packet = json.loads((producer.packet_root / "2330.TW.json").read_text())
    us_packet = json.loads((producer.packet_root / "MSFT.json").read_text())
    assert tw_packet["source_url"] == TW_FINANCIAL_URL
    assert tw_packet["raw_metadata"]["raw_row"]["單位"] == "仟元"
    assert "source-reported" in " ".join(tw_packet["verified_facts"])
    baseline = us_packet["raw_metadata"]["financial_baseline"]
    assert baseline and {item["unit"] for item in baseline} == {"USD"}
    assert all(item["start"] and item["end"] for item in baseline)

    loader = PersistedResearchPacketLoader(
        producer.packet_root,
        trusted_manifest=producer.manifest,
    )
    direct_packet = loader.load("MSFT", clock[0])
    direct_observation, direct_context = build_canonical_observation(
        direct_packet, "MSFT", "TEST_ONLY_CHAIN_SESSION"
    )
    scheduled = make_packet_observation_provider(
        producer.packet_root,
        trusted_manifest=producer.manifest,
    )
    scheduled_observation = scheduled(symbol="MSFT", inputs={}, now=clock[0])
    scheduled_context = scheduled.context_for("MSFT")
    assert scheduled_observation == direct_observation
    assert scheduled_context.model_dump(mode="json") == direct_context.model_dump(mode="json")

    frozen_plan = Path(us["plan_path"])
    frozen_hash = _sha(frozen_plan)
    cached = producer.refresh("MSFT", now=clock[0])
    assert cached["status"] == "CACHED_IMMUTABLE_PLAN"
    assert _sha(frozen_plan) == frozen_hash

    clock[0] = T0 + timedelta(days=1)
    refreshed = producer.refresh("MSFT", now=clock[0])
    assert refreshed["plan_path"] != str(frozen_plan)
    assert _sha(frozen_plan) == frozen_hash
    next_packet = loader.load("MSFT", clock[0])
    _, next_context = build_canonical_observation(
        next_packet, "MSFT", "TEST_ONLY_CHAIN_SESSION"
    )
    assert next_context.context_id != direct_context.context_id

    rejected, reason = reader.add_evidence({
        "research_id": "TEST_ONLY_SOCIAL_ONLY",
        "symbol": "MSFT",
        "source_url": "https://reddit.com/r/TEST_ONLY",
        "source_tier": "social",
        "observed_at": clock[0].isoformat(),
        "is_fixture": False,
        "verification_status": "unverified",
        "verified_facts": ["TEST_ONLY social narrative"],
    }, now=clock[0])
    assert rejected is False
    assert "REJECTED" in reason
    verified, _ = reader.get_verified_research_for_symbols(["MSFT"], now=clock[0])
    assert all(item["research_id"] != "TEST_ONLY_SOCIAL_ONLY" for item in verified)

    bad_root = tmp_path / "bad"
    bad = DailyResearchPlanProducer(
        root=bad_root,
        packet_root=bad_root / "packets",
        session_id="TEST_ONLY_BAD",
        workspace_root=str(tmp_path),
        learning_store=store,
        reader=PublicResearchInboxReader(bad_root / "inbox"),
        now_fn=lambda: clock[0],
    )

    def missing_fields_get(url, *args, **kwargs):
        if url == SEC_TICKERS_URL:
            return FakeResponse({"0": {"ticker": "MSFT", "cik_str": 789019}})
        if "companyfacts/CIK0000789019.json" in url:
            return FakeResponse({"cik": 789019, "entityName": "MSFT TEST_ONLY", "facts": {}})
        return FakeResponse({}, 404)

    monkeypatch.setattr(bad.network, "get", missing_fields_get)
    with pytest.raises(RuntimeError, match="official source intake unavailable"):
        bad.refresh("MSFT", now=clock[0])
    assert not (bad.packet_root / "MSFT.json").exists()


def test_full_research_decision_outcome_lesson_fresh_process_chain(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    clock = [T0]
    store = CIODecisionLearningStore(runtime)
    producer, reader = _producer(tmp_path, monkeypatch, clock, store)
    initial_refresh = producer.refresh("MSFT", now=clock[0])
    old_plan = Path(initial_refresh["plan_path"])
    old_hash = _sha(old_plan)

    adapter = ChainMarketAdapter(clock[0], price=100.0)
    app = create_app(
        workspace_root=ROOT,
        runtime_dir=runtime,
        fixture_mode=True,
        is_read_only=False,
        market_adapter=adapter,
    )
    runner = app.state.app_state.runner
    try:
        runner._now_fn = lambda: clock[0]
        runner.allow_fixture_quotes = True
        runner.cio_session_id = "TEST_ONLY_CHAIN_SESSION"
        runner.material_observation_provider = make_packet_observation_provider(
            producer.packet_root,
            trusted_manifest=producer.manifest,
        )
        settings = PaperExperimentSettings(
            strategy_id="TEST_ONLY_CHAIN",
            enabled=True,
            market="US",
            base_currency="USD",
            initial_cash=100000.0,
            universe=["MSFT"],
        )
        runner.configure(settings)
        executor = TestOnlyDailyExecutor(clock[0], "TEST_ONLY_CHAIN_CASE_1")
        runner.set_cio_executor(executor)

        decision = runner._review_daily_unarmed_plan(
            "TEST_ONLY_INITIAL",
            settings,
            "MSFT",
        )
        assert decision.action == "NO_TRADE"
        assert executor.call_count == 1
        assert executor.last_request.prior_lessons == []
        assert executor.last_request.verified_research
        assert all(
            item["source_url"].startswith("https://")
            and "reddit.com" not in item["source_url"]
            for item in executor.last_request.verified_research
        )

        first = runner.learning_store.get_record("TEST_ONLY_CHAIN_CASE_1")
        assert first is not None and first.status == "ACTIVE"
        assert first.applied_lesson_ids == []
        assert first.decision_delta == {}
        assert first.pre_decision_quotes["MSFT"] == 100.0
        due = datetime.fromisoformat(first.packet.conditions["observation_deadline"])

        repeated = runner._review_daily_unarmed_plan(
            "TEST_ONLY_INITIAL_REPEAT",
            settings,
            "MSFT",
        )
        assert repeated.action == "NO_TRADE"
        assert executor.call_count == 1
        assert list(runner.learning_store._records).count("TEST_ONLY_CHAIN_CASE_1") == 1

        clock[0] = due - timedelta(seconds=1)
        adapter.set_now(clock[0])
        assert runner.evaluate_elapsed_non_actions("MSFT") == []
        assert runner.learning_store.get_record(first.case_id).status == "ACTIVE"

        clock[0] = due
        adapter.set_now(clock[0])
        adapter.quote = None
        assert runner.evaluate_elapsed_non_actions("MSFT") == []
        assert runner.learning_store.get_record(first.case_id).status == "ACTIVE"

        adapter.quote = Quote(
            symbol="MSFT",
            timestamp=due + timedelta(seconds=1),
            observed_at=due + timedelta(seconds=1),
            bid=109.9,
            ask=110.1,
            bid_size=10,
            ask_size=10,
            last_price=110.0,
            last_size=1,
            source="TEST_ONLY_RESEARCH_CHAIN",
            quality="good",
            is_fixture=True,
            is_stale=False,
            is_synthetic=False,
        )
        assert runner.evaluate_elapsed_non_actions("MSFT") == []
        assert runner.learning_store.get_record(first.case_id).status == "ACTIVE"

        adapter.quote = adapter.quote.model_copy(update={
            "timestamp": due,
            "observed_at": due,
        })
        assert runner.evaluate_elapsed_non_actions("MSFT") == [first.case_id]
        closed = runner.learning_store.get_record(first.case_id)
        assert closed.status == "CLOSED"
        assert closed.outcome["no_fill"] is True
        assert closed.outcome["no_trade_pnl"] is True
        assert closed.outcome["realized_pnl"] == 0
        assert closed.outcome["counterfactual_price_change_pct"] == 10.0
        assert len(runner.learning_store._lessons) == 1
        lesson_id = runner.learning_store._lessons[0].lesson_id

        duplicate = runner.learning_store.record_outcome(
            first.case_id,
            dict(closed.outcome),
            lessons=list(closed.lessons),
            as_of=due,
        )
        assert duplicate.lesson_id == lesson_id
        assert len(runner.learning_store._lessons) == 1

        # Filter-only negative setup: these records are not used as chain proof.
        # They exercise existing public store filtering for future/superseded lessons.
        future_packet = executor.request_decision(executor.last_request).model_copy(update={
            "case_id": "TEST_ONLY_FUTURE_FILTER",
            "as_of": due + timedelta(days=2),
            "expiry": due + timedelta(days=2, hours=1),
        })
        runner.learning_store.record_decision(future_packet, {}, {})
        runner.learning_store.record_outcome(
            future_packet.case_id,
            {
                "realized_pnl": 0,
                "no_fill": True,
                "no_trade_pnl": True,
                "as_of": (due + timedelta(days=2)).isoformat(),
            },
            lessons=["TEST_ONLY future lesson"],
            as_of=due + timedelta(days=2),
        )
        assert lesson_id in {
            item["lesson_id"]
            for item in runner.learning_store.retrieve_context_lessons(
                "MSFT", due + timedelta(minutes=1)
            )
        }
        assert "TEST_ONLY_FUTURE_FILTER" not in {
            item["case_id"]
            for item in runner.learning_store.retrieve_context_lessons(
                "MSFT", due + timedelta(minutes=1)
            )
        }

        superseded_packet = executor.request_decision(executor.last_request).model_copy(update={
            "case_id": "TEST_ONLY_SUPERSEDED_FILTER",
            "as_of": due - timedelta(minutes=2),
            "expiry": due + timedelta(hours=1),
        })
        runner.learning_store.record_decision(superseded_packet, {}, {})
        runner.learning_store.record_outcome(
            superseded_packet.case_id,
            {
                "realized_pnl": 0,
                "no_fill": True,
                "no_trade_pnl": True,
                "as_of": (due - timedelta(minutes=1)).isoformat(),
            },
            lessons=["TEST_ONLY superseded integration lesson"],
            as_of=due - timedelta(minutes=1),
        )
        replacement_packet = superseded_packet.model_copy(update={
            "case_id": "TEST_ONLY_SUPERSEDING_CASE",
            "as_of": due,
            "expiry": due + timedelta(hours=1),
        })
        runner.learning_store.version_hypothesis(
            superseded_packet.case_id,
            replacement_packet,
            "TEST_ONLY supersession filter",
        )
        eligible_ids = {
            item["lesson_id"]
            for item in runner.learning_store.retrieve_context_lessons(
                "MSFT", due + timedelta(minutes=1)
            )
        }
        assert lesson_id in eligible_ids
        assert not any(
            item["case_id"] == "TEST_ONLY_SUPERSEDED_FILTER"
            for item in runner.learning_store.retrieve_context_lessons(
                "MSFT", due + timedelta(minutes=1)
            )
        )

        # Refresh only the new session input; prior frozen receipt remains immutable.
        clock[0] = due + timedelta(minutes=1)
        adapter.set_now(clock[0])
        producer.refresh("MSFT", now=clock[0])
        assert _sha(old_plan) == old_hash
    finally:
        runner.shutdown()

    proc = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--workspace-root", str(ROOT),
            "--runtime-dir", str(runtime),
            "--packet-root", str(producer.packet_root),
            "--manifest", str(producer.manifest),
            "--now", clock[0].isoformat(),
            "--lesson-id", lesson_id,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    result = json.loads(proc.stdout.splitlines()[-1])
    assert result["decision_action"] == "NO_TRADE"
    assert result["executor_calls"] == 1
    assert result["prior_lesson_ids"].count(lesson_id) == 1
    assert all(item["source_url"].startswith("https://") for item in result["verified_research"])

    record = result["record"]
    assert record["applied_lesson_ids"] == [lesson_id]
    assert record["decision_delta"]["lesson_id"] == lesson_id
    assert record["conditions"]["delivered_lesson_ids"] == [lesson_id]

    cases = {item["case_id"]: item for item in result["readback"]["cases"]}
    first_readback = cases["TEST_ONLY_CHAIN_CASE_1"]
    next_readback = cases["TEST_ONLY_CHAIN_CASE_2"]
    assert first_readback["lesson_used"] is False
    assert first_readback["applied_lesson_ids"] == []
    assert next_readback["lesson_used"] is True
    assert next_readback["applied_lesson_ids"] == [lesson_id]
    assert next_readback["decision_delta"]["lesson_id"] == lesson_id

    final_store = CIODecisionLearningStore(runtime)
    assert len([x for x in final_store._lessons if x.case_id == "TEST_ONLY_CHAIN_CASE_1"]) == 1
    assert final_store.get_record("TEST_ONLY_CHAIN_CASE_2").applied_lesson_ids == [lesson_id]
