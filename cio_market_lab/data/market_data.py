from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Dict, Iterable, Iterator, List, Optional

from cio_market_lab.data.base import MarketDataAdapter
from cio_market_lab.domain.models import Bar, Quote


@dataclass(frozen=True)
class SymbolRecord:
    symbol: str
    name: str
    market: str
    exchange: str
    asset_type: str = "equity"

    def as_dict(self) -> Dict[str, str]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "market": self.market,
            "exchange": self.exchange,
            "asset_type": self.asset_type,
        }


# This is intentionally a small, reviewable liquid universe. It is used for
# breadth only; arbitrary symbols still work through YahooAdapter on demand.
LIQUID_UNIVERSE: tuple[SymbolRecord, ...] = (
    SymbolRecord("2330.TW", "TSMC", "TW", "TWSE"),
    SymbolRecord("2454.TW", "MediaTek", "TW", "TWSE"),
    SymbolRecord("2317.TW", "Hon Hai", "TW", "TWSE"),
    SymbolRecord("3231.TW", "Wistron", "TW", "TWSE"),
    SymbolRecord("2382.TW", "Quanta Computer", "TW", "TWSE"),
    SymbolRecord("2308.TW", "Delta Electronics", "TW", "TWSE"),
    SymbolRecord("2303.TW", "United Microelectronics", "TW", "TWSE"),
    SymbolRecord("3711.TW", "ASE Technology", "TW", "TWSE"),
    SymbolRecord("2881.TW", "Fubon Financial", "TW", "TWSE"),
    SymbolRecord("2882.TW", "Cathay Financial", "TW", "TWSE"),
    SymbolRecord("NVDA", "NVIDIA", "US", "NASDAQ"),
    SymbolRecord("AAPL", "Apple", "US", "NASDAQ"),
    SymbolRecord("MSFT", "Microsoft", "US", "NASDAQ"),
    SymbolRecord("AMZN", "Amazon", "US", "NASDAQ"),
    SymbolRecord("GOOGL", "Alphabet", "US", "NASDAQ"),
    SymbolRecord("META", "Meta Platforms", "US", "NASDAQ"),
    SymbolRecord("TSLA", "Tesla", "US", "NASDAQ"),
    SymbolRecord("AVGO", "Broadcom", "US", "NASDAQ"),
    SymbolRecord("AMD", "Advanced Micro Devices", "US", "NASDAQ"),
    SymbolRecord("JPM", "JPMorgan Chase", "US", "NYSE"),
    SymbolRecord("ES", "Eversource Energy", "US", "NYSE"),
    SymbolRecord("TX", "Ternium", "US", "NYSE"),
    SymbolRecord("PLTR", "Palantir Technologies", "US", "NASDAQ"),
    SymbolRecord("1519.TW", "Fortune Electric", "TW", "TWSE"),
    SymbolRecord("2383.TW", "Elite Material", "TW", "TWSE"),
    SymbolRecord("3017.TW", "Asia Vital Components", "TW", "TWSE"),
    SymbolRecord("1101.TW", "Taiwan Cement", "TW", "TWSE"),
    SymbolRecord("1216.TW", "Uni-President Enterprises", "TW", "TWSE"),
    SymbolRecord("1301.TW", "Formosa Plastics", "TW", "TWSE"),
    SymbolRecord("2603.TW", "Evergreen Marine", "TW", "TWSE"),
    SymbolRecord("0050.TW", "Yuanta Taiwan 50 ETF", "TW", "TWSE", "etf"),
    SymbolRecord("0056.TW", "Yuanta High Dividend ETF", "TW", "TWSE", "etf"),
    SymbolRecord("00878.TW", "Cathay ESG Sustainability High Dividend ETF", "TW", "TWSE", "etf"),
    SymbolRecord("00919.TW", "Capital TIP Customized Taiwan Select High Dividend ETF", "TW", "TWSE", "etf"),
    SymbolRecord("SPY", "SPDR S&P 500 ETF", "US", "NYSE Arca", "etf"),
    SymbolRecord("QQQ", "Invesco QQQ Trust", "US", "NASDAQ", "etf"),
    SymbolRecord("SGOV", "iShares 0-3 Month US Treasury Bond ETF", "US", "NYSE Arca", "etf"),
    SymbolRecord("BIL", "SPDR Bloomberg 1-3 Month T-Bill ETF", "US", "NYSE Arca", "etf"),
    SymbolRecord("SHY", "iShares 1-3 Year Treasury Bond ETF", "US", "NASDAQ", "etf"),
)


def _record_index() -> Dict[str, SymbolRecord]:
    return {item.symbol.upper(): item for item in LIQUID_UNIVERSE}


class SymbolCatalog:
    """On-demand symbol normalization and search without broker metadata."""

    def __init__(self, records: Iterable[SymbolRecord] = LIQUID_UNIVERSE):
        self._records = tuple(records)
        self._index = {item.symbol.upper(): item for item in self._records}

    @staticmethod
    def normalize(symbol: str, market: Optional[str] = None) -> str:
        value = symbol.strip().upper()
        if not value or not re.fullmatch(r"[A-Z0-9._-]+", value):
            raise ValueError("symbol must contain only letters, digits, '.', '_' or '-'")
        requested = market.upper() if market else None
        if requested == "TW" and ".TW" not in value and ".TWO" not in value:
            value = f"{value}.TW"
        if requested == "US" and value.endswith((".TW", ".TWO")):
            raise ValueError("TW symbol cannot be used with market=US")
        return value

    @staticmethod
    def infer_market(symbol: str) -> str:
        return "TW" if symbol.upper().endswith((".TW", ".TWO")) else "US"

    def lookup(self, symbol: str, market: Optional[str] = None) -> Dict[str, Any]:
        normalized = self.normalize(symbol, market)
        known = self._index.get(normalized)
        if known:
            result = known.as_dict()
            result["coverage"] = "catalog"
            return result
        inferred_market = self.infer_market(normalized)
        return {
            "symbol": normalized,
            "name": normalized,
            "market": inferred_market,
            "exchange": "TWSE/TPEX" if inferred_market == "TW" else "NASDAQ/NYSE",
            "asset_type": "equity",
            "coverage": "on_demand",
        }

    def search(self, query: str = "", market: Optional[str] = None, limit: int = 25) -> List[Dict[str, Any]]:
        needle = query.strip().lower()
        requested = market.upper() if market else None
        matches = [
            item for item in self._records
            if (not requested or item.market == requested)
            and (not needle or needle in item.symbol.lower() or needle in item.name.lower())
        ]
        return [item.as_dict() for item in matches[:limit]]


