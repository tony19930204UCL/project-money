"""Issue #8 durable lifecycle, replay/PAPER alignment, and OOS anti-leak acceptance.

All market bars are explicit TEST_ONLY fixtures. OOS results are retrospective engineering
evidence only and never live strategy approval or capital authority.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml
from fastapi.testclient import TestClient

from cio_market_lab.api.app import create_app
from cio_market_lab.domain.models import (
    Bar,
    CIODecisionPacket,
    DecisionScope,
    Fill,
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
from tests.browser.server_helper import TestOnlyMarketAdapter, _fixture_fill
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


def _fresh_app(workspace_root: Path, runtime_dir: Path) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--workspace-root",
            str(workspace_root),
            "--runtime-dir",
            str(runtime_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(proc.stdout.splitlines()[-1])


def test_durable_strategy_version_hot_swap_and_rollback_preserve_execution_state(
    tmp_path, monkeypatch
):
    source = ROOT / "strategies" / "opening_range_breakout"
    workspace = tmp_path / "workspace"
    strategies_dir = workspace / "strategies"
    v1_dir = strategies_dir / "opening_range_breakout"
    v2_dir = tmp_path / "candidate-v2" / "opening_range_breakout"
    runtime = tmp_path / "runtime"
    shutil.copytree(source, v1_dir)
    shutil.copytree(source, v2_dir)

    manifest_path = v2_dir / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["version"] = "2.0.0-TEST_ONLY"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    with (v2_dir / "strategy.py").open("a", encoding="utf-8") as fh:
        fh.write("\n# TEST_ONLY_VERSION_2\n")

    adapter = TestOnlyMarketAdapter()
    app = create_app(
        workspace_root=workspace,
        runtime_dir=runtime,
        fixture_mode=False,
        is_read_only=False,
        market_adapter=adapter,
    )
    state = app.state.app_state
    state.runner.allow_fixture_quotes = True
    client = TestClient(app, base_url="http://127.0.0.1:21322")

    try:
        assert state.runner.market_adapter is state.market_adapter
        assert state.runner.cio_executor is state.cio_executor
        adapter_identity = id(state.market_adapter)
        executor_identity = id(state.cio_executor)
        provider_snapshot = (
            state.cio_executor.provider_id,
            state.cio_executor.model_id,
            state.cio_executor.session_id,
        )

        v1 = state.registry.get_registered("opening_range_breakout")
        assert v1 is not None
        activate = client.post(
            f"/api/strategies/{v1.id}/activate",
            json={"authority": "Main CIO"},
        )
        assert activate.status_code == 200
        v1 = state.registry.get_registered(v1.id)
        assert v1.status.value == "PAPER_ACTIVE"
        assert state.registry._active_version[v1.id] == v1.code_hash

        pending = state.paper_orders.submit(PaperOrderRequest(
            currency="TWD",
            symbol="2330.TW",
            market="TW",
            bucket=DecisionScope.SWING,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=2,
            limit_price=101,
            origin=OrderOrigin.MANUAL,
            reason="TEST_ONLY lifecycle partial continuity",
            data=PaperDataContext(source="fixture://ISSUE8_CONNECTED", last_price=100),
        ))
        _fixture_fill(
            state,
            pending.order_id,
            1,
            "TEST_ONLY_CONNECTED_PARTIAL_FILL",
        )
        pending = state.paper_orders.find_order(pending.order_id)
        assert pending.status.value == "PARTIALLY_FILLED"
        assert pending.filled_quantity == 1
        assert pending.remaining_quantity == 1
        ledger = state.portfolio_manager.get_ledger(DecisionScope.SWING)
        order_snapshot = pending.model_dump(mode="json")
        cash_snapshot = ledger.cash
        position_snapshot = ledger.positions["2330.TW"].model_dump(mode="json")
        fill_ids_snapshot = [fill.fill_id for fill in ledger.fills]

        v2 = state.registry.register_or_reload(v2_dir)
        assert v2.code_hash != v1.code_hash
        assert v2.status.value == "CANDIDATE"
        assert state.registry.get_registered(v1.id).code_hash == v1.code_hash

        denied = client.post(
            f"/api/strategies/{v1.id}/hot-swap",
            json={"code_hash": v2.code_hash, "authority": "worker"},
        )
        assert denied.status_code == 400
        assert state.registry.get_registered(v1.id).code_hash == v1.code_hash

        swapped = client.post(
            f"/api/strategies/{v1.id}/hot-swap",
            json={"code_hash": v2.code_hash, "authority": "Main CIO"},
        )
        assert swapped.status_code == 200
        active_v2 = state.registry.get_registered(v1.id)
        assert active_v2.code_hash == v2.code_hash
        assert active_v2.version == "2.0.0-TEST_ONLY"
        assert active_v2.status.value == "PAPER_ACTIVE"
        assert state.registry._active_version[v1.id] == v2.code_hash

        assert id(state.market_adapter) == adapter_identity
        assert id(state.cio_executor) == executor_identity
        assert state.runner.market_adapter is state.market_adapter
        assert state.runner.cio_executor is state.cio_executor
        assert (
            state.cio_executor.provider_id,
            state.cio_executor.model_id,
            state.cio_executor.session_id,
        ) == provider_snapshot
        current = state.paper_orders.find_order(pending.order_id)
        assert current.model_dump(mode="json") == order_snapshot
        assert ledger.cash == cash_snapshot
        assert ledger.positions["2330.TW"].model_dump(mode="json") == position_snapshot
        assert [fill.fill_id for fill in ledger.fills] == fill_ids_snapshot

        state.runner._persist_portfolios()
        fresh_v2 = _fresh_app(workspace, runtime)
        fresh_reg_v2 = fresh_v2["registry"][v1.id]
        assert fresh_reg_v2["active_code_hash"] == v2.code_hash
        assert fresh_reg_v2["code_hash"] == v2.code_hash
        assert fresh_reg_v2["version"] == "2.0.0-TEST_ONLY"
        assert fresh_v2["adapter"]["runner_same_adapter"] is True
        assert fresh_v2["provider"]["runner_same_executor"] is True
        assert (
            fresh_v2["provider"]["provider_id"],
            fresh_v2["provider"]["model_id"],
            fresh_v2["provider"]["session_id"],
        ) == provider_snapshot
        fresh_partial = next(
            item for item in fresh_v2["orders"] if item["order_id"] == pending.order_id
        )
        assert fresh_partial["status"] == "PARTIALLY_FILLED"
        assert fresh_partial["audit_metadata"]["filled_quantity"] == 1
        assert [f["fill_id"] for f in fresh_v2["fills"]] == fill_ids_snapshot

        rolled = client.post(
            f"/api/strategies/{v1.id}/rollback",
            json={"code_hash": v1.code_hash, "authority": "Main CIO"},
        )
        assert rolled.status_code == 200
        active_v1 = state.registry.get_registered(v1.id)
        assert active_v1.code_hash == v1.code_hash
        assert active_v1.status.value == "PAPER_ACTIVE"
        assert state.registry._active_version[v1.id] == v1.code_hash

        assert id(state.market_adapter) == adapter_identity
        assert id(state.cio_executor) == executor_identity
        assert (
            state.cio_executor.provider_id,
            state.cio_executor.model_id,
            state.cio_executor.session_id,
        ) == provider_snapshot
        current = state.paper_orders.find_order(pending.order_id)
        assert current.model_dump(mode="json") == order_snapshot
        assert ledger.cash == cash_snapshot
        assert ledger.positions["2330.TW"].model_dump(mode="json") == position_snapshot
        assert [fill.fill_id for fill in ledger.fills] == fill_ids_snapshot

        state.runner._persist_portfolios()
        fresh_v1 = _fresh_app(workspace, runtime)
        fresh_reg_v1 = fresh_v1["registry"][v1.id]
        assert fresh_reg_v1["active_code_hash"] == v1.code_hash
        assert fresh_reg_v1["code_hash"] == v1.code_hash
        assert any(
            item["action"] == "ROLLED_BACK" for item in fresh_reg_v1["audit_log"]
        )
        fresh_partial = next(
            item for item in fresh_v1["orders"] if item["order_id"] == pending.order_id
        )
        assert fresh_partial["status"] == "PARTIALLY_FILLED"
        assert fresh_partial["audit_metadata"]["filled_quantity"] == 1
        assert [f["fill_id"] for f in fresh_v1["fills"]] == fill_ids_snapshot
    finally:
        state.runner.shutdown()


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
        strategy_version="1",
        is_fixture=True,
        conditions={
            "paper_execution_model": "NEXT_BAR_OPEN",
            "strategy_config": config,
        },
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

    replay_signal = replay._order_signals[replay_order.order_id]
    expected_config_hash = hashlib.sha256(
        json.dumps(config, sort_keys=True).encode()
    ).hexdigest()
    assert replay_signal.version == p.strategy_version == "1"
    assert replay_signal.config_hash == expected_config_hash
    assert p.conditions["strategy_config"] == config
    assert paper_order.strategy_version == "1"
    assert replay_order.symbol == paper_order.symbol == "MSFT"
    assert paper_order.side == replay_order.side == OrderSide.BUY
    assert paper_order.quantity == replay_order.quantity == 1
    assert paper_fill.quantity == replay_buy.quantity == 1
    assert paper_fill.fill_price == pytest.approx(replay_buy.fill_price)
    assert paper_fill.fee == pytest.approx(replay_buy.fee)
    assert paper_fill.tax == pytest.approx(replay_buy.tax)
    assert paper_ledger.positions["MSFT"].quantity == replay_ledger.positions["MSFT"].quantity
    assert paper_ledger.cash == pytest.approx(replay_ledger.cash)
    paper_ledger.update_mark_to_market(bars[-1])
    assert paper_ledger.positions["MSFT"].current_price == replay_ledger.positions["MSFT"].current_price == bars[-1].close
    assert paper_ledger._latest_prices["MSFT"] == replay_ledger._latest_prices["MSFT"] == bars[-1].close
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

    with pytest.raises(ValueError, match="FUTURE_TRAINING_INPUT_FORBIDDEN"):
        validation_selected_oos(
            bars,
            "MSFT",
            "USD",
            10_000,
            [{"fast": 2, "slow": 4}],
            tmp_path / "future-training",
            allow_test_only=True,
            fit_inputs=[
                {
                    "kind": "TEST_ONLY_FUTURE_TRAINING_BAR_OR_LESSON",
                    "available_at": bars[60].timestamp,
                }
            ],
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
