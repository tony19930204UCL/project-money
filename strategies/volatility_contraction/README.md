# Volatility Contraction Pattern (VCP) Strategy

## Overview
Swing strategy modeled after Mark Minervini's Volatility Contraction Pattern.
Identifies contractions in price range across successive waves within a lookback window,
generating swing signals when price moves out of contraction with expanding relative volume.

## Parameters
- `lookback_bars`: Window length for measuring baseline vs recent range.
- `contraction_threshold`: Maximum ratio of recent wave range to prior wave range.
- `rvol_threshold`: Minimum volume expansion multiplier.

## Authority & Scope
- Simulation software only.
- Simulated capital bucket: `swing`.
- Authority: Main CIO only.
