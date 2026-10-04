from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from cio_market_lab.api.app import create_app
from cio_market_lab.domain.models import Bar, CIODecisionPacket, CIOExecutionReceipt, DecisionScope, Quote
from cio_market_lab.engine.cio_packet import sign_cio_packet
from cio_market_lab.engine.learning_readback import read_learning_snapshot
from cio_market_lab.engine.paper_orders import PaperExperimentSettings
from cio_market_lab.engine.stage_d_observation import make_packet_observation_provider


class ChainMarketAdapter:
    source_name = "TEST_ONLY_RESEARCH_CHAIN"

    def __init__(self, now: datetime, price: float = 100.0) -> None:
        self.now = now
        self.price = price
        self.quote = None
        self.last_fetch_mode = "TEST_ONLY"
        self.last_error = None

    def set_now(self, now: datetime) -> None:
        self.now = now

    def get_bars(self, symbol, start=None, end=None, timeframe="1D", limit=80):
        return [
            Bar(
                symbol=symbol,
                timestamp=self.now - timedelta(minutes=1),
                observed_at=self.now - timedelta(minutes=1),
                open=self.price,
                high=self.price,
                low=self.price,
                close=self.price,
                volume=1,
                source=self.source_name,
                quality="TEST_ONLY",
                is_fixture=True,
                is_synthetic=False,
            )
        ]

    def get_latest_bar(self, symbol):
        return self.get_bars(symbol, limit=1)[-1]

    def get_latest_quote(self, symbol):
        return self.quote

    def timeframe_metadata(self, timeframe):
        return {"timeframe": timeframe, "interval": "TEST_ONLY"}


class TestOnlyDailyExecutor:
    def __init__(self, now: datetime, case_id: str, lesson_id: str | None = None) -> None:
        self.now = now
        self.case_id = case_id
        self.lesson_id = lesson_id
        self.call_count = 0
        self.last_request = None
        self.last_receipt = None

    def is_available(self):
        return True

    def request_decision(self, request):
        self.call_count += 1
        self.last_request = request
        conditions = {}
        if self.lesson_id is not None:
            conditions = {
                "applied_lesson_ids": [self.lesson_id],
                "applied_lesson_reasons": {
                    self.lesson_id: "TEST_ONLY prior matured lesson changes evidence handling",
                },
                "applied_lesson_evidence": {
                    self.lesson_id: "TEST_ONLY lesson was present in the real caller request",
                },
                "decision_delta": {
                    "kind": "TEST_ONLY_LESSON_APPLICATION",
                    "lesson_id": self.lesson_id,
                    "change": "retain NO_TRADE pending refreshed official evidence",
                },
            }
        packet = CIODecisionPacket(
            case_id=self.case_id,
            as_of=self.now,
            evidence=["TEST_ONLY_FROZEN_RESEARCH"],
            thesis="TEST_ONLY official evidence review remains non-actionable",
            selected_instrument="MSFT",
            action="NO_TRADE",
            holding_horizon=DecisionScope.SWING,
            quantity=0,
            conditions=conditions,
            expiry=self.now + timedelta(hours=1),
            confidence=0.5,
            strategy_version="TEST_ONLY_CHAIN_V1",
            is_fixture=True,
        )
        packet = sign_cio_packet(packet, signer_id="fixture-test-signer")
        self.last_receipt = CIOExecutionReceipt(
            receipt_id=f"TEST_ONLY_RECEIPT_{self.case_id}",
            case_id=self.case_id,
            session_id="TEST_ONLY_CHAIN_SESSION",
            provider_id="TEST_ONLY",
            model_id="TEST_ONLY",
            timestamp=self.now,
            raw_prompt_hash="TEST_ONLY_PROMPT",
            raw_response_hash="TEST_ONLY_RESPONSE",
            readback_verified=True,
            evidence_strength="TEST_ONLY",
        )
        return packet


def run_next(args) -> dict:
    now = datetime.fromisoformat(args.now)
    adapter = ChainMarketAdapter(now)
    app = create_app(
        workspace_root=Path(args.workspace_root),
        runtime_dir=Path(args.runtime_dir),
        fixture_mode=True,
        is_read_only=False,
        market_adapter=adapter,
    )
    state = app.state.app_state
    runner = state.runner
    try:
        runner._now_fn = lambda: now
        runner.allow_fixture_quotes = True
        runner.cio_session_id = "TEST_ONLY_CHAIN_SESSION"
        runner.material_observation_provider = make_packet_observation_provider(
            Path(args.packet_root),
            trusted_manifest=Path(args.manifest),
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
        executor = TestOnlyDailyExecutor(now, "TEST_ONLY_CHAIN_CASE_2", args.lesson_id)
        runner.set_cio_executor(executor)

        decision = runner._review_daily_unarmed_plan(
            "TEST_ONLY_FRESH_PROCESS",
            settings,
            "MSFT",
        )
        record = runner.learning_store.get_record("TEST_ONLY_CHAIN_CASE_2")
        snapshot = read_learning_snapshot(
            Path(args.runtime_dir),
            as_of=now + timedelta(seconds=1),
            symbols=["MSFT"],
        )
        prior_ids = [
            item.get("lesson_id") for item in executor.last_request.prior_lessons
            if isinstance(item, dict)
        ]
        return {
            "decision_action": getattr(decision, "action", None),
            "executor_calls": executor.call_count,
            "prior_lesson_ids": prior_ids,
            "verified_research": executor.last_request.verified_research,
            "record": record.model_dump(mode="json") if record else None,
            "readback": snapshot,
        }
    finally:
        runner.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--packet-root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--now", required=True)
    parser.add_argument("--lesson-id", required=True)
    args = parser.parse_args()
    print(json.dumps(run_next(args), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
