"""TEST_ONLY accounting fixtures; never live quote/FX acceptance."""
from datetime import datetime, timezone
import pytest
from cio_market_lab.domain.models import DecisionScope, Fill, OrderSide
from cio_market_lab.engine.paper_orders import PaperExperimentSettings
from cio_market_lab.engine.portfolio import CanonicalCashAccount, Ledger


def fill(currency="USD", **changes):
    values = dict(fill_id="test-buy", order_id="test-order", symbol="MSFT",
                  bucket=DecisionScope.SWING, side=OrderSide.BUY,
                  quantity=2, fill_price=100, fee=1, tax=0, slippage=2,
                  timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc),
                  currency=currency, assumptions={"test_only": True, "slippage_embedded": True})
    values.update(changes)
    return Fill(**values)


def test_usd_native_cash_and_position_are_typed():
    ledger = Ledger(DecisionScope.SWING, initial_cash=1000, currency="USD")
    assert ledger.apply_fill(fill()) is True
    assert ledger.cash == 799
    assert ledger.positions["MSFT"].currency == "USD"
    assert ledger.get_portfolio().currency == "USD"
    assert ledger.equity == 999  # embedded slippage must not be debited again


@pytest.mark.parametrize("currency", ["USD", "TWD"])
def test_fill_roundtrip_preserves_native_currency(currency):
    assert Fill.model_validate_json(fill(currency).model_dump_json()).currency == currency


def test_mismatch_rejected_without_any_ledger_mutation():
    ledger = Ledger(DecisionScope.SWING, initial_cash=1000, currency="TWD")
    with pytest.raises(ValueError, match="CURRENCY_MISMATCH"):
        ledger.apply_fill(fill())
    assert ledger.cash == 1000 and ledger.fills == [] and ledger.positions == {}


def test_shared_cash_currency_must_match_ledger():
    account = CanonicalCashAccount(1000, currency="TWD")
    with pytest.raises(ValueError, match="CURRENCY_MISMATCH"):
        Ledger(DecisionScope.SWING, cash_account=account, currency="USD")


def test_cash_account_rejects_wrong_currency_before_debit():
    account = CanonicalCashAccount(1000, currency="TWD")
    with pytest.raises(ValueError, match="CURRENCY_MISMATCH"):
        account.apply_fill(fill())
    assert account.cash == 1000 and not account._applied_fill_ids


def test_native_roundtrip_deduplicates_fill():
    ledger = Ledger(DecisionScope.SWING, initial_cash=1000, currency="USD")
    first = fill()
    ledger.apply_fill(first)
    restored = Ledger(DecisionScope.SWING, initial_cash=1000, currency="USD")
    for persisted in ledger.get_portfolio().fills:
        restored.apply_fill(Fill.model_validate_json(persisted.model_dump_json()))
    assert restored.cash == ledger.cash
    assert restored.apply_fill(first) is False
    assert restored.cash == 799 and restored.positions["MSFT"].quantity == 2


def test_native_sale_costs_and_pnl():
    ledger = Ledger(DecisionScope.SWING, initial_cash=1000, currency="USD")
    ledger.apply_fill(fill())
    ledger.apply_fill(fill(fill_id="test-sell", side=OrderSide.SELL,
                           fill_price=110, quantity=1, fee=1, tax=1))
    assert ledger.cash == 907
    assert ledger.realized_pnl == 8
    assert ledger.positions["MSFT"].quantity == 1


def test_unambiguous_legacy_taiwan_settings_migrate_to_twd():
    assert PaperExperimentSettings(strategy_id="tw", market="TW").base_currency == "TWD"
    assert PaperExperimentSettings(strategy_id="tw", universe=["2330.TW", "6488.TWO"]).base_currency == "TWD"
    assert PaperExperimentSettings(strategy_id="us", universe=["MSFT"]).base_currency == "USD"


@pytest.mark.parametrize("values", [
    {"market": "TW", "base_currency": "USD"},
    {"universe": ["2330.TW"], "base_currency": "USD"},
    {"universe": ["2330.TW", "MSFT"]},
])
def test_ambiguous_or_conflicting_currency_settings_fail_closed(values):
    with pytest.raises(ValueError, match="UNSUPPORTED_MIXED_NATIVE_CURRENCY"):
        PaperExperimentSettings(strategy_id="conflict", **values)
