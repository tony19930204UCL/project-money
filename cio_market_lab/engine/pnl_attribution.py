"""Deterministic PAPER-only PnL attribution and decision-learning feedback.

Inputs are journal event mappings and closed-trade mappings. PnL amounts must
share one reporting currency; benchmark_return is a decimal return.
"""
from __future__ import annotations

from collections import defaultdict
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
    feedback = dict(what_worked=worked, what_failed=failed,
                    suggested_parameter_changes=suggestions,
                    strategies_to_downweight=downweight,
                    validation_status="RECORDED",
                    paper_only=True,
                    note="Observational feedback; requires out-of-sample validation before changing strategy parameters")
    day = str(as_of or date.today())
    lines = [f"# Paper PnL Attribution — {day}", "",
             f"Realized: {overall['realized_pnl']:.2f} | Unrealized: {overall['unrealized_pnl']:.2f} | Fees/tax: {overall['fee_drag']:.2f}",
             f"Closed trades: {overall['closed_trades']} | Max drawdown: {overall['max_drawdown']:.2f}", "",
             "| Strategy | Realized | Unrealized | Win rate | Expectancy | Fee drag |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for strategy, m in by_strategy.items():
        win = "N/A" if m["win_rate"] is None else f"{m['win_rate']:.1%}"
        exp = "N/A" if m["expectancy"] is None else f"{m['expectancy']:.2f}"
        lines.append(f"| {strategy.replace('|', '/')} | {m['realized_pnl']:.2f} | {m['unrealized_pnl']:.2f} | {win} | {exp} | {m['fee_drag']:.2f} |")
    lines.extend(["", "Learning feedback is observational, not a live trade instruction."])
    return dict(overall=overall, by_strategy=by_strategy, by_strategy_instrument_horizon=segments,
                learning_feedback=feedback, daily_markdown="\n".join(lines))


class PnLAttribution:
    """Convenience adapter for the existing decision-learning pipeline."""

    def run(self, journal=(), closed_trades=(), **kwargs):
        return attribute_pnl(journal, closed_trades, **kwargs)
