from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any, Dict, Iterator, List, Optional
from zoneinfo import ZoneInfo
import requests

from cio_market_lab.data.base import MarketDataAdapter
from cio_market_lab.domain.models import Bar, Market, Quote

TWSE_QUOTES_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_QUOTES_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TWSE_PROFILE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_PROFILE_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
TWSE_INTRADAY_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"

INDUSTRY_NAMES = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "07": "化學生技醫療", "08": "玻璃陶瓷",
    "09": "造紙工業", "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業",
    "14": "建材營造", "15": "航運業", "16": "觀光餐旅", "17": "金融保險",
    "18": "貿易百貨", "20": "其他", "21": "化學工業", "22": "生技醫療業",
    "23": "油電燃氣業", "24": "半導體業", "25": "電腦及週邊設備業",
    "26": "光電業", "27": "通信網路業", "28": "電子零組件業", "29": "電子通路業",
    "30": "資訊服務業", "31": "其他電子業", "32": "文化創意業", "33": "農業科技業",
    "34": "電子商務", "35": "綠能環保", "36": "數位雲端", "37": "運動休閒",
    "38": "居家生活", "80": "管理股票", "91": "存託憑證",
}

_QUOTE_CACHE: Dict[str, Any] = {"expires_at": 0.0, "rows": {}, "error": None}
_PROFILE_CACHE: Dict[str, Any] = {"expires_at": 0.0, "rows": {}, "error": None}


def _number(value: Any) -> float:
    text = str(value or "0").replace(",", "").replace("+", "").strip()
    if text in {"", "--", "---", "除權", "除息", "除權息"}:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _fetch_json(url: str, timeout: int = 4) -> List[Dict[str, Any]]:
    response = requests.get(url, headers={"User-Agent": "CIO-Market-Lab/1.0"}, timeout=timeout)
    response.raise_for_status()
    value = response.json()
    return value if isinstance(value, list) else []


def _latest_by_code(rows: List[Dict[str, Any]], code_key: str, date_key: str) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        code = str(row.get(code_key, "")).strip().upper()
        if not code:
            continue
        existing = result.get(code)
        if existing is None or str(row.get(date_key, "")) >= str(existing.get(date_key, "")):
            result[code] = row
    return result


def quote_rows(force: bool = False) -> Dict[str, Dict[str, Any]]:
    now = time.monotonic()
    if not force and now < float(_QUOTE_CACHE["expires_at"]):
        return dict(_QUOTE_CACHE["rows"])
    combined: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    try:
        twse = _latest_by_code(_fetch_json(TWSE_QUOTES_URL), "Code", "Date")
        for code, row in twse.items():
            combined[code] = {
                "code": code, "name": str(row.get("Name", "")).strip(), "market": "TWSE",
                "date": str(row.get("Date", "")), "open": _number(row.get("OpeningPrice")),
                "high": _number(row.get("HighestPrice")), "low": _number(row.get("LowestPrice")),
                "close": _number(row.get("ClosingPrice")), "change": _number(row.get("Change")),
                "volume": int(_number(row.get("TradeVolume"))), "amount": int(_number(row.get("TradeValue"))),
                "bid": 0.0, "ask": 0.0,
            }
    except Exception as exc:
        errors.append(f"TWSE {type(exc).__name__}: {exc}")
    try:
        tpex = _latest_by_code(_fetch_json(TPEX_QUOTES_URL), "SecuritiesCompanyCode", "Date")
        for code, row in tpex.items():
            combined[code] = {
                "code": code, "name": str(row.get("CompanyName", "")).strip(), "market": "TPEX",
                "date": str(row.get("Date", "")), "open": _number(row.get("Open")),
                "high": _number(row.get("High")), "low": _number(row.get("Low")),
                "close": _number(row.get("Close")), "change": _number(row.get("Change")),
                "volume": int(_number(row.get("TradingShares"))), "amount": int(_number(row.get("TransactionAmount"))),
                "bid": _number(row.get("LatestBidPrice")), "ask": _number(row.get("LatesAskPrice")),
            }
    except Exception as exc:
        errors.append(f"TPEX {type(exc).__name__}: {exc}")
    if combined:
        _QUOTE_CACHE.update({"expires_at": now + 30.0, "rows": combined, "error": "; ".join(errors) or None})
    else:
        _QUOTE_CACHE.update({"expires_at": now + 10.0, "error": "; ".join(errors) or "empty quote response"})
    return dict(_QUOTE_CACHE["rows"])


