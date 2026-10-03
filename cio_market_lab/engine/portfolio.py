from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set
from cio_market_lab.domain.models import (
    Bar,
    DecisionScope,
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    PaperPortfolio,
    Position,
)


def is_slippage_embedded(fill: Fill) -> bool:
    """Determine whether slippage is already embedded in fill.fill_price based on assumptions / convention.

    Specification:
    - cash = executed-price notional plus explicit fees/taxes only when slippage is already embedded;
      retain slippage as informational execution cost without double debit.
    - Legacy persisted fills default to False unless marked with embedded slippage or
      slippage_adjusted timing assumption.
    """
    assumptions = getattr(fill, "assumptions", {}) or {}
    if "slippage_embedded" in assumptions:
        return bool(assumptions["slippage_embedded"])
    timing = str(assumptions.get("timing_assumption", ""))
    if "slippage_adjusted" in timing:
        return True
    return False


class CanonicalCashAccount:
    """Canonical TWD cash account shared across holding horizons."""

    def __init__(self, initial_cash: float = 2_378_465.0, currency: str = "TWD") -> None:
        self.currency = currency.upper()
        if self.currency not in {"USD", "TWD"}:
            raise ValueError("UNSUPPORTED_ACCOUNT_CURRENCY")
        self.initial_cash = float(initial_cash)
        self.cash = float(initial_cash)
        self._applied_fill_ids: Set[str] = set()

    def debit(self, amount: float) -> None:
        self.cash -= float(amount)

    def credit(self, amount: float) -> None:
        self.cash += float(amount)

    def apply_fill(self, fill: Fill) -> bool:
        """Apply fill cash flow idempotently. Returns True if applied, False if already applied."""
        if fill.currency != self.currency:
            raise ValueError("CURRENCY_MISMATCH: fill and cash account differ; evidenced conversion required")
        if fill.fill_id in self._applied_fill_ids:
            return False
        mult = getattr(fill, "multiplier", 1.0) or 1.0
        cost_basis = fill.quantity * fill.fill_price * mult
        explicit_costs = fill.fee + fill.tax
        cash_costs = explicit_costs if is_slippage_embedded(fill) else (explicit_costs + fill.slippage)

        if fill.side == OrderSide.BUY:
            self.cash -= (cost_basis + cash_costs)
        elif fill.side == OrderSide.SELL:
            self.cash += (cost_basis - cash_costs)

        self._applied_fill_ids.add(fill.fill_id)
        return True


