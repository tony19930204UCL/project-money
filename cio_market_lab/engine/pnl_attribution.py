"""Deterministic PAPER-only PnL attribution and decision-learning feedback.

Inputs are journal event mappings and closed-trade mappings. PnL amounts must
share one reporting currency; benchmark_return is a decimal return.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import date
import math


def _number(value, field, default=0.0):
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(field + " must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(field + " must be finite")
    return result


def _key(row):
    return (str(row.get("strategy_id") or row.get("strategy") or "UNASSIGNED"),
            str(row.get("instrument_type") or "stock").lower(),
            str(row.get("horizon") or "UNSPECIFIED").lower())


def _metrics(rows, benchmark_return):
    realized = sum(x["realized"] for x in rows)
    unrealized = sum(x["unrealized"] for x in rows)
    fees = sum(x["fees"] for x in rows)
    closed = [x for x in rows if x["closed"]]
    wins = sum(x["realized"] > 0 for x in closed)
    count = len(closed)
    invested = sum(x["capital"] for x in rows)
    # Drawdown uses ordered realized trade outcomes, starting at zero.
    equity = peak = drawdown = 0.0
    for x in sorted(closed, key=lambda item: (item["timestamp"], item["index"])):
        equity += x["realized"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    net = realized + unrealized
    return dict(realized_pnl=realized, unrealized_pnl=unrealized,
                total_pnl=net, closed_trades=count,
                win_rate=wins / count if count else None,
                expectancy=realized / count if count else None,
                max_drawdown=drawdown, fee_drag=fees,
                return_rate=net / invested if invested > 0 else None,
                benchmark_relative_return=(net / invested - benchmark_return
                                           if invested > 0 and benchmark_return is not None else None))


def attribute_pnl(journal=(), closed_trades=(), *, benchmark_return=None,
                  as_of=None):
    """Attribute closed trades plus journal open marks without double counting.

    Closed trades: realized_pnl, optional fees/tax, capital/notional,
    strategy_id, instrument_type, horizon, closed_at.
    Journal: SIMULATED fills (fee/tax tracked but no realized PnL), or
    OPEN_POSITION snapshots with unrealized_pnl and capital. Rejected
    journal events are ignored. For a position represented in closed_trades,
    only explicitly OPEN_POSITION journal rows contribute unrealized PnL.
    """
    if benchmark_return is not None:
        benchmark_return = _number(benchmark_return, "benchmark_return")
    derived, incomplete = _match_synthetic_fills(journal)
    closed_trades = list(closed_trades) + derived
    rows = []
    for index, trade in enumerate(closed_trades):
        if not isinstance(trade, dict):
            raise TypeError("closed trade must be a mapping")
        fees = _number(trade.get("fee", trade.get("fees", 0)), "fees") + _number(trade.get("tax", 0), "tax")
        realized = _number(trade.get("realized_pnl", 0), "realized_pnl")
        capital = _number(trade.get("capital", trade.get("notional", 0)), "capital")
        if fees < 0 or capital < 0:
            raise ValueError("negative fees or capital")
        rows.append(dict(key=_key(trade), realized=realized, unrealized=0.,
                         fees=fees, capital=capital, closed=True,
                         timestamp=str(trade.get("closed_at") or trade.get("timestamp") or ""),
                         index=index))
    offset = len(rows)
    for index, event in enumerate(journal):
        if not isinstance(event, dict):
            raise TypeError("journal event must be a mapping")
        status = str(event.get("status", "")).upper()
        if status not in ("SIMULATED", "OPEN_POSITION"):
            continue
        fees = _number(event.get("fee", 0), "fee") + _number(event.get("tax", 0), "tax")
        capital = _number(event.get("capital", event.get("notional", 0)), "capital")
        if fees < 0 or capital < 0:
            raise ValueError("negative fees or capital")
        rows.append(dict(key=_key(event), realized=0.,
                         unrealized=_number(event.get("unrealized_pnl", 0), "unrealized_pnl") if status == "OPEN_POSITION" else 0.,
                         fees=fees, capital=capital if status == "OPEN_POSITION" else 0.,
                         closed=False, timestamp=str(event.get("quote_timestamp") or ""),
                         index=offset + index))
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["key"]].append(row)
    segments = {}
    for key in sorted(grouped):
        strategy, instrument, horizon = key
        segments.setdefault(strategy, {}).setdefault(instrument, {})[horizon] = _metrics(grouped[key], benchmark_return)
    by_strategy = {}
    for strategy in sorted({key[0] for key in grouped}):
        by_strategy[strategy] = _metrics([row for row in rows if row["key"][0] == strategy], benchmark_return)
    overall = _metrics(rows, benchmark_return)
    currencies = sorted({str(x.get("currency")) for x in closed_trades if x.get("currency")})
    if len(currencies) > 1 or incomplete:
        for field in ("realized_pnl", "total_pnl", "expectancy", "return_rate", "benchmark_relative_return"):
            overall[field] = None
    overall["accounting_status"] = "INCOMPLETE" if incomplete else ("MULTI_CURRENCY" if len(currencies) > 1 else "COMPLETE")
    overall["incomplete_events"] = incomplete
    overall["currencies"] = currencies
    worked, failed, downweight, suggestions = [], [], [], {}
    for strategy, metrics in by_strategy.items():
        if not metrics["closed_trades"]:
            continue
        if metrics["expectancy"] > 0:
            worked.append(strategy)
        elif metrics["expectancy"] < 0:
            failed.append(strategy)
            downweight.append(strategy)
            suggestions[strategy] = {"position_size_multiplier": 0.5,
                                     "reason": "Negative realized expectancy; paper-only review"}
        if metrics["fee_drag"] > abs(metrics["realized_pnl"]) and metrics["fee_drag"] > 0:
            suggestions.setdefault(strategy, {})["review_transaction_costs"] = True
    if incomplete or len(currencies) > 1:
        worked, failed, downweight, suggestions = [], [], [], {}
    feedback = dict(what_worked=worked, what_failed=failed,
                    suggested_parameter_changes=suggestions,
                    strategies_to_downweight=downweight,
                    validation_status="RECORDED",
                    paper_only=True,
                    note="Observational feedback; requires out-of-sample validation before changing strategy parameters")
    day = str(as_of or date.today())
    lines = [f"# Paper PnL Attribution — {day}", "",
             f"Realized: {overall['realized_pnl'] if overall['realized_pnl'] is not None else 'N/A'} | Unrealized: {overall['unrealized_pnl']:.2f} | Fees/tax: {overall['fee_drag']:.2f}",
             f"Closed trades: {overall['closed_trades']} | Max drawdown: {overall['max_drawdown']:.2f}", "",
             "| Strategy | Realized | Unrealized | Win rate | Expectancy | Fee drag |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for strategy, m in by_strategy.items():
        win = "N/A" if m["win_rate"] is None else f"{m['win_rate']:.1%}"
        exp = "N/A" if m["expectancy"] is None else f"{m['expectancy']:.2f}"
        lines.append(f"| {strategy.replace('|', '/')} | {m['realized_pnl']:.2f} | {m['unrealized_pnl']:.2f} | {win} | {exp} | {m['fee_drag']:.2f} |")
    lines.extend(["", "Learning feedback is observational, not a live trade instruction."])
    return dict(overall=overall, by_currency=_currency_metrics(rows, closed_trades, benchmark_return), by_strategy=by_strategy, by_strategy_instrument_horizon=segments,
                learning_feedback=feedback, daily_markdown="\n".join(lines))



def _currency_metrics(rows, trades, benchmark):
    currencies = sorted({str(t.get("currency")) for t in trades if t.get("currency")})
    result = {}
    for currency in currencies:
        selected = [rows[i] for i, trade in enumerate(trades)
                    if trade.get("currency") == currency]
        result[currency] = _metrics(selected, benchmark)
    return result


def _match_synthetic_fills(journal):
    """FIFO paper fills; never manufacture a cost basis or ownership.

    The loop and session can journal the same event twice. Their timestamps
    differ, but sequence, quote timestamp and economic fill fields agree.
    """
    inventory = defaultdict(deque)
    seen = set()
    closed = []
    incomplete = []
    for index, event in enumerate(journal):
        if not isinstance(event, dict) or event.get("status") != "SIMULATED":
            continue
        side = str(event.get("side", "")).upper()
        if side not in ("BUY", "SELL"):
            continue
        fingerprint = tuple(str(event.get(k)) for k in
            ("sequence", "quote_timestamp", "ticker", "strategy_id",
             "side", "size", "fill_price", "fee", "tax"))
        if event.get("sequence") is not None and fingerprint in seen:
            continue
        seen.add(fingerprint)
        owner, ticker = event.get("strategy_id"), event.get("ticker")
        currency = event.get("currency")
        if not currency:
            market = str(event.get("market", "")).upper()
            currency = {"US": "USD", "TW": "TWD"}.get(market)
            if not currency and isinstance(ticker, str) and ticker.upper().endswith((".TW", ".TWO")):
                currency = "TWD"
        try:
            qty = _number(event.get("size"), "size", None)
            price = _number(event.get("fill_price"), "fill_price", None)
            fee = _number(event.get("fee"), "fee", None)
            tax = _number(event.get("tax"), "tax", None)
            if any(v is None or v < 0 for v in (qty, price, fee, tax)) or qty == 0:
                raise ValueError("invalid fill economics")
        except (ValueError, TypeError):
            incomplete.append({"index": index, "reason": "MISSING_OR_INVALID_FILL"})
            continue
        if not owner or not ticker or not currency:
            incomplete.append({"index": index, "reason": "UNKNOWN_OWNER_OR_CURRENCY"})
            continue
        key = (str(owner), str(ticker), str(currency))
        if side == "BUY":
            inventory[key].append([qty, price, fee, tax, event])
            continue
        available = sum(lot[0] for lot in inventory[key])
        if available + 1e-9 < qty:
            incomplete.append({"index": index, "reason": "UNMATCHED_SELL"})
            continue
        remaining = qty
        while remaining > 1e-9:
            lot = inventory[key][0]
            taken = min(remaining, lot[0])
            fraction = taken / qty
            entry_fraction = taken / lot[0]
            entry_fee = lot[2] * entry_fraction
            entry_tax = lot[3] * entry_fraction
            exit_fee, exit_tax = fee * fraction, tax * fraction
            gross = (price - lot[1]) * taken
            closed.append({
                "strategy_id": owner, "ticker": ticker, "currency": currency,
                "instrument_type": event.get("instrument_type") or "stock",
                "horizon": event.get("horizon") or "UNSPECIFIED",
                "realized_pnl": gross - entry_fee - entry_tax - exit_fee - exit_tax,
                "gross_realized_pnl": gross,
                "fee": entry_fee + exit_fee, "tax": entry_tax + exit_tax,
                "capital": lot[1] * taken,
                "closed_at": event.get("quote_timestamp") or event.get("timestamp") or "",
            })
            lot[0] -= taken
            lot[2] -= entry_fee
            lot[3] -= entry_tax
            remaining -= taken
            if lot[0] <= 1e-9:
                inventory[key].popleft()
    return closed, incomplete


class PnLAttribution:
    """Convenience adapter for the existing decision-learning pipeline."""

    def run(self, journal=(), closed_trades=(), **kwargs):
        return attribute_pnl(journal, closed_trades, **kwargs)
