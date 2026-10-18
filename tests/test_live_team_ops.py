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
    assert set(snap) == set(old)
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
    assert account["entry_price"] is account["cost_basis"] is None
    assert all(q["change_pct"] is None and q["timestamp"] is None and q["freshness"] == "UNKNOWN"
               for q in snap["quotes"])
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
