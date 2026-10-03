from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Iterator, List, Optional

from cio_market_lab.domain.models import Bar, Quote


class MarketDataAdapter(ABC):
    """Abstract interface for all market data providers."""

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Name of the data provider source."""
        pass

    @abstractmethod
    def get_bars(
        self,
        symbol: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> List[Bar]:
        """Fetch historical or fixture bars."""
        pass

    @abstractmethod
    def stream_bars(self, symbols: List[str]) -> Iterator[Bar]:
        """Stream or iterate through bars in chronological order."""
        pass

    @abstractmethod
    def get_latest_bar(self, symbol: str) -> Optional[Bar]:
        """Get latest observed bar for a symbol."""
        pass

    @abstractmethod
    def get_latest_quote(self, symbol: str) -> Optional[Quote]:
        """Get latest quote for a symbol."""
        pass
