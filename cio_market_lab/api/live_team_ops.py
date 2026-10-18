"""Read-only paper_runtime -> Team Ops public projection.

Each independent source contributes its own cash + positions exactly once.
Same symbols in different books remain separate, labelled holdings. Native USD
and TWD totals are never added without an evidenced FX rate. Swing's persisted
equity is its own close valuation, NOT another cash contribution or a quote.
No network calls, execution imports, writes, or canonical snapshot/cache reads.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cio_market_lab.engine.team_ops import to_public_team_ops_snapshot

EQUITY_RULE = (
    "Per source equity = own cash + positions value, counted once; TW_account, "
    "swing_book and US_account are independent books, including overlapping symbols. "
    "swing_book persisted equity supplies its own recorded positions total when marks "
    "are absent; it is not added again. Sum only within the same currency; "
    "combined equity/cash remain null without verified FX."
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def _timestamp(event: dict) -> str | None:
    return event.get("timestamp") or event.get("ts") or event.get("quote_timestamp") or event.get("at")


def _freshness(timestamp: Any, now: datetime) -> str:
    dt = _time(timestamp)
    if dt is None or dt > now:
        return "UNKNOWN"
    return "fresh" if (now - dt).total_seconds() <= 300 else "stale"


def _read(path: Path, warnings: list[str], *, jsonl: bool = False) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        warnings.append(f"RUNTIME_SOURCE_UNAVAILABLE:{path.name}")
        return [] if jsonl else {}
    if not jsonl:
        try:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
        warnings.append(f"RUNTIME_SOURCE_INVALID:{path.name}")
        return {}
    rows = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("not an object")
            rows.append(row)
        except ValueError:
            warnings.append(f"RUNTIME_ROW_INVALID:{path.name}:{line_no}")
    return rows


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _unknown_fields(template: dict) -> dict[str, Any]:
    return {key: None for key in template}


def _market(symbol: str) -> str:
    return "TW" if symbol.upper().endswith((".TW", ".TWO")) else "US"


def _event_id(source: str, row: dict) -> str:
    # Journals and loops contain the same executions; sequence alone resets each
    # session. Quote timestamp (also present in loop) distinguishes those resets.
    identity = {k: row.get(k) for k in (
        "ticker", "side", "size", "fill_price", "status", "sequence", "reason")}
    identity["time"] = row.get("quote_timestamp") or _timestamp(row)
    encoded = json.dumps(identity, sort_keys=True).encode()
    return source + ":" + hashlib.sha256(encoded).hexdigest()[:24]


def _events(root: Path, market: str, warnings: list[str]) -> list[dict]:
    journal = _read(root / f"{market}_journal.jsonl", warnings, jsonl=True)
    loop = _read(root / f"{market}_loop.jsonl", warnings, jsonl=True)
    unique = {}
    # Prefer the journal's execution timestamp over the loop's quote timestamp.
    for row in journal + loop:
        key = _event_id(market, row)
        if key not in unique:
            unique[key] = row
    return sorted(unique.values(), key=lambda r: _timestamp(r) or "")


def _role(source: str, events: list[dict]) -> dict:
    last = events[-1] if events else {}
    task = None
    if last:
        task = " | ".join(str(v) for v in (
            _timestamp(last), last.get("status") or last.get("act"),
            last.get("ticker") or last.get("t") or last.get("symbol"),
            last.get("reason") or last.get("why") or last.get("error") or last.get("detail"),
        ) if v is not None)
    return {
        "role_id": source, "role_name": source, "title": source,
        "scope": "PAPER_RUNTIME_OBSERVER", "status": "LAST_RECORDED_EVENT" if last else "UNKNOWN",
        "current_task": task, "next_review_time": None,
    }


def build_live_team_ops(root: Path, *, now: datetime | None = None) -> dict:
    """Build a fresh projection on every GET; unknown data stays null/UNKNOWN.

    Safety booleans describe this observer's fail-closed policy, not a claim
    about a running worker or its kill-switch state (which is not recorded).
    """
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    warnings = [
        "LIVE_PAPER_RUNTIME_READ_ONLY; not a canonical snapshot or broker state",
        EQUITY_RULE,
        "UNKNOWN_RUNTIME_KILL_SWITCH: observer fail-closed safety defaults; not worker telemetry",
        "UNKNOWN_MARK_TIMESTAMPS: account marks are recorded values, not live tick claims; "
        "feed_latency timestamps do not timestamp account marks",
        "UNKNOWN_INITIAL_CAPITAL_FX_BENCHMARK: no fabricated returns or combined-currency NAV",
    ]
    # Use the canonical public contract as the shape, but replace inferred/default
    # analytics. Do not run the legacy normalizer again: it fabricates defaults
    # for unknown fields and strips source metadata from holdings.
    out = to_public_team_ops_snapshot({
        "server_time": now.isoformat(), "portfolio": {"nav_status": "OK"},
        "safety": {"paper_only": True}, "integrity_warnings": warnings,
    })
    out["safety"].update(
        paper_only=True, broker_connected=False, broker_state="DISCONNECTED",
        autonomous_capital_decisions=False, autonomous_live_capital_decisions=False,
        autonomous_paper_execution=None, derivative_trading_supported=False,
        kill_switch=True, kill_switch_active=True, fail_safe_defaults_applied=True,
    )
    risk = _unknown_fields(out["risk"])
    risk.update(kill_switch_active=True, kill_switch_armed=None, breaches=[])
    out["risk"] = risk
    for market in ("tw", "us"):
        out["sessions"][market].update(status="UNKNOWN", session_label="UNKNOWN")
    portfolio = _unknown_fields(out["portfolio"])
    portfolio.update(reporting_currency="MIXED", nav_status="NAV_UNAVAILABLE",
                            sources={}, equity_by_currency={}, equity_rule=EQUITY_RULE)
    out["portfolio"] = portfolio
    posture = _unknown_fields(out["posture"])
    posture.update(
        posture="UNKNOWN", posture_label="UNKNOWN", allow_new_entries=False, fail_closed=True,
        rationale="Read-only runtime projection; no recorded team regime or review schedule",
        **{f"{m.lower()}_regime": {"market": m, "regime": "UNKNOWN", "trend": "UNKNOWN",
                                  "volatility": "UNKNOWN", "updated_at": None} for m in ("TW", "US")},
    )
    out["posture"] = posture
    benchmark = _unknown_fields(out["benchmark"])
    benchmark.update(benchmark_name="UNKNOWN", name="UNKNOWN", period="UNKNOWN", currency="MIXED")
    out["benchmark"] = benchmark
    out["holdings"], out["quotes"], out["orders"], out["fills"], out["activity"] = [], [], [], [], []
    roles, all_events = [], []
    sources = out["portfolio"]["sources"]


    for market in ("TW", "US"):
        source = f"{market}_account"
        account = _read(root / f"{source}.json", warnings)
        events = _events(root, market, warnings)
        roles.append(_role(source, events))
        all_events.extend((source, row) for row in events)
        marks = _mapping(account.get("marks"))
        positions = _mapping(account.get("positions"))
        values = []
        for symbol, raw_qty in positions.items():
            qty = _number(raw_qty)
            if qty == 0:
                continue
            if qty is None:
                warnings.append(f"INVALID_POSITION_QUANTITY:{source}:{symbol}")
                values.append(None)
                continue
            px = _number(marks.get(symbol))
            value = qty * px if px is not None else None
            values.append(value)
            owner = _mapping(_mapping(account.get("strategy_state")).get(symbol))
            holding = _holding(symbol, market, source, qty, _number(owner.get("entry")), px, value,
                _mapping(account.get("quote_timestamps")).get(symbol), now)
            holding.update(strategy_id=owner.get("strategy_id"), held_ticks=owner.get("held_ticks"))
            if owner.get("strategy_id"):
                holding["strategy_attribution"] = {owner["strategy_id"]: {"quantity": qty}}
            out["holdings"].append(holding)
        for symbol in sorted(marks.keys() | positions.keys()):
            px = _number(marks.get(symbol))
            previous = _number(_mapping(account.get("previous_close")).get(symbol))
            timestamp = _mapping(account.get("quote_timestamps")).get(symbol)
            out["quotes"].append({"symbol": symbol, "market": market, "price": px,
                "change_pct": (px / previous - 1) * 100 if px is not None and previous and previous > 0 else None,
                "source": source + ".marks" if px is not None else "UNKNOWN", "timestamp": timestamp,
                "freshness": _freshness(timestamp, now) if px is not None else "UNKNOWN"})
        cash = _number(account.get("cash"))
        position_value = sum(values) if all(v is not None for v in values) and isinstance(account.get("positions"), dict) else None
        equity = cash + position_value if cash is not None and position_value is not None else None
        sources[source] = {"currency": "TWD" if market == "TW" else "USD", "cash": cash,
            "positions_value": position_value, "equity": equity,
            "realized_pnl": _number(account.get("realized_pnl")),
            "as_of": account.get("timestamp") or account.get("as_of"),
            "freshness": _freshness(account.get("timestamp") or account.get("as_of"), now),
            "valuation_basis": "own cash + own account marks"}

    book = _read(root / "swing_book.json", warnings)
    swing_events = _read(root / "swing_decisions.jsonl", warnings, jsonl=True)
    swing_events.sort(key=lambda r: _timestamp(r) or "")
    roles.append(_role("swing_book", swing_events))
    all_events.extend(("swing_book", row) for row in swing_events)
    positions = _mapping(book.get("pos"))
    currencies = {"TWD" if _market(t) == "TW" else "USD" for t in positions}
    currency = book.get("currency") or (next(iter(currencies)) if len(currencies) == 1 else "UNKNOWN")
    cash = _number(book.get("cash"))
    values = []
    for symbol, raw_pos in positions.items():
        pos = _mapping(raw_pos)
        qty, entry = _number(pos.get("sh")), _number(pos.get("e"))
        if qty == 0:
            continue
        if qty is None:
            warnings.append(f"INVALID_POSITION_QUANTITY:swing_book:{symbol}")
            values.append(None)
            continue
        px = _number(_mapping(book.get("marks")).get(symbol))
        value = qty * px if px is not None else None
        values.append(value)
        timestamp = _mapping(book.get("quote_timestamps")).get(symbol)
        holding = _holding(symbol, _market(symbol), "swing_book", qty, entry, px, value, timestamp, now)
        holding.update(stop=_number(pos.get("stop")), strategy_id=pos.get("fam"), entry_day=pos.get("entry_day"))
        out["holdings"].append(holding)
        # Keep each recorded source independently selectable, even for the same
        # ticker. Missing marks stay null; entry/peak is never a quote substitute.
        out["quotes"].append({"symbol": symbol, "market": _market(symbol), "price": px,
            "change_pct": None, "source": "swing_book.marks",
            "timestamp": timestamp, "freshness": _freshness(timestamp, now) if px is not None else "UNKNOWN"})
    complete_marks = isinstance(book.get("pos"), dict) and all(v is not None for v in values)
    recorded = _number(book.get("equity"))
    # The producer stores close equity even though it does not persist each mark.
    position_value = sum(values) if complete_marks else (
        recorded - cash if recorded is not None and cash is not None else None)
    equity = cash + position_value if cash is not None and position_value is not None else None
    if len(currencies) > 1 or currency not in {"TWD", "USD"} or (currencies and currency not in currencies):
        equity, position_value = None, None
        warnings.append("SWING_CURRENCY_UNKNOWN_OR_MIXED: cannot value unlike currencies without FX")
    sources["swing_book"] = {"currency": currency, "cash": cash, "positions_value": position_value,
        "equity": equity, "realized_pnl": None, "as_of": book.get("last"),
        "freshness": "UNKNOWN", "valuation_basis": "own cash + own marks" if complete_marks else
        "own persisted close equity (cash + positions); individual marks unavailable"}
    if not complete_marks:
        warnings.append("SWING_INDIVIDUAL_MARKS_UNAVAILABLE: total uses own persisted close equity, not entry/peak/bench0")

    for currency in {s["currency"] for s in sources.values()}:
        group = [s for s in sources.values() if s["currency"] == currency]
        out["portfolio"]["equity_by_currency"][currency] = (
            sum(s["equity"] for s in group) if all(s["equity"] is not None for s in group) else None)

    # Real observations, not invented next-review cadence or working status.
    latency = _read(root / "feed_latency.jsonl", warnings, jsonl=True)
    latency.sort(key=lambda r: _timestamp(r) or "")
    roles.append(_role("feed", latency))
    all_events.extend(("feed", row) for row in latency[-100:])
    params = _read(root / "paper_params.json", warnings)
    version = params.get("version")
    enabled = [key for key, value in _mapping(params.get("per_market")).items()
               if isinstance(value, dict) and value.get("enabled") is True]
    playbook = {"playbook_id": version, "playbook_name": "paper_params" if version else None,
        "description": "Enabled families: " + ", ".join(enabled) if params else None,
        "selection_rationale": params.get("note"), "regime": "UNKNOWN", "data_quality": "UNKNOWN",
        "session_state": "UNKNOWN", "gross_exposure": None, "target_cash_pct": None,
        "risk_multiplier": None, "selected_at": None, "next_review_time": None}
    desk = {"desk_id": "paper_runtime", "desk_name": "Paper runtime (read-only)",
        "active_playbook": playbook, "roles": roles, "next_review_time": None}
    # This is the latest evidenced duty, not a fabricated running-worker claim.
    dated_events = []
    for src, event in all_events:
        observed = _time(_timestamp(event))
        if observed is not None and observed <= now:
            dated_events.append((observed, src, event))
    latest = max(dated_events, key=lambda item: item[0], default=None)
    desk["current_duty"] = ({
        "source": latest[1], "task": _role(latest[1], [latest[2]])["current_task"],
        "observed_at": _timestamp(latest[2]), "status": "LAST_RECORDED_EVENT",
        "freshness": _freshness(_timestamp(latest[2]), now),
    } if latest else None)
    out["posture"].update(desk=desk, active_playbook=playbook, next_review_time=None)
    if book.get("params_ver") and book["params_ver"] != version:
        warnings.append(f"SWING_PARAMS_VERSION_DIFFERS: book={book['params_ver']}; file={version}")

    all_events.sort(key=lambda item: _timestamp(item[1]) or "", reverse=True)
    seen = set()
    for source, event in all_events:
        identity = _event_id(source, event) if source.endswith("_account") else source + ":" + hashlib.sha256(
            json.dumps(event, sort_keys=True).encode()).hexdigest()[:24]
        if identity in seen:
            continue
        seen.add(identity)
        timestamp = _timestamp(event)
        symbol = event.get("ticker") or event.get("t") or event.get("symbol")
        status = event.get("status") or event.get("act") or ("ERROR" if event.get("error") else "OBSERVED")
        if len(out["activity"]) < 100:
            out["activity"].append({"id": identity, "actor": event.get("strategy_id") or source,
                "status": status, "action": event.get("act") or event.get("side") or status,
                "target": symbol, "rationale": event.get("reason") or event.get("why") or event.get("detail") or event.get("error"),
                "timestamp": timestamp})
        is_order = status in {"SIMULATED", "REJECTED"} or (source == "swing_book" and status in {"ENTER", "EXIT"})
        if not is_order:
            continue
        order = {"order_id": identity, "symbol": symbol, "market": _market(symbol) if symbol else None,
            "side": event.get("side") or ("BUY" if status == "ENTER" else "SELL" if status == "EXIT" else None),
            "quantity": _number(event.get("size", event.get("sh"))),
            "price": _number(event.get("fill_price", event.get("e"))), "order_type": None,
            "status": "FILLED" if status in {"SIMULATED", "ENTER", "EXIT"} else "REJECTED",
            "strategy_id": event.get("strategy_id") or event.get("fam"),
            "rationale": event.get("thesis") or event.get("reason") or event.get("why"),
            "created_at": timestamp, "source": source}
        if len(out["orders"]) < 100:
            out["orders"].append(order)
        if order["status"] == "FILLED" and len(out["fills"]) < 100:
            out["fills"].append({"fill_id": identity + ":fill", "order_id": identity, "symbol": symbol,
                "market": order["market"], "side": order["side"], "quantity": order["quantity"],
                "price": order["price"], "fee": _number(event.get("fee")), "tax": _number(event.get("tax")),
                "strategy_id": order["strategy_id"], "timestamp": timestamp, "source": source})
    warnings.append("HISTORY_BOUNDED: newest 100 orders/fills/activity; runtime logs are history, not active broker orders")
    quote_freshness = [q["freshness"] for q in out["quotes"]]
    out["data_freshness"] = ("fresh" if quote_freshness and all(f == "fresh" for f in quote_freshness)
                             else "stale" if "stale" in quote_freshness else "unavailable")
    out["integrity_warnings"] = warnings
    return out


def _holding(symbol, market, source, qty, entry, px, value, timestamp, now):
    cost = qty * entry if entry is not None else None
    pnl = value - cost if value is not None and cost is not None else None
    ret = pnl / cost * 100 if pnl is not None and cost else None
    return {"symbol": symbol, "market": market, "currency": "TWD" if market == "TW" else "USD",
        "quantity": qty, "entry_price": entry, "average_entry_price": entry, "cost_basis": cost,
        "current_price": px, "market_value": value, "unrealized_pnl": pnl, "realized_pnl": None,
        "return_pct": ret, "unrealized_pnl_pct": ret,
        "price_freshness": _freshness(timestamp, now) if px is not None else "UNKNOWN",
        "strategy_attribution": {source: {"quantity": qty}}, "source": source}
