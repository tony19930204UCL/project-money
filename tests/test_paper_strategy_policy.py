"""Regression for paper strategy policy: paper-only sizing and ownership.

This module is deliberately executable rather than a placeholder for acceptance.
"""
from types import SimpleNamespace

from cio_market_lab.engine.paper_trading_loop import PaperAccount, PaperTradingLoop, Signal
from cio_market_lab.engine.strategy_feedback import apply_weights


class FixedStrategy:
    ticker = "AAA"
    strategy_id = "policy-test"

    def signals(self, quotes, account):
        return [Signal("AAA", "stock", "BUY", 4, None, None, "synthetic")]


def test_weight_zero_disables_paper_entry():
    strategy = apply_weights(
        [FixedStrategy()],
        {"schema_version": 1, "weights": {"policy-test": {"weight": 0}}},
    )[0]
    assert strategy.signals({}, PaperAccount(10000)) == []


def test_weight_scales_paper_entry_without_mutating_original():
    original = FixedStrategy()
    weighted = apply_weights(
        [original],
        {"schema_version": 1, "weights": {"policy-test": {"weight": 0.5}}},
    )[0]
    assert weighted.signals({}, PaperAccount(10000))[0].size == 2
    assert original.signals({}, PaperAccount(10000))[0].size == 4


def test_disabled_strategy_cannot_open_position(tmp_path):
    class Orders:
        def _risk_decision(self, request):
            return SimpleNamespace(allowed=True, reasons=[])

        def submit(self, request):
            return SimpleNamespace(order_id="synthetic-paper-order")

    class Feed:
        def latest(self, symbol):
            return SimpleNamespace(
                c=100.0, source="synthetic", staleness_seconds=0
            )

    strategy = apply_weights(
        [FixedStrategy()],
        {"schema_version": 1, "weights": {"policy-test": {"weight": 0}}},
    )[0]
    loop = PaperTradingLoop(Orders(), tmp_path / "journal.jsonl")
    account = PaperAccount(10000)
    result = loop.run_once(Feed(), account, [strategy])
    assert not any(event.get("side") == "BUY" for event in result["events"])
    assert not account.positions.get("AAA")
