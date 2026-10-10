# All-instrument paper trading + learning system (design v1)

Scope: PAPER ONLY. No live order path; no auto promotion to real money.

## Tiers
- T1 (now): TW/US common stocks, ETFs, REITs, preferreds. Costs: commission, TW securities tax, dividends/ex-rights.
- T1b: leveraged/inverse ETFs. Mandatory daily compounding of the underlying return, never n x total return.
- T2: stock/index futures (margin, daily mark-to-market, margin call, roll), TW warrants (issuer terms, BS repricing, spread penalty).
- T3 (blocked until a reliable data source is proven): options chains, VIX futures, commodity/FX futures. 0DTE/day-trading excluded by mandate (swing only).

## Data
- Unified instrument table: type, market, multiplier, tick size, fee/tax, session, margin rule.
- Immutable daily history + close snapshot, each row with source tier and as_of.
- Derivatives store only point-in-time parameters (no look-ahead).

## Simulation engine
- Signal uses data up to close of T; fill at T+1 open plus volume/spread-based slippage.
- Every trade records thesis, expected outcome, invalidation, result.
- Single-name and leverage caps; leverage products have their own max-drawdown stop.

## Learning loop
- Each decision pre-registers what it bets and how it is wrong; auto-scored at horizon.
- Symmetric ledger: action errors vs inaction (missed) errors.
- Reports split by strategy class; insufficient sample is labelled, not concluded.

## Promotion gate
- >=90 days, multiple regimes, net-of-cost beats benchmark, client approval. Leverage/futures have separate drawdown limits.