def profile_rows(force: bool = False) -> Dict[str, Dict[str, Any]]:
    now = time.monotonic()
    if not force and now < float(_PROFILE_CACHE["expires_at"]):
        return dict(_PROFILE_CACHE["rows"])
    try:
        result: Dict[str, Dict[str, Any]] = {}
        for row in _fetch_json(TWSE_PROFILE_URL, timeout=4):
            code = str(row.get("公司代號", "")).strip().upper()
            if code:
                result[code] = {
                    "industry": str(row.get("產業別", "20")).strip().zfill(2),
                    "shares": int(_number(row.get("已發行普通股數或TDR原股發行股數"))),
                }
        for row in _fetch_json(TPEX_PROFILE_URL, timeout=4):
            code = str(row.get("SecuritiesCompanyCode", "")).strip().upper()
            if code:
                result[code] = {
                    "industry": str(row.get("SecuritiesIndustryCode", "20")).strip().zfill(2),
                    "shares": int(_number(row.get("IssueShares"))),
                }
        _PROFILE_CACHE.update({"expires_at": now + 21600.0, "rows": result, "error": None})
    except Exception as exc:
        _PROFILE_CACHE.update({"expires_at": now + 60.0, "error": f"{type(exc).__name__}: {exc}"})
    return dict(_PROFILE_CACHE["rows"])


def resolve_tw_symbol(code: str) -> str:
    value = str(code).strip().upper()
    if value.endswith((".TW", ".TWO")):
        return value
    row = quote_rows().get(value)
    return f"{value}.TWO" if row and row.get("market") == "TPEX" else f"{value}.TW"


def official_quote(code: str) -> Optional[Dict[str, Any]]:
    value = str(code).strip().upper().removesuffix(".TWO").removesuffix(".TW")
    return quote_rows().get(value)


def _iso_date(roc_date: str) -> str:
    raw = str(roc_date).strip()
    if len(raw) == 7 and raw.isdigit():
        value = f"{int(raw[:3]) + 1911:04d}-{raw[3:5]}-{raw[5:7]}"
        datetime.fromisoformat(value)
        return value
    raise ValueError("INVALID_OFFICIAL_REPORT_DATE")


