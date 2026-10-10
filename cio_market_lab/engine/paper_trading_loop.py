"""Offline PAPER-only tick loop. Never routes orders to a broker."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import json
import math

from cio_market_lab.engine.paper_orders import PaperOrderRequest, PaperDataContext
from cio_market_lab.domain.models import Market, DecisionScope, OrderSide, OrderType, OrderOrigin


@dataclass(frozen=True)
class Signal:
    ticker: str
    instrument_type: str
    side: str
    size: float
    stop: float | None
    target: float | None
    thesis: str
    expected_outcome: str = ""
    invalidation: str = ""


@dataclass
class PaperAccount:
    cash: float
    positions: dict = field(default_factory=dict)
    realized_pnl: float = 0.0
    daily_start_equity: float | None = None
    marks: dict = field(default_factory=dict)


class MomentumSwing:
    def __init__(self, ticker, size=1):
        self.ticker, self.size, self.previous = ticker, size, None

    def signals(self, quotes, account):
        price = quotes[self.ticker].c
        prior, self.previous = self.previous, price
        if prior is None or price <= prior:
            return []
        return [Signal(self.ticker, "stock", "BUY", self.size, prior,
                       price + 2 * (price - prior), "Positive momentum",
                       "Continuation", "Break below previous close")]


class MeanReversion:
    def __init__(self, ticker, size=1, lookback=5):
        self.ticker, self.size, self.lookback = ticker, size, lookback
        self.history = []

    def signals(self, quotes, account):
        price = quotes[self.ticker].c
        mean = sum(self.history) / len(self.history) if self.history else price
        self.history.append(price)
        self.history = self.history[-self.lookback:]
        if price >= mean:
            return []
        return [Signal(self.ticker, "stock", "BUY", self.size, price * .95,
                       mean, "Mean reversion", "Recovery", "Further decline")]


class PaperTradingLoop:
    def __init__(self, order_service, journal_path, *, max_position_pct=.2,
                 max_leverage=1., daily_loss_limit=.05, fee_rate=.001,
                 tw_sell_tax=.003, spread_bps=5, seed=0, derivative_engine=None,
                 contracts=None):
        self.orders = order_service
        self.journal_path = Path(journal_path)
        self.max_position_pct = max_position_pct
        self.max_leverage = max_leverage
        self.daily_loss_limit = daily_loss_limit
        self.fee_rate = fee_rate
        self.tw_sell_tax = tw_sell_tax
        self.spread_bps = spread_bps
        self.seed = seed
        self.sequence = 0
        self.derivatives = derivative_engine
        self.contracts = contracts or {}

    def _journal(self, record):
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")

    @staticmethod
    def _equity(account, quotes):
        return account.cash + sum(qty * quotes[sym].c
                                  for sym, qty in account.positions.items()
                                  if sym in quotes)

    def run_once(self, feed, account, strategies):
        symbols = sorted({s.ticker for s in strategies} | set(account.positions))
        quotes = {symbol: feed.latest(symbol) for symbol in symbols}
        if any(q is None or not math.isfinite(q.c) or q.c <= 0
               or q.staleness_seconds > 300 or q.staleness_seconds < 0
               for q in quotes.values()):
            raise ValueError("INVALID_OR_STALE_QUOTE")
        equity = self._equity(account, quotes)
        if account.daily_start_equity is None:
            account.daily_start_equity = equity
        breaker = equity <= account.daily_start_equity * (1 - self.daily_loss_limit)
        results = []
        for strategy in strategies:
            for signal in strategy.signals(quotes, account):
                self.sequence += 1
                quote = quotes.get(signal.ticker)
                side = signal.side.upper()
                kind = signal.instrument_type.lower()
                reason = None
                fill = None
                fee = tax = 0.
                if quote is None or side not in ("BUY", "SELL") or not math.isfinite(signal.size) or signal.size <= 0:
                    reason = "INVALID_SIGNAL"
                elif kind not in ("stock", "etf", "option", "warrant", "future"):
                    reason = "INVALID_INSTRUMENT"
                elif breaker:
                    reason = "DAILY_LOSS_CIRCUIT_BREAKER"
                elif kind in ("option", "warrant", "future"):
                    # Existing derivatives engine requires authenticated contract,
                    # margin and expiry lifecycle. No synthetic approval is allowed.
                    spec = self.contracts.get(signal.ticker)
                    if spec is None or self.derivatives is None:
                        reason = "DERIVATIVE_CONTRACT_OR_ENGINE_REQUIRED"
                    elif spec.expiry is not None and quote.ts >= spec.expiry:
                        reason = "CONTRACT_EXPIRED"
                    else:
                        reason = "DERIVATIVE_EXECUTION_ADAPTER_NOT_ACCEPTED"
                else:
                    price = quote.c * (1 + self.spread_bps / 20000 * (1 if side == "BUY" else -1))
                    signed = signal.size if side == "BUY" else -signal.size
                    old_qty = account.positions.get(signal.ticker, 0)
                    new_qty = old_qty + signed
                    gross = sum(abs(qty * quotes[sym].c) for sym, qty in account.positions.items())
                    projected = gross - abs(old_qty * quote.c) + abs(new_qty * quote.c)
                    if new_qty < 0:
                        reason = "NO_SHORT_STOCK"
                    elif new_qty * quote.c > equity * self.max_position_pct + 1e-8:
                        reason = "POSITION_LIMIT"
                    elif projected > equity * self.max_leverage + 1e-8:
                        reason = "LEVERAGE_LIMIT"
                    else:
                        fee = price * signal.size * self.fee_rate
                        tax = price * signal.size * self.tw_sell_tax if side == "SELL" and signal.ticker.upper().endswith((".TW", ".TWO")) else 0.
                        cash_delta = (-1 if side == "BUY" else 1) * price * signal.size - fee - tax
                        if account.cash + cash_delta < -1e-8:
                            reason = "INSUFFICIENT_CASH"
                        else:
                            request = PaperOrderRequest(
                                symbol=signal.ticker,
                                market=Market.TW if signal.ticker.upper().endswith((".TW", ".TWO")) else Market.US,
                                bucket=DecisionScope.SWING, side=OrderSide(side),
                                order_type=OrderType.MARKET, quantity=signal.size,
                                origin=OrderOrigin.STRATEGY, reason=signal.thesis,
                                strategy_id=getattr(strategy, "strategy_id", None),
                                data=PaperDataContext(source=quote.source, observed_at=quote.ts,
                                                      age_seconds=quote.staleness_seconds,
                                                      last_price=quote.c),
                                instrument_type="ETF" if kind == "etf" else "EQUITY")
                            decision = self.orders._risk_decision(request)
                            if not decision.allowed:
                                reason = "PAPER_ORDER_REJECTED:" + ",".join(decision.reasons)
                            else:
                                try:
                                    order = self.orders.submit(request)
                                except ValueError as exc:
                                    reason = "PAPER_ORDER_REJECTED:" + str(exc)
                                else:
                                    account.cash += cash_delta
                                    account.positions[signal.ticker] = new_qty
                                    account.marks[signal.ticker] = quote.c
                                    fill = price
                record = dict(sequence=self.sequence, ticker=signal.ticker,
                              instrument_type=kind, side=side, size=signal.size,
                              thesis=signal.thesis, expected_outcome=signal.expected_outcome,
                              invalidation=signal.invalidation, stop=signal.stop,
                              target=signal.target, status="REJECTED" if reason else "SIMULATED",
                              reason=reason or "PAPER_ORDER_SUBMITTED_LOCAL_FILL_ESTIMATE",
                              fill_price=fill, fee=fee if not reason else 0.,
                              tax=tax if not reason else 0., paper_only=True,
                              quote_timestamp=quote.ts.isoformat() if quote else None)
                self._journal(record)
                results.append(record)
        return dict(events=results, equity=self._equity(account, quotes),
                    circuit_breaker=breaker, marks={s: q.c for s, q in quotes.items()})
