from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from cio_market_lab.domain.models import Bar, Position, Signal


class StrategyContext(BaseModel):
    """Context provided to a strategy during execution."""

    strategy_id: str
    config: Dict[str, Any] = Field(default_factory=dict)
    bars_history: Dict[str, List[Bar]] = Field(default_factory=dict)
    current_positions: Dict[str, Position] = Field(default_factory=dict)
    custom_state: Dict[str, Any] = Field(default_factory=dict)


class BaseStrategy(ABC):
    """Base interface for all strategy plugins."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}

    @abstractmethod
    def describe(self) -> Dict[str, Any]:
        """Returns metadata and parameter specifications."""
        pass

    @abstractmethod
    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        """Validates configuration parameters. Returns list of error messages, or empty list if valid."""
        pass

    @abstractmethod
    def on_bar(self, context: StrategyContext, bar: Bar) -> List[Signal]:
        """Pure signal generation on each incoming bar."""
        pass

    def on_event(self, context: StrategyContext, event: Any) -> List[Signal]:
        """Optional hook for material market/corporate events."""
        return []

    def on_session_close(self, context: StrategyContext) -> List[Signal]:
        """Optional hook for end-of-session logic."""
        return []
