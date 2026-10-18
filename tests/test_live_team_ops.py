"""Read-only runtime bridge regressions; no broker or market network calls."""
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from cio_market_lab.api.app import create_app
from cio_market_lab.engine.team_ops import to_public_team_ops_snapshot

NOW = datetime(2026, 10, 18, 10, tzinfo=timezone.utc)


def put(root, name, value):
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(json.dumps(value))


def lines(root, name, rows):
    (root / name).write_text("".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "data" / "paper_runtime"
    put(root, "TW_account.json", {"cash": 1000, "positions": {"2454.TW": 2, "ZERO.TW": 0},
                                  "marks": {"2454.TW": 100}, "realized_pnl": 4})
    put(root, "US_account.json", {"cash": 50, "positions": {"NVDA": 3}, "marks": {"NVDA": 10}})
    put(root, "swing_book.json", {"cash": 200, "pos": {"2454.TW": {"e": 80, "sh": 4,
        "stop": 70, "fam": "pullback", "entry_day": "2026-10-16"}}, "equity": 580,
        "last": "2026-10-16", "bench0": {"2454.TW": 90}, "bench_equity": 1000000})
    put(root, "paper_params.json", {"version": "v1", "per_market": {"TW:pullback": {"enabled": True}}})
    event = {"status": "SIMULATED", "ticker": "2454.TW", "side": "BUY", "size": 2,
             "fill_price": 95, "fee": 1, "sequence": 1, "strategy_id": "MeanRev_TW",
             "quote_timestamp": "2026-10-16T05:00:00+00:00", "timestamp": "2026-10-16T05:00:02+00:00"}
    lines(root, "TW_journal.jsonl", [event, {"status": "REJECTED", "ticker": "2454.TW", "side": "BUY",
        "size": 1, "sequence": 2, "reason": "POSITION_LIMIT", "timestamp": "2026-10-16T05:01:00+00:00"}])
    lines(root, "TW_loop.jsonl", [{k: v for k, v in event.items() if k != "timestamp"}])
    lines(root, "US_journal.jsonl", [{"status": "SKIPPED", "reason": "MARKET_CLOSED",
                                      "timestamp": "2026-10-16T21:00:00+00:00"}])
    lines(root, "swing_decisions.jsonl", [{"act": "ENTER", "t": "2454.TW", "e": 80, "sh": 4,
        "fam": "pullback", "ts": "2026-10-16T20:00:00+00:00"}])
    lines(root, "feed_latency.jsonl", [{"at": "2026-10-18T09:59:59+00:00", "symbol": "NVDA",
                                        "source": "YAHOO_1M", "age_s": 1, "error": None}])
    return root


def build(root):
    from cio_market_lab.api.live_team_ops import build_live_team_ops
    return build_live_team_ops(root, now=NOW)


def test_schema_key_sets(runtime):
    snap = build(runtime)
    old = to_public_team_ops_snapshot({"portfolio": {"nav_status": "OK"}, "safety": {"paper_only": True},
                                       "integrity_warnings": []})
    assert set(snap) == set(old) | {"round_trips"}
    for section in ("safety", "sessions", "benchmark"):
        assert set(snap[section]) == set(old[section])
    for section in ("portfolio", "posture", "risk"):
        assert set(old[section]) <= set(snap[section])
    assert set(snap["portfolio"]) == set(old["portfolio"]) | {"sources", "equity_by_currency", "equity_rule"}
    assert set(snap["posture"]) == set(old["posture"]) | {"desk", "active_playbook", "next_review_time"}
    assert set(snap["risk"]) == set(old["risk"])
    old_h = to_public_team_ops_snapshot({"holdings": [{"symbol": "X"}]})["holdings"][0]
    assert set(old_h) <= set(snap["holdings"][0])
    assert set(snap["quotes"][0]) == {"symbol", "market", "price", "change_pct", "source", "timestamp", "freshness"}


def test_merge_source_identity_and_no_double_count(runtime):
    snap = build(runtime)
    holdings = snap["holdings"]
    assert {(h["source"], h["symbol"], h["quantity"]) for h in holdings} == {
        ("TW_account", "2454.TW", 2), ("swing_book", "2454.TW", 4), ("US_account", "NVDA", 3)}
    sources = snap["portfolio"]["sources"]
    assert sources["TW_account"]["equity"] == 1200
    assert sources["US_account"]["equity"] == 80
    assert sources["swing_book"]["equity"] == 580
    assert sources["swing_book"]["positions_value"] == 380
    assert snap["portfolio"]["equity"] is None  # never add USD to TWD without FX
    assert snap["portfolio"]["equity_by_currency"] == {"TWD": 1780, "USD": 80}
    assert "cash + positions" in snap["portfolio"]["equity_rule"]
    assert len([f for f in snap["fills"] if f["source"] == "TW_account"]) == 1
    assert len(snap["orders"]) == 3  # TW fill + reject, swing entry; not loop duplicate
    assert len({f["fill_id"] for f in snap["fills"]}) == len(snap["fills"])


def test_unknowns_not_entry_peak_or_latency_as_quotes(runtime):
    snap = build(runtime)
    h = next(h for h in snap["holdings"] if h["source"] == "swing_book")
    assert h["entry_price"] == 80
    assert h["current_price"] is h["market_value"] is h["return_pct"] is None
    assert h["price_freshness"] == "UNKNOWN"
    account = next(h for h in snap["holdings"] if h["source"] == "TW_account")
    # Entry/strategy are rebuilt from recorded fills (evidence), never from marks.
    assert account["entry_price"] == 95 and account["cost_basis"] == 190
    assert account["strategy_id"] == "MeanRev_TW"
    # No per-tick stamps: marks carry the account file's save time and are never "fresh".
    assert all(q["change_pct"] is None and q["freshness"] in {"UNKNOWN", "stale", "fresh"}
               for q in snap["quotes"])
    assert all(q["timestamp"] is None for q in snap["quotes"] if q["price"] is None)
    assert snap["portfolio"]["return_pct"] is None
    assert snap["risk"]["max_drawdown_pct"] is None
    assert snap["benchmark"]["benchmark_return_pct"] is None
    assert snap["posture"]["desk"]["roles"][0]["next_review_time"] is None
    assert snap["posture"]["active_playbook"]["playbook_id"] == "v1"


def test_roles_use_real_events(runtime):
    roles = {r["role_id"]: r for r in build(runtime)["posture"]["desk"]["roles"]}
    assert "POSITION_LIMIT" in roles["TW_account"]["current_task"]
    assert "MARKET_CLOSED" in roles["US_account"]["current_task"]
    assert "ENTER" in roles["swing_book"]["current_task"]
    assert "2026-10-16" in roles["TW_account"]["current_task"]


def test_prior_close_only_when_explicit(runtime):
    p = runtime / "US_account.json"
    account = json.loads(p.read_text())
    account.update(previous_close={"NVDA": 8}, quote_timestamps={"NVDA": NOW.isoformat()})
    put(runtime, p.name, account)
    q = next(q for q in build(runtime)["quotes"] if q["symbol"] == "NVDA")
    assert q["change_pct"] == 25
    assert q["freshness"] == "fresh"


def test_read_again_not_cached(runtime):
    assert build(runtime)["portfolio"]["sources"]["TW_account"]["cash"] == 1000
    put(runtime, "TW_account.json", {"cash": 7, "positions": {}, "marks": {}})
    assert build(runtime)["portfolio"]["sources"]["TW_account"]["equity"] == 7


@pytest.mark.parametrize("read_only", [True, False])
def test_both_app_roles_live_and_route_present(runtime, read_only):
    app = create_app(workspace_root=runtime.parent.parent, is_read_only=read_only)
    with TestClient(app) as client:
        snap = client.get("/api/paper/team-ops").json()
        assert {h["source"] for h in snap["holdings"]} == {"TW_account", "swing_book", "US_account"}
        assert client.get("/api/paper/live").status_code == 200
        put(runtime, "TW_account.json", {"cash": 123, "positions": {}, "marks": {}})
        assert client.get("/api/paper/team-ops").json()["portfolio"]["sources"]["TW_account"]["cash"] == 123


def test_missing_runtime_fallback_is_labelled(tmp_path):
    app = create_app(workspace_root=tmp_path, is_read_only=True)
    # A valid old snapshot need not contain the optional warnings key.
    app.state.app_state.runner.get_canonical_team_ops = lambda **_: {}
    with TestClient(app) as client:
        snap = client.get("/api/paper/team-ops").json()
        assert any("PAPER_RUNTIME_ABSENT" in w for w in snap["integrity_warnings"])


def test_partial_corrupt_runtime_does_not_revert_to_old_snapshot(runtime):
    (runtime / "TW_account.json").write_text('{"cash":')
    with (runtime / "TW_journal.jsonl").open("a") as out:
        out.write('{"partial":')
    snap = build(runtime)
    assert snap["portfolio"]["sources"]["TW_account"]["equity"] is None
    assert any("TW_account.json" in w for w in snap["integrity_warnings"])
    assert any("TW_journal.jsonl" in w for w in snap["integrity_warnings"])
    assert snap["safety"]["paper_only"] is True


def test_sequence_reset_does_not_hide_later_fill(runtime):
    p = runtime / "TW_journal.jsonl"
    original = json.loads(p.read_text().splitlines()[0])
    later = {**original, "timestamp": "2026-10-17T05:00:02+00:00", "quote_timestamp": "2026-10-17T05:00:00+00:00"}
    with p.open("a") as f:
        f.write(json.dumps(later) + "\n")
    assert len([f for f in build(runtime)["fills"] if f["source"] == "TW_account"]) == 2


def test_missing_mark_cannot_be_zero_equity(runtime):
    put(runtime, "TW_account.json", {"cash": 50, "positions": {"XYZ.TW": 2}, "marks": {}})
    snap = build(runtime)
    assert snap["portfolio"]["sources"]["TW_account"]["equity"] is None
    assert snap["portfolio"]["equity_by_currency"]["TWD"] is None


def test_loop_only_events_are_used(runtime):
    (runtime / "TW_journal.jsonl").unlink()
    snap = build(runtime)
    assert len([f for f in snap["fills"] if f["source"] == "TW_account"]) == 1
    role = next(r for r in snap["posture"]["desk"]["roles"] if r["role_id"] == "TW_account")
    assert "SIMULATED" in role["current_task"]


def test_environment_workspace_root(monkeypatch, runtime):
    monkeypatch.setenv("CIO_WORKSPACE_ROOT", str(runtime.parent.parent))
    with TestClient(create_app(is_read_only=True)) as client:
        assert len(client.get("/api/paper/team-ops").json()["holdings"]) == 3


def test_runtime_reads_do_not_write(runtime):
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in runtime.iterdir()}
    build(runtime)
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in runtime.iterdir()}
    assert before == after


