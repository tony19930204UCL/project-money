"""Deterministic, paper-only compatibility facade for the restored Shioaji UI.

This module intentionally does not import or connect to shioaji.  It exposes the
small REST/SSE surface the upstream UI uses, backed by the existing deterministic
market adapter and in-memory watchlists.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.request import urlopen

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from cio_market_lab.api.security import assert_owner_port, is_owner_port
from cio_market_lab.data.market_data import LIQUID_UNIVERSE
from cio_market_lab.data.tw_official import index_components_snapshot, official_quote, resolve_tw_symbol
from cio_market_lab.domain.models import DecisionScope, Market, OrderOrigin, OrderSide, OrderStatus, OrderType
from cio_market_lab.engine.paper_orders import PaperDataContext, PaperOrderRequest


router = APIRouter(tags=["Shioaji compatibility"])

TAIFEX_FUTURES_DAILY_URL = "https://openapi.taifex.com.tw/v1/DailyMarketReportFut"
_TAIFEX_CACHE: Dict[str, Any] = {"expires_at": 0.0, "rows": []}

PAPER_ACCOUNT = {
    "account_type": "S",
    "person_id": "paper-person",
    "broker_id": "PAPER",
    "account_id": "PAPER-SIM",
    "signed": True,
    "username": "paper",
}
PAPER_FUTURES_ACCOUNT = {
    **PAPER_ACCOUNT,
    "account_type": "F",
    "account_id": "PAPER-FUTURES-SIM",
}


def _contract_base(code: str, security_type: str = "STK", exchange: str = "TSE") -> Dict[str, Any]:
    if security_type == "IND":
        exchange = "TAIFEX"
    return {
        "region": "TW",
        "exchange": exchange,
        "code": code,
        "security_type": security_type,
        "target_code": None,
    }


def _contract_info(base: Dict[str, Any], name: str, reference: float = 0.0) -> Dict[str, Any]:
    return {
        **base,
        "name": name,
        "currency": "TWD",
        "limit_up": reference * 1.1 if reference else 0.0,
        "limit_down": reference * 0.9 if reference else 0.0,
        "reference": reference,
        "day_trade": "Yes" if base["security_type"] == "STK" else "",
        "update_date": "2026-09-23",
        "category": "equity" if base["security_type"] == "STK" else "index",
        "margin_trading_balance": 0,
        "short_selling_balance": 0,
    }


def _stock_contracts() -> List[Dict[str, Any]]:
    return [
        _contract_base(item.symbol.removesuffix(".TW"), "STK", "TSE")
        for item in LIQUID_UNIVERSE
        if item.market == "TW"
    ]


def _all_contracts(security_type: str) -> List[Dict[str, Any]]:
    if security_type == "STK":
        return _stock_contracts()
    if security_type == "IND":
        return [_contract_base("IX0001", "IND", "TAIFEX")]
    if security_type == "FUT":
        return [
            _contract_base("TXF202610", "FUT", "TAIFEX"),
            _contract_base("TXF202611", "FUT", "TAIFEX"),
            _contract_base("MTX202610", "FUT", "TAIFEX"),
            _contract_base("MTX202611", "FUT", "TAIFEX"),
        ]
    if security_type == "OPT":
        return [_contract_base("TXO202610C20000", "OPT", "TAIFEX"), _contract_base("TXO202610P20000", "OPT", "TAIFEX")]
    if security_type == "WRT":
        return []
    raise HTTPException(status_code=422, detail="unsupported security_type")


def _contract_infos(security_type: str, root: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return stable metadata rows for the UI's discovery panels."""
    rows = _all_contracts(security_type)
    if root:
        root_upper = root.upper()
        rows = [row for row in rows if row["code"].startswith(root_upper)]
    result = []
    for row in rows:
        info = _contract_info(row, row["code"])
        if security_type == "FUT":
            info.update({"root": row["code"][:3], "multiplier": 200, "contract_size": 200})
        elif security_type == "OPT":
            info.update({"root": "TXO", "strike_price": 20000, "option_right": "C" if "C" in row["code"] else "P", "multiplier": 50})
        result.append(info)
    return result


def _normalize_contract(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="contract must be an object")
    if "legs" in value:
        return value
    code = str(value.get("code", "")).strip().upper()
    if not code:
        raise HTTPException(status_code=422, detail="contract.code is required")
    security_type = str(value.get("security_type", "STK"))
    exchange = value.get("exchange") or ("TAIFEX" if security_type in {"FUT", "OPT", "IND"} else "TSE")
    return _contract_base(code, security_type, exchange)


def _combo_contract(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="combo contract must be an object")
    legs = value.get("legs")
    if not isinstance(legs, list) or len(legs) != 2:
        raise HTTPException(status_code=422, detail="paper combo requires exactly two legs")
    normalized = [_normalize_contract(leg) for leg in legs]
    if any(leg["security_type"] not in {"FUT", "OPT"} for leg in normalized):
        raise HTTPException(status_code=422, detail="paper combo supports futures/options legs only")
    combo_type = str(value.get("combo_type") or "TimeSpread")
    code = f'{normalized[0]["code"]}/{normalized[1]["code"]}'
    return {
        "code": code,
        "legs": normalized,
        "region": "TW",
        "exchange": "TAIFEX",
        "combo_type": combo_type,
        "managed": True,
    }