def freshness_metadata(adapter: Any) -> Dict[str, Any]:
    return {
        "provider": getattr(adapter, "source_name", "unknown"),
        "provider_mode": getattr(adapter, "last_fetch_mode", "unknown"),
        "provider_error": getattr(adapter, "last_error", None),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "live_quotes": False,
        "broker_connected": False,
    }


def classify_breadth(change_pct: Optional[float]) -> str:
    if change_pct is None or abs(change_pct) < 1e-9:
        return "unchanged"
    return "advance" if change_pct > 0 else "decline"


class CompositeMarketDataAdapter(MarketDataAdapter):
    """Route TW to official/Yahoo, US bars to Yahoo and Nasdaq last sales to CNBC."""

    def __init__(
        self,
        tw_adapter: Optional[MarketDataAdapter] = None,
        us_adapter: Optional[MarketDataAdapter] = None,
        offline_mode: bool = True,
    ):
        from cio_market_lab.data.tw_official import TwOfficialAdapter
        from cio_market_lab.data.cnbc import CnbcNasdaqAdapter

        self.offline_mode = offline_mode
        self.tw_adapter = tw_adapter or TwOfficialAdapter(offline_mode=offline_mode)
        self.us_adapter = us_adapter or CnbcNasdaqAdapter(offline_mode=offline_mode)

    @property
    def source_name(self) -> str:
        return "composite_public"

    @property
    def last_fetch_mode(self) -> str:
        return f"tw:{getattr(self.tw_adapter, 'last_fetch_mode', 'unknown')};us:{getattr(self.us_adapter, 'last_fetch_mode', 'unknown')}"

    @property
    def last_error(self) -> Optional[str]:
        errs = []
        if getattr(self.tw_adapter, "last_error", None):
            errs.append(f"TW: {self.tw_adapter.last_error}")
        if getattr(self.us_adapter, "last_error", None):
            errs.append(f"US: {self.us_adapter.last_error}")
        return "; ".join(errs) if errs else None

    def _select_adapter(self, symbol: str) -> MarketDataAdapter:
        upper = symbol.strip().upper()
        if upper.endswith((".TW", ".TWO")) or (upper.isdigit() and len(upper) >= 4):
            return self.tw_adapter
        return self.us_adapter

    def get_bars(
        self,
        symbol: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        timeframe: str = "1D",
        limit: Optional[int] = None,
    ) -> List[Bar]:
        adapter = self._select_adapter(symbol)
        # Official TW daily OHLC is not an intraday candle. Yahoo supplies
        # actual exchange-timestamped intervals; retain official EOD as an
        # explicitly labelled fallback, never a fabricated 15-minute bar.
        if adapter is self.tw_adapter and self.us_adapter and not self.offline_mode:
            try:
                bars = self.us_adapter.get_bars(symbol, start=start, end=end, timeframe=timeframe, limit=limit)
                if bars:
                    return bars
            except Exception:
                pass
        try:
            bars = adapter.get_bars(symbol, start=start, end=end, timeframe=timeframe, limit=limit)
        except TypeError:
            bars = adapter.get_bars(symbol, start=start, end=end)
            if limit:
                bars = bars[-limit:]
        # Fallback to us_adapter (Yahoo) for TW if tw_adapter returned empty
        if not bars and adapter is self.tw_adapter and self.us_adapter:
            try:
                bars = self.us_adapter.get_bars(symbol, start=start, end=end, timeframe=timeframe, limit=limit)
            except Exception:
                pass
        return bars

    def get_latest_bar(self, symbol: str) -> Optional[Bar]:
        bars = self.get_bars(symbol)
        return bars[-1] if bars else None

    def get_latest_quote(self, symbol: str) -> Optional[Quote]:
        adapter = self._select_adapter(symbol)
        quote = adapter.get_latest_quote(symbol)
        if quote is None and adapter is self.tw_adapter and self.us_adapter:
            quote = self.us_adapter.get_latest_quote(symbol)
        return quote

    def stream_bars(self, symbols: List[str]) -> Iterator[Bar]:
        for s in symbols:
            bar = self.get_latest_bar(s)
            if bar:
                yield bar

    def timeframe_metadata(self, timeframe: str) -> Dict[str, str]:
        if hasattr(self.us_adapter, "timeframe_metadata"):
            return self.us_adapter.timeframe_metadata(timeframe)
        from cio_market_lab.data.yahoo import YahooAdapter
        return YahooAdapter().timeframe_metadata(timeframe)

    def normalize_timeframe(self, timeframe: str) -> str:
        if hasattr(self.us_adapter, "normalize_timeframe"):
            return self.us_adapter.normalize_timeframe(timeframe)
        from cio_market_lab.data.yahoo import YahooAdapter
        return YahooAdapter.normalize_timeframe(timeframe)