def test_missing_positions_not_a_known_flat_account(runtime):
    put(runtime, "TW_account.json", {"cash": 50})
    assert build(runtime)["portfolio"]["sources"]["TW_account"]["equity"] is None


def test_missing_quote_and_nonfinite_mark_remain_unknown(runtime):
    put(runtime, "TW_account.json", {"cash": 50, "positions": {"XYZ.TW": 2}, "marks": {"ABC.TW": float("nan")}})
    snap = build(runtime)
    quotes = {q["symbol"]: q for q in snap["quotes"]}
    for ticker in ("XYZ.TW", "ABC.TW"):
        assert quotes[ticker]["price"] is None
        assert quotes[ticker]["freshness"] == "UNKNOWN"
    json.dumps(snap, allow_nan=False)


def test_swing_mixed_currencies_never_aggregate(runtime):
    put(runtime, "swing_book.json", {"cash": 100, "equity": 900,
        "pos": {"XYZ.TW": {"e": 10, "sh": 3}, "XYZ": {"e": 50, "sh": 2}}})
    snap = build(runtime)
    assert snap["portfolio"]["sources"]["swing_book"]["equity"] is None
    assert any("SWING_CURRENCY_UNKNOWN_OR_MIXED" in w for w in snap["integrity_warnings"])


