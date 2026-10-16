#!/usr/bin/env python3
"""Daily-bar swing/core paper book (PAPER ONLY). Separate from the intraday runner.
Reads paper_params.json (walk-forward validated; disabled families never trade), manages positions with ATR stops/trailing/time stop,
sizes by risk (0.75% equity risk per trade, 15% max per name, 8 names max), logs each decision with reason, and tracks a buy&hold
benchmark of the same universe so 'does the system beat doing nothing' is always visible. Loop: once per weekday after US close; optimizer Saturdays."""
import json, time, sys, subprocess, datetime as dt
from pathlib import Path
sys.path.insert(0,"/home/chihcheng/project-money-github/custom_scripts")
import pandas as pd, yfinance as yf
ROOT=Path("/home/chihcheng/project-money-github/data/paper_runtime"); BOOK=ROOT/"swing_book.json"; LOG=ROOT/"swing_decisions.jsonl"
import importlib.util as _u
_sp=_u.spec_from_file_location("po","/home/chihcheng/project-money-github/custom_scripts/paper_optimizer.py"); _po=_u.module_from_spec(_sp); _sp.loader.exec_module(_po)
MKT={t:m for m,L in _po.UNI.items() for t in L}
COST={"US":0.0005,"TW":0.00585/2+0.0005}  # per side
START=1_000_000.0
def log(**k): open(LOG,"a").write(json.dumps({"ts":dt.datetime.now(dt.timezone.utc).isoformat(),**k})+"\n")
def feats(df):
    f=df.copy(); f["ma20"]=f.Close.rolling(20).mean(); f["ma50"]=f.Close.rolling(50).mean(); f["ma200"]=f.Close.rolling(200).mean()
    f["hi20"]=f.High.rolling(20).max().shift(1)
    tr=pd.concat([f.High-f.Low,(f.High-f.Close.shift()).abs(),(f.Low-f.Close.shift()).abs()],axis=1).max(axis=1); f["atr"]=tr.rolling(14).mean(); return f
def step():
    params=json.loads((ROOT/"paper_params.json").read_text())["per_market"]; ver=json.loads((ROOT/"paper_params.json").read_text())["version"]
    b=json.loads(BOOK.read_text()) if BOOK.exists() else {"cash":START,"pos":{},"closed":[],"bench0":None,"last":None}
    d=yf.download(list(MKT),period="1y",interval="1d",auto_adjust=True,progress=False,group_by="ticker")
    F={t:feats(d[t].dropna()) for t in MKT}
    day=str(max(f.index[-1] for f in F.values()).date())
    if b["last"]==day: return "already "+day
    px={t:float(F[t].Close.iloc[-1]) for t in F}
    if b["bench0"] is None: b["bench0"]=px
    # exits
    for t,pos in list(b["pos"].items()):
        f=F[t]; r=f.iloc[-1]; m=MKT[t]; pr=params.get(f"{m}:{pos['fam']}",{}); p=pr.get("params") or {}
        pos["peak"]=max(pos["peak"],float(r.High)); held=len(f.loc[pos["entry_day"]:])-1; why=None
        if pos["fam"]=="core_trend":
            if r.Close<r.ma200*(1-p.get("buf",0.03)): why="trend_broken"
        else:
            trail=pos["peak"]-p.get("atr_trail",0)*pos["atr"] if p.get("atr_trail",0)>0 else -1e9
            stop=max(pos["stop"],trail)
            if r.Low<=stop: why=f"stop {stop:.2f}"
            elif held>=p.get("max_hold",20): why="time_stop"
            elif p.get("tgt") and r.High>=pos["e"]*(1+p["tgt"]): why="target"
        if not pr.get("enabled",False) and why is None and pos["fam"]!="core_trend": why="family_disabled_by_optimizer"
        if why:
            xp=float(r.Close)*(1-COST[m]); b["cash"]+=pos["sh"]*xp; ret=xp/pos["e"]-1
            b["closed"].append(dict(t=t,fam=pos["fam"],entry_day=pos["entry_day"],exit_day=day,ret=ret,pnl=pos["sh"]*(xp-pos["e"]),why=why)); del b["pos"][t]
            log(act="EXIT",t=t,fam=pos["fam"],why=why,ret=round(ret,4),params_ver=ver)
    eq=b["cash"]+sum(pos["sh"]*px[t] for t,pos in b["pos"].items())
    # entries
    for t in MKT:
        if t in b["pos"] or len(b["pos"])>=8: continue
        f=F[t]; r=f.iloc[-1]; m=MKT[t]
        for fam in ("breakout","pullback"):
            pr=params.get(f"{m}:{fam}",{})
            if not pr.get("enabled"): continue
            p=pr["params"]
            ok = (r.Close>r.hi20 and r.ma50>r.ma200 and r.Close>r.ma200) if fam=="breakout" else (r.Close>r.ma200 and r.ma50>r.ma200 and r.Close<r.ma20*(1-p["dip"]))
            if not ok: continue
            risk=p["atr_stop"]*float(r.atr); e=float(r.Close)*(1+COST[m])
            sh=min(0.0075*eq/risk, 0.15*eq/e, b["cash"]/e)
            if sh*e<0.02*eq: log(act="SKIP",t=t,fam=fam,why="size_too_small"); continue
            b["cash"]-=sh*e; b["pos"][t]=dict(fam=fam,e=e,sh=sh,peak=float(r.High),stop=e-risk,atr=float(r.atr),entry_day=day)
            log(act="ENTER",t=t,fam=fam,e=round(e,2),sh=round(sh,2),stop=round(e-risk,2),params_ver=ver); break
    eq=b["cash"]+sum(pos["sh"]*px[t] for t,pos in b["pos"].items())
    bench=sum(px[t]/b["bench0"][t] for t in MKT)/len(MKT)*START
    b["last"]=day; b["equity"]=eq; b["bench_equity"]=bench; b["params_ver"]=ver
    BOOK.write_text(json.dumps(b,indent=1)); log(act="DAY",day=day,equity=round(eq),bench=round(bench),open=list(b["pos"]))
    return f"{day} eq={eq:.0f} bench={bench:.0f} open={list(b['pos'])}"
if __name__=="__main__":
    if "--once" in sys.argv: print(step()); sys.exit(0)
    while True:
        n=dt.datetime.now(dt.timezone.utc)
        try:
            if n.weekday()<5 and n.hour==20: subprocess.run([sys.executable,"/home/chihcheng/project-money-github/custom_scripts/paper_optimizer.py"],timeout=900)
            if n.weekday()<5 and n.hour>=21: print(step(),flush=True)
        except Exception as ex: log(act="ERROR",err=str(ex)[:300])
        time.sleep(3000)
