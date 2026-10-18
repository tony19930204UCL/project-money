from types import SimpleNamespace as NS
from cio_market_lab.engine.paper_runner import Momentum, MeanRev, _restore_entries
import json

def acct(q): return NS(positions={"X": q}, strategy_state={})

def test_non_owner_does_not_sell():
    m = MeanRev("X", 5)
    assert m._exit(100.0, acct(5)) is None
    assert m._exit(50.0, acct(5)) is None  # even on a big move

def test_owner_sells_on_stop():
    m = Momentum("X", 5); m.entry = 100.0
    assert m._exit(99.0, acct(5)) is None  # swing stop is 3%, not the retired 0.8%
    assert m._exit(100.0 * (1 - m.exit_policy.stop_pct), acct(5))[0].side == "SELL"

def test_restore_after_restart(tmp_path):
    (tmp_path/"a.json").write_text(json.dumps({"positions": {"X": 5}}))
    (tmp_path/"l.jsonl").write_text(json.dumps({"status":"SIMULATED","side":"BUY","ticker":"X","thesis":"1m momentum continuation","fill_price":100.0})+"\n")
    mo, mr = Momentum("X",5), MeanRev("X",5)
    _restore_entries([mo, mr], tmp_path/"l.jsonl", tmp_path/"a.json")
    assert mo.entry == 100.0 and mr.entry is None
