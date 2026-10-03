"""TEST_ONLY native funding/currency guards; no live broker operations."""
import pytest
from tests.test_autonomous_runner import make_runner, FixtureAdapter
from cio_market_lab.engine.paper_orders import PaperExperimentSettings, PaperOrderRequest


def request(**updates):
    values = dict(symbol="AAPL", market="US", bucket="swing", side="BUY", quantity=1,
                  order_type="LIMIT", limit_price=100, origin="MANUAL", reason="TEST_ONLY native guard",
                  data=dict(source="TEST_ONLY", age_seconds=0, last_price=100, is_stale=False))
    values.update(updates)
    return PaperOrderRequest(**values)


def test_omitting_currency_does_not_spend_twd_for_us_security(tmp_path):
    runner, service, pm = make_runner(tmp_path, FixtureAdapter())
    before = pm.get_portfolio("swing").cash
    verdict = service.preview(request())
    assert not verdict["risk_decision"]["allowed"]
    assert "CURRENCY_MISMATCH" in verdict["risk_decision"]["reasons"]
    assert pm.get_portfolio("swing").cash == before
    assert not service.all_orders()


def test_disabled_preview_does_not_create_or_fund_unknown_strategy(tmp_path):
    runner, service, pm = make_runner(tmp_path, FixtureAdapter())
    verdict = service.preview(request(strategy_id="unknown-disabled", origin="STRATEGY"))
    assert not verdict["risk_decision"]["allowed"]
    assert "STRATEGY_AUTONOMOUS_PAPER_DISABLED" in verdict["risk_decision"]["reasons"]
    assert "unknown-disabled" not in pm.strategy_ids()
    assert "unknown-disabled" not in pm._strategy_capital
    assert not service.all_orders()


def test_native_configuration_funds_usd_and_rejected_change_is_atomic(tmp_path):
    runner, service, pm = make_runner(tmp_path, FixtureAdapter())
    service.configure_experiment(PaperExperimentSettings(
        strategy_id="usd-native", enabled=True, universe=["AAPL"], base_currency="USD", initial_cash=1000,
    ))
    verdict = service.preview(request(strategy_id="usd-native"))
    assert verdict["risk_decision"]["allowed"]
    ledger = pm.get_strategy_portfolio("usd-native", "swing")
    assert ledger.currency == "USD" and ledger.initial_cash == 1000
    before_events = list(service.event_store.get_events())
    with pytest.raises(ValueError, match="CURRENCY_MISMATCH"):
        service.configure_experiment(PaperExperimentSettings(
            strategy_id="usd-native", enabled=True, universe=["2330.TW"], base_currency="TWD", initial_cash=1000,
        ))
    assert service.experiments["usd-native"].base_currency == "USD"
    assert pm.get_strategy_portfolio("usd-native", "swing").cash == ledger.cash
    assert list(service.event_store.get_events()) == before_events
