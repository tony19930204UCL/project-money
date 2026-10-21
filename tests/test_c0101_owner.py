import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from cio_market_lab.engine.paper_trading_loop import PaperAccount, PaperTradingLoop, Signal
from cio_market_lab.engine.strategy_feedback import WeightedStrategy

class OwnerFillTests(unittest.TestCase):
    def test_full_partial_and_rejected_owner(self):
        class Strategy:
            ticker = "SPY"
            strategy_id = "owner"
            def __init__(self):
                self.quantity = 1
                self.calls = []
            def signals(self, quotes, account):
                return [Signal("SPY", "stock", "SELL", self.quantity, None, None, "exit")]
            def on_execution(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return "forwarded"
        underlying = Strategy()
        wrapped = WeightedStrategy(underlying, 0)
        self.assertEqual(wrapped.on_execution("probe", key=1), "forwarded")
        underlying.calls.clear()
        quote = SimpleNamespace(c=100., staleness_seconds=0, ts=datetime(2026,10,20,tzinfo=timezone.utc),source="synthetic")
        feed = SimpleNamespace(latest=lambda symbol: quote)
        decision = SimpleNamespace(allowed=True,reasons=[])
        ledger = Mock()
        orders = Mock()
        orders._risk_decision.return_value = decision
        orders.portfolio_manager.get_ledger.return_value = ledger
        with tempfile.TemporaryDirectory() as directory:
            loop = PaperTradingLoop(orders, Path(directory)/"journal.jsonl", max_position_pct=1,spread_bps=0,fee_rate=0)
            account = PaperAccount(cash=1000,positions={"SPY":3},strategy_state={"SPY":{"strategy_id":"owner"},"QQQ":{"strategy_id":"other"}})
            partial = loop.run_once(feed,account,[wrapped])
            self.assertEqual(partial["events"][0]["status"],"SIMULATED")
            self.assertEqual(account.positions["SPY"],2)
            self.assertIn("SPY",account.strategy_state)
            self.assertEqual(len(underlying.calls),1)
            underlying.quantity=2
            orders._risk_decision.return_value=SimpleNamespace(allowed=False,reasons=["synthetic"])
            rejected=loop.run_once(feed,account,[wrapped])
            self.assertEqual(rejected["events"][0]["status"],"REJECTED")
            self.assertIn("SPY",account.strategy_state)
            orders._risk_decision.return_value=decision
            full=loop.run_once(feed,account,[wrapped])
            self.assertEqual(full["events"][0]["status"],"SIMULATED")
            self.assertEqual(account.positions["SPY"],0)
            self.assertNotIn("SPY",account.strategy_state)
            self.assertIn("QQQ",account.strategy_state)
            self.assertEqual(len(underlying.calls),3)
