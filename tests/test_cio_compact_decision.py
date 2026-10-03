"""Regression: slow live JSON echo must not prevent real decision receipts.
Fixtures test binding only; they never prove market performance.
"""
from types import SimpleNamespace
from cio_market_lab.integrations.hermes_chat import HermesCIODecisionExecutor
from tests.hermes_test_doubles import mock_injected_transport_for_test


def test_compact_response_binds_exact_frozen_snapshot_without_model_echo():
    snapshot = {"canonical_portfolio": {"cash_twd": 2378465.0},
                "verified_quotes": {"MSFT": {"price": 482.25}}, "version": "frozen-v2"}
    class Context(SimpleNamespace):
        def model_dump(self, **kwargs):
            return vars(self)
    context = Context(symbol="MSFT", session_id="stable-paper-session",
                      predecision_snapshot=snapshot, predecision_version="frozen-v2")
    captured = []
    def transport(prompt, **kwargs):
        captured.append(prompt)
        return mock_injected_transport_for_test(prompt, **kwargs)
    executor = HermesCIODecisionExecutor(transport=transport, allow_fixture=True)
    packet = executor.request_decision(context)
    assert "DO NOT echo canonical_portfolio" in captured[0]
    assert "at most 2500 characters" in captured[0]
    assert "justified BUY/SELL is permitted" in captured[0]
    assert packet.predecision_snapshot == snapshot
    assert packet.predecision_version == "frozen-v2"
    assert executor.last_receipt is not None
    # Deep copy: neither the model nor later callers can overwrite input evidence.
    packet.predecision_snapshot["canonical_portfolio"]["cash_twd"] = 0
    assert snapshot["canonical_portfolio"]["cash_twd"] == 2378465.0