class Ledger:
    """Paper portfolio ledger for a holding horizon (e.g. SWING or INTRADAY)."""

    def __init__(
        self,
        bucket: DecisionScope,
        initial_cash: float = 1_000_000.0,
        cash_account: Optional[CanonicalCashAccount] = None,
        currency: str = "TWD",
    ) -> None:
        self.bucket = bucket
        self.currency = currency.upper()
        if self.currency not in {"USD", "TWD"}:
            raise ValueError("UNSUPPORTED_ACCOUNT_CURRENCY")
        if cash_account is not None and cash_account.currency != self.currency:
            raise ValueError("CURRENCY_MISMATCH: ledger and shared cash account differ")
        self._cash_account = cash_account
        self._initial_cash = float(initial_cash)
        self._cash = float(initial_cash)
        self.equity = float(initial_cash)
        self.realized_pnl = 0.0
        self.unrealized_pnl = 0.0
        self.positions: Dict[str, Position] = {}
        self.orders: List[Order] = []
        self.fills: List[Fill] = []
        self._latest_prices: Dict[str, float] = {}

    @property
    def cash(self) -> float:
        if self._cash_account is not None:
            return self._cash_account.cash
        return self._cash

    @cash.setter
    def cash(self, value: float) -> None:
        if self._cash_account is not None:
            self._cash_account.cash = float(value)
        else:
            self._cash = float(value)

    @property
    def initial_cash(self) -> float:
        if self._cash_account is not None:
            return self._cash_account.initial_cash
        return self._initial_cash

    @initial_cash.setter
    def initial_cash(self, value: float) -> None:
        if self._cash_account is not None:
            self._cash_account.initial_cash = float(value)
        else:
            self._initial_cash = float(value)

    def can_afford(self, symbol: str, price: float, quantity: float, cost_estimate: float = 0.0, multiplier: float = 1.0) -> bool:
        required = (price * quantity * multiplier) + cost_estimate
        return self.cash >= required

    def get_position(self, symbol: str) -> Position:
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol=symbol, bucket=self.bucket, currency=self.currency)
        return self.positions[symbol]

    def add_order(self, order: Order) -> None:
        self.orders.append(order)

    def record_rejection(self, order_id: str, reason: str) -> None:
        for o in self.orders:
            if o.order_id == order_id:
                o.status = OrderStatus.REJECTED
                o.rejection_reason = reason
                break

    def apply_fill(self, fill: Fill) -> bool:
        if fill.currency != self.currency:
            raise ValueError("CURRENCY_MISMATCH: fill and ledger differ; evidenced conversion required")
        if self._cash_account is not None and self._cash_account.currency != fill.currency:
            raise ValueError("CURRENCY_MISMATCH: fill and shared cash account differ")
        if any(f.fill_id == fill.fill_id for f in self.fills):
            return False

        # Partial receipts must not close the original order. Validate the
        # cumulative quantity before touching fills, cash or positions.
        import math
        if not math.isfinite(fill.quantity) or fill.quantity <= 0:
            raise ValueError("INVALID_FILL_QUANTITY")
        matched_order = next((o for o in self.orders if o.order_id == fill.order_id), None)
        cumulative_quantity = sum(f.quantity for f in self.fills if f.order_id == fill.order_id) + fill.quantity
        if matched_order is not None:
            # A corporate-action cancellation terminates the unfilled remainder
            # in pre-action share terms. A late distinct receipt must not revive
            # it after a split/restart. Duplicates remain idempotent above.
            if matched_order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                raise ValueError("FILL_FOR_TERMINAL_ORDER")
            if (not math.isfinite(matched_order.quantity) or matched_order.quantity <= 0
                    or cumulative_quantity > matched_order.quantity + 1e-9):
                raise ValueError("FILL_EXCEEDS_ORDER_QUANTITY")

        self.fills.append(fill)

        if matched_order is not None:
            matched_order.audit_metadata["filled_quantity"] = cumulative_quantity
            matched_order.status = (OrderStatus.FILLED
                if math.isclose(cumulative_quantity, matched_order.quantity, rel_tol=0, abs_tol=1e-9)
                else OrderStatus.PARTIALLY_FILLED)

        sym = fill.symbol
        pos = self.get_position(sym)
        mult = getattr(fill, "multiplier", 1.0) or 1.0
        cost_basis = fill.quantity * fill.fill_price * mult
        slippage_embedded = is_slippage_embedded(fill)
        explicit_costs = fill.fee + fill.tax
        cash_costs = explicit_costs if slippage_embedded else (explicit_costs + fill.slippage)

        # Synchronize contract attributes
        if getattr(fill, "instrument_type", None):
            pos.instrument_type = fill.instrument_type
        if getattr(fill, "underlying_symbol", None):
            pos.underlying_symbol = fill.underlying_symbol
        if getattr(fill, "option_right", None):
            pos.option_right = fill.option_right
        if getattr(fill, "strike", None) is not None:
            pos.strike = fill.strike
        if getattr(fill, "expiry", None) is not None:
            pos.expiry = fill.expiry
        pos.multiplier = mult
        if getattr(fill, "provenance", None):
            pos.provenance = fill.provenance

        if self._cash_account is not None:
            self._cash_account.apply_fill(fill)
        else:
            if fill.side == OrderSide.BUY:
                self.cash -= (cost_basis + cash_costs)
            elif fill.side == OrderSide.SELL:
                self.cash += (cost_basis - cash_costs)

        if fill.side == OrderSide.BUY:
            cash_debit = cost_basis + cash_costs
            fill.cash_flow = -cash_debit
            new_qty = pos.quantity + fill.quantity
            if new_qty > 0:
                pos.average_entry_price = (
                    (pos.average_entry_price * pos.quantity * mult) + cost_basis
                ) / (new_qty * mult)
            pos.quantity = new_qty
        elif fill.side == OrderSide.SELL:
            cash_credit = cost_basis - cash_costs
            fill.cash_flow = cash_credit
            costs_to_deduct = explicit_costs if slippage_embedded else (explicit_costs + fill.slippage)
            realized = (
                fill.fill_price - pos.average_entry_price
            ) * fill.quantity * mult - costs_to_deduct
            pos.realized_pnl += realized
            self.realized_pnl += realized
            pos.quantity -= fill.quantity
            if pos.quantity <= 0:
                pos.quantity = 0.0
                pos.average_entry_price = 0.0

        if sym not in self._latest_prices:
            self._latest_prices[sym] = fill.fill_price
        curr_px = self._latest_prices.get(sym)
        if curr_px is not None:
            pos.current_price = curr_px
            pos.market_value = pos.quantity * curr_px * mult
            pos.unrealized_pnl = pos.market_value - (pos.quantity * pos.average_entry_price * mult)
        else:
            pos.current_price = None
            pos.market_value = None
            pos.unrealized_pnl = None
        self.positions[sym] = pos

        self._recalculate_equity()
        return True

    def update_mark_to_market(self, item: Any) -> None:
        sym = getattr(item, "symbol", None)
        if not sym:
            return
        price = getattr(item, "close", None)
        if price is None:
            price = getattr(item, "last_price", None)
        if price is None:
            return
        self._latest_prices[sym] = price

        for s, pos in self.positions.items():
            mult = getattr(pos, "multiplier", 1.0) or 1.0
            if s == sym:
                if pos.instrument_type in {"OPTION", "FUTURE"}:
                    # Spot bars cannot mark a specific contract; only executable
                    # per-contract bid/ask via the derivative lifecycle may do so.
                    continue
                pos.current_price = price
                pos.market_value = pos.quantity * pos.current_price * mult
                pos.unrealized_pnl = pos.market_value - (pos.quantity * pos.average_entry_price * mult)


        self._recalculate_equity()

    def settle_expired_options(self, current_time: datetime, underlying_prices: Dict[str, float]) -> List[Dict[str, Any]]:
        """Report overdue contracts without inventing cash/physical settlement."""
        unresolved: List[Dict[str, Any]] = []
        for sym, pos in list(self.positions.items()):
            if pos.quantity <= 0 or pos.instrument_type != "OPTION" or pos.expiry is None:
                continue
            exp = pos.expiry if pos.expiry.tzinfo else pos.expiry.replace(tzinfo=timezone.utc)
            now = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
            if now < exp:
                continue

            unresolved.append({"symbol": sym, "expiry": exp.isoformat(),
                               "quantity": pos.quantity,
                               "status": "UNRESOLVED_EXPIRY_DELIVERY_UNSUPPORTED",
                               "cash_settlement": False})
        return unresolved


    def _recalculate_equity(self) -> None:
        has_missing_mark = any(
            pos.quantity != 0 and (pos.current_price is None or pos.market_value is None)
            for pos in self.positions.values()
        )
        if has_missing_mark:
            self.unrealized_pnl = None
            self.equity = None
        else:
            self.unrealized_pnl = sum((pos.unrealized_pnl or 0.0) for pos in self.positions.values())
            total_market_value = sum((pos.market_value or 0.0) for pos in self.positions.values())
            self.equity = self.cash + total_market_value

    def get_portfolio(self) -> PaperPortfolio:
        return PaperPortfolio(
            currency=self.currency,
            bucket=self.bucket,
            cash=self.cash,
            initial_cash=self.initial_cash,
            equity=self.equity,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=self.unrealized_pnl,
            nav_status="NAV_UNAVAILABLE" if self.equity is None else "OK",
            positions={k: v.model_copy() for k, v in self.positions.items()},
            orders=[o.model_copy() for o in self.orders],
            fills=[f.model_copy() for f in self.fills],
        )


