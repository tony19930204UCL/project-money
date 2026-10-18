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


def _session_state(market: str, now: datetime) -> dict:
    """Weekday/time-of-day estimate only (no holiday calendar), labelled as such."""
    from zoneinfo import ZoneInfo
    tz, (o, c) = (("Asia/Taipei", ((9, 0), (13, 30))) if market == "tw" else ("America/New_York", ((9, 30), (16, 0))))
    local = now.astimezone(ZoneInfo(tz))
    minutes = local.hour * 60 + local.minute
    is_open = local.weekday() < 5 and o[0] * 60 + o[1] <= minutes < c[0] * 60 + c[1]
    label = ("開盤中" if is_open else ("週末休市" if local.weekday() >= 5 else "非交易時段")) + "（依時段推算，未含假日）"
    return {"status": "OPEN" if is_open else "CLOSED", "session_label": label}


def _file_stamp(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


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


def _fifo(source: str, market: str, events: list[dict]) -> tuple[dict, list[dict]]:
    """Rebuild open lots and closed long round trips from recorded simulated fills (FIFO)."""
    lots: dict[str, list[dict]] = {}
    trips: list[dict] = []
    for ev in events:
        if ev.get("status") != "SIMULATED":
            continue
        sym, side = ev.get("ticker"), ev.get("side")
        qty, px = _number(ev.get("size")), _number(ev.get("fill_price"))
        if not sym or qty is None or px is None or qty <= 0 or side not in {"BUY", "SELL"}:
            continue
        fee = (_number(ev.get("fee")) or 0.0) + (_number(ev.get("tax")) or 0.0)
        if side == "BUY":
            lots.setdefault(sym, []).append({"qty": qty, "px": px, "fee": fee, "q0": qty,
                "ts": _timestamp(ev), "strategy": ev.get("strategy_id"), "thesis": ev.get("thesis"),
                "stop": _number(ev.get("stop")), "target": _number(ev.get("target"))})
            continue
        left, legs, cost, buy_fee = qty, [], 0.0, 0.0
        queue = lots.get(sym, [])
        while left > 1e-9 and queue:
            lot = queue[0]
            take = min(left, lot["qty"])
            share = take / lot["q0"]
            legs.append({"qty": take, "price": lot["px"], "time": lot["ts"], "fee": lot["fee"] * share,
                         "strategy": lot["strategy"], "thesis": lot["thesis"]})
            cost += take * lot["px"]
            buy_fee += lot["fee"] * share
            lot["qty"] -= take
            left -= take
            if lot["qty"] <= 1e-9:
                queue.pop(0)
        matched = qty - left
        if matched <= 1e-9:
            continue
        proceeds = matched * px
        pnl = proceeds - cost - buy_fee - fee * matched / qty
        strategies = sorted({l["strategy"] for l in legs if l["strategy"]})
        trips.append({"trip_id": f"{source}:{sym}:{_timestamp(ev)}", "symbol": sym, "market": market,
            "currency": "TWD" if market == "TW" else "USD", "source": source, "direction": "LONG",
            "strategy_id": "+".join(strategies) or ev.get("strategy_id"), "quantity": matched,
            "buy_price": cost / matched, "sell_price": px, "buy_time": legs[0]["time"],
            "sell_time": _timestamp(ev), "pnl": pnl, "pnl_pct": pnl / cost * 100 if cost else None,
            "fees": buy_fee + fee * matched / qty, "exit_reason": ev.get("thesis"),
            "entry_reason": legs[0]["thesis"],
            "legs": {"buys": legs, "sell": {"qty": matched, "price": px, "time": _timestamp(ev), "fee": fee * matched / qty}}})
    return lots, trips


def _open_lot_summary(lots: list[dict]) -> dict | None:
    qty = sum(l["qty"] for l in lots)
    if qty <= 1e-9:
        return None
    strategies = sorted({l["strategy"] for l in lots if l["strategy"]})
    return {"entry": sum(l["qty"] * l["px"] for l in lots) / qty, "strategy": "+".join(strategies) or None,
            "entered_at": lots[0]["ts"], "stop": lots[-1]["stop"], "target": lots[-1]["target"]}


def _latest_eod(root: Path) -> tuple[dict, str | None]:
    """Latest official end-of-day closes (TWSE+TPEX) stored beside the runtime dir."""
    folder = Path(root).parent / "official_eod"
    closes: dict[str, float] = {}
    day = None
    try:
        files = sorted(folder.glob("twse_*.json"))
        if not files:
            return {}, None
        stem = files[-1].stem.split("_")[1]
        day = f"{int(stem[:-4]) + 1911}-{stem[-4:-2]}-{stem[-2:]}"
        for venue in ("twse", "tpex"):
            f = folder / f"{venue}_{stem}.json"
            if f.exists():
                for code, row in json.loads(f.read_text()).items():
                    c = _number(row.get("close")) if isinstance(row, dict) else None
                    if c:
                        closes[code] = c
    except (OSError, ValueError):
        return {}, None
    return closes, day


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
        out["sessions"][market].update(**_session_state(market, now))
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
    out["round_trips"] = []
    roles, all_events = [], []
    sources = out["portfolio"]["sources"]


    for market in ("TW", "US"):
        source = f"{market}_account"
        account = _read(root / f"{source}.json", warnings)
        events = _events(root, market, warnings)
        roles.append(_role(source, events))
        all_events.extend((source, row) for row in events)
        open_lots, trips = _fifo(source, market, events)
        out["round_trips"].extend(trips)
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
            derived = _open_lot_summary(open_lots.get(symbol, []))
            entry = _number(owner.get("entry"))
            strategy = owner.get("strategy_id")
            if derived and abs(sum(l["qty"] for l in open_lots[symbol]) - qty) < 1e-6:
                entry = entry if entry is not None else derived["entry"]
                strategy = strategy or derived["strategy"]
            holding = _holding(symbol, market, source, qty, entry, px, value,
                _mapping(account.get("quote_timestamps")).get(symbol) or (_file_stamp(root / f"{source}.json") if px is not None else None), now)
            holding.update(strategy_id=strategy, held_ticks=owner.get("held_ticks"))
            if derived:
                holding.update(entry_day=(derived["entered_at"] or "")[:10] or None, stop=derived["stop"],
                               entry_basis="由逐筆成交紀錄(FIFO)還原")
            if strategy:
                holding["strategy_attribution"] = {strategy: {"quantity": qty}}
            out["holdings"].append(holding)
        for symbol in sorted(marks.keys() | positions.keys()):
            px = _number(marks.get(symbol))
            previous = _number(_mapping(account.get("previous_close")).get(symbol))
            timestamp = _mapping(account.get("quote_timestamps")).get(symbol)
            from_file = False
            if timestamp is None and px is not None:
                timestamp, from_file = _file_stamp(root / f"{source}.json"), True
            out["quotes"].append({"symbol": symbol, "market": market, "price": px,
                "change_pct": (px / previous - 1) * 100 if px is not None and previous and previous > 0 else None,
                "source": (source + ".marks") if px is not None else "UNKNOWN", "timestamp": timestamp,
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
    eod_close, eod_day = _latest_eod(root)
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
        timestamp = _mapping(book.get("quote_timestamps")).get(symbol)
        eod_ref = None
        if px is None and eod_close.get(symbol.split(".")[0]) is not None:
            eod_ref = eod_close[symbol.split(".")[0]]
        value = qty * px if px is not None else None
        values.append(value)
        holding = _holding(symbol, _market(symbol), "swing_book", qty, entry, px, value, timestamp, now)
        holding.update(stop=_number(pos.get("stop")), strategy_id=pos.get("fam"), entry_day=pos.get("entry_day"))
        if eod_ref is not None:
            # Reference-only: official close is shown, but never enters NAV/equity.
            holding.update(current_price=eod_ref, market_value=qty * eod_ref,
                unrealized_pnl=(eod_ref - entry) * qty if entry is not None else None,
                return_pct=(eod_ref / entry - 1) * 100 if entry else None,
                unrealized_pnl_pct=(eod_ref / entry - 1) * 100 if entry else None,
                price_freshness="stale", price_basis=f"官方收盤價 {eod_day}（參考，未計入淨值）")
        out["holdings"].append(holding)
        # Keep each recorded source independently selectable, even for the same
        # ticker. Missing marks stay null; entry/peak is never a quote substitute.
        out["quotes"].append({"symbol": symbol, "market": _market(symbol),
            "price": px if px is not None else eod_ref,
            "change_pct": None, "source": "swing_book.marks" if px is not None else (
                "official_eod.close" if eod_ref is not None else "swing_book.marks"),
            "timestamp": timestamp or (eod_day if eod_ref is not None else None),
            "freshness": _freshness(timestamp, now) if px is not None else ("stale" if eod_ref is not None else "UNKNOWN")})
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
    _per_book_results(out, root)
    return out


def _per_book_results(out: dict, root: Path) -> None:
    """Per-book realized/unrealized/return so cards never stay empty. Initial capital of the two
    intraday accounts is not recorded: it is back-solved from equity - pnl and rounded to 1,000 (marked as estimate);
    swing_book uses its script constant (1,000,000)."""
    sources = out["portfolio"].get("sources") or {}
    for name, src in sources.items():
        mine = [t for t in out["round_trips"] if t["source"] == name]
        hold = [h for h in out["holdings"] if h.get("source") == name]
        realized = sum(t["pnl"] for t in mine)
        unreal = sum(h["unrealized_pnl"] for h in hold if h.get("unrealized_pnl") is not None)
        src.update(realized_pnl_from_fills=realized, unrealized_pnl=unreal, trade_count=len(mine),
                   win_count=sum(1 for t in mine if t["pnl"] > 0))
        if name == "swing_book":
            initial, basis = 1_000_000.0, "程式固定起始資金"
        elif src.get("equity") is not None:
            initial, basis = round((src["equity"] - realized - unreal) / 1000) * 1000.0, "由損益回推（估計）"
        else:
            continue
        src.update(initial_capital=initial, initial_basis=basis,
                   return_pct=(src["equity"] / initial - 1) * 100 if src.get("equity") is not None and initial else None)
    bench = None
    try:
        book = json.loads((root / "swing_book.json").read_text())
        if book.get("bench_equity") and "swing_book" in sources:
            bench = (book["bench_equity"] / 1_000_000.0 - 1) * 100
    except (OSError, ValueError):
        pass
    out["portfolio"]["swing_benchmark_return_pct"] = bench


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