def test_future_quote_timestamp_cannot_claim_freshness(runtime):
    put(runtime, "US_account.json", {"cash": 50, "positions": {}, "marks": {"NVDA": 10},
        "quote_timestamps": {"NVDA": "2026-10-19T10:00:00+00:00"}})
    assert next(q for q in build(runtime)["quotes"] if q["symbol"] == "NVDA")["freshness"] == "UNKNOWN"


def test_quote_sources_preserve_same_symbol_and_duty_is_recorded(runtime):
    snap = build(runtime)
    same_symbol = [q for q in snap["quotes"] if q["symbol"] == "2454.TW"]
    assert {q["source"] for q in same_symbol} == {"TW_account.marks", "swing_book.marks"}
    assert next(q for q in same_symbol if q["source"] == "swing_book.marks")["price"] is None
    duty = snap["posture"]["desk"]["current_duty"]
    assert duty["source"] == "feed"
    assert duty["status"] == "LAST_RECORDED_EVENT"
    assert duty["observed_at"] == "2026-10-18T09:59:59+00:00"
    assert duty["task"] == next(r["current_task"] for r in snap["posture"]["desk"]["roles"]
                                if r["role_id"] == "feed")
    assert duty["freshness"] == "fresh"


def test_no_events_or_only_future_events_cannot_claim_current_duty(runtime):
    for path in runtime.glob("*.jsonl"):
        path.write_text("")
    assert build(runtime)["posture"]["desk"]["current_duty"] is None
    lines(runtime, "TW_journal.jsonl", [{"status": "SIMULATED", "ticker": "2454.TW",
        "timestamp": "2026-10-19T05:00:00+00:00"}])
    assert build(runtime)["posture"]["desk"]["current_duty"] is None


