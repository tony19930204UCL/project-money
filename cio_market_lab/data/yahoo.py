from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
from time import monotonic
from typing import Dict, Iterator, List, Optional

from cio_market_lab.data.base import MarketDataAdapter
from cio_market_lab.data.replay import generate_synthetic_bars
from cio_market_lab.domain.models import Bar, Quote


_TIMEFRAME_CONFIG = {
    # Chart ranges are deliberately different in both lookback and granularity.
    "1D": {"period": "1d", "interval": "15m", "count": 26, "step_minutes": 15},
    "1W": {"period": "7d", "interval": "1h", "count": 56, "step_minutes": 60},
    "1M": {"period": "1mo", "interval": "1d", "count": 31, "step_minutes": 1440},
}


def history_interval_for_range(
    start_date: date,
    end_date: date,
    *,
    today: Optional[date] = None,
) -> str:
    """Pick a Yahoo interval without exceeding intraday retention limits."""
    today = today or date.today()
    span_days = max(1, (end_date - start_date).days + 1)
    oldest_age_days = max(0, (today - start_date).days)
    if span_days <= 7 and oldest_age_days <= 7:
        return "1m"
    if span_days <= 60 and oldest_age_days < 60:
        return "5m"
    if span_days <= 120 and oldest_age_days < 60:
        return "15m"
    if span_days <= 730:
        return "60m"
    return "1d"