def index_components_snapshot(limit: int = 160) -> Dict[str, Any]:
    quotes = quote_rows()
    profiles = profile_rows()
    candidates: List[Dict[str, Any]] = []
    for code, profile in profiles.items():
        quote = quotes.get(code)
        if not quote or quote["close"] <= 0 or profile["shares"] <= 0:
            continue
        market_cap = quote["close"] * profile["shares"]
        reference = quote["close"] - quote["change"]
        pct = (quote["change"] / reference * 100.0) if reference else 0.0
        candidates.append({**quote, **profile, "market_cap": market_cap, "reference": reference, "pct": pct})
    candidates.sort(key=lambda row: row["market_cap"], reverse=True)
    candidates = candidates[: max(1, limit)]
    # No eligible constituent, or an undated constituent, cannot establish an
    # official reference date.  Keep the strict parser; do not date a missing
    # report with the wall clock or advertise a ready index.
    try:
        report_dates = [_iso_date(row.get("date", "")) for row in candidates]
        if not report_dates:
            raise ValueError("NO_ELIGIBLE_OFFICIAL_COMPONENTS")
    except ValueError as exc:
        now = datetime.now(timezone.utc)
        return {
            "contract": {"code": "IX0001"}, "date": None, "time": None,
            "calculated_at": now.isoformat(), "reference_date": None,
            "market_phase": "Unavailable", "refresh_state": "unavailable",
            "reason": str(exc), "simtrade": True, "total_amount": None,
            "info": [], "category": [], "price": [], "reference": [],
            "price_chg": [], "pct_chg": [], "points": [],
            "reference_weight_ppm": [], "total_amounts": [],
            "amount_share_bps": [], "price_source": [],
            "trading_status": [], "data_status": [],
            "entries": [], "groups": [], "source": "TWSE_TPEX_OPENAPI",
            "paper_only": True,
        }
    total_cap = sum(row["market_cap"] for row in candidates) or 1.0
    total_amount = sum(row["amount"] for row in candidates) or 1
    entries: List[Dict[str, Any]] = []
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in candidates:
        weight = row["market_cap"] / total_cap
        entry = {
            "contract": {"code": row["code"]}, "category": row["industry"],
            "price": f'{row["close"]:.6f}', "reference": f'{row["reference"]:.6f}',
            "price_chg": f'{row["change"]:.6f}', "pct_chg": f'{row["pct"]:.6f}',
            "points": f'{row["pct"] * weight:.6f}', "reference_weight_ppm": round(weight * 1_000_000),
            "total_amount": row["amount"], "amount_share_bps": round(row["amount"] / total_amount * 10_000),
            "price_source": f'{row["market"]}_OPENAPI_DAILY', "trading_status": "Normal", "data_status": "Ready",
        }
        entries.append(entry)
        grouped.setdefault(row["industry"], []).append({**row, "weight": weight})
    groups: List[Dict[str, Any]] = []
    for category, rows in grouped.items():
        group_weight = sum(row["weight"] for row in rows)
        weighted_pct = sum(row["pct"] * row["weight"] for row in rows) / group_weight if group_weight else 0.0
        equal_pct = sum(row["pct"] for row in rows) / len(rows)
        amount = sum(row["amount"] for row in rows)
        groups.append({
            "category": category, "name": INDUSTRY_NAMES.get(category, f"產業 {category}"),
            "item_count": len(rows), "equal_weight_pct_chg": f"{equal_pct:.6f}",
            "weighted_pct_chg": f"{weighted_pct:.6f}", "points": f"{weighted_pct * group_weight:.6f}",
            "reference_weight_ppm": round(group_weight * 1_000_000), "total_amount": amount,
            "amount_share_bps": round(amount / total_amount * 10_000),
            "advance_count": sum(1 for row in rows if row["change"] > 0),
            "decline_count": sum(1 for row in rows if row["change"] < 0),
            "unchanged_count": sum(1 for row in rows if row["change"] == 0),
            "breadth_bps": round((sum(1 for row in rows if row["change"] > 0) - sum(1 for row in rows if row["change"] < 0)) / len(rows) * 10_000),
        })
    groups.sort(key=lambda row: row["reference_weight_ppm"], reverse=True)
    latest_date = max(report_dates)
    now = datetime.now(timezone.utc)
    return {
        "contract": {"code": "IX0001"}, "date": latest_date, "time": now.strftime("%H:%M:%S"),
        "calculated_at": now.isoformat(), "reference_date": latest_date, "market_phase": "DelayedOfficial",
        "refresh_state": "ready", "simtrade": True, "total_amount": int(total_amount),
        # Shioaji 1.7 parallel-array contract consumed by normalizeIndexSnapshot.
        "info": [row["contract"] for row in entries],
        "category": [row["category"] for row in entries],
        "price": [row["price"] for row in entries],
        "reference": [row["reference"] for row in entries],
        "price_chg": [row["price_chg"] for row in entries],
        "pct_chg": [row["pct_chg"] for row in entries],
        "points": [row["points"] for row in entries],
        "reference_weight_ppm": [row["reference_weight_ppm"] for row in entries],
        "total_amounts": [row["total_amount"] for row in entries],
        "amount_share_bps": [row["amount_share_bps"] for row in entries],
        "price_source": [row["price_source"] for row in entries],
        "trading_status": [row["trading_status"] for row in entries],
        "data_status": [row["data_status"] for row in entries],
        "entries": entries, "groups": groups, "source": "TWSE_TPEX_OPENAPI", "paper_only": True,
    }


