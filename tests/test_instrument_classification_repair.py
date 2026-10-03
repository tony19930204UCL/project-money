"""Isolated packet-boundary identity regressions; no service, broker or live ledger."""

from tests.fixture_next_quote import submit_after_new_fixture_quote
from datetime import datetime, timedelta, timezone

import pytest

from cio_market_lab.data.base import MarketDataAdapter
from cio_market_lab.domain.models import Bar, CIODecisionPacket, DecisionScope, Quote
from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner
from cio_market_lab.engine.cio_packet import sign_cio_packet
from cio_market_lab.engine.paper_orders import PaperExperimentSettings, PaperOrderService
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.events.store import EventStore

NOW = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)


class OnlySpotFixture(MarketDataAdapter):
    def __init__(self):
        self.requested = []

    @property
    def source_name(self):
        return "isolated_spot_fixture"

    def get_bars(self, symbol, start=None, end=None):
        return [self.get_latest_bar(symbol)]

    def stream_bars(self, symbols):
        return iter(())

    def get_latest_bar(self, symbol):
        self.requested.append(symbol)
        return Bar(symbol=symbol, timestamp=NOW - timedelta(minutes=1), observed_at=NOW,
                   open=99, high=101, low=98, close=100, volume=1000,
                   source="isolated_fixture", quality="good")

    def get_latest_quote(self, symbol):
        return Quote(bid_size=1000, ask_size=1000,
            quote_id=f"TEST_ONLY_{symbol}_{(NOW).isoformat()}",
            session="ODD_LOT" if (symbol).endswith(".TW") else "REGULAR",
            source_capabilities={"source": "isolated_fixture", "two_sided_book": True,
                "size_backed": True, "exchange_session_attested": True,
                "entitlement_evidence_id": "TEST_ONLY_FIXTURE_ODD_LOT",
                "entitlement_status": "TEST_ONLY", "odd_lot_book": True,
                "supported_sessions": ["REGULAR", "ODD_LOT"]},
            symbol=symbol, timestamp=NOW, observed_at=NOW,
                     bid=99, ask=101, last_price=100, source="isolated_fixture")


def setup_runner(tmp_path, monkeypatch, configure=True, symbol="AAPL"):
    monkeypatch.setenv("CIO_ALLOW_CLOSED_MARKET_TEST_ORDERS", "1")
    adapter = OnlySpotFixture()
    pm = PortfolioManager(initial_cash_swing=100_000, initial_cash_intraday=100_000)
    orders = PaperOrderService(pm, EventStore(":memory:"))
    runner = AutonomousPaperRunner(tmp_path, pm, orders, adapter, now_fn=lambda: NOW)
    runner.allow_fixture_quotes = True
    if configure:
        runner.configure(PaperExperimentSettings(strategy_id="identity-gate", enabled=True,
                                                  universe=[symbol],
                                                   base_currency="TWD" if symbol.endswith(".TW") else "USD",
                                                  initial_cash=100_000, max_position_notional=5000))
    return runner, orders, adapter


def packet(symbol, case_id, conditions=None):
    return sign_cio_packet(CIODecisionPacket(
        case_id=case_id, as_of=NOW, expiry=NOW + timedelta(hours=1),
        thesis="isolated instrument identity regression", selected_instrument=symbol,
        action="BUY", quantity=1, holding_horizon=DecisionScope.SWING,
        conditions={**({"strategy_id": "identity-gate", **(conditions or {})}), "allow_odd_lot": True}, is_fixture=True,
    ))


@pytest.mark.parametrize("symbol,conditions", [
    ("AAPL261016C00150000", {}),  # OCC compact option, no spec or type; formerly fell into spot path
    ("ZZZZ", {}),
    ("ZZZZ", {"instrument_type": "EQUITY"}),  # caller assertion cannot confer eligibility
    ("TX00", {}),
    ("AAPL261016C00150000", {"instrument_type": "EQUITY"}),
])
def test_unknown_identity_never_uses_spot_bar_or_fills(tmp_path, monkeypatch, symbol, conditions):
    runner, orders, adapter = setup_runner(tmp_path, monkeypatch, symbol=symbol)
    decision = runner.submit_cio_packet(packet(symbol, "blocked", conditions))
    assert decision.action == "NO_TRADE"
    assert decision.reason == "NO_TRADE_INSTRUMENT_IDENTITY_UNKNOWN"
    assert decision.terminal_status == "TERMINAL_RISK_BLOCK"
    assert adapter.requested == []
    assert orders.all_orders() == []


def test_direct_execution_boundary_rejects_ambiguous_identity_even_with_bar(tmp_path, monkeypatch):
    runner, orders, adapter = setup_runner(tmp_path, monkeypatch)
    malicious = packet("AAPL261016C00150000", "direct")
    fake_bar = adapter.get_latest_bar(malicious.selected_instrument)
    decision = runner._execute_cio_packet("direct", orders.experiment_for("identity-gate"),
                                           malicious, fake_bar, {"close": fake_bar.close})
    assert decision.reason == "NO_TRADE_INSTRUMENT_IDENTITY_UNKNOWN"
    assert orders.all_orders() == []


@pytest.mark.parametrize("symbol", ["AAPL", "ES", "TX", "SPY", "0050.TW"])
def test_reviewed_equities_and_etfs_remain_spot_eligible(tmp_path, monkeypatch, symbol):
    runner, orders, adapter = setup_runner(tmp_path, monkeypatch, symbol=symbol)
    clock = [NOW]
    runner._now_fn = lambda: clock[0]
    orders._now_fn = runner._now_fn
    decision = submit_after_new_fixture_quote(runner, clock, packet(symbol, "spot"))
    assert decision.action == "BUY_FILLED", decision.reason
    assert adapter.requested[0] == symbol  # subsequent NAV refresh can request other universe bars
    assert len(orders.all_orders()) == 1


def test_contradictory_type_and_incomplete_derivative_never_spot_fill(tmp_path, monkeypatch):
    runner, orders, adapter = setup_runner(tmp_path, monkeypatch)
    conflict = runner.submit_cio_packet(packet("SPY", "conflict", {"instrument_type": "EQUITY"}))
    assert conflict.reason == "NO_TRADE_INSTRUMENT_IDENTITY_CONFLICT"
    derivative = runner.submit_cio_packet(packet("AAPL261016C00150000", "derivative",
                                                   {"instrument_type": "OPTION"}))
    assert derivative.reason.startswith("CAPABILITY_UNAVAILABLE:LONG_PREMIUM_OPTIONS")
    quote_only = runner.submit_cio_packet(packet("AAPL", "quote-only",
                                                   {"derivative_quote": {"symbol": "AAPL"}}))
    assert quote_only.reason.startswith("CAPABILITY_UNAVAILABLE:LONG_PREMIUM_OPTIONS")
    assert adapter.requested == []
    assert orders.all_orders() == []


def test_restored_enabled_experiment_still_applies_packet_gate(tmp_path, monkeypatch):
    runner, _, _ = setup_runner(tmp_path, monkeypatch)
    assert runner.paper_orders.experiment_for("identity-gate").enabled
    restored, orders, adapter = setup_runner(tmp_path, monkeypatch, configure=False)
    assert restored.paper_orders.experiment_for("identity-gate").enabled
    outcome = restored.submit_cio_packet(packet("AAPL261016C00150000", "restored"))
    assert outcome.reason == "NO_TRADE_INSTRUMENT_IDENTITY_UNKNOWN"
    assert adapter.requested == []
    assert orders.all_orders() == []
