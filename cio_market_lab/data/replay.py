from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional

from cio_market_lab.data.base import MarketDataAdapter
from cio_market_lab.domain.models import Bar, Quote


def generate_synthetic_bars(
    symbol: str,
    count: int = 60,
    start_price: float = 100.0,
    base_time: Optional[datetime] = None,
    time_step_minutes: int = 15,
    seed: int = 42,
    volatility: float = 0.015,
    inject_stale_indices: Optional[List[int]] = None,
) -> List[Bar]:
    """Deterministically generates realistic OHLCV bars without any external dependencies."""
    if base_time is None:
        base_time = datetime(2026, 9, 23, 9, 0, 0, tzinfo=timezone.utc)
    if inject_stale_indices is None:
        inject_stale_indices = []

    bars: List[Bar] = []
    current_price = start_price

    # Pseudo-random generator using deterministic LCG to guarantee reproducibility across all platforms
    state = seed

    def lcg() -> float:
        nonlocal state
        state = (state * 1664525 + 1013904223) % (2**32)
        return (state / (2**32)) * 2.0 - 1.0  # -1.0 to 1.0

    for i in range(count):
        bar_ts = base_time + timedelta(minutes=i * time_step_minutes)
        # Random walk step
        shock = lcg() * volatility
        open_px = round(current_price, 2)
        close_px = round(max(1.0, open_px * (1.0 + shock)), 2)
        high_px = round(max(open_px, close_px) * (1.0 + abs(lcg()) * 0.005), 2)
        low_px = round(min(open_px, close_px) * (1.0 - abs(lcg()) * 0.005), 2)
        # Volume with occasional surges
        vol_mult = 1.0 + 3.0 * abs(lcg()) if i in [10, 25, 40] else 1.0 + abs(lcg())
        volume = round(10000.0 * vol_mult)

        is_stale = i in inject_stale_indices
        obs_delay = 3600.0 if is_stale else 1.0
        observed_at = bar_ts + timedelta(seconds=obs_delay)

        bar = Bar(
            symbol=symbol,
            timestamp=bar_ts,
            observed_at=observed_at,
            open=open_px,
            high=high_px,
            low=low_px,
            close=close_px,
            volume=volume,
            source="deterministic_fixture",
            delay_seconds=obs_delay,
            quality="good" if not is_stale else "stale",
            is_stale=is_stale,
        )
        bars.append(bar)
        current_price = close_px

    return bars


class ReplayAdapter(MarketDataAdapter):
    """Deterministic fixture and replay data adapter."""

    def __init__(self, bars_by_symbol: Optional[Dict[str, List[Bar]]] = None):
        self._bars: Dict[str, List[Bar]] = bars_by_symbol or {}
        if not self._bars:
            self._load_default_fixtures()

    @property
    def source_name(self) -> str:
        return "deterministic_replay"

    def _load_default_fixtures(self) -> None:
        """Preload standard deterministic fixtures for TW and US markets."""
        base_t = datetime(2026, 9, 23, 9, 0, 0, tzinfo=timezone.utc)
        self._bars["2330.TW"] = generate_synthetic_bars(
            symbol="2330.TW", count=60, start_price=950.0, base_time=base_t, seed=101
        )
        self._bars["2454.TW"] = generate_synthetic_bars(
            symbol="2454.TW", count=60, start_price=1200.0, base_time=base_t, seed=202
        )
        self._bars["AAPL"] = generate_synthetic_bars(
            symbol="AAPL", count=60, start_price=220.0, base_time=base_t, seed=303
        )
        self._bars["NVDA"] = generate_synthetic_bars(
            symbol="NVDA", count=60, start_price=125.0, base_time=base_t, seed=404
        )

    def add_bars(self, symbol: str, bars: List[Bar]) -> None:
        self._bars[symbol] = sorted(bars, key=lambda b: b.timestamp)

    def get_bars(
        self,
        symbol: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> List[Bar]:
        bars = self._bars.get(symbol, [])
        if start:
            bars = [b for b in bars if b.timestamp >= start]
        if end:
            bars = [b for b in bars if b.timestamp <= end]
        return bars

    def stream_bars(self, symbols: List[str]) -> Iterator[Bar]:
        all_bars: List[Bar] = []
        for s in symbols:
            all_bars.extend(self._bars.get(s, []))
        all_bars.sort(key=lambda b: (b.timestamp, b.symbol))
        for bar in all_bars:
            yield bar

    def get_latest_bar(self, symbol: str) -> Optional[Bar]:
        bars = self._bars.get(symbol, [])
        return bars[-1] if bars else None

    def get_latest_quote(self, symbol: str) -> Optional[Quote]:
        bar = self.get_latest_bar(symbol)
        if not bar:
            return None
        spread = round(bar.close * 0.001, 2)
        return Quote(
            symbol=symbol,
            timestamp=bar.timestamp,
            observed_at=bar.observed_at,
            bid=round(bar.close - spread / 2, 2),
            ask=round(bar.close + spread / 2, 2),
            bid_size=100.0,
            ask_size=100.0,
            last_price=bar.close,
            last_size=50.0,
            source=self.source_name,
            delay_seconds=bar.delay_seconds,
            is_stale=bar.is_stale,
        )
