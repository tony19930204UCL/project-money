
import json,tempfile,os
from datetime import datetime,timezone,timedelta
from cio_market_lab.engine.trade_discipline import TradeDiscipline,build_lessons
def row(t,side,px,ts,th,fee=100,tax=0,size=10):
    return dict(status="SIMULATED",ticker=t,side=side,fill_price=px,timestamp=ts,thesis=th,fee=fee,tax=tax,size=size)
def test_lessons_net_of_cost_and_gates(tmp_path):
    j=tmp_path/"TW_journal.jsonl"
    rows=[row("2330.TW","BUY",100,"2026-10-14T01:00:00+00:00","dip below 20-tick mean"),
          row("2330.TW","SELL",99,"2026-10-14T01:30:00+00:00","exit:hard_stop")]
    j.write_text("".join(json.dumps(x)+"\n" for x in rows))
    ls=build_lessons(rows); assert ls[0]["gross"]==-10 and ls[0]["net"]==-210
    now=datetime(2026,10,15,1,10,tzinfo=timezone.utc)
    d=TradeDiscipline("TW",j,now=lambda:now)
    ok,why=d.allow_entry("2330.TW","dip below 20-tick mean",0.06); assert not ok and why=="cooldown_after_stop"
    ok,why=d.allow_entry("2330.TW","dip below 20-tick mean",0.012); assert not ok and why.startswith("edge_below_cost")
    d2=TradeDiscipline("TW",j,now=lambda:now+timedelta(days=2))
    assert d2.allow_entry("2330.TW","dip below 20-tick mean",0.06)[0]
    assert d2.allow_entry("2317.TW","dip below 20-tick mean",0.06)[0]
    assert (tmp_path/"TW_lessons.jsonl").exists()