class TwOfficialAdapter(MarketDataAdapter):
    """TWSE/TPEX OpenAPI public market data adapter."""

    def __init__(self, offline_mode: bool = True):
        self.offline_mode = offline_mode
        self.last_fetch_mode: str = "offline_fixture" if offline_mode else "live_pending"
        self.last_error: Optional[str] = None
        self.is_eod: bool = True
        self.supports_intraday_ticks: bool = not offline_mode
        self._intraday_cache: Dict[str, tuple[float, Optional[Quote]]] = {}

    @property
    def source_name(self) -> str:
        return "twse_tpex_openapi"

    def get_bars(
        self,
        symbol: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        timeframe: str = "1D",
        limit: Optional[int] = None,
    ) -> List[Bar]:
        if self.offline_mode:
            self.last_fetch_mode = "offline_fixture"
            return []
        try:
            row = official_quote(symbol)
            if not row:
                self.last_fetch_mode = "live_unavailable"
                self.last_error = f"No quote found for symbol {symbol}"
                return []
            close_px = float(row.get("close", 0.0))
            if close_px <= 0:
                self.last_fetch_mode = "live_unavailable"
                return []
            iso_d = _iso_date(str(row.get("date", "")))
            bar_date = datetime.fromisoformat(iso_d).replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            age = max(0.0, (now - bar_date).total_seconds())
            bar = Bar(
                symbol=resolve_tw_symbol(symbol),
                market=Market.TW,
                timestamp=bar_date,
                observed_at=now,
                open=float(row.get("open") or close_px),
                high=float(row.get("high") or close_px),
                low=float(row.get("low") or close_px),
                close=close_px,
                volume=float(row.get("volume", 0.0)),
                source="twse_tpex_openapi",
                delay_seconds=age,
                quality="official_eod",
                is_stale=age > 172800.0,
            )
            self.last_fetch_mode = "live_twse_tpex_openapi"
            self.last_error = None
            bars = [bar]
            if start and bar.timestamp < start:
                bars = []
            if end and bar.timestamp > end:
                bars = []
            return bars[-limit:] if limit is not None else bars
        except Exception as exc:
            self.last_fetch_mode = "live_unavailable"
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []

    def get_latest_bar(self, symbol: str) -> Optional[Bar]:
        bars = self.get_bars(symbol)
        return bars[-1] if bars else None

    def get_latest_quote(self, symbol: str) -> Optional[Quote]:
        if self.offline_mode:
            return None
        # MIS exposes an exchange-dated last trade and book, unlike the daily
        # OpenAPI close. Missing/invalid trade data must not become a quote.
        code = symbol.upper().removesuffix(".TW").removesuffix(".TWO")
        channel = "otc" if symbol.upper().endswith(".TWO") else "tse"
        cached = self._intraday_cache.get(symbol)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        try:
            response = requests.get(
                TWSE_INTRADAY_URL,
                params={"ex_ch": f"{channel}_{code}.tw", "json": "1", "delay": "0"},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=3,
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("msgArray", []) if payload.get("rtcode") == "0000" else []
            row = next((r for r in rows if r.get("c") == code), None)
            if row is None or _number(row.get("z")) <= 0 or _number(row.get("tv")) <= 0:
                raise ValueError("MIS_NO_TIMESTAMPED_LAST_TRADE")
            exchange_at = datetime.strptime(
                row["d"] + " " + row["t"], "%Y%m%d %H:%M:%S"
            ).replace(tzinfo=ZoneInfo("Asia/Taipei")).astimezone(timezone.utc)
            observed = datetime.now(timezone.utc)
            age = (observed - exchange_at).total_seconds()
            if age < -60:
                raise ValueError("MIS_FUTURE_EXCHANGE_TIMESTAMP")
            def level(key: str) -> Optional[float]:
                first = str(row.get(key, "")).split("_")[0]
                price = _number(first)
                return price if price > 0 else None
            quote = Quote(
                symbol=symbol.upper(), timestamp=exchange_at, observed_at=observed,
                last_price=_number(row["z"]), last_size=_number(row["tv"]),
                bid=level("b"), ask=level("a"),
                source="twse_mis_intraday", quality="official_last_trade_snapshot",
                delay_seconds=max(0.0, age), is_stale=age > 1800,
                is_synthetic=False,
            )
            self.last_fetch_mode = "live_twse_mis_intraday"
            self.last_error = None
            self._intraday_cache[symbol] = (time.monotonic() + 5, quote)
            return quote
        except Exception as exc:
            self.last_fetch_mode = "live_intraday_unavailable"
            self.last_error = f"MIS {type(exc).__name__}: {exc}"
            self._intraday_cache[symbol] = (time.monotonic() + 3, None)
            return None

    def stream_bars(self, symbols: List[str]) -> Iterator[Bar]:
        for s in symbols:
            bar = self.get_latest_bar(s)
            if bar:
                yield bar

