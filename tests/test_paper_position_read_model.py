from cio_market_lab.engine.paper_position_read_model import build_position_read_model

def fill(side, qty, price, market="US", owner="momentum", fee=1, tax=0):
    return dict(status="SIMULATED", side=side, size=qty, fill_price=price,
                ticker="ABC", market=market, strategy_id=owner, fee=fee, tax=tax)

def test_partial_fifo_and_costs():
    r = build_position_read_model([fill("BUY", 10, 100, fee=2),
                                   fill("SELL", 4, 110, fee=1, tax=2)])
    assert r["open_positions"][0]["qty"] == 6
    assert r["closed_trades"][0]["gross_pnl"] == 40
    assert abs(r["closed_trades"][0]["net_pnl"] - 36.2) < 1e-9

def test_owners_never_cross_match():
    r = build_position_read_model([fill("BUY", 5, 100, owner="momentum"),
                                   fill("SELL", 5, 110, owner="meanrev")])
    assert r["closed_trades"] == []
    assert r["open_positions"][0]["strategy_id"] == "momentum"

def test_native_currencies_remain_separate():
    r = build_position_read_model([fill("BUY", 1, 100), fill("SELL", 1, 110),
                                   fill("BUY", 1, 100, market="TW"),
                                   fill("SELL", 1, 110, market="TW")])
    assert set(r["realized_by_currency"]) == {"USD", "TWD"}

def test_missing_cost_is_unknown():
    buy = fill("BUY", 1, 100)
    buy.pop("fee")
    r = build_position_read_model([buy, fill("SELL", 1, 110)])
    assert r["closed_trades"][0]["net_pnl"] is None

def test_missing_entry_is_not_closed_trade():
    r = build_position_read_model([fill("SELL", 1, 110)])
    assert r["closed_trades"] == []

def test_mark_freshness_not_fabricated():
    r = build_position_read_model([fill("BUY", 1, 100)], marks={"US": {"ABC": 110}})
    assert r["open_positions"][0]["mark_freshness"] == "UNKNOWN"
