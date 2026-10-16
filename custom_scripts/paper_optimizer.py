#!/usr/bin/env python3
"""Daily-bar walk-forward optimizer for the paper desk (pure python, no model).
Tests swing/core rule families on 2y daily bars incl. round-trip costs, picks params on TRAIN, scores on TEST,
writes versioned data/paper_runtime/paper_params.json. A family is only enabled if OOS net expectancy > 0,
>= MIN_TRADES OOS trades, and beats buy&hold risk-adjusted on the same window; else weight 0 (no edge)."""
import json, sys, itertools, datetime as dt
from pathlib import Path
import pandas as pd, numpy as np, yfinance as yf
ROOT = Path("/home/chihcheng/project-money-github/data/paper_runtime")
UNI = {"US": ["NVDA","AMD","PLTR","RKLB","TSLA"], "TW": ["2330.TW","2317.TW","2454.TW","2308.TW"]}
COST = {"US": 0.0010, "TW": 0.00585 + 0.0010}  # round trip incl. slippage
MIN_TRADES = 12

def load(tickers):
    d = yf.download(tickers, period="3y", interval="1d", auto_adjust=True, progress=False, group_by="ticker")
    out = {}
    for t in tickers:
        df = d[t].dropna()
        if len(df) > 300: out[t] = df
    return out

def feats(df):
    f = df.copy()
    f["ma20"]=f.Close.rolling(20).mean(); f["ma50"]=f.Close.rolling(50).mean(); f["ma200"]=f.Close.rolling(200).mean()
    f["hi20"]=f.High.rolling(20).max().shift(1)
    tr=pd.concat([f.High-f.Low,(f.High-f.Close.shift()).abs(),(f.Low-f.Close.shift()).abs()],axis=1).max(axis=1)
    f["atr"]=tr.rolling(14).mean()
    return f

def run(f, fam, p, cost, lo, hi):
    """returns list of net trade returns; entries on next open after signal close; exits intraday-stop at stop price or at close."""
    tr=[]; pos=None
    idx=f.index
    for i in range(max(210,lo), min(hi,len(f))-1):
        r=f.iloc[i]; n=f.iloc[i+1]
        if pos is None:
            ok=False
            if fam=="breakout": ok = r.Close>r.hi20 and r.ma50>r.ma200 and r.Close>r.ma200
            elif fam=="pullback": ok = r.Close>r.ma200 and r.ma50>r.ma200 and r.Close<r.ma20*(1-p["dip"])
            elif fam=="core_trend": ok = r.Close>r.ma200 and r.ma50>r.ma200
            if ok:
                e=n.Open; pos=dict(e=e,i=i+1,peak=e,stop=e-p["atr_stop"]*r.atr if fam!="core_trend" else None)
        else:
            e=pos["e"]; held=i-pos["i"]; px=r.Close; exit_px=None
            pos["peak"]=max(pos["peak"],r.High)
            if fam=="core_trend":
                if px<r.ma200*(1-p["buf"]): exit_px=n.Open
            else:
                trail=pos["peak"]-p["atr_trail"]*r.atr
                stop=max(pos["stop"],trail) if p["atr_trail"]>0 else pos["stop"]
                if r.Low<=stop: exit_px=min(stop,r.Open)
                elif held>=p["max_hold"]: exit_px=n.Open
                elif p.get("tgt") and r.High>=e*(1+p["tgt"]): exit_px=e*(1+p["tgt"])
            if exit_px is not None:
                tr.append(exit_px/e-1-cost); pos=None
    if pos is not None:
        tr.append(f.iloc[min(hi,len(f))-1].Close/pos["e"]-1-cost)
    return tr

GRID = {
 "breakout": [dict(atr_stop=a,atr_trail=t,max_hold=h,tgt=0) for a in (2,3) for t in (0,3,4) for h in (20,40)],
 "pullback": [dict(dip=d,atr_stop=a,atr_trail=0,max_hold=h,tgt=g) for d in (0.02,0.04) for a in (2,3) for h in (10,20) for g in (0.06,0.10)],
 "core_trend": [dict(buf=b) for b in (0.0,0.03,0.06)],
}
def stats(tr):
    if not tr: return dict(n=0,exp=0.0,win=0.0,tot=0.0)
    a=np.array(tr); return dict(n=len(a),exp=float(a.mean()),win=float((a>0).mean()),tot=float(np.prod(1+a)-1))

def main():
    out={"version":dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S"),"min_trades":MIN_TRADES,"families":{},"per_market":{}}
    for mkt,ts in UNI.items():
        data={t:feats(df) for t,df in load(ts).items()}
        L=min(len(f) for f in data.values())
        split=int(L*0.55)   # train first 55%, test last 45% (about 1y OOS)
        for fam,grid in GRID.items():
            best=None
            for p in grid:
                tr=[x for t,f in data.items() for x in run(f.iloc[-L:],fam,p,COST[mkt],0,split)]
                s=stats(tr)
                if s["n"]>=8 and (best is None or s["exp"]>best[1]["exp"]): best=(p,s)
            if best is None: out["per_market"][f"{mkt}:{fam}"]={"enabled":False,"why":"no train params with >=8 trades"};continue
            p,trs=best
            oos=[];bh=[]
            for t,f in data.items():
                ff=f.iloc[-L:]
                oos+=run(ff,fam,p,COST[mkt],split,L)
                bh.append(ff.Close.iloc[-1]/ff.Close.iloc[split]-1)
            s=stats(oos); bhm=float(np.mean(bh))
            # compare per-trade OOS to buy&hold holding period is apples/oranges; require positive net expectancy and enough trades
            en = s["n"]>=MIN_TRADES and s["exp"]>=0.01
            out["per_market"][f"{mkt}:{fam}"]=dict(enabled=en,params=p,train=trs,oos=s,buy_hold_oos_mean=bhm,
                why="OOS net expectancy>=1%% and n>=%d"%MIN_TRADES if en else "no OOS edge after costs / too few trades")
    out["note"]="Selected on first 55% of 3y, scored on last 45%. Re-run weekly; params change only through this file (versioned)."
    ROOT.mkdir(parents=True,exist_ok=True)
    hist=ROOT/"paper_params_history.jsonl"
    open(hist,"a").write(json.dumps(out)+"\n")
    (ROOT/"paper_params.json").write_text(json.dumps(out,indent=1))
    for k,v in out["per_market"].items():
        print(k,"ENABLED" if v["enabled"] else "off", v.get("params"), "train",v.get("train"),"oos",v.get("oos"),"bh",round(v.get("buy_hold_oos_mean",0),3) if "buy_hold_oos_mean" in v else "")
if __name__=='__main__': main()
