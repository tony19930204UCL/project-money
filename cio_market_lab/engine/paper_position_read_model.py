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


def build_position_read_model(fills, *, marks=None, feed=None, accounts=None):
    """Return verified open lots, matched closes and diagnostic fills.

    Matching is partitioned by market, instrument and explicit owner. Legacy
    fills without owner are not assigned to a strategy. Fees are allocated
    proportionally on partial closes; tax is accounted on each matched exit.
    """
    lots = defaultdict(list)
    closed = []
    diagnostics = []
    unmatched_exits = defaultdict(float)
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
        if remaining > 1e-9:
            unmatched_exits[(market, currency, ticker)] += remaining
            diagnostics[-1]["unmatched_exit_qty"] = remaining
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
    reconciliation = {}
    for market, account in (accounts or {}).items():
        if account.get("state") != "OK":
            continue
        currency = {"US": "USD", "TW": "TWD"}.get(market)
        canonical = {p["ticker"]: float(p["qty"]) for p in account.get("positions", [])}
        for ticker, expected in canonical.items():
            candidates = [p for p in opened if p["market"] == market and p["ticker"] == ticker]
            derived = sum(p["qty"] for p in candidates)
            raw_derived = derived
            if derived > expected + 1e-9:
                for p in candidates:
                    opened.remove(p)
                derived = 0.0
            remainder = expected - derived
            if remainder > 1e-9:
                source = next(p for p in account["positions"] if p["ticker"] == ticker)
                mark = _number(source.get("mark"))
                opened.append({
                    "ticker": ticker, "market": market, "currency": currency,
                    "strategy_id": None, "horizon": None, "product": "spot",
                    "qty": remainder, "entry_vwap": None, "entry_reason": None,
                    "mark": mark, "mark_timestamp": None, "mark_freshness": "UNKNOWN",
                    "value": remainder * mark if mark is not None else None,
                    "unrealized_gross": None, "unrealized_net": None,
                    "basis_status": "UNKNOWN_HISTORY",
                })
            reconciliation[f"{market}:{ticker}"] = {
                "account_qty": expected, "journal_matched_open_qty": derived,
                "journal_reconstructed_open_qty": raw_derived,
                "journal_account_delta_qty": raw_derived - expected,
                "quarantined_journal_qty": raw_derived if raw_derived > expected + 1e-9 else 0.0,
                "quarantine_reason": "JOURNAL_EXCEEDS_CANONICAL_ACCOUNT" if raw_derived > expected + 1e-9 else None,
                "unknown_basis_qty": max(0.0, remainder),
                "status": ("JOURNAL_ACCOUNT_CONFLICT" if raw_derived > expected + 1e-9 else
                           "MATCHED" if abs(remainder) < 1e-9 else "INCOMPLETE_HISTORY"),
            }
        for p in opened:
            if p["market"] == market and p["ticker"] not in canonical:
                key = f"{market}:{p['ticker']}"
                rec = reconciliation.setdefault(key, {"account_qty": 0.0, "journal_matched_open_qty": 0.0, "journal_reconstructed_open_qty": 0.0, "journal_account_delta_qty": 0.0, "quarantined_journal_qty": 0.0, "unknown_basis_qty": 0.0, "status": "JOURNAL_ACCOUNT_CONFLICT", "quarantine_reason": "orphan_ledger_position", "discrepancies": []})
                rec["journal_reconstructed_open_qty"] += p["qty"]
                rec["journal_account_delta_qty"] += p["qty"]
                rec["quarantined_journal_qty"] += p["qty"]
                rec["discrepancies"].append({"type": "orphan_ledger_position", "ticker": p["ticker"], "strategy": p["strategy_id"], "raw_ledger_qty": p["qty"], "canonical_qty": 0.0, "reconciled_qty": 0.0})
        opened[:] = [p for p in opened if p["market"] != market or p["ticker"] in canonical]
    realized = {}
    completeness = {}
    for currency in ({r["currency"] for r in closed} | {k[1] for k in unmatched_exits}):
        subset = [r for r in closed if r["currency"] == currency]
        unknown = [r for r in subset if r["net_pnl"] is None]
        subtotal = sum(r["net_pnl"] for r in subset if r["net_pnl"] is not None)
        incomplete_exit = any(k[1] == currency for k in unmatched_exits)
        incomplete_account = any(k.startswith(("US:" if currency == "USD" else "TW:")) and v["status"] != "MATCHED" for k, v in reconciliation.items())
        complete = not unknown and not incomplete_exit and not incomplete_account
        realized[currency] = subtotal if complete else None
        completeness[currency] = {
            "verified_subtotal": subtotal,
            "unknown_cost_trades": len(unknown),
            "unknown_cost_qty": sum(r["closed_qty"] for r in unknown),
            "unmatched_exit_qty": sum(v for k, v in unmatched_exits.items() if k[1] == currency),
            "complete": complete,
            "account_reconciliation": "NOT_VERIFIED",
        }
    return {"open_positions": opened, "closed_trades": closed, "fills": diagnostics,
            "realized_by_currency": realized, "realized_completeness": completeness,
            "position_reconciliation": reconciliation,
            "unmatched_exits": [{"market": k[0], "currency": k[1], "ticker": k[2], "qty": v}
                                for k, v in unmatched_exits.items()],
            "feed_freshness": "UNKNOWN" if not feed else "UNVERIFIED"}