def paper_chain(root, *, reject=False, cash=10000):
    """Real paper order service, loop, persistence and API; fixture quotes only."""
    from types import SimpleNamespace
    from cio_market_lab.engine.paper_runner import Momentum, MeanRev, _configure, _default_orders
    from cio_market_lab.engine.paper_session import PaperSession
    from cio_market_lab.engine.paper_trading_loop import PaperTradingLoop

    opened = datetime(2026, 10, 13, 2, tzinfo=timezone.utc)

    class RecordedFeed:
        price = 100

        def clock(self):
            return opened

        def latest(self, symbol):
            return SimpleNamespace(c=self.price, ts=opened, source="TWSE_MIS", staleness_seconds=0)

    root.mkdir(parents=True, exist_ok=True)
    account_path = root / "TW_account.json"
    if not account_path.exists():
        put(root, account_path.name, {"cash": cash, "positions": {}, "marks": {},
            "realized_pnl": 0, "daily_start_equity": None})
    orders = _default_orders(root)
    orders._now_fn = lambda: opened
    _configure(orders, "TW", {"cash": 10000, "tickers": {"2454.TW": 2}})
    if reject:
        orders.experiments["Momentum_TW"].enabled = False
    momentum = Momentum("2454.TW", 2, market="TW", max_hold_ticks=3)
    other = MeanRev("2454.TW", 2, market="TW", max_hold_ticks=1)
    feed = RecordedFeed()
    loop = PaperTradingLoop(orders, root / "TW_loop.jsonl", max_position_pct=0.2)
    session = PaperSession(feed, loop, [other, momentum], account_path,
                           root / "TW_journal.jsonl", ["2454.TW"])
    return session, feed, momentum, other


