"""PAPER-only daily runner: wires feed, strategies, risk-checked order service,
persistent accounts and the scheduler. Long-lived: one process per market open."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import time

from cio_market_lab.data.intraday_feed import IntradayFeed
from cio_market_lab.engine.paper_scheduler import run_daily
from cio_market_lab.engine.paper_session import PaperSession
from cio_market_lab.engine.paper_trading_loop import PaperTradingLoop, Signal
from cio_market_lab.engine.strategy_feedback import apply_weights, save_table

UNIVERSE = {
    "US": {"cash": 30000.0, "tickers": {"NVDA": 5, "AMD": 5, "PLTR": 20, "RKLB": 30, "TSLA": 3}, "ticks": 385},
    "TW": {"cash": 1000000.0, "tickers": {"2330.TW": 100, "2317.TW": 500, "2454.TW": 100, "2308.TW": 200}, "ticks": 265},
}
TICK_SECONDS = 60


class _Base:
    take, stop_pct = 0.012, 0.008

    def __init__(self, ticker, size, market="US"):
        self.ticker, self.size, self.entry = ticker, size, None
        self.strategy_id = type(self).__name__ + "_" + market

    def _exit(self, price, account):
        qty = account.positions.get(self.ticker, 0)
        if qty <= 0:
            self.entry = None
            return None
        if self.entry is None:
            self.entry = price
        if price >= self.entry * (1 + self.take) or price <= self.entry * (1 - self.stop_pct):
            return [Signal(self.ticker, "stock", "SELL", qty, None, None,
                           "exit take-profit/stop", "realize", "n/a")]
        return None


class Momentum(_Base):
    def __init__(self, ticker, size, market="US"):
        super().__init__(ticker, size, market)
        self.prev = None

    def signals(self, quotes, account):
        p = quotes[self.ticker].c
        prior, self.prev = self.prev, p
        out = self._exit(p, account)
        if out is not None:
            return out
        if prior is None or p <= prior * 1.001 or account.positions.get(self.ticker, 0) > 0:
            return []
        self.entry = p
        return [Signal(self.ticker, "stock", "BUY", self.size, p * (1 - self.stop_pct),
                       p * (1 + self.take), "1m momentum continuation",
                       "+1.2% swing", "-0.8% stop")]


class MeanRev(_Base):
    def __init__(self, ticker, size, lookback=20, market="US"):
        super().__init__(ticker, size, market)
        self.hist, self.lookback = [], lookback

    def signals(self, quotes, account):
        p = quotes[self.ticker].c
        out = self._exit(p, account)
        mean = sum(self.hist) / len(self.hist) if self.hist else p
        self.hist = (self.hist + [p])[-self.lookback:]
        if out is not None:
            return out
        if len(self.hist) < 5 or p >= mean * 0.997 or account.positions.get(self.ticker, 0) > 0:
            return []
        self.entry = p
        return [Signal(self.ticker, "stock", "BUY", self.size, p * (1 - self.stop_pct),
                       mean, "dip below 20-tick mean", "revert to mean", "-0.8% stop")]


class _RealSleepSession(PaperSession):
    def run_session(self, max_ticks, sleep=None):
        return super().run_session(max_ticks, sleep or (lambda _s: time.sleep(TICK_SECONDS)))


def _seed(path, cash):
    path = Path(path)
    if path.exists():
        st = json.loads(path.read_text())
        st["daily_start_equity"] = None
    else:
        st = {"cash": cash, "positions": {}, "realized_pnl": 0.0, "marks": {}, "daily_start_equity": None}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st))


def build_config(root, feed=None, order_service=None, ticks=None, sleep_free=False):
    root = Path(root)
    wt = root / "weights.json"
    if not wt.exists():
        save_table(wt, {"schema_version": 1, "weights": {}})
    from cio_market_lab.engine.strategy_feedback import load_table
    feed = feed or IntradayFeed()
    if getattr(feed, 'latency_log', 1) is None:
        feed.latency_log = root / 'feed_latency.jsonl'
    markets = {}
    for mkt, spec in UNIVERSE.items():
        acct, jr = root / (mkt + "_account.json"), root / (mkt + "_journal.jsonl")

        def factory(mkt=mkt, spec=spec, acct=acct, jr=jr):
            _seed(acct, spec["cash"])
            orders = order_service or _default_orders(root)
            _configure(orders, mkt, spec)
            strategies = []
            for t, n in spec["tickers"].items():
                strategies += [Momentum(t, n, market=mkt), MeanRev(t, n, market=mkt)]
            strategies = apply_weights(strategies, load_table(wt))
            loop = PaperTradingLoop(orders, root / (mkt + "_loop.jsonl"), max_position_pct=0.2)
            cls = PaperSession if sleep_free else _RealSleepSession
            return cls(feed, loop, strategies, acct, jr, list(spec["tickers"]))

        markets[mkt] = {"session_factory": factory, "ticks": ticks or spec["ticks"],
                        "holidays": [], "journal_path": str(jr), "weight_table_path": str(wt)}
    return {"report_dir": str(root / "reports"), "markets": markets}


def _configure(orders, mkt, spec):
    from cio_market_lab.engine.paper_orders import PaperExperimentSettings
    from cio_market_lab.domain.models import Market
    ccy = "USD" if mkt == "US" else "TWD"
    for name in ("Momentum", "MeanRev"):
        sid = name + "_" + mkt
        if sid in orders.experiments:
            continue
        orders.configure_experiment(PaperExperimentSettings(
            strategy_id=sid, strategy_name=sid, market=Market[mkt], initial_cash=spec["cash"],
            base_currency=ccy, reporting_currency=ccy, enabled=True,
            universe=list(spec["tickers"]), max_position_notional=spec["cash"] * 0.2,
            max_daily_loss=spec["cash"] * 0.05, max_open_positions=5, max_data_age_seconds=300))


def _default_orders(root):
    from cio_market_lab.engine.paper_orders import PaperOrderService
    from cio_market_lab.events.store import EventStore
    from cio_market_lab.engine.portfolio import PortfolioManager
    pm = PortfolioManager(initial_cash_swing=10_000_000, initial_cash_intraday=10_000_000)
    return PaperOrderService(pm, EventStore(Path(root) / "events.db"))


def main(root="data/paper_runtime"):
    cfg = build_config(root)
    only = os.environ.get("PAPER_MARKETS")
    if only:
        cfg["markets"] = {k: v for k, v in cfg["markets"].items() if k in only.split(",")}
    print(json.dumps(run_daily(datetime.now(timezone.utc), cfg)))


if __name__ == "__main__":
    main()
