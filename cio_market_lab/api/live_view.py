"""Read-only view of the PAPER runner's own state (data/paper_runtime)."""
from __future__ import annotations
import json
from pathlib import Path

START_CASH = {"US": 30000.0, "TW": 1000000.0}


def _jsonl(path, limit):
    rows = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return rows


def build_live(root):
    root = Path(root)
    out = {"paper_only": True, "markets": {}, "fills": [], "feed": {}}
    for mkt, start in START_CASH.items():
        try:
            acct = json.loads((root / f"{mkt}_account.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out["markets"][mkt] = {"state": "NO_DATA", "start_cash": start}
            continue
        marks = acct.get("marks", {})
        pos = [{"ticker": t, "qty": q, "mark": marks.get(t, 0.0), "value": q * marks.get(t, 0.0)}
               for t, q in acct.get("positions", {}).items() if q]
        equity = acct.get("cash", 0.0) + sum(p["value"] for p in pos)
        events = _jsonl(root / f"{mkt}_journal.jsonl", 2000)
        fills = [e for e in events if e.get("status") == "SIMULATED"]
        rejects = [e for e in events if e.get("status") == "REJECTED"]
        out["markets"][mkt] = {
            "state": "OK", "start_cash": start, "cash": acct.get("cash", 0.0),
            "equity": equity, "pnl": equity - start, "pnl_pct": (equity - start) / start * 100,
            "realized_pnl": acct.get("realized_pnl", 0.0), "positions": pos,
            "fills_count": len(fills), "rejects_count": len(rejects),
            "last_event": events[-1].get("timestamp") if events else None}
        for f in fills[-20:]:
            out["fills"].append({**{k: f.get(k) for k in ("timestamp", "ticker", "side", "size", "fill_price", "fee", "tax", "thesis")}, "market": mkt})
    out["fills"].sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    out["fills"] = out["fills"][:30]
    lat = _jsonl(root / "feed_latency.jsonl", 200)
    out["feed"] = {"samples": len(lat), "last": lat[-1] if lat else None}
    return out
