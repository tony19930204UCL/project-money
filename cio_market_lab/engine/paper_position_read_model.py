"""Conservative FIFO matched-lot read model for simulated paper fills.

Only journal-proven attributes are reported. Missing cost, owner or entry history
never becomes a fabricated realized profit.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from math import isfinite


def _number(value):
    try:
        n = float(value)
        return n if isfinite(n) else None
    except (TypeError, ValueError):
        return None


def build_position_read_model(fills, *, marks=None, feed=None):
    """Return verified open lots, matched closes and diagnostic fills.

    Matching is partitioned by market, instrument and explicit owner. Legacy
    fills without owner are not assigned to a strategy. Fees are allocated
    proportionally on partial closes; tax is accounted on each matched exit.
    """
    lots = defaultdict(list)
    closed = []
    diagnostics = []
    marks = marks or {}
    for index, original in enumerate(fills):
        row = dict(original)
        diagnostics.append(row)
        if row.get("status") not in (None, "SIMULATED"):
            continue
        qty = _number(row.get("size"))
        price = _number(row.get("fill_price"))
        side = str(row.get("side") or "").upper()
        if qty is None or qty <= 0 or price is None or price < 0 or side not in ("BUY", "SELL"):
            continue
        market = row.get("market")
        currency = row.get("currency") or {"TW": "TWD", "US": "USD"}.get(market)
        ticker = row.get("ticker")
        owner = row.get("strategy_id")
        key = (market, currency, ticker, owner)
        fee = _number(row.get("fee"))
        tax = _number(row.get("tax"))
        if side == "BUY":
            lots[key].append({"remaining": qty, "original": qty, "price": price,
                              "fee_remaining": fee, "tax_remaining": tax,
                              "timestamp": row.get("timestamp"),
                              "reason": row.get("thesis"), "strategy_id": owner,
                              "horizon": row.get("horizon"), "product": row.get("product") or "spot"})
            continue
        remaining = qty
        available = sum(l["remaining"] for l in lots[key])
        # No invented basis for an unmatched historical exit.
        if available < qty - 1e-9:
            row["matching_status"] = "UNKNOWN_ENTRY_HISTORY"
        for lot in lots[key]:
            if remaining <= 1e-9:
                break
            take = min(remaining, lot["remaining"])
            if take <= 0:
                continue
            fraction = take / qty
            entry_fraction = take / lot["remaining"]
            entry_fee = None if lot["fee_remaining"] is None else lot["fee_remaining"] * entry_fraction
            entry_tax = None if lot["tax_remaining"] is None else lot["tax_remaining"] * entry_fraction
            exit_fee = None if fee is None else fee * fraction
            exit_tax = None if tax is None else tax * fraction
            costs = (entry_fee, entry_tax, exit_fee, exit_tax)
            gross = (price - lot["price"]) * take
            total_cost = sum(costs) if all(c is not None for c in costs) else None
            closed.append({
                "trade_id": f"{market}:{ticker}:{index}:{len(closed)}",
                "ticker": ticker, "market": market, "currency": currency,
                "strategy_id": owner, "horizon": lot["horizon"], "product": lot["product"],
                "entry_time": lot["timestamp"], "exit_time": row.get("timestamp"),
                "entry_vwap": lot["price"], "exit_vwap": price,
                "entry_reason": lot["reason"], "exit_reason": row.get("thesis"),
                "closed_qty": take, "gross_pnl": gross,
                "fees_tax": total_cost, "net_pnl": gross - total_cost if total_cost is not None else None,
                "cost_status": "VERIFIED" if total_cost is not None else "UNKNOWN_COST",
            })
            lot["remaining"] -= take
            if entry_fee is not None:
                lot["fee_remaining"] -= entry_fee
            if entry_tax is not None:
                lot["tax_remaining"] -= entry_tax
            remaining -= take
        lots[key] = [l for l in lots[key] if l["remaining"] > 1e-9]
    opened = []
    for (market, currency, ticker, owner), entries in lots.items():
        qty = sum(l["remaining"] for l in entries)
        if not qty:
            continue
        mark_data = marks.get(market, {}).get(ticker)
        mark = _number(mark_data.get("price")) if isinstance(mark_data, dict) else _number(mark_data)
        timestamp = mark_data.get("timestamp") if isinstance(mark_data, dict) else None
        basis = sum(l["remaining"] * l["price"] for l in entries) / qty
        known_cost = all(l["fee_remaining"] is not None and l["tax_remaining"] is not None for l in entries)
        open_cost = sum(l["fee_remaining"] + l["tax_remaining"] for l in entries) if known_cost else None
        gross = (mark - basis) * qty if mark is not None else None
        opened.append({
            "ticker": ticker, "market": market, "currency": currency, "strategy_id": owner,
            "horizon": entries[0]["horizon"], "product": entries[0]["product"],
            "qty": qty, "entry_vwap": basis, "entry_reason": entries[0]["reason"],
            "mark": mark, "mark_timestamp": timestamp, "value": qty * mark if mark is not None else None,
            "unrealized_gross": gross,
            "unrealized_net": gross - open_cost if gross is not None and open_cost is not None else None,
            "mark_freshness": "UNKNOWN" if timestamp is None else "TIMESTAMP_AVAILABLE_AGE_UNVERIFIED",
        })
    return {"open_positions": opened, "closed_trades": closed, "fills": diagnostics,
            "realized_by_currency": {ccy: sum(r["net_pnl"] for r in closed if r["currency"] == ccy and r["net_pnl"] is not None)
                                     for ccy in {r["currency"] for r in closed}},
            "feed_freshness": "UNKNOWN" if not feed else "UNVERIFIED"}
