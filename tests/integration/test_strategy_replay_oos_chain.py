"""Issue #8 durable lifecycle, replay/PAPER alignment, and OOS anti-leak acceptance.

All market bars are explicit TEST_ONLY fixtures. OOS results are retrospective engineering
evidence only and never live strategy approval or capital authority.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from cio_market_lab.domain.models import (
    Bar,
    CIODecisionPacket,
    DecisionScope,
    OrderOrigin,
    OrderSide,
    OrderType,
)
from cio_market_lab.engine.cio_packet import sign_cio_packet
from cio_market_lab.engine.execution import ExecutionCostConfig
from cio_market_lab.engine.paper_orders import PaperDataContext, PaperOrderRequest
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.engine.replay_lab import (
    FrozenMACrossover,
    NativeCurrencyReplayEngine,
    NativeReplayEventStore,
    validation_selected_oos,
)
from cio_market_lab.engine.paper_orders import PaperOrderService
from cio_market_lab.events.store import EventStore
from cio_market_lab.strategies.durable_registry import DurableStrategyRegistry
from tests.test_source_aligned_next_bar import runner_harness

ROOT = Path(__file__).resolve().parents[2]
HELPER = Path(__file__).with_name("strategy_registry_process_helper.py")
BASE = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)


def _bar(index: int, close: float, *, symbol: str = "MSFT", fixture: bool = True) -> Bar:
    ts = BASE + timedelta(minutes=index)
    return Bar(
        symbol=symbol,
        timestamp=ts,
        observed_at=ts,
        open=close,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=1000,
        source="TEST_ONLY_STRATEGY_OOS",
        quality="TEST_ONLY_CAPTURE",
        is_fixture=fixture,
        is_synthetic=False,
    )


def _crossover_bars() -> list[Bar]:
    closes = [10, 10, 9, 9, 12, 13, 13, 13]
    return [_bar(i, value) for i, value in enumerate(closes)]


def _oos_bars(count: int = 90) -> list[Bar]:
    result = []
    for i in range(count):
        cycle = i % 12
        close = 100 + (cycle if cycle < 6 else 12 - cycle) + i * 0.02
        result.append(_bar(i, close))
    return result


def _fresh_registry(strategies_dir: Path, state_dir: Path) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--strategies-dir",
            str(strategies_dir),
            "--state-dir",
            str(state_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return json.loads(proc.stdout.splitlines()[-1])


def test_durable_strategy_lifecycle_survives_fresh_process_and_preserves_external_state(tmp_path):
    source = ROOT / "strategies" / "opening_range_breakout"
    strategies_dir = tmp_path / "strategies"
    strategy_dir = strategies_dir / "opening_range_breakout"
    state_dir = tmp_path / "registry-state"
    shutil.copytree(source, strategy_dir)

    adapter = object()
    provider_identity = id(adapter)
    provider_config = {"source": "TEST_ONLY_PROVIDER", "mode": "paper"}

    pm = PortfolioManager(initial_cash_swing=10_000, initial_cash_intraday=10_000)
    service = PaperOrderService(pm, EventStore(":memory:"))
    seeded_order = service.submit(PaperOrderRequest(
        currency="USD",
        symbol="MSFT",
        market="US",
        bucket=DecisionScope.SWING,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=1,
        limit_price=50,
        origin=OrderOrigin.MANUAL,
        reason="TEST_ONLY lifecycle continuity",
        data=PaperDataContext(source="fixture://ISSUE8", last_price=50),
    ))
    order_snapshot = seeded_order.model_dump(mode="json")
    cash_snapshot = pm.get_ledger(DecisionScope.SWING).cash

    registry = DurableStrategyRegistry(strategies_dir, state_dir)
    v1 = registry.register_or_reload(strategy_dir)
    registry.activate_strategy(v1.id, authority="Main CIO")
    assert registry.get_registered(v1.id).code_hash == v1.code_hash

    manifest_path = strategy_dir / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["version"] = "2.0.0-TEST_ONLY"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    with (strategy_dir / "strategy.py").open("a", encoding="utf-8") as fh:
        fh.write("\n# TEST_ONLY_VERSION_2\n")
    v2 = registry.register_or_reload(strategy_dir)
    assert v2.code_hash != v1.code_hash
    assert v2.status.value == "CANDIDATE"
    assert registry.get_registered(v1.id).code_hash == v1.code_hash

    with pytest.raises(PermissionError):
        registry.hot_swap_strategy(v1.id, v2.code_hash, authority="worker")
    assert registry.get_registered(v1.id).code_hash == v1.code_hash

    registry.hot_swap_strategy(v1.id, v2.code_hash, authority="Main CIO")
    assert registry.get_registered(v1.id).code_hash == v2.code_hash
    assert id(adapter) == provider_identity
    assert provider_config == {"source": "TEST_ONLY_PROVIDER", "mode": "paper"}
    assert service.find_order(seeded_order.order_id).model_dump(mode="json") == order_snapshot
    assert pm.get_ledger(DecisionScope.SWING).cash == cash_snapshot

    fresh_v2 = _fresh_registry(strategies_dir, state_dir)[v1.id]
    assert fresh_v2["active_code_hash"] == v2.code_hash
    assert fresh_v2["code_hash"] == v2.code_hash
    assert fresh_v2["version"] == "2.0.0-TEST_ONLY"
    assert any(item["action"] == "HOT_SWAPPED" for item in fresh_v2["audit_log"])

    restored = DurableStrategyRegistry(strategies_dir, state_dir)
    restored.rollback_strategy(v1.id, v1.code_hash, authority="Main CIO")
    assert restored.get_registered(v1.id).code_hash == v1.code_hash
    fresh_v1 = _fresh_registry(strategies_dir, state_dir)[v1.id]
    assert fresh_v1["active_code_hash"] == v1.code_hash
    assert fresh_v1["code_hash"] == v1.code_hash
    assert any(item["action"] == "ROLLED_BACK" for item in fresh_v1["audit_log"])
    assert service.find_order(seeded_order.order_id).model_dump(mode="json") == order_snapshot
    assert pm.get_ledger(DecisionScope.SWING).cash == cash_snapshot


def test_frozen_replay_and_real_paper_next_bar_path_align_on_economics(tmp_path):
    bars = _crossover_bars()
    config = {"fast": 2, "slow": 3}
    costs = ExecutionCostConfig()

    replay_store = NativeReplayEventStore(tmp_path / "replay.sqlite", "USD")
    replay = NativeCurrencyReplayEngine(
        replay_store,
        currency="USD",
        cost_config=costs,
        initial_cash_swing=1000,
        initial_cash_intraday=0,
        reject_stale_bars=False,
        default_order_shares=1,
    )
    replay_result = replay.run(bars, [FrozenMACrossover(config)])
    replay_ledger = replay.portfolio_manager.get_ledger(DecisionScope.SWING)
    assert replay_result.total_orders >= 1
    assert replay_result.total_fills >= 1
    replay_buy = next(fill for fill in replay_ledger.fills if fill.side == OrderSide.BUY)
    replay_order = next(order for order in replay_ledger.orders if order.order_id == replay_buy.order_id)
    signal_bar = next(bar for bar in bars if bar.timestamp == replay_order.created_at)
    next_bar = next(bar for bar in bars if bar.timestamp == replay_buy.timestamp)

    clock = {"now": signal_bar.observed_at}
    data = {"bar": signal_bar}
    paper, paper_pm, paper_service = runner_harness(tmp_path / "paper", clock, data)
    paper.paper_orders.cost_config = costs
    p = CIODecisionPacket(
        case_id="TEST_ONLY_REPLAY_PAPER_ALIGNMENT",
        as_of=signal_bar.timestamp,
        expiry=signal_bar.timestamp + timedelta(hours=1),
        thesis="TEST_ONLY frozen replay/PAPER economics alignment",
        selected_instrument="MSFT",
        action="BUY",
        quantity=1,
        is_fixture=True,
        conditions={"paper_execution_model": "NEXT_BAR_OPEN"},
    )
    p = sign_cio_packet(p, signer_id="fixture-test-signer")
    submitted = paper.submit_cio_packet(p, strategy_id="TEST_ONLY_native")
    assert submitted.action == "BUY_PENDING"
    data["bar"] = next_bar
    clock["now"] = next_bar.observed_at
    decisions = paper.process_pending_orders()
    assert [item.action for item in decisions] == ["BUY_FILLED"]

    paper_ledger = paper_pm.get_strategy_ledger("TEST_ONLY_native", DecisionScope.SWING)
    assert len(paper_ledger.fills) == 1
    paper_fill = paper_ledger.fills[0]
    paper_order = paper_service.all_orders()[0]

    assert paper_order.symbol == replay_order.symbol == "MSFT"
    assert paper_order.side == replay_order.side == OrderSide.BUY
    assert paper_order.quantity == replay_order.quantity == 1
    assert paper_fill.quantity == replay_buy.quantity == 1
    assert paper_fill.fill_price == pytest.approx(replay_buy.fill_price)
    assert paper_fill.fee == pytest.approx(replay_buy.fee)
    assert paper_fill.tax == pytest.approx(replay_buy.tax)
    assert paper_ledger.positions["MSFT"].quantity == replay_ledger.positions["MSFT"].quantity
    assert paper_ledger.cash == pytest.approx(replay_ledger.cash)
    assert paper_ledger.equity == pytest.approx(replay_ledger.equity)

    replay_types = [
        event.event_type.value
        for _, event in replay_store.get_events(limit=1000, strict=True)
    ]
    paper_types = [
        event.event_type.value
        for _, event in paper_service.event_store.get_events(limit=1000, strict=True)
    ]
    assert replay_types.count("ORDER_CREATED") == paper_types.count("ORDER_CREATED") == 1
    assert replay_types.count("ORDER_FILLED") == paper_types.count("ORDER_FILLED") == 1
    assert replay_types.count("BAR_OBSERVED") == len(bars)
    assert paper_types.count("BAR_OBSERVED") == 0  # expected mode delta: paper reads adapter bars.


def test_validation_selection_freezes_before_single_oos_and_reports_non_live_evidence(tmp_path):
    bars = _oos_bars()
    report = validation_selected_oos(
        bars,
        "MSFT",
        "USD",
        10_000,
        [{"fast": 2, "slow": 4}, {"fast": 3, "slow": 5}],
        tmp_path / "oos",
        allow_test_only=True,
        selection_inputs=[
            {
                "kind": "TEST_ONLY_LESSON",
                "available_at": bars[60].timestamp,
            }
        ],
    )
    assert report["status"] == "COMPLETE_TEST_ONLY"
    assert report["strategy_id"] == "frozen_ma_replay"
    assert report["strategy_version"] == "1"
    assert len(report["strategy_code_hash"]) == 64
    assert report["data_source_tag"] == "TEST_ONLY_STRATEGY_OOS"
    assert report["selection_metric"] == "VALIDATION_NET_RETURN_ONLY"
    assert report["selection_frozen_before_oos"] is True
    assert report["oos_evaluation_count"] == 1
    assert report["live_approved"] is False
    assert report["completion_claim_allowed"] is False
    assert report["train_range"]["end"] < report["validation_range"]["start"]
    assert report["validation_range"]["end"] < report["oos_range"]["start"]
    assert report["oos"]["bar_count"] > 0
    assert set(report["cost_assumptions"]) == {
        "slippage_bps",
        "fee_rate_tw",
        "fee_rate_us",
        "min_fee_tw",
        "min_fee_us",
    }
    assert len(report["candidate_results"]) == 2
    assert (tmp_path / "oos" / "validation_selected_oos.json").is_file()


def test_oos_negative_inputs_fail_closed_or_report_unavailable(tmp_path):
    bars = _oos_bars()
    duplicate = list(bars)
    duplicate[10] = duplicate[9].model_copy()
    with pytest.raises(ValueError, match="DUPLICATE_OR_NONCHRONOLOGICAL_BARS"):
        validation_selected_oos(
            duplicate, "MSFT", "USD", 10_000, [{"fast": 2, "slow": 4}],
            tmp_path / "dup", allow_test_only=True,
        )

    unordered = list(bars)
    unordered[20], unordered[21] = unordered[21], unordered[20]
    with pytest.raises(ValueError, match="DUPLICATE_OR_NONCHRONOLOGICAL_BARS"):
        validation_selected_oos(
            unordered, "MSFT", "USD", 10_000, [{"fast": 2, "slow": 4}],
            tmp_path / "unordered", allow_test_only=True,
        )

    unavailable = validation_selected_oos(
        bars[:20], "MSFT", "USD", 10_000, [{"fast": 2, "slow": 4}],
        tmp_path / "short", allow_test_only=True,
    )
    assert unavailable["status"] == "UNAVAILABLE"
    assert unavailable["reason"] == "INSUFFICIENT_HELDOUT_WINDOW"

    with pytest.raises(ValueError, match="INVALID_FROZEN_CONFIG_KEYS"):
        validation_selected_oos(
            bars, "MSFT", "USD", 10_000,
            [{"fast": 2, "slow": 4, "holdout_feedback": 1}],
            tmp_path / "feedback", allow_test_only=True,
        )

    with pytest.raises(ValueError, match="FUTURE_SELECTION_INPUT_FORBIDDEN"):
        validation_selected_oos(
            bars,
            "MSFT",
            "USD",
            10_000,
            [{"fast": 2, "slow": 4}],
            tmp_path / "future",
            allow_test_only=True,
            selection_inputs=[
                {
                    "kind": "TEST_ONLY_FUTURE_LESSON",
                    "available_at": bars[-1].timestamp,
                }
            ],
        )

    with pytest.raises(ValueError, match="TEST_ONLY_SOURCE_REQUIRES_EXPLICIT_OPT_IN"):
        validation_selected_oos(
            bars, "MSFT", "USD", 10_000, [{"fast": 2, "slow": 4}],
            tmp_path / "no-fixture-opt-in",
        )
