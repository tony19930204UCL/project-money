from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from cio_market_lab.data.market_data import (
    LIQUID_UNIVERSE,
    SymbolCatalog,
    classify_breadth,
    freshness_metadata,
)

from cio_market_lab.data.tw_official import resolve_tw_symbol

router = APIRouter(tags=["Markets"])
catalog = SymbolCatalog()


def _change_pct(bars: List[Any]) -> Optional[float]:
    if len(bars) < 2 or not bars[-2].close:
        return None
    return ((bars[-1].close / bars[-2].close) - 1.0) * 100.0


@router.get("/symbols/search")
@router.get("/search")
def search_symbols(
    q: str = Query(default="", max_length=80),
    market: Optional[str] = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
) -> Dict[str, Any]:
    if market and market.upper() not in {"TW", "US"}:
        raise HTTPException(status_code=422, detail="market must be TW or US")
    return {
        "query": q,
        "market": market.upper() if market else None,
        "results": catalog.search(q, market, limit),
        "coverage": "catalog plus on-demand symbol lookup",
        "broker_connected": False,
    }


@router.get("/symbols/{symbol}")
def lookup_symbol(symbol: str, market: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    try:
        return catalog.lookup(symbol, market)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/bars/{symbol}")
def get_chart_bars(
    request: Request,
    symbol: str,
    timeframe: str = Query(default="1D"),
    limit: int = Query(default=500, ge=2, le=2000),
) -> Dict[str, Any]:
    adapter = request.app.state.app_state.market_adapter
    try:
        normalized = resolve_tw_symbol(symbol) if symbol[:1].isdigit() else catalog.normalize(symbol)
        metadata = adapter.timeframe_metadata(timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    bars = adapter.get_bars(normalized, timeframe=timeframe, limit=limit)
    return {
        "symbol": normalized,
        **metadata,
        "count": len(bars),
        "bars": [bar.model_dump(mode="json") for bar in bars],
        "freshness": freshness_metadata(adapter),
        "data_status": "ok" if bars else "unavailable",
        "fallback_disclaimer": "Synthetic fallback is deterministic and not live market data." if adapter.offline_mode else None,
    }


@router.get("/breadth")
def get_market_breadth(
    request: Request,
    market: Optional[str] = Query(default=None),
    timeframe: str = Query(default="1D"),
    limit: int = Query(default=500, ge=2, le=2000),
) -> Dict[str, Any]:
    if market and market.upper() not in {"TW", "US"}:
        raise HTTPException(status_code=422, detail="market must be TW or US")
    adapter = request.app.state.app_state.market_adapter
    try:
        key = adapter.normalize_timeframe(timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    records = [item for item in LIQUID_UNIVERSE if not market or item.market == market.upper()]
    rows: List[Dict[str, Any]] = []
    for item in records:
        bars = adapter.get_bars(item.symbol, timeframe=key, limit=limit)
        latest = bars[-1] if bars else None
        change = _change_pct(bars)
        rows.append({
            **item.as_dict(),
            "last": latest.close if latest else None,
            "change_pct": round(change, 4) if change is not None else None,
            "volume": latest.volume if latest else None,
            "timestamp": latest.timestamp.isoformat() if latest else None,
            "classification": classify_breadth(change),
            "is_stale": latest.is_stale if latest else True,
        })
    advances = [row for row in rows if row["classification"] == "advance"]
    declines = [row for row in rows if row["classification"] == "decline"]
    unchanged = [row for row in rows if row["classification"] == "unchanged"]
    return {
        "market": market.upper() if market else "ALL",
        "timeframe": key,
        "universe": "defined_liquid_universe",
        "coverage_count": len(rows),
        "advances": len(advances),
        "declines": len(declines),
        "unchanged": len(unchanged),
        "advance_decline_ratio": round(len(advances) / len(declines), 4) if declines else None,
        "leaders": sorted(rows, key=lambda row: row["change_pct"] if row["change_pct"] is not None else float("-inf"), reverse=True)[:5],
        "laggards": sorted(rows, key=lambda row: row["change_pct"] if row["change_pct"] is not None else float("inf"))[:5],
        "items": rows,
        "freshness": freshness_metadata(adapter),
        "disclaimer": "Breadth is calculated from the defined liquid universe, not the complete exchange.",
    }


@router.get("/leaders")
def get_market_leaders(
    request: Request,
    market: Optional[str] = Query(default=None),
    timeframe: str = Query(default="1D"),
    limit: int = Query(default=500, ge=2, le=2000),
) -> Dict[str, Any]:
    breadth = get_market_breadth(request, market=market, timeframe=timeframe, limit=limit)
    return {
        "market": breadth["market"],
        "timeframe": breadth["timeframe"],
        "leaders": breadth["leaders"],
        "laggards": breadth["laggards"],
        "coverage_count": breadth["coverage_count"],
        "freshness": breadth["freshness"],
        "disclaimer": breadth["disclaimer"],
    }
