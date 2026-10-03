from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, List

from cio_market_lab.domain.models import Bar, DecisionScope, Market, OrderSide, Signal
from cio_market_lab.strategies.base import BaseStrategy, StrategyContext


class OpeningRangeBreakoutStrategy(BaseStrategy):
    """Opening Range Breakout with Relative Volume (RVOL) Confirmation."""

    def describe(self) -> Dict[str, Any]:
        return {
            "name": "Opening Range Breakout",
            "version": "1.0.0",
            "description": "Calculates opening range high/low for initial bars and signals breakout when volume confirms.",
            "parameters": {
                "range_bars": "Number of initial bars defining opening range (int >= 1)",
                "rvol_threshold": "Relative volume multiplier required for confirmation (float > 0.0)",
                "risk_per_trade_pct": "Simulated risk allocation per trade",
                "max_shares": "Maximum order size cap",
            },
        }

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        errors = []
        if "range_bars" in config and config["range_bars"] < 1:
            errors.append("range_bars must be at least 1")
        if "rvol_threshold" in config and config["rvol_threshold"] <= 0.0:
            errors.append("rvol_threshold must be positive")
        return errors

    def _get_config_hash(self) -> str:
        s = json.dumps(self.config, sort_keys=True)
        return hashlib.sha256(s.encode()).hexdigest()[:12]

    def on_bar(self, context: StrategyContext, bar: Bar) -> List[Signal]:
        signals: List[Signal] = []
        sym = bar.symbol
        history = context.bars_history.get(sym, [])

        range_bars = int(self.config.get("range_bars", 4))
        rvol_thresh = float(self.config.get("rvol_threshold", 1.2))

        # We need at least range_bars + 1 to evaluate breakout
        if len(history) < range_bars:
            return signals

        # Opening range formed by the first `range_bars`
        opening_bars = history[:range_bars]
        range_high = max(b.high for b in opening_bars)
        range_low = min(b.low for b in opening_bars)
        avg_vol = sum(b.volume for b in opening_bars) / len(opening_bars)

        market = Market.TW if sym.endswith(".TW") else Market.US
        pos = context.current_positions.get(sym)
        has_pos = pos is not None and pos.quantity > 0

        # Breakout entry condition
        if not has_pos and bar.close > range_high:
            current_rvol = bar.volume / avg_vol if avg_vol > 0 else 1.0
            if current_rvol >= rvol_thresh:
                sig = Signal(
                    signal_id=str(uuid.uuid4()),
                    strategy_id="opening_range_breakout",
                    version="1.0.0",
                    config_hash=self._get_config_hash(),
                    symbol=sym,
                    market=market,
                    decision_scope=DecisionScope.INTRADAY,
                    side=OrderSide.BUY,
                    exchange_ts=bar.timestamp,
                    reason_codes=["ORB_BREAKOUT_HIGH", "RVOL_CONFIRMED"],
                    evidence={
                        "range_high": range_high,
                        "range_low": range_low,
                        "close": bar.close,
                        "volume": bar.volume,
                        "avg_volume": avg_vol,
                        "rvol": round(current_rvol, 2),
                    },
                    entry_model="NEXT_OPEN",
                    invalidation={"stop_loss": range_low},
                    max_simulated_risk=round(bar.close - range_low, 2),
                )
                signals.append(sig)

        # Exit/invalidation condition
        elif has_pos and bar.close < range_low:
            sig = Signal(
                signal_id=str(uuid.uuid4()),
                strategy_id="opening_range_breakout",
                version="1.0.0",
                config_hash=self._get_config_hash(),
                symbol=sym,
                market=market,
                decision_scope=DecisionScope.INTRADAY,
                side=OrderSide.SELL,
                exchange_ts=bar.timestamp,
                reason_codes=["ORB_STOP_INVALIDATION"],
                evidence={"range_low": range_low, "close": bar.close},
                entry_model="MARKET",
                invalidation={},
                max_simulated_risk=0.0,
            )
            signals.append(sig)

        return signals