def test_e2e_strategy_owner_time_stop_survives_restart_and_reaches_api(tmp_path):
    root = tmp_path / "data" / "paper_runtime"
    session, feed, owner, other = paper_chain(root)
    session.run_session(120)
    feed.price = 101
    session.run_session(1)
    state = json.loads((root / "TW_account.json").read_text())
    assert state["positions"]["2454.TW"] == 2
    assert state["strategy_state"]["2454.TW"]["strategy_id"] == "Momentum_TW"
    assert state["strategy_state"]["2454.TW"]["held_ticks"] == 0
    assert other.entry is None
    session.run_session(1)
    assert session.account.strategy_state["2454.TW"]["held_ticks"] == 1
    assert other.held_ticks == 0  # max_hold_ticks=1 on the non-owner cannot sell
    put(root, "swing_book.json", {"cash": 200, "pos": {"2454.TW": {
        "e": 80, "sh": 4, "fam": "pullback"}}})
    app = create_app(workspace_root=tmp_path, is_read_only=True)
    with TestClient(app) as client:
        snap = client.get("/api/paper/team-ops").json()
        rows = [h for h in snap["holdings"] if h["symbol"] == "2454.TW"]
        assert {(h["source"], h["strategy_id"], h["quantity"]) for h in rows} == {
            ("TW_account", "Momentum_TW", 2), ("swing_book", "pullback", 4)}
        assert next(h for h in rows if h["source"] == "TW_account")["held_ticks"] == 1
        assert snap["posture"]["desk"]["current_duty"]["source"] == "TW_account"

        restarted, resumed_feed, resumed_owner, resumed_other = paper_chain(root)
        resumed_feed.price = 101
        restarted.run_session(1)
        assert restarted.account.positions["2454.TW"] == 2
        assert restarted.account.strategy_state["2454.TW"]["held_ticks"] == 2
        restarted.run_session(1)
        assert restarted.account.positions["2454.TW"] == 0
        assert "2454.TW" not in restarted.account.strategy_state
        assert resumed_other.entry is None
        snap = client.get("/api/paper/team-ops").json()
        assert [(h["source"], h["quantity"]) for h in snap["holdings"]] == [("swing_book", 4)]
        exits = [r for r in snap["fills"] if r["side"] == "SELL"]
        assert len(exits) == 1
        assert exits[0]["source"] == "TW_account"
    events = [json.loads(line) for line in (root / "TW_loop.jsonl").read_text().splitlines()]
    assert [(e["strategy_id"], e["side"], e["status"]) for e in events] == [
        ("Momentum_TW", "BUY", "SIMULATED"), ("Momentum_TW", "SELL", "SIMULATED")]
    assert events[-1]["thesis"] == "exit:time_stop"
    assert all(e["paper_only"] is True for e in events)


def test_e2e_rejected_entry_never_acquires_ownership(tmp_path):
    session, feed, owner, other = paper_chain(tmp_path, reject=True)
    session.run_session(120)
    feed.price = 101
    session.run_session(1)
    events = [json.loads(line) for line in (tmp_path / "TW_loop.jsonl").read_text().splitlines()]
    assert len(events) == 1 and events[0]["status"] == "REJECTED"
    assert not session.account.positions and not session.account.strategy_state
    assert owner.entry is owner.peak is None and owner.held_ticks == 0


def test_e2e_wrong_strategy_cannot_sell_owned_position(tmp_path):
    from cio_market_lab.engine.paper_trading_loop import Signal

    session, feed, owner, other = paper_chain(tmp_path)
    session.run_session(120)
    feed.price = 101
    session.run_session(1)
    other.signals = lambda quotes, account: [Signal("2454.TW", "stock", "SELL", 2,
                                                   None, None, "forced wrong-owner sell")]
    session.strategies = [other]
    session.run_session(1)
    events = [json.loads(line) for line in (tmp_path / "TW_loop.jsonl").read_text().splitlines()]
    assert events[-1]["reason"] == "STRATEGY_OWNERSHIP_MISMATCH"
    assert events[-1]["status"] == "REJECTED"
    assert session.account.positions["2454.TW"] == 2
    assert session.account.strategy_state["2454.TW"]["strategy_id"] == "Momentum_TW"


