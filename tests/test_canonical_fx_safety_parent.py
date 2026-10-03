"""TEST_ONLY receipts and prices; never substituted for observed market data."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from dataclasses import asdict
import json
import pytest
from tests.test_autonomous_runner import make_runner, FixtureAdapter
from cio_market_lab.engine.historical_fx import FxRateReceipt
from cio_market_lab.engine.paper_orders import PaperExperimentSettings
from cio_market_lab.engine.team_ops import DurableQuoteSnapshot

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def setup(tmp_path):
    runner, orders, pm = make_runner(tmp_path, FixtureAdapter())
    orders.configure_experiment(PaperExperimentSettings(
        strategy_id="usd", enabled=True, universe=["AAPL"], base_currency="USD", initial_cash=1000,
    ))
    return runner, orders, pm


def receipt(observed_at=NOW, source_date=NOW.date()):
    return FxRateReceipt(pair="USD/TWD", rate=Decimal("31.5"), observed_at=observed_at,
        source_date=source_date, source="TEST_ONLY", source_url="https://example.invalid/test-only-fx-receipt",
        provenance="TEST_ONLY")


def test_configured_assumption_cannot_leak_into_non_test_runtime(tmp_path):
    runner, _, pm = setup(tmp_path)
    runner.configured_fx_rates = {"USD": 32.0}
    assert runner.resolve_canonical_valuation_fx(now=NOW)["status"] == "MISSING"
    snap = runner.generate_canonical_team_ops(NOW)
    assert snap["portfolio"]["nav_status"] == "FX_UNAVAILABLE"
    assert snap["portfolio"]["cash"] is None
    assert snap["portfolio"]["initial_capital"] is None
    assert snap["portfolio"]["equity"] is None
    assert pm.get_strategy_portfolio("usd", "swing").cash == 1000


@pytest.mark.parametrize("item", [receipt(NOW + timedelta(seconds=1)),
    receipt(source_date=(NOW + timedelta(days=1)).date()),
    receipt(source_date=(NOW - timedelta(days=6)).date())])
def test_future_or_stale_receipt_never_uses_assumption_fallback(tmp_path, item):
    runner, _, _ = setup(tmp_path)
    runner.allow_test_only_fx = True
    runner.configured_fx_rates = {"USD": 32.0}
    runner.canonical_fx_receipts = [item]
    assert runner.resolve_canonical_valuation_fx(now=NOW)["status"] == "MISSING"
    assert runner.generate_canonical_team_ops(NOW)["portfolio"]["nav_status"] == "FX_UNAVAILABLE"


def test_persisted_receipt_restart_and_test_only_authorization(tmp_path):
    runner, _, _ = setup(tmp_path)
    path = runner.runtime_dir / "canonical_fx_receipts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(receipt())], default=str))
    assert runner.resolve_canonical_valuation_fx(now=NOW)["status"] == "MISSING"
    runner.allow_test_only_fx = True
    snap = runner.generate_canonical_team_ops(NOW)
    assert snap["portfolio"]["equity"] == 1000 * 31.5
    assert snap["portfolio"]["cash"] == snap["portfolio"]["initial_capital"] == 1000 * 31.5
    assert snap["portfolio"]["fx_accounting"]["is_simulated"] is True
    restarted, _, _ = setup(tmp_path)
    restarted.allow_test_only_fx = True
    restored = restarted.generate_canonical_team_ops(NOW)
    assert restored["portfolio"]["equity"] == snap["portfolio"]["equity"]
    assert restored["portfolio"]["fx_accounting"]["observed_at"] == NOW.isoformat()


@pytest.mark.parametrize("observed,bar", [(NOW + timedelta(seconds=1), NOW),
    (NOW, NOW + timedelta(seconds=1)), (NOW, NOW - timedelta(days=6))])
def test_market_quote_observation_and_source_time_checked(tmp_path, observed, bar):
    runner, _, _ = setup(tmp_path)
    quote = DurableQuoteSnapshot(symbol="USDTWD=X", market="US", source="TEST_ONLY_MARKET_QUOTE",
        observed_at=observed, bar_time=bar, last_price=31.5, is_stale=False, is_synthetic=False)
    runner.get_durable_quote = lambda symbol, **kw: quote
    assert runner.resolve_canonical_valuation_fx(now=NOW)["status"] == "MISSING"