def _latest_bar(request: Request, contract: Dict[str, Any]):
    code = str(contract.get("code", ""))
    if contract.get("region", "TW") != "TW":
        return request.app.state.app_state.market_adapter.get_latest_bar(code)
    primary = resolve_tw_symbol(code)
    candidates = [primary]
    alternate = f"{code.removesuffix('.TW').removesuffix('.TWO')}.TWO" if primary.endswith(".TW") else f"{code.removesuffix('.TW').removesuffix('.TWO')}.TW"
    if alternate not in candidates:
        candidates.append(alternate)
    for symbol in candidates:
        bar = request.app.state.app_state.market_adapter.get_latest_bar(symbol)
        if bar is not None:
            return bar
    return None


def _number(value: Any) -> float:
    text = str(value or "0").replace(",", "").replace("%", "").strip()
    try:
        return float(text)
    except ValueError:
        return 0.0


def _official_futures_snapshot(contract: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    code = str(contract.get("code", "")).upper()
    root = "TX" if code.startswith("TXF") else "MTX" if code.startswith("MTX") else ""
    month = code[3:] if root else ""
    if not root or not month:
        return None
    now = time.monotonic()
    if now >= float(_TAIFEX_CACHE["expires_at"]):
        try:
            with urlopen(TAIFEX_FUTURES_DAILY_URL, timeout=8) as response:
                rows = json.loads(response.read())
            _TAIFEX_CACHE.update({"expires_at": now + 300, "rows": rows if isinstance(rows, list) else []})
        except Exception:
            _TAIFEX_CACHE.update({"expires_at": now + 30, "rows": []})
    row = next(
        (
            item for item in _TAIFEX_CACHE["rows"]
            if str(item.get("Contract", "")).strip() == root
            and str(item.get("ContractMonth(Week)", "")).strip() == month
            and str(item.get("TradingSession", "")).strip() in {"一般", ""}
        ),
        None,
    )
    if row is None:
        return None
    close = _number(row.get("Last"))
    change = _number(row.get("Change"))
    bid = _number(row.get("BestBid"))
    ask = _number(row.get("BestAsk"))
    return {
        "code": code,
        "exchange": "TAIFEX",
        "datetime": str(row.get("Date", "")),
        "open": _number(row.get("Open")),
        "high": _number(row.get("High")),
        "low": _number(row.get("Low")),
        "close": close,
        "average_price": close,
        "buy_price": bid,
        "buy_volume": 0,
        "sell_price": ask,
        "sell_volume": 0,
        "volume": int(_number(row.get("Volume"))),
        "total_volume": int(_number(row.get("Volume"))),
        "amount": 0,
        "total_amount": 0,
        "change_price": change,
        "change_rate": _number(row.get("%")),
        "change_type": "Rise" if change > 0 else "Fall" if change < 0 else "Equal",
        "tick_type": "Buy" if change >= 0 else "Sell",
        "volume_ratio": 0,
        "yesterday_volume": 0,
        "paper_only": True,
        "is_stale": False,
        "source": "TAIFEX_OPENAPI_DAILY",
    }


def _official_stock_snapshot(contract: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    code = str(contract.get("code", "")).upper()
    row = official_quote(code)
    if row is None or float(row.get("close", 0)) <= 0:
        return None
    close = float(row["close"])
    change = float(row.get("change", 0))
    reference = close - change
    rate = (change / reference * 100.0) if reference else 0.0
    bid = float(row.get("bid", 0)) or close
    ask = float(row.get("ask", 0)) or close
    return {
        "code": code.removesuffix(".TWO").removesuffix(".TW"),
        "exchange": "OTC" if row.get("market") == "TPEX" else "TSE",
        "datetime": str(row.get("date", "")),
        "open": float(row.get("open", 0)), "high": float(row.get("high", 0)),
        "low": float(row.get("low", 0)), "close": close,
        "average_price": round((float(row.get("high", 0)) + float(row.get("low", 0)) + close) / 3, 6),
        "buy_price": bid, "buy_volume": 0, "sell_price": ask, "sell_volume": 0,
        "volume": int(row.get("volume", 0)), "total_volume": int(row.get("volume", 0)),
        "amount": int(row.get("amount", 0)), "total_amount": int(row.get("amount", 0)),
        "change_price": change, "change_rate": round(rate, 6),
        "change_type": "Rise" if change > 0 else "Fall" if change < 0 else "Equal",
        "tick_type": "Buy" if change >= 0 else "Sell", "volume_ratio": 0,
        "yesterday_volume": 0, "paper_only": True, "is_stale": True,
        "source": f'{row.get("market", "TW")}_OPENAPI_DAILY',
    }


def _snapshot(request: Request, contract: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(contract, dict) and "legs" in contract:
        combo = _combo_contract(contract)
        first = _snapshot(request, combo["legs"][0])
        second = _snapshot(request, combo["legs"][1])
        close = round(float(first["close"]) - float(second["close"]), 4)
        buy = round(float(first["buy_price"]) - float(second["sell_price"]), 4)
        sell = round(float(first["sell_price"]) - float(second["buy_price"]), 4)
        return {
            "code": combo["code"],
            "exchange": "TAIFEX",
            "datetime": max(str(first["datetime"]), str(second["datetime"])),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "average_price": close,
            "buy_price": buy,
            "buy_volume": min(int(first["buy_volume"]), int(second["sell_volume"])),
            "sell_price": sell,
            "sell_volume": min(int(first["sell_volume"]), int(second["buy_volume"])),
            "volume": 0,
            "total_volume": 0,
            "amount": 0,
            "total_amount": 0,
            "change_price": 0,
            "change_rate": 0,
            "change_type": "Equal",
            "tick_type": "Buy",
            "volume_ratio": 0,
            "yesterday_volume": 0,
            "paper_only": True,
            "is_stale": bool(first.get("is_stale") or second.get("is_stale")),
            "source": "paper_combo_derived",
        }
    normalized = _normalize_contract(contract)
    code = normalized.get("code", "")
    if normalized.get("security_type") == "FUT":
        official = _official_futures_snapshot(normalized)
        if official is not None:
            return official
    bar = _latest_bar(request, normalized)
    if bar is None:
        official = _official_stock_snapshot(normalized)
        if official is not None:
            return official
        raise HTTPException(status_code=404, detail=f"no market data for {code}")
    recent = request.app.state.app_state.market_adapter.get_bars(bar.symbol, limit=2)
    previous = recent[-2] if len(recent) >= 2 else bar
    change = round(bar.close - previous.close, 4)
    rate = round((change / previous.close) * 100 if previous.close else 0.0, 6)
    dt = bar.timestamp.isoformat()
    return {
        "code": code,
        "exchange": "OTC" if str(bar.symbol).endswith(".TWO") else normalized.get("exchange", "TSE"),
        "datetime": dt,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "average_price": round((bar.high + bar.low + bar.close) / 3, 6),
        # Yahoo delayed bars have no order-book payload.  Null means unavailable;
        # displaying invented depth is more dangerous than an empty five-level panel.
        "buy_price": None,
        "buy_volume": None,
        "sell_price": None,
        "sell_volume": None,
        "volume": bar.volume,
        "total_volume": bar.volume,
        "amount": round(bar.close * bar.volume, 4),
        "total_amount": round(bar.close * bar.volume, 4),
        "change_price": change,
        "change_rate": rate,
        "change_type": "Rise" if change > 0 else "Fall" if change < 0 else "Equal",
        # A delayed OHLC bar does not disclose aggressor side.
        "tick_type": None,
        "volume_ratio": 1.0,
        "yesterday_volume": previous.volume,
        "paper_only": True,
        "is_stale": bar.is_stale,
        "source": bar.source,
        "quality": bar.quality,
    }


class WatchlistContracts(BaseModel):
    contracts: List[Dict[str, Any]] = Field(default_factory=list)


class WatchlistCreate(WatchlistContracts):
    name: str = Field(min_length=1, max_length=80)


class KbarsRequest(BaseModel):
    contract: Dict[str, Any]
    start: Optional[str] = None
    end: Optional[str] = None


def _watchlist(state: Any, item: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": item["id"], "name": item["name"], "contracts": list(item["contracts"])}


def _ensure_watchlists(state: Any) -> List[Dict[str, Any]]:
    if not hasattr(state, "shioaji_watchlists"):
        state.shioaji_watchlists = [{
            "id": "paper-default",
            "name": "Paper Watchlist",
            "contracts": [_normalize_contract({"code": code}) for code in ("2330", "2454", "2317")],
        }]
        state.shioaji_watchlist_seq = 0
    return state.shioaji_watchlists


@router.get("/api/v1/health")
def facade_health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "version": "paper-facade-1",
        "mode": "simulation_only",
        "simulation": True,
        "paper_only": True,
        "broker_connected": False,
        "last_maintenance": None,
    }


@router.get("/api/v1/info")
def facade_info() -> Dict[str, Any]:
    return {
        "name": "CIO Market Lab paper compatibility facade",
        "version": "paper-facade-1",
        "description": "Deterministic local facade; no broker connection.",
        "protocols": ["REST", "SSE"],
        "simulation": True,
    }


@router.get("/api/v1/auth/accounts")
def accounts() -> List[Dict[str, Any]]:
    return [dict(PAPER_ACCOUNT), dict(PAPER_FUTURES_ACCOUNT)]


@router.get("/api/v1/auth/ca_expiretime")
def ca_expiretime(person_id: str) -> Dict[str, str]:
    return {"person_id": person_id, "expire_time": "2099-12-31T23:59:59+00:00"}


@router.post("/api/v1/auth/subscribe_trade")
def subscribe_trade() -> Dict[str, Any]:
    return {"success": False, "message": "broker trade subscriptions are disabled in paper-only mode", "paper_only": True, "broker_connected": False}


@router.get("/api/v1/data/contracts")
def contracts(
    security_type: str = Query(default="STK"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
) -> Dict[str, Any]:
    rows = _all_contracts(security_type.upper())
    start = (page - 1) * page_size
    selected = rows[start:start + page_size]
    return {"contracts": selected, "security_type": security_type.upper(), "region": "TW", "total": len(rows), "page": page, "page_size": page_size, "max_page": max(1, (len(rows) + page_size - 1) // page_size)}


@router.get("/api/v1/data/contracts/futures/roots")
def futures_roots() -> List[Dict[str, str]]:
    return [{"root": "TXF", "name": "臺指期"}, {"root": "MTX", "name": "小臺指"}]


@router.get("/api/v1/data/contracts/options/roots")
def options_roots() -> List[Dict[str, str]]:
    return [{"root": "TXO", "name": "臺指選"}]


@router.get("/api/v1/data/contracts/futures")
def futures(root: Optional[str] = None) -> List[Dict[str, Any]]:
    return _contract_infos("FUT", root)


@router.get("/api/v1/data/contracts/options")
def options(root: str = "TXO", option_right: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = _contract_infos("OPT", root)
    if option_right:
        rows = [row for row in rows if row.get("option_right") == option_right.upper()]
    return rows


@router.post("/api/v1/data/contracts/combo")
def build_combo(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _combo_contract(payload)


@router.get("/api/v1/data/contracts/combo/futures")
def combo_futures(root: str = "TXF", region: str = "TW") -> List[Dict[str, Any]]:
    del region
    legs = _contract_infos("FUT", root)
    return [
        _combo_contract({"legs": [legs[index], legs[index + 1]], "combo_type": "TimeSpread"})
        for index in range(max(0, len(legs) - 1))
    ]


@router.get("/api/v1/data/contracts/warrants")
def warrants(underlying_code: str, code: Optional[str] = None) -> List[Dict[str, Any]]:
    # No broker warrant catalogue is exposed in paper mode.
    _ = underlying_code, code
    return []


@router.get("/api/v1/data/contracts/warrants/underlyings")
def warrant_underlyings() -> List[Dict[str, Any]]:
    return [
        {"underlying_code": item.symbol.removesuffix(".TW"), "name": item.name, "warrant_count": 0}
        for item in LIQUID_UNIVERSE if item.market == "TW"
    ]


@router.get("/api/v1/data/contracts/tick-bands/{rule}")
def tick_bands(rule: str, security_type: str = Query(default="FUT")) -> Dict[str, Any]:
    kind = security_type.upper()
    if kind not in {"FUT", "OPT"}:
        raise HTTPException(status_code=422, detail="tick bands require FUT or OPT")
    return {
        "region": "TW",
        "security_type": kind,
        "rule": rule,
        "basis": "premium" if kind == "OPT" else "price",
        "bands": [{"min": 0, "max": None, "tick": 50 if kind == "FUT" else 1}],
    }


@router.get("/api/v1/data/contracts/{code}/info")
def contract_info(code: str, security_type: str = Query(default="STK")) -> Dict[str, Any]:
    base = _normalize_contract({"code": code, "security_type": security_type})
    return _contract_info(base, code)


@router.get("/api/v1/data/contracts/{code}")
def contract_base(code: str, security_type: str = Query(default="STK")) -> Dict[str, Any]:
    return _normalize_contract({"code": code, "security_type": security_type})


@router.post("/api/v1/data/snapshots")
def snapshots(request: Request, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    contracts = payload.get("contracts")
    if not isinstance(contracts, list):
        raise HTTPException(status_code=422, detail="contracts must be a list")
    return [_snapshot(request, item) for item in contracts]


@router.post("/api/v1/data/kbars")
def kbars(request: Request, payload: KbarsRequest) -> Dict[str, Any]:
    contract = _normalize_contract(payload.contract)
    symbol = str(contract["code"])
    region = contract.get("region", "TW")
    if region == "TW":
        symbol = resolve_tw_symbol(symbol)
    adapter = request.app.state.app_state.market_adapter
    bars = adapter.get_history_range(symbol, payload.start, payload.end)
    if not bars and region == "TW":
        base = str(contract["code"]).removesuffix(".TW").removesuffix(".TWO")
        alternate = f"{base}.TWO" if symbol.endswith(".TW") else f"{base}.TW"
        bars = adapter.get_history_range(alternate, payload.start, payload.end)
    return {
        "datetime": [bar.timestamp.isoformat() for bar in bars],
        "Open": [bar.open for bar in bars],
        "High": [bar.high for bar in bars],
        "Low": [bar.low for bar in bars],
        "Close": [bar.close for bar in bars],
        "Volume": [bar.volume for bar in bars],
        "Amount": [round(bar.close * bar.volume, 4) for bar in bars],
        "paper_only": True,
        "is_stale": any(bar.is_stale for bar in bars),
        "data_status": "ok" if bars else "unavailable",
        "source": bars[-1].source if bars else None,
        "quality": bars[-1].quality if bars else "missing",
    }


@router.post("/api/v1/data/ticks")
def ticks(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    contract = _normalize_contract(payload.get("contract"))
    code = str(contract["code"])
    # This deployment has delayed OHLC bars, not exchange tick history.  Daily
    # bars must not be relabelled as Buy ticks because that fabricates tape data.
    return {
        "code": code,
        "date": payload.get("date"),
        "ticks": [],
        "data_status": "unsupported",
        "reason": "tick_history_provider_not_configured",
        "paper_only": True,
    }


@router.get("/api/v1/watchlist")
def list_watchlists(request: Request) -> List[Dict[str, Any]]:
    return [_watchlist(request.app.state.app_state, item) for item in _ensure_watchlists(request.app.state.app_state)]


@router.post("/api/v1/watchlist")
def create_watchlist(request: Request, payload: WatchlistCreate) -> Dict[str, Any]:
    assert_owner_port(request, "Watchlist mutations")
    state = request.app.state.app_state
    lists = _ensure_watchlists(state)
    state.shioaji_watchlist_seq += 1
    item = {"id": f"paper-list-{state.shioaji_watchlist_seq}", "name": payload.name.strip(), "contracts": [_normalize_contract(c) for c in payload.contracts]}
    lists.append(item)
    return _watchlist(state, item)


@router.put("/api/v1/watchlist/{watchlist_id}")
def sync_watchlist(request: Request, watchlist_id: str, payload: WatchlistContracts) -> Dict[str, Any]:
    assert_owner_port(request, "Watchlist mutations")
    state = request.app.state.app_state
    item = next((x for x in _ensure_watchlists(state) if x["id"] == watchlist_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="watchlist not found")
    item["contracts"] = [_normalize_contract(c) for c in payload.contracts]
    return _watchlist(state, item)


@router.post("/api/v1/watchlist/{watchlist_id}/contracts")
def add_watchlist_contracts(request: Request, watchlist_id: str, payload: WatchlistContracts) -> Dict[str, Any]:
    assert_owner_port(request, "Watchlist mutations")
    state = request.app.state.app_state
    item = next((x for x in _ensure_watchlists(state) if x["id"] == watchlist_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="watchlist not found")
    existing = {(x.get("security_type"), x.get("exchange"), x.get("code")) for x in item["contracts"]}
    for contract in payload.contracts:
        normalized = _normalize_contract(contract)
        key = (normalized.get("security_type"), normalized.get("exchange"), normalized.get("code"))
        if key not in existing:
            item["contracts"].append(normalized)
            existing.add(key)
    return _watchlist(state, item)


@router.delete("/api/v1/watchlist/{watchlist_id}/contracts")
def remove_watchlist_contracts(request: Request, watchlist_id: str, payload: WatchlistContracts) -> Dict[str, Any]:
    assert_owner_port(request, "Watchlist mutations")
    state = request.app.state.app_state
    item = next((x for x in _ensure_watchlists(state) if x["id"] == watchlist_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="watchlist not found")
    remove = {(x.get("security_type"), x.get("exchange"), x.get("code")) for x in payload.contracts}
    item["contracts"] = [x for x in item["contracts"] if (x.get("security_type"), x.get("exchange"), x.get("code")) not in remove]
    return _watchlist(state, item)


@router.delete("/api/v1/watchlist/{watchlist_id}")
def delete_watchlist(request: Request, watchlist_id: str) -> Dict[str, Any]:
    assert_owner_port(request, "Watchlist mutations")
    state = request.app.state.app_state
    lists = _ensure_watchlists(state)
    if not any(x["id"] == watchlist_id for x in lists):
        raise HTTPException(status_code=404, detail="watchlist not found")
    state.shioaji_watchlists = [x for x in lists if x["id"] != watchlist_id]
    return {"success": True, "id": watchlist_id, "paper_only": True}


@router.post("/api/v1/stream/subscribe")
async def stream_subscribe(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    assert_owner_port(request, "Stream mutations")
    state = request.app.state.app_state
    if not hasattr(state, "shioaji_subscriptions"):
        state.shioaji_subscriptions = set()
    key = f"{payload.get('code', '')}:{payload.get('quote_type', '')}"
    state.shioaji_subscriptions.add(key)
    return {"success": True, "message": "paper stream subscription registered", "subscription": payload, "paper_only": True}


@router.post("/api/v1/stream/unsubscribe")
async def stream_unsubscribe(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    assert_owner_port(request, "Stream mutations")
    state = request.app.state.app_state
    key = f"{payload.get('code', '')}:{payload.get('quote_type', '')}"
    getattr(state, "shioaji_subscriptions", set()).discard(key)
    return {"success": True, "message": "paper stream subscription removed", "paper_only": True}


@router.post("/api/v1/stream/{action}/{capability:path}")
async def stream_capability(
    request: Request,
    action: str,
    capability: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    assert_owner_port(request, "Stream mutations")
    if action not in {"subscribe", "unsubscribe"}:
        raise HTTPException(status_code=404, detail="unsupported stream operation")
    state = request.app.state.app_state
    if not hasattr(state, "shioaji_capability_subscriptions"):
        state.shioaji_capability_subscriptions = set()
    key = json.dumps({"capability": capability, "payload": payload}, sort_keys=True, default=str)
    if action == "subscribe":
        state.shioaji_capability_subscriptions.add(key)
    else:
        state.shioaji_capability_subscriptions.discard(key)
    return {
        "success": True,
        "message": f"paper capability {action}d",
        "capability": capability,
        "paper_only": True,
        "broker_connected": False,
    }


@router.get("/api/v1/stream/data")
async def stream_data(request: Request, region: str = Query(default="TW")) -> StreamingResponse:
    async def events():
        yield f"event: status\ndata: {json.dumps({'status': 'paper', 'region': region.upper(), 'broker_connected': False})}\n\n"
        while not await request.is_disconnected():
            now = datetime.now(timezone.utc).isoformat()
            yield f"event: heartbeat\ndata: {json.dumps({'timestamp': now, 'paper_only': True})}\n\n"
            await asyncio.sleep(10)
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- paper order / portfolio compatibility ---------------------------------

def _paper_trade(request: Request, order: Any) -> Dict[str, Any]:
    account = PAPER_FUTURES_ACCOUNT if order.market == Market.TW and order.symbol.startswith(("TXF", "MTX", "TXO")) else PAPER_ACCOUNT
    status_map = {
        OrderStatus.PENDING: "Submitted",
        OrderStatus.FILLED: "Filled",
        OrderStatus.PARTIALLY_FILLED: "PartFilled",
        OrderStatus.CANCELLED: "Cancelled",
        OrderStatus.REJECTED: "Failed",
    }
    security_type = "OPT" if order.symbol.startswith("TXO") else "FUT" if order.symbol.startswith(("TXF", "MTX")) else "STK"
    deals = []
    for ledger in request.app.state.app_state.portfolio_manager._ledgers.values():
        for fill in ledger.fills:
            if fill.order_id == order.order_id:
                deals.append({"seq": fill.fill_id, "price": fill.fill_price, "quantity": fill.quantity, "ts": int(fill.timestamp.timestamp())})
    deal_qty = sum(float(row["quantity"]) for row in deals)
    return {
        "contract": {**_contract_base(order.symbol.removesuffix(".TW"), security_type, "TAIFEX" if security_type != "STK" else "TSE"), "name": order.symbol},
        "order": {
            "id": order.order_id,
            "seqno": order.order_id[:8],
            "ordno": order.order_id[-8:],
            "action": "Buy" if order.side == OrderSide.BUY else "Sell",
            "price": order.limit_price or order.stop_price or 0.0,
            "quantity": order.quantity,
            "order_type": "ROD",
            "price_type": "MKT" if order.order_type == OrderType.MARKET else "LMT",
            "order_lot": "Common",
            "custom_field": str(order.audit_metadata.get("custom_field", "")),
            "account": {k: account[k] for k in ("broker_id", "account_id", "account_type")},
        },
        "status": {
            "id": order.order_id,
            "status": status_map[order.status],
            "status_code": "0" if order.status != OrderStatus.REJECTED else "PAPER_REJECTED",
            "order_ts": int(order.created_at.timestamp()),
            "order_quantity": order.quantity,
            "deal_quantity": deal_qty,
            "cancel_quantity": order.quantity - deal_qty if order.status == OrderStatus.CANCELLED else 0,
            "modified_price": order.limit_price or order.stop_price or 0.0,
            "msg": order.rejection_reason or "paper-only simulated order",
            "deals": deals,
        },
        "paper_only": True,
        "broker_connected": False,
    }


def _order_request(request: Request, payload: Dict[str, Any]) -> PaperOrderRequest:
    contract = _normalize_contract(payload.get("contract") or {})
    body = payload.get("stock_order") or payload.get("futures_order") or payload.get("order") or {}
    code = str(contract["code"])
    bar = _latest_bar(request, contract)
    price = float(body.get("price") or (bar.close if bar else 0.0))
    if price <= 0:
        raise HTTPException(status_code=422, detail="paper order requires a positive price")
    custom = str(body.get("custom_field") or "")
    bucket = DecisionScope.INTRADAY if body.get("daytrade_short") or str(body.get("octype", "")).lower() == "daytrade" else DecisionScope.SWING
    return PaperOrderRequest(
        symbol=code,
        market=Market.TW,
        bucket=bucket,
        side=OrderSide.BUY if body.get("action") == "Buy" else OrderSide.SELL,
        order_type=OrderType.MARKET if body.get("price_type") in {"MKT", "MKP"} else OrderType.LIMIT,
        quantity=float(body.get("quantity", 0)),
        limit_price=None if body.get("price_type") in {"MKT", "MKP"} else price,
        origin=OrderOrigin.STRATEGY if custom.startswith("auto:") else OrderOrigin.MANUAL,
        reason="Shioaji UI paper order",
        audit_metadata={"custom_field": custom, "facade": "shioaji", "paper_only": True},
        explicit_user_instruction=not custom.startswith("auto:"),
        data=PaperDataContext(source=bar.source if bar else "paper-facade", age_seconds=0, last_price=price, is_stale=False, is_fallback=False),
    )


@router.post("/api/v1/order/place_order")
def place_order(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    assert_owner_port(request, "Order mutations")
    state = request.app.state.app_state
    req = _order_request(request, payload)
    key = str(req.audit_metadata.get("custom_field") or "")
    if key:
        for existing in state.paper_orders.all_orders():
            if existing.audit_metadata.get("custom_field") == key:
                return _paper_trade(request, existing)
    try:
        order = state.paper_orders.submit(req)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _paper_trade(request, order)


@router.post("/api/v1/order/trades")
def trades(request: Request, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [_paper_trade(request, order) for order in request.app.state.app_state.paper_orders.all_orders()]


@router.post("/api/v1/order/trade_cache_health")
def trade_cache_health() -> Dict[str, Any]:
    return {"state": "Healthy", "reasons": [], "paper_only": True}


def _find_trade(request: Request, trade_id: str) -> Any:
    order = request.app.state.app_state.paper_orders.find_order(trade_id)
    if order is None:
        raise HTTPException(status_code=404, detail="paper trade not found")
    return order


@router.post("/api/v1/order/cancel_order")
def cancel_order(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    assert_owner_port(request, "Order mutations")
    nested = payload.get("trade") or {}
    trade_id = payload.get("trade_id") or (nested.get("order") or {}).get("id") or (nested.get("status") or {}).get("id")
    if not trade_id:
        raise HTTPException(status_code=422, detail="trade_id is required")
    try:
        order = request.app.state.app_state.paper_orders.cancel(str(trade_id))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _paper_trade(request, order)


def _replace_order(request: Request, payload: Dict[str, Any], *, price: Optional[float] = None, quantity: Optional[float] = None) -> Dict[str, Any]:
    current = _find_trade(request, str(payload.get("trade_id", "")))
    replacement = PaperOrderRequest(
        symbol=current.symbol, market=current.market, bucket=current.bucket, side=current.side,
        order_type=current.order_type, quantity=float(quantity or current.quantity),
        limit_price=float(price) if price is not None else current.limit_price,
        stop_price=current.stop_price, origin=current.origin, reason="paper cancel-replace",
        audit_metadata={**current.audit_metadata, "replaces": current.order_id},
        strategy_id=current.strategy_id, strategy_version=current.strategy_version,
        explicit_user_instruction=current.origin != OrderOrigin.STRATEGY,
        data=PaperDataContext(source="paper-facade", age_seconds=0, last_price=float(price or current.limit_price or current.stop_price or 1), is_stale=False, is_fallback=False),
    )
    try:
        order = request.app.state.app_state.paper_orders.cancel_replace(current.order_id, replacement)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _paper_trade(request, order)


@router.post("/api/v1/order/update_price")
def update_order_price(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    assert_owner_port(request, "Order mutations")
    if not payload.get("trade_id"):
        raise HTTPException(status_code=422, detail="trade_id is required")
    if "price" not in payload:
        raise HTTPException(status_code=422, detail="price is required")
    try:
        price = float(payload["price"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="price must be numeric") from exc
    if price <= 0:
        raise HTTPException(status_code=422, detail="price must be greater than zero")
    return _replace_order(request, payload, price=price)


@router.post("/api/v1/order/update_qty")
def update_order_qty(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    assert_owner_port(request, "Order mutations")
    if not payload.get("trade_id"):
        raise HTTPException(status_code=422, detail="trade_id is required")
    if "quantity" not in payload:
        raise HTTPException(status_code=422, detail="quantity is required")
    try:
        quantity = float(payload["quantity"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="quantity must be numeric") from exc
    if quantity <= 0:
        raise HTTPException(status_code=422, detail="quantity must be greater than zero")
    return _replace_order(request, payload, quantity=quantity)


@router.post("/api/v1/portfolio/position_unit")
def position_unit(request: Request, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    idx = 0
    for portfolio in request.app.state.app_state.portfolio_manager.get_all_portfolios().values():
        for position in portfolio.positions.values():
            if position.quantity <= 0:
                continue
            idx += 1
            rows.append({"id": idx, "code": position.symbol.removesuffix(".TW"), "direction": "Buy", "quantity": position.quantity, "price": position.average_entry_price, "last_price": position.current_price, "pnl": position.unrealized_pnl, "yd_quantity": position.quantity, "cond": "Cash"})
    return rows


@router.post("/api/v1/portfolio/account_balance")
def account_balance(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = request.app.state.app_state.runner.get_canonical_team_ops(
        is_read_only=not is_owner_port(request)
    )
    portfolio = snapshot.get("portfolio") or {}
    return {
        "acc_balance": portfolio.get("cash"),
        "date": datetime.now(timezone.utc).date().isoformat(),
        "errmsg": "",
        "paper_only": True,
        "reporting_currency": portfolio.get("reporting_currency", "TWD"),
        "as_of": portfolio.get("as_of"),
    }


@router.post("/api/v1/portfolio/margin")
def margin(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = request.app.state.app_state.runner.get_canonical_team_ops(
        is_read_only=not is_owner_port(request)
    )
    portfolio = snapshot.get("portfolio") or {}
    nav_available = portfolio.get("nav_status") == "OK" and portfolio.get("equity") is not None
    total = portfolio.get("equity") if nav_available else None
    cash = portfolio.get("cash")
    return {"yesterday_balance": total, "today_balance": total, "deposit_withdrawal": 0, "fee": 0, "tax": 0, "initial_margin": 0, "maintenance_margin": 0, "margin_call": 0, "risk_indicator": 999, "royalty_revenue_expenditure": 0, "equity": total, "equity_amount": total, "nav_status": "OK" if nav_available else "NAV_UNAVAILABLE", "nav_reason": None if nav_available else "One or more open positions lack a current market mark.", "option_openbuy_market_value": 0, "option_opensell_market_value": 0, "option_open_position": 0, "option_settle_profitloss": 0, "future_open_position": 0, "today_future_open_position": 0, "future_settle_profitloss": 0, "available_margin": cash, "plus_margin": 0, "plus_margin_indicator": 0, "security_collateral_amount": 0, "order_margin_premium": 0, "collateral_amount": 0, "paper_only": True}


@router.post("/api/v1/portfolio/settlements")
def settlements() -> List[Dict[str, Any]]:
    return []


@router.post("/api/v1/portfolio/profit_loss")
def profit_loss(request: Request, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for portfolio in request.app.state.app_state.portfolio_manager.get_all_portfolios().values():
        for fill in portfolio.fills:
            rows.append({"id": fill.fill_id, "code": fill.symbol.removesuffix(".TW"), "quantity": fill.quantity, "price": fill.fill_price, "pnl": 0, "date": fill.timestamp.date().isoformat(), "action": "Buy" if fill.side == OrderSide.BUY else "Sell"})
    return rows


@router.post("/api/v1/portfolio/profitloss_sum")
def profitloss_sum(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    realized = sum(p.realized_pnl for p in request.app.state.app_state.portfolio_manager.get_all_portfolios().values())
    return {"profitloss": realized, "summary": [], "paper_only": True}


@router.post("/api/v1/portfolio/trading_limits")
def trading_limits(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    limits = request.app.state.app_state.paper_orders.risk_limits
    return {"trading_limit": limits.max_order_notional, "trading_limit_used": 0, "margin_limit": limits.max_position_notional, "margin_limit_used": 0, "paper_only": True}


@router.get("/api/v1/data/scanner")
@router.post("/api/v1/data/scanner")
def scanner(request: Request, payload: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    params = dict(request.query_params)
    options = {**params, **(payload or {})}
    scanner_type = str(options.get("scanner_type", "ChangePercentRank"))
    ascending_raw = options.get("ascending", False)
    ascending = ascending_raw if isinstance(ascending_raw, bool) else str(ascending_raw).lower() in {"1", "true", "yes"}
    try:
        count = max(1, min(200, int(options.get("count", 100))))
    except (TypeError, ValueError):
        count = 100
    rows = []
    for contract in _stock_contracts():
        try:
            snap = _snapshot(request, contract)
        except HTTPException:
            continue
        rows.append({"code": contract["code"], "name": contract["code"], "close": snap["close"], "change_price": snap["change_price"], "change_rate": snap["change_rate"], "volume": snap["volume"], "amount": snap["amount"], "source": snap.get("source"), "is_stale": snap.get("is_stale", True)})
    sort_key = "amount" if "Amount" in scanner_type else "volume" if "Volume" in scanner_type else "change_rate"
    rows.sort(key=lambda row: row.get(sort_key) if row.get(sort_key) is not None else float("-inf"), reverse=not ascending)
    selected = rows[:count]
    for rank, row in enumerate(selected, start=1):
        row["rank"] = rank
    return selected


@router.get("/api/v1/data/index_components")
@router.post("/api/v1/data/index_components")
def index_components() -> Dict[str, Any]:
    # The frontend consumes the Shioaji 1.7 snapshot contract, not a bare
    # constituent list.  Build it from official TWSE/TPEX quote + profile APIs.
    return index_components_snapshot()


@router.post("/api/v1/data/credit_enquire")
def credit_enquire(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    code = str((payload.get("contract") or {}).get("code", ""))
    return [{"code": code, "margin_unit": 0, "short_unit": 0, "paper_only": True}]


@router.post("/api/v1/data/short_stock_sources")
def short_stock_sources(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    code = str((payload.get("contract") or {}).get("code", ""))
    return [{"code": code, "short_stock_source": 0, "paper_only": True}]


@router.get("/api/v1/data/regulatory_punish")
def regulatory_punish() -> Dict[str, Any]:
    return {"code": []}


@router.get("/api/v1/auth/usage")
def auth_usage() -> Dict[str, Any]:
    return {"bytes_sent": 0, "bytes_received": 0, "requests": 0, "paper_only": True}


@router.get("/api/v1/monitor/subscriptions")
def monitor_subscriptions(request: Request, limit: int = 100) -> Dict[str, Any]:
    items = sorted(getattr(request.app.state.app_state, "shioaji_subscriptions", set()))[:limit]
    return {"items": items, "total": len(items), "paper_only": True}


@router.get("/api/v1/monitor/metrics")
def monitor_metrics() -> Dict[str, Any]:
    return {"series": [], "paper_only": True, "broker_connected": False}


@router.post("/api/v1/monitor/quota")
def monitor_quota() -> Dict[str, Any]:
    return {"limit": 0, "used": 0, "paper_only": True, "broker_connected": False}


@router.post("/api/v1/monitor/usage")
def monitor_usage() -> Dict[str, Any]:
    return {"requests": 0, "bytes": 0, "paper_only": True, "broker_connected": False}


@router.post("/api/v1/order/place_comboorder")
def place_combo_order(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    assert_owner_port(request, "Combo order mutations")
    combo = _combo_contract(payload.get("combo_contract") or {})
    order = payload.get("order") or {}
    quantity = int(order.get("quantity") or 0)
    price = float(order.get("price") or 0)
    if quantity <= 0:
        raise HTTPException(status_code=422, detail="combo quantity must be positive")
    if str(order.get("price_type", "LMT")) == "LMT" and price <= 0:
        raise HTTPException(status_code=422, detail="combo limit price must be positive")
    state = request.app.state.app_state
    if not hasattr(state, "shioaji_combo_trades"):
        state.shioaji_combo_trades = []
    trade_id = f"paper-combo-{len(state.shioaji_combo_trades) + 1}"
    trade = {
        "contract": {"legs": combo["legs"], "combo_type": combo["combo_type"], "code": combo["code"]},
        "order": {
            "id": trade_id,
            "seqno": trade_id,
            "action": str(order.get("action", "Buy")),
            "price": price,
            "quantity": quantity,
            "price_type": str(order.get("price_type", "LMT")),
            "order_type": str(order.get("order_type", "ROD")),
            "account": PAPER_FUTURES_ACCOUNT,
        },
        "status": {"id": trade_id, "status": "Submitted", "msg": "paper combo order accepted"},
        "paper_only": True,
        "broker_connected": False,
    }
    state.shioaji_combo_trades.append(trade)
    return trade


@router.post("/api/v1/order/cancel_comboorder")
def cancel_combo_order(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    assert_owner_port(request, "Combo order mutations")
    trade_id = str(payload.get("trade_id", ""))
    for trade in getattr(request.app.state.app_state, "shioaji_combo_trades", []):
        if str(trade["order"]["id"]) == trade_id:
            trade["status"] = {"id": trade_id, "status": "Cancelled", "msg": "paper combo order cancelled"}
            return trade
    raise HTTPException(status_code=404, detail="paper combo trade not found")


@router.post("/api/v1/order/combotrades")
def combo_trades(request: Request) -> List[Dict[str, Any]]:
    return list(getattr(request.app.state.app_state, "shioaji_combo_trades", []))


@router.post("/api/v1/order/stock_reserve_summary")
@router.post("/api/v1/order/stock_reserve_detail")
@router.post("/api/v1/order/earmarking_detail")
def unavailable_inventory() -> List[Dict[str, Any]]:
    return []


@router.post("/api/v1/order/reserve_stock")
@router.post("/api/v1/order/reserve_earmarking")
def reject_broker_inventory_mutation(request: Request) -> Dict[str, Any]:
    assert_owner_port(request, "Inventory mutations")
    raise HTTPException(status_code=409, detail={"message": "broker inventory mutations are unavailable in paper-only mode", "paper_only": True, "broker_connected": False})