def test_e2e_rejected_time_stop_preserves_owner_until_success(tmp_path):
    session, feed, owner, other = paper_chain(tmp_path)
    session.run_session(120)
    feed.price = 101
    session.run_session(1)
    session.loop.orders.experiments["Momentum_TW"].enabled = False
    session.run_session(3)
    assert session.account.positions["2454.TW"] == 2
    assert session.account.strategy_state["2454.TW"]["held_ticks"] == 3
    assert owner.entry is not None
    assert owner.entry == session.account.strategy_state["2454.TW"]["entry"]
    assert owner.entry > 101 and owner.held_ticks == 3  # actual fill includes paper slippage
    session.loop.orders.experiments["Momentum_TW"].enabled = True
    session.run_session(1)
    assert session.account.positions["2454.TW"] == 0
    assert not session.account.strategy_state
    events = [json.loads(line) for line in (tmp_path / "TW_loop.jsonl").read_text().splitlines()]
    assert [(e["side"], e["status"]) for e in events] == [
        ("BUY", "SIMULATED"), ("SELL", "REJECTED"), ("SELL", "SIMULATED")]


@pytest.mark.parametrize("override", [
    {"strategy_id": ""}, {"entry": 0}, {"peak": float("nan")},
    {"held_ticks": -1}, {"held_ticks": True}, {"held_ticks": 1.5},
])
def test_invalid_persisted_strategy_state_is_fail_closed(tmp_path, override):
    state = {"cash": 10000, "positions": {"2454.TW": 2}, "marks": {"2454.TW": 101},
             "realized_pnl": 0, "daily_start_equity": None,
             "strategy_state": {"2454.TW": {"strategy_id": "Momentum_TW", "entry": 101,
                                              "peak": 101, "held_ticks": 1, **override}}}
    put(tmp_path, "TW_account.json", state)
    before = (tmp_path / "TW_account.json").read_bytes()
    with pytest.raises(ValueError, match="CORRUPT_PAPER_ACCOUNT_STATE"):
        paper_chain(tmp_path)
    assert (tmp_path / "TW_account.json").read_bytes() == before


def test_round_trip_fifo_pairs_buy_and_sell(runtime):
    buy = {"status": "SIMULATED", "ticker": "AMD", "side": "BUY", "size": 10, "fill_price": 100,
           "fee": 1, "sequence": 1, "strategy_id": "Momentum_US", "thesis": "in",
           "timestamp": "2026-10-16T14:00:00+00:00"}
    sell = dict(buy, side="SELL", size=4, fill_price=110, sequence=2, strategy_id=None,
                thesis="exit:x", timestamp="2026-10-16T15:00:00+00:00")
    lines(runtime, "US_journal.jsonl", [buy, sell])
    snap = build(runtime)
    trip = next(t for t in snap["round_trips"] if t["symbol"] == "AMD")
    assert trip["direction"] == "LONG" and trip["strategy_id"] == "Momentum_US"
    assert (trip["quantity"], trip["buy_price"], trip["sell_price"]) == (4, 100, 110)
    assert abs(trip["pnl"] - (40 - 0.4 - 1)) < 1e-9  # 4/10 of buy fee 1 = 0.4; sell fee 1
    assert trip["legs"]["sell"]["qty"] == 4


def test_swing_reference_close_does_not_enter_nav(runtime):
    eod = runtime.parent / "official_eod"
    eod.mkdir()
    (eod / "twse_1151015.json").write_text(json.dumps({"2454": {"close": 90.0}}))
    snap = build(runtime)
    h = next(h for h in snap["holdings"] if h["source"] == "swing_book")
    assert h["current_price"] == 90.0 and h["price_freshness"] == "stale"
    assert snap["portfolio"]["sources"]["swing_book"]["equity"] == 580
