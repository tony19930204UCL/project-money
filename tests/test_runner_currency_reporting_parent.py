"""TEST_ONLY actual runner order/fill path and isolated currency reporting."""
from datetime import datetime, timezone
from decimal import Decimal
import pytest
from tests.test_autonomous_runner import FixtureAdapter
from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner
from cio_market_lab.engine.paper_orders import PaperOrderService, PaperExperimentSettings
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.events.store import EventStore
from cio_market_lab.engine.historical_fx import FxRateReceipt, FxReportingBlocked


def runner(tmp_path):
    pm=PortfolioManager(initial_cash_swing=10000, initial_cash_intraday=10000)
    orders=PaperOrderService(pm, EventStore(':memory:'))
    r=AutonomousPaperRunner(tmp_path, pm, orders, FixtureAdapter(), require_cio_provider=False)
    r.allow_fixture_quotes=True
    return r,orders,pm


@pytest.mark.parametrize('symbol,currency',[('AAPL','USD'),('2330.TW','TWD')])
def test_actual_runner_native_buy_and_reporting(tmp_path,symbol,currency):
    r,orders,pm=runner(tmp_path)
    r.configure(PaperExperimentSettings(strategy_id='native',enabled=True, universe=[symbol],
        base_currency=currency, initial_cash=10000,max_position_notional=206,max_open_positions=1))
    result=r.run_one_cycle('native')
    assert result['run']['fills_count']==1
    ledger=pm.get_strategy_ledger('native','swing')
    assert orders.all_orders()[0].currency==ledger.fills[0].currency==currency
    assert ledger.cash==pytest.approx(10000-ledger.fills[0].quantity*ledger.fills[0].fill_price-ledger.fills[0].fee)
    now=datetime.now(timezone.utc)
    if currency=='USD':
        with pytest.raises(FxReportingBlocked):r.report_strategy_nav('native',as_of=now)
        rate=FxRateReceipt('USD/TWD',Decimal('31'),now,'TEST_ONLY','https://example.invalid/fixture',now.date(),provenance='TEST_ONLY')
        with pytest.raises(FxReportingBlocked,match='TEST_ONLY'):r.report_strategy_nav('native',as_of=now,rate_receipts=[rate])
        report=r.report_strategy_nav('native',as_of=now,rate_receipts=[rate],allow_test_only=True)
        assert Decimal(report['reporting_nav'])==Decimal(str(report['native_nav']))*31
        assert report['conversion_receipt']['rate_provenance']=='TEST_ONLY'
    else:
        report=r.report_strategy_nav('native',as_of=now)
        assert report['reporting_nav']==report['native_nav'] and report['conversion_receipt'] is None
    assert ledger.cash==pytest.approx(10000-ledger.fills[0].quantity*ledger.fills[0].fill_price-ledger.fills[0].fee)


def test_mixed_market_currency_config_fails_closed(tmp_path):
    r,_,_=runner(tmp_path)
    with pytest.raises(ValueError):
        r.configure(PaperExperimentSettings(strategy_id='mixed',enabled=True,universe=['AAPL','2330.TW'],base_currency='USD'))


def test_competition_summary_fails_closed_for_partial_mixed_currency_total(tmp_path):
    r, orders, pm = runner(tmp_path)
    from cio_market_lab.engine.historical_fx import FxRateReceipt
    from decimal import Decimal
    from datetime import timedelta
    from cio_market_lab.domain.models import DecisionScope
    now = datetime.now(timezone.utc)
    for sid, market, currency, cash, symbol in [('tw-live','TW','TWD',5000,'2330.TW'),('us-live','US','USD',1000,'MSFT')]:
        r.configure(PaperExperimentSettings(strategy_id=sid, market=market, base_currency=currency, reporting_currency='TWD', initial_cash=cash, universe=[symbol], enabled=False))
    r.canonical_fx_receipts = []
    summary = r.competition_summary(now)
    assert summary['nav_status'] == 'UNAVAILABLE_FX'
    assert summary['native_totals']['TWD']['cash'] == 5000
    assert summary['native_totals']['USD']['cash'] == 1000
    assert summary['total']['currency'] == 'TWD'
    assert summary['total']['cash'] is None
    assert summary['total']['initial_cash'] is None
    assert summary['total']['equity'] is None


def test_competition_summary_adds_twd_usd_once_with_authoritative_fx(tmp_path):
    r, orders, pm = runner(tmp_path)
    from cio_market_lab.engine.historical_fx import FxRateReceipt
    from decimal import Decimal
    now = datetime.now(timezone.utc)
    for sid, market, currency, cash, symbol in [('tw-live','TW','TWD',5000,'2330.TW'),('us-live','US','USD',1000,'MSFT')]:
        r.configure(PaperExperimentSettings(strategy_id=sid, market=market, base_currency=currency, reporting_currency='TWD', initial_cash=cash, universe=[symbol], enabled=False))
    r.allow_test_only_fx = True
    r.resolve_canonical_valuation_fx = lambda currency, now=None: ({'status':'AVAILABLE','rate':30.0,'source':'TEST_ONLY','source_tier':'TEST_ONLY','source_url':'https://example.invalid/test-only','label':'test-only','is_simulated':True,'observed_at':now.isoformat(),'source_timestamp':now.date().isoformat()} if currency == 'USD' else {'status':'AVAILABLE','rate':1.0})
    summary = r.competition_summary(now)
    assert summary['nav_status'] == 'VALID'
    assert summary['total']['cash'] == 35000
    assert summary['native_totals']['TWD']['cash'] == 5000
    assert summary['native_totals']['USD']['cash'] == 1000