class PortfolioManager:
    """Manages aggregate ledgers plus isolated strategy ledgers.

    Aggregate SWING/INTRADAY ledgers remain the compatibility surface for the
    existing API. Strategy ledgers are separate virtual accounts and mirror
    strategy fills into the aggregate view.
    """

    def __init__(
        self,
        initial_cash_swing: float = 1_000_000.0,
        initial_cash_intraday: float = 1_000_000.0,
        currency: str = "TWD",
    ):
        self._ledgers: Dict[DecisionScope, Ledger] = {
            DecisionScope.SWING: Ledger(DecisionScope.SWING, initial_cash_swing, currency=currency),
            DecisionScope.INTRADAY: Ledger(DecisionScope.INTRADAY, initial_cash_intraday, currency=currency),
        }
        self._strategy_ledgers: Dict[str, Dict[DecisionScope, Ledger]] = {}
        self._strategy_cash_accounts: Dict[str, CanonicalCashAccount] = {}
        self._strategy_initial_cash = {
            DecisionScope.SWING: initial_cash_swing,
            DecisionScope.INTRADAY: initial_cash_intraday,
        }
        self._strategy_capital: Dict[str, Dict[DecisionScope, float]] = {}

    def register_strategy(
        self,
        strategy_id: str,
        initial_cash: float,
        bucket_capital_allocations: Optional[Dict[str, float]] = None,
        unified_cash: bool = True,
        currency: str = "TWD",
    ) -> None:
        existing = self._strategy_ledgers.get(strategy_id, {})
        if any(ledger.currency != currency for ledger in existing.values()):
            raise ValueError("CURRENCY_MISMATCH: cannot change an existing strategy currency")
        allocations = bucket_capital_allocations or {}
        swing_cash = float(allocations.get(DecisionScope.SWING.value, 0.0))
        intraday_cash = float(allocations.get(DecisionScope.INTRADAY.value, 0.0))
        if swing_cash < 0 or intraday_cash < 0:
            raise ValueError("bucket capital allocations must be non-negative")
        allocated_total = swing_cash + intraday_cash
        if allocated_total == 0:
            swing_cash = float(initial_cash)
        elif abs(allocated_total - float(initial_cash)) > 0.01:
            raise ValueError("bucket capital allocations must sum to initial_cash")
        self._strategy_capital[strategy_id] = {
            DecisionScope.SWING: swing_cash,
            DecisionScope.INTRADAY: intraday_cash,
        }

        # Canonical single TWD cash ledger: holding horizon (SWING vs INTRADAY) is metadata, not fixed cash compartments
        if unified_cash:
            ledgers = self._strategy_ledgers.setdefault(strategy_id, {})
            if strategy_id in self._strategy_cash_accounts:
                cash_acct = self._strategy_cash_accounts[strategy_id]
                for b in (DecisionScope.SWING, DecisionScope.INTRADAY):
                    if b not in ledgers:
                        ledgers[b] = Ledger(b, float(initial_cash), cash_account=cash_acct, currency=currency)
                    else:
                        ledgers[b]._cash_account = cash_acct
            else:
                cash_acct = CanonicalCashAccount(initial_cash=float(initial_cash), currency=currency)
                self._strategy_cash_accounts[strategy_id] = cash_acct
                ledgers[DecisionScope.SWING] = Ledger(DecisionScope.SWING, float(initial_cash), cash_account=cash_acct, currency=currency)
                ledgers[DecisionScope.INTRADAY] = Ledger(DecisionScope.INTRADAY, float(initial_cash), cash_account=cash_acct, currency=currency)

    def get_ledger(self, bucket: DecisionScope, strategy_id: Optional[str] = None) -> Ledger:
        if strategy_id is None:
            return self._ledgers[bucket]
        return self.get_strategy_ledger(strategy_id, bucket)

    def get_strategy_ledger(
        self, strategy_id: str, bucket: DecisionScope, initial_cash: Optional[float] = None
    ) -> Ledger:
        ledgers = self._strategy_ledgers.setdefault(strategy_id, {})
        if bucket not in ledgers:
            cash_acct = self._strategy_cash_accounts.get(strategy_id)
            init_c = initial_cash if initial_cash is not None else self._strategy_capital.get(
                strategy_id, {}
            ).get(bucket, self._strategy_initial_cash[bucket])
            ledgers[bucket] = Ledger(
                bucket,
                init_c,
                cash_account=cash_acct,
            )
        return ledgers[bucket]

    def reconcile_canonical_cash(self, strategy_id: Optional[str] = None) -> float:
        """Reconcile canonical cash balance against historical fill ledger.

        Enforces invariant: cash == initial_cash + sum(fill_cash_flows).
        Deduplicates fills across horizon aliases (SWING vs INTRADAY).
        """
        if strategy_id and strategy_id in self._strategy_cash_accounts:
            acct = self._strategy_cash_accounts[strategy_id]
            expected_cash = acct.initial_cash
            ledgers = self._strategy_ledgers.get(strategy_id, {})
            seen_fill_ids: Set[str] = set()
            unique_fills: List[Fill] = []
            for l in ledgers.values():
                for f in l.fills:
                    if f.fill_id not in seen_fill_ids:
                        seen_fill_ids.add(f.fill_id)
                        unique_fills.append(f)
            unique_fills.sort(key=lambda f: f.timestamp)
            for f in unique_fills:
                mult = getattr(f, "multiplier", 1.0) or 1.0
                cost = f.quantity * f.fill_price * mult
                explicit_costs = f.fee + f.tax
                costs = explicit_costs if is_slippage_embedded(f) else (explicit_costs + f.slippage)
                if f.side == OrderSide.BUY:
                    expected_cash -= (cost + costs)
                else:
                    expected_cash += (cost - costs)
            expected_cash += sum(getattr(self, '_corporate_cash_flows', {}).get(strategy_id, {}).values())
            return round(expected_cash, 4)
        return 0.0

    def restore_portfolio(self, portfolio: PaperPortfolio) -> None:
        """Install a reconstructed native aggregate ledger without replaying fills twice."""
        ledger = Ledger(portfolio.bucket, portfolio.initial_cash, currency=portfolio.currency)
        ledger.cash = portfolio.cash
        ledger.equity = portfolio.equity
        ledger.realized_pnl = portfolio.realized_pnl
        ledger.unrealized_pnl = portfolio.unrealized_pnl
        ledger.positions = dict(portfolio.positions)
        ledger.orders = list(portfolio.orders)
        ledger.fills = list(portfolio.fills)
        self._ledgers[portfolio.bucket] = ledger

    def get_portfolio(self, bucket: DecisionScope) -> PaperPortfolio:
        return self._ledgers[bucket].get_portfolio()

    def get_all_portfolios(self) -> Dict[DecisionScope, PaperPortfolio]:
        return {b: ledger.get_portfolio() for b, ledger in self._ledgers.items()}

    def get_strategy_portfolio(self, strategy_id: str, bucket: DecisionScope) -> PaperPortfolio:
        return self.get_strategy_ledger(strategy_id, bucket).get_portfolio()

    def get_all_strategy_portfolios(self, strategy_id: str) -> Dict[DecisionScope, PaperPortfolio]:
        return {b: self.get_strategy_portfolio(strategy_id, b) for b in self._ledgers}

    def get_strategy_equity(
        self,
        strategy_id: str,
        buckets: Optional[Iterable[DecisionScope]] = None,
    ) -> Optional[float]:
        """Calculate total strategy equity across buckets without duplicating shared cash.

        When unified_cash is used, cash belongs to the shared CanonicalCashAccount and must
        be counted exactly once: total_equity = cash + sum(position market values).
        If any active position lacks market_value (missing mark), returns None (NAV_UNAVAILABLE).
        """
        target_buckets = list(buckets) if buckets is not None else list(self._strategy_ledgers.get(strategy_id, {}).keys())
        if not target_buckets:
            return None

        # Check if strategy uses unified cash account
        if strategy_id in self._strategy_cash_accounts:
            cash = self._strategy_cash_accounts[strategy_id].cash
            total_mv = 0.0
            for b in target_buckets:
                ledger = self.get_strategy_ledger(strategy_id, b)
                for pos in ledger.positions.values():
                    if pos.quantity > 0:
                        if pos.market_value is None:
                            return None
                        total_mv += pos.market_value
            return round(cash + total_mv, 4)
        else:
            # Independent cash accounts per bucket
            total = 0.0
            for b in target_buckets:
                p = self.get_strategy_portfolio(strategy_id, b)
                if p.equity is None:
                    return None
                total += p.equity
            return round(total, 4)

    def strategy_ids(self) -> List[str]:
        return list(self._strategy_ledgers)

    def get_positions(self, bucket: DecisionScope) -> Dict[str, Position]:
        return dict(self._ledgers[bucket].positions)

    def add_order(self, order: Order) -> None:
        if order.strategy_id and self.get_strategy_ledger(order.strategy_id, order.bucket).currency != order.currency:
            raise ValueError("CURRENCY_MISMATCH: order does not match native account")
        if order.currency == self._ledgers[order.bucket].currency:
            self._ledgers[order.bucket].add_order(order)
        elif not order.strategy_id:
            raise ValueError("CURRENCY_MISMATCH: aggregate order has no native account")
        if order.strategy_id:
            self.get_strategy_ledger(order.strategy_id, order.bucket).add_order(order)

    def record_rejection(self, bucket: DecisionScope, order_id: str, reason: str) -> None:
        self._ledgers[bucket].record_rejection(order_id, reason)

    def apply_fill(self, fill: Fill, strategy_id: Optional[str] = None) -> None:
        native = self.get_strategy_ledger(strategy_id, fill.bucket) if strategy_id else None
        if native is not None and native.currency != fill.currency:
            raise ValueError("CURRENCY_MISMATCH: native account does not match fill")
        if self._ledgers[fill.bucket].currency == fill.currency:
            self._ledgers[fill.bucket].apply_fill(fill)
        elif native is None:
            raise ValueError("CURRENCY_MISMATCH: fill has no native account")
        if strategy_id:
            native.apply_fill(fill)

    def update_mark_to_market(self, item: Any) -> None:
        for ledger in self._ledgers.values():
            ledger.update_mark_to_market(item)
        for ledgers in self._strategy_ledgers.values():
            for ledger in ledgers.values():
                ledger.update_mark_to_market(item)

    def settle_expired_options(self, current_time: datetime, underlying_prices: Dict[str, float]) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for ledger in self._ledgers.values():
            res = ledger.settle_expired_options(current_time, underlying_prices)
            results.extend(res)
        for ledgers in self._strategy_ledgers.values():
            for ledger in ledgers.values():
                ledger.settle_expired_options(current_time, underlying_prices)
        return results