class YahooAdapter(MarketDataAdapter):
    """Yahoo delayed adapter with deterministic, explicitly non-live fallback."""

    def __init__(self, offline_mode: bool = True):
        self.offline_mode = offline_mode
        self._cached_bars: Dict[tuple[str, str], List[Bar]] = {}
        self._live_bars: Dict[tuple[str, str], tuple[float, List[Bar]]] = {}
        self.last_fetch_mode: str = "offline_fixture" if offline_mode else "live_pending"
        self.last_error: Optional[str] = None

    @property
    def source_name(self) -> str:
        return "yahoo_delayed"

    @staticmethod
    def normalize_timeframe(timeframe: str) -> str:
        value = timeframe.strip().upper()
        if value not in _TIMEFRAME_CONFIG:
            raise ValueError("timeframe must be one of: 1D, 1W, 1M")
        return value

    def timeframe_metadata(self, timeframe: str) -> Dict[str, str]:
        key = self.normalize_timeframe(timeframe)
        cfg = _TIMEFRAME_CONFIG[key]
        return {"timeframe": key, "interval": cfg["interval"], "period": cfg["period"]}

    def _ensure_bars(self, symbol: str, timeframe: str) -> List[Bar]:
        key = (symbol, timeframe)
        if key not in self._cached_bars:
            cfg = _TIMEFRAME_CONFIG[timeframe]
            # Stable hash avoids Python's process-randomized hash() values.
            seed = int(hashlib.sha256(f"{symbol}:{timeframe}".encode()).hexdigest()[:8], 16)
            base_t = datetime(2026, 9, 23, 13, 30, tzinfo=timezone.utc) - timedelta(
                minutes=(cfg["count"] - 1) * cfg["step_minutes"]
            )
            bars = generate_synthetic_bars(
                symbol=symbol,
                count=cfg["count"],
                start_price=150.0,
                base_time=base_t,
                time_step_minutes=cfg["step_minutes"],
                seed=seed,
            )
            for bar in bars:
                bar.source = "deterministic_yahoo_fallback"
                bar.delay_seconds = 900.0
                bar.quality = "synthetic_fixture"
                bar.is_stale = True
            self._cached_bars[key] = bars
        return self._cached_bars[key]

    @staticmethod
    def _to_bar(symbol: str, ts, row, observed_at: datetime) -> Bar:
        bar_time = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if bar_time.tzinfo is None:
            bar_time = bar_time.replace(tzinfo=timezone.utc)
        else:
            bar_time = bar_time.astimezone(timezone.utc)
        age_seconds = max(0.0, (observed_at - bar_time).total_seconds())
        return Bar(
            symbol=symbol,
            timestamp=bar_time,
            observed_at=observed_at,
            open=float(row["Open"]),
            high=float(row["High"]),
            low=float(row["Low"]),
            close=float(row["Close"]),
            volume=float(row["Volume"]),
            source="yahoo_delayed",
            delay_seconds=age_seconds,
            quality="delayed",
            is_stale=age_seconds > 172800.0,
        )

    def get_bars(
        self,
        symbol: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        timeframe: str = "1D",
        limit: Optional[int] = None,
    ) -> List[Bar]:
        key = self.normalize_timeframe(timeframe)
        bars: List[Bar] = []
        if not self.offline_mode:
            cached = self._live_bars.get((symbol, key))
            if cached and cached[0] > monotonic():
                bars = list(cached[1])
                self.last_fetch_mode = "cached_yahoo_delayed" if bars else "cached_unavailable"
                self.last_error = None if bars else "Yahoo temporarily unavailable; retry after cache expiry"
                return self._filter_bars(bars, start, end, limit)
            import os
            if os.getenv('CIO_PUBLIC_YAHOO_CHART', '0') == '1':
                # One bounded transport avoids yfinance cookie/crumb retries
                # exhausting the enclosing eight-second process deadline.
                try:
                    from cio_market_lab.data.yahoo_public_chart import fetch_chart
                    cfg = _TIMEFRAME_CONFIG[key]
                    bars = fetch_chart(symbol, cfg['period'], cfg['interval'])
                    self._live_bars[(symbol, key)] = (monotonic() + (15 if bars else 3), list(bars))
                    self.last_fetch_mode = 'live_yahoo_public_chart' if bars else 'live_unavailable'
                    self.last_error = None if bars else 'Public chart returned no usable bars'
                    return self._filter_bars(bars, start, end, limit)
                except Exception as exc:
                    self.last_fetch_mode = 'live_unavailable'
                    self.last_error = str(exc)
                    return []
            try:
                import yfinance as yf
                cfg = _TIMEFRAME_CONFIG[key]
                df = yf.Ticker(symbol).history(period=cfg["period"], interval=cfg["interval"], auto_adjust=False, timeout=4)
                # Yahoo occasionally returns an in-progress row with a missing
                # close.  A null close is not a tradable bar and previously
                # rendered as 0.00 in the chart desk.  Drop incomplete OHLC
                # rows before they enter the domain model.
                if not df.empty:
                    df = df.dropna(subset=["Open", "High", "Low", "Close"])
                if not df.empty:
                    observed_at = datetime.now(timezone.utc)
                    bars = [self._to_bar(symbol, ts, row, observed_at) for ts, row in df.iterrows()]
                    self.last_fetch_mode = "live_yahoo_delayed"
                    self.last_error = None
            except Exception as exc:
                self.last_fetch_mode = "live_unavailable"
                self.last_error = str(exc)
            self._live_bars[(symbol, key)] = (monotonic() + (15 if bars else 3), list(bars))
        if not bars:
            if not self.offline_mode:
                # Fail closed: deterministic fixtures are valid in explicit
                # offline mode only and must never masquerade as live prices.
                self.last_fetch_mode = "live_unavailable"
                return []
            bars = list(self._ensure_bars(symbol, key))
        return self._filter_bars(bars, start, end, limit)

    @staticmethod
    def _filter_bars(bars: List[Bar], start: Optional[datetime], end: Optional[datetime], limit: Optional[int]) -> List[Bar]:
        if start:
            bars = [bar for bar in bars if bar.timestamp >= start]
        if end:
            bars = [bar for bar in bars if bar.timestamp <= end]
        return bars[-limit:] if limit is not None else bars

    def get_history_range(
        self,
        symbol: str,
        start: Optional[str],
        end: Optional[str],
        limit: int = 5000,
    ) -> List[Bar]:
        """Fetch a Yahoo-supported interval for the requested chart window."""
        if self.offline_mode:
            return self.get_bars(symbol, timeframe="1D", limit=limit)
        start_date = date.fromisoformat(start[:10]) if start else date.today() - timedelta(days=7)
        end_date = date.fromisoformat(end[:10]) if end else date.today()
        interval = history_interval_for_range(start_date, end_date)
        try:
            import yfinance as yf

            frame = yf.Ticker(symbol).history(
                start=start_date.isoformat(),
                end=(end_date + timedelta(days=1)).isoformat(),
                interval=interval,
                auto_adjust=False,
                timeout=4,
            )
            if frame is None or frame.empty:
                self.last_fetch_mode = "live_unavailable"
                self.last_error = f"Yahoo returned no {interval} rows for {symbol}"
                return []
            frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
            observed_at = datetime.now(timezone.utc)
            bars = [self._to_bar(symbol, ts, row, observed_at) for ts, row in frame.iterrows()]
            self.last_fetch_mode = f"live_yahoo_{interval}_delayed"
            self.last_error = None
            return bars[-limit:]
        except Exception as exc:
            self.last_fetch_mode = "live_unavailable"
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []

    def stream_bars(self, symbols: List[str], timeframe: str = "1D") -> Iterator[Bar]:
        all_bars: List[Bar] = []
        for symbol in symbols:
            all_bars.extend(self.get_bars(symbol, timeframe=timeframe))
        all_bars.sort(key=lambda bar: (bar.timestamp, bar.symbol))
        yield from all_bars

    def get_latest_bar(self, symbol: str, timeframe: str = "1D") -> Optional[Bar]:
        bars = self.get_bars(symbol, timeframe=timeframe)
        return bars[-1] if bars else None

    def get_latest_quote(self, symbol: str) -> Optional[Quote]:
        bar = self.get_latest_bar(symbol)
        if not bar:
            return None
        # Defect 2 fix: Never fabricate bid/ask or artificial book depth from daily bars
        is_syn = (
            "synthetic" in (bar.quality or "").lower()
            or "synthetic" in (bar.source or "").lower()
            or "fallback" in (bar.source or "").lower()
        )
        return Quote(
            symbol=symbol,
            timestamp=bar.timestamp,
            observed_at=bar.observed_at,
            bid=None,
            ask=None,
            bid_size=0.0,
            ask_size=0.0,
            last_price=bar.close,
            last_size=0.0,
            source=bar.source,
            delay_seconds=bar.delay_seconds,
            is_stale=bar.is_stale,
            session="REGULAR",
            regular_price=bar.close,
            extended_price=None,
            quality=f"{bar.quality}_chart_close_proxy",
            is_synthetic=is_syn,
        )

