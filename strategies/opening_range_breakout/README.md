# Opening Range Breakout (ORB) Strategy

## Overview
An intraday breakout strategy tracking the initial `range_bars` of the session.
When the price breaks above the opening range high with relative volume exceeding `rvol_threshold`, an intraday long signal is produced.

## Risk & Execution
- Decision Scope: `intraday`
- Simulated capital bucket: `intraday`
- Stop Loss: Opening range low
- Watermark: SIMULATION ONLY — Authority Main CIO
