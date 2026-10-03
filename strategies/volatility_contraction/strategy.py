from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, List

from cio_market_lab.domain.models import Bar, DecisionScope, Market, OrderSide, Signal
from cio_market_lab.strategies.base import BaseStrategy, StrategyContext


class VolatilityContractionStrategy(BaseStrategy):
    """Volatility Contraction Pattern (VCP) with Relative Volume Confirmation."""

    def describe(self) -> Dict[str, Any]:
        return {
            "name": "Volatility Contraction Pattern",
            "version": "1.0.0",
            "description": "Identifies volatility contraction within a lookback window and triggers swing entries upon volume expansion.",
            "parameters": {
                "lookback_bars": "Total bars examined for contraction baseline (int >= 5)",
                "contraction_threshold": "Ratio of recent volatility to prior volatility (float < 1.0)",
                "rvol_threshold": "Relative volume expansion factor",
                "risk_per_trade_pct": "Capital risk per swing setup",
            },
        }

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        errors = []
        if "lookback_bars" in config and config["lookback_bars"] < 5:
            errors.append("lookback_bars must be at least 5")
        if "contraction_threshold" in config and config["contraction_threshold"] <= 0.0:
            errors.append("contraction_threshold must be positive")
        return errors

    def _get_config_hash(self) -> str:
        s = json.dumps(self.config, sort_keys=True)
        return hashlib.sha256(s.encode()).hexdigest()[:12]

    def on_bar(self, context: StrategyContext, bar: Bar) -> List[Signal]:
        signals: List[Signal] = []
        sym = bar.symbol
        history = context.bars_history.get(sym, [])

        lookback = int(self.config.get("lookback_bars", 10))
        contraction_thresh = float(self.config.get("contraction_threshold", 0.7))
        rvol_thresh = float(self.config.get("rvol_threshold", 1.2))

        if len(history) < lookback:
            return signals

        recent_window = history[-lookback:]
        half = lookback // 2
        prior_bars = recent_window[:half]
        latest_bars = recent_window[half:]

        prior_range = max(b.high for b in prior_bars) - min(b.low for b in prior_bars)
        recent_range = max(b.high for b in latest_bars) - min(b.low for b in latest_bars)

        if prior_range <= 0:
            return signals

        contraction_ratio = recent_range / prior_range
        avg_vol = sum(b.volume for b in recent_window) / len(recent_window)
        rvol = bar.volume / avg_vol if avg_vol > 0 else 1.0

        market = Market.TW if sym.endswith(".TW") else Market.US
        pos = context.current_positions.get(sym)
        has_pos = pos is not None and pos.quantity > 0

        # Breakout after contraction
        recent_high = max(b.high for b in latest_bars)
        recent_low = min(b.low for b in latest_bars)

        if not has_pos and contraction_ratio <= contraction_thresh and bar.close >= recent_high:
            if rvol >= rvol_thresh:
                sig = Signal(
                    signal_id=str(uuid.uuid4()),
                    strategy_id="volatility_contraction",
                    version="1.0.0",
                    config_hash=self._get_config_hash(),
                    symbol=sym,
                    market=market,
                    decision_scope=DecisionScope.SWING,
                    side=OrderSide.BUY,
                    exchange_ts=bar.timestamp,
                    reason_codes=["VCP_CONTRACTION_MET", "VCP_VOLUME_EXPANSION"],
                    evidence={
                        "prior_range": round(prior_range, 2),
                        "recent_range": round(recent_range, 2),
                        "contraction_ratio": round(contraction_ratio, 3),
                        "rvol": round(rvol, 2),
                        "recent_high": recent_high,
                        "recent_low": recent_low,
                    },
                    entry_model="NEXT_OPEN",
                    invalidation={"stop_loss": recent_low},
                    max_simulated_risk=round(bar.close - recent_low, 2),
                )
                signals.append(sig)

        elif has_pos and bar.close < recent_low:
            sig = Signal(
                signal_id=str(uuid.uuid4()),
                strategy_id="volatility_contraction",
                version="1.0.0",
                config_hash=self._get_config_hash(),
                symbol=sym,
                market=market,
                decision_scope=DecisionScope.SWING,
                side=OrderSide.SELL,
                exchange_ts=bar.timestamp,
                reason_codes=["VCP_STOP_INVALIDATION"],
                evidence={"recent_low": recent_low, "close": bar.close},
                entry_model="MARKET",
                invalidation={},
                max_simulated_risk=0.0,
            )
            signals.append(sig)

        return signals
