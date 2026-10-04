"""Frozen historical replay / out-of-sample diagnostics, never capital authority.

Uses the existing next-bar simulator with disjoint cash accounts and source bars
unchanged. Download-time observations are not represented as historical live
receipts. This lab cannot manufacture Main CIO BUY decisions or production fills.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import hashlib
import json
import math
import uuid
from typing import Any

from cio_market_lab.domain.models import Bar, DecisionScope, Market, OrderSide, Signal
from cio_market_lab.engine.execution import ExecutionCostConfig
from cio_market_lab.engine.portfolio import PortfolioManager
from cio_market_lab.engine.simulation import SimulationEngine
from cio_market_lab.events.store import EventStore
from cio_market_lab.strategies.base import BaseStrategy, StrategyContext

AUTHORITY = 'HISTORICAL_REPLAY_ONLY_NOT_LIVE_ACCEPTANCE'


def partition_bars(bars: list[Bar], train_fraction: float = 0.7) -> tuple[list[Bar], list[Bar]]:
    if not 0.5 <= train_fraction <= 0.8 or len(bars) < 30:
        raise ValueError('INSUFFICIENT_DATA_OR_INVALID_SPLIT')
    if any(a.timestamp >= b.timestamp for a, b in zip(bars, bars[1:])):
        raise ValueError('DUPLICATE_OR_NONCHRONOLOGICAL_BARS')
    if any(not all(math.isfinite(v) and v > 0 for v in (b.open, b.high, b.low, b.close))
           or b.low > min(b.open, b.close) or b.high < max(b.open, b.close)
           or b.volume < 0 for b in bars):
        raise ValueError('INVALID_SOURCE_OHLC')
    split = int(len(bars) * train_fraction)
    return bars[:split], bars[split:]


class FrozenMACrossover(BaseStrategy):
    """Predeclared diagnostic strategy, not Main CIO and not a live worker."""
    def describe(self) -> dict[str, Any]:
        return {'name': 'frozen_ma_replay', 'authority': AUTHORITY, 'version': '1'}

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        fast, slow = int(config.get('fast', 5)), int(config.get('slow', 20))
        return [] if 2 <= fast < slow <= 200 else ['INVALID_MA_PERIODS']

    def on_bar(self, context: StrategyContext, bar: Bar) -> list[Signal]:
        history = context.bars_history.get(bar.symbol, [])
        fast, slow = int(self.config.get('fast', 5)), int(self.config.get('slow', 20))
        if len(history) < slow + 1:
            return []
        closes = [b.close for b in history]
        f = sum(closes[-fast:]) / fast
        s = sum(closes[-slow:]) / slow
        pf = sum(closes[-fast-1:-1]) / fast
        ps = sum(closes[-slow-1:-1]) / slow
        held = context.current_positions.get(bar.symbol)
        if f > s and pf <= ps and (held is None or held.quantity == 0):
            side = OrderSide.BUY
        elif f < s and held is not None and held.quantity > 0:
            side = OrderSide.SELL
        else:
            return []
        config_hash = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()
        return [Signal(signal_id=str(uuid.uuid4()), strategy_id='frozen_ma_replay', version='1',
                       config_hash=config_hash, symbol=bar.symbol,
                       market=Market.TW if bar.symbol.endswith(('.TW', '.TWO')) else Market.US,
                       decision_scope=DecisionScope.SWING, side=side,
                       generated_at=bar.timestamp, exchange_ts=bar.timestamp,
                       valid_until=bar.timestamp + timedelta(days=10),
                       reason_codes=['PREDECLARED_HISTORICAL_MA_CROSS'],
                       evidence={'authority': AUTHORITY, 'fast': f, 'slow': s}, entry_model='NEXT_OPEN')]


class NativeReplayEventStore(EventStore):
    def __init__(self, db_path: Path, currency: str):
        super().__init__(db_path)
        self.currency = currency

    def reconstruct_portfolio(self, bucket: DecisionScope, initial_cash: float = 1_000_000.0):
        restored = super().reconstruct_portfolio(bucket, initial_cash, currency=self.currency)
        if any(f.currency != self.currency for f in restored.fills):
            raise ValueError('REPLAY_RECONSTRUCTION_CURRENCY_MISMATCH')
        positions = {symbol: pos.model_copy(update={'currency': self.currency})
                     for symbol, pos in restored.positions.items()}
        return restored.model_copy(update={'currency': self.currency, 'positions': positions})


class NativeCurrencyReplayEngine(SimulationEngine):
    def __init__(self, *args: Any, currency: str, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.currency = currency
        self.portfolio_manager = PortfolioManager(initial_cash_swing=self.initial_cash_swing,
                                                 initial_cash_intraday=self.initial_cash_intraday,
                                                 currency=currency)
        self.portfolio_manager.register_strategy('frozen_ma_replay', self.initial_cash_swing,
                                                 currency=currency)

    def _convert_signal_to_order(self, sig: Signal, bar: Bar):
        order = super()._convert_signal_to_order(sig, bar)
        return order.model_copy(update={'currency': self.currency}) if order else None


def frozen_replay(bars: list[Bar], symbol: str, currency: str, initial_cash: float,
                  config: dict[str, Any], output_dir: Path, train_fraction: float = 0.7) -> dict[str, Any]:
    expected_currency = 'TWD' if symbol.endswith(('.TW', '.TWO')) else 'USD'
    if currency != expected_currency or any(b.symbol != symbol for b in bars):
        raise ValueError('SOURCE_SYMBOL_OR_NATIVE_CURRENCY_MISMATCH')
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError('INVALID_PAPER_INITIAL_CASH')
    # No parameter fitting is performed. All three runs use this identical config.
    frozen_config = {'fast': int(config.get('fast', 5)), 'slow': int(config.get('slow', 20))}
    strategy = FrozenMACrossover(frozen_config)
    if strategy.validate_config(frozen_config):
        raise ValueError('INVALID_FROZEN_CONFIG')
    train, test = partition_bars(bars, train_fraction)
    source_blob = json.dumps([b.model_dump(mode='json') for b in bars], sort_keys=True).encode()
    source_sha = hashlib.sha256(source_blob).hexdigest()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        'authority': AUTHORITY, 'completion_claim_allowed': False,
        'source_sha256': source_sha, 'source_bar_count': len(bars), 'symbol': symbol,
        'currency': currency, 'fx_used': False, 'config': frozen_config,
        'config_frozen_before_evaluation': True,
        'negative_expectancy_not_suppressed': True, 'run_ids': [],
        'limitations': ['Not a CIO decision or live PAPER acceptance.',
                        'Retrospective capture is not contemporaneous historical execution evidence.',
                        'Single frozen chronological holdout is not the complete multi-regime OOS gate.',
                        'Fees/slippage are explicit scenario assumptions, not broker receipts.'],
    }
    for label, partition, multiplier in [('train', train, 1), ('test', test, 1), ('cost_stress', test, 2)]:
        run_id = f'{label}-{uuid.uuid4()}'
        run_dir = output_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        store = NativeReplayEventStore(run_dir / 'events.sqlite', currency)
        costs = ExecutionCostConfig(slippage_bps=5.0 * multiplier,
                                    fee_rate_tw=0.001425 * multiplier,
                                    fee_rate_us=0.0005 * multiplier,
                                    min_fee_tw=20.0 * multiplier,
                                    min_fee_us=1.0 * multiplier)
        engine = NativeCurrencyReplayEngine(store, currency=currency, cost_config=costs,
                                            initial_cash_swing=initial_cash, initial_cash_intraday=0,
                                            reject_stale_bars=False, default_order_shares=1)
        # Historical replay explicitly permits old bars, without mutating source quality.
        summary = engine.run(partition, [FrozenMACrossover(frozen_config)])
        ledger = engine.portfolio_manager.get_ledger(DecisionScope.SWING)
        fills = list(ledger.fills)
        orders = {o.order_id: o for o in ledger.orders}
        if any(f.timestamp <= orders[f.order_id].created_at for f in fills):
            raise ValueError('SAME_BAR_OR_LOOKAHEAD_FILL')
        if any(f.currency != currency for f in fills):
            raise ValueError('REPLAY_FILL_CURRENCY_MISMATCH')
        result['run_ids'].append(run_id)
        result[label] = {'initial_cash': initial_cash, 'terminal_nav': ledger.equity,
                         'cash': ledger.cash, 'net_return_pct': (ledger.equity / initial_cash - 1) * 100,
                         'signals': summary.total_signals, 'orders': summary.total_orders,
                         'fills': summary.total_fills, 'rejections': summary.total_rejections,
                         'first_bar': partition[0].timestamp.isoformat(),
                         'last_bar': partition[-1].timestamp.isoformat(),
                         'slippage_multiplier': multiplier,
                         'next_bar_all_fills_verified': True, 'runtime_dir': str(run_dir)}
    if hashlib.sha256(json.dumps([b.model_dump(mode='json') for b in bars], sort_keys=True).encode()).hexdigest() != source_sha:
        raise ValueError('SOURCE_MUTATED_DURING_REPLAY')
    (output_dir / 'replay_result.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def walk_forward_replay(bars: list[Bar], symbol: str, currency: str, initial_cash: float,
                        config: dict[str, Any], output_dir: Path, *, folds: int = 3,
                        holdout_fraction: float = 0.20) -> dict[str, Any]:
    """Expanding train, disjoint tests, final untouched holdout, frozen MA rule.

    Training supplies indicator warmup only. No fitting, winner selection or
    orders/positions carried out of training. Every window has separate virtual
    cash. Historical fills do not prove the live Main CIO PAPER path.
    """
    from datetime import datetime, timezone
    expected_currency = 'TWD' if symbol.endswith(('.TW', '.TWO')) else 'USD'
    if currency != expected_currency or any(b.symbol != symbol for b in bars):
        raise ValueError('SOURCE_SYMBOL_OR_NATIVE_CURRENCY_MISMATCH')
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError('INVALID_PAPER_INITIAL_CASH')
    if type(folds) is not int or not 2 <= folds <= 5 or not 0.15 <= holdout_fraction <= 0.30:
        raise ValueError('INVALID_WALK_FORWARD_SPLIT')
    if any(b.is_synthetic or b.is_fixture for b in bars):
        raise ValueError('REAL_CAPTURE_REQUIRED_NO_SYNTHETIC_FALLBACK')
    partition_bars(bars)  # Existing chronology, duplicate and OHLC validation.
    allowed = {'fast', 'slow', 'fast_period', 'slow_period'}
    if set(config) - allowed or ('fast' in config and 'fast_period' in config) or ('slow' in config and 'slow_period' in config):
        raise ValueError('INVALID_FROZEN_CONFIG_KEYS')
    fast, slow = config.get('fast', config.get('fast_period', 5)), config.get('slow', config.get('slow_period', 20))
    if type(fast) is not int or type(slow) is not int or not 2 <= fast < slow <= 200:
        raise ValueError('INVALID_FROZEN_CONFIG')
    frozen_config = {'fast': fast, 'slow': slow}
    split = int(len(bars) * (1 - holdout_fraction))
    development, holdout = bars[:split], bars[split:]
    training_count = len(development) // 2
    width = (len(development) - training_count) // folds
    if training_count < slow + 1 or width < slow + 2 or len(holdout) < slow + 2:
        raise ValueError('INSUFFICIENT_WALK_FORWARD_BARS')
    config_hash = hashlib.sha256(json.dumps(frozen_config, sort_keys=True).encode()).hexdigest()
    source_blob = json.dumps([b.model_dump(mode='json') for b in bars], sort_keys=True)
    source_hash = hashlib.sha256(source_blob.encode()).hexdigest()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    specs = []
    for i in range(folds):
        start = training_count + i * width
        end = len(development) if i == folds - 1 else start + width
        specs.append((development[:start], development[start:end]))
    manifest = {
        'run_id': 'walk-forward-' + str(uuid.uuid4()),
        'frozen_at': datetime.now(timezone.utc).isoformat(), 'authority': AUTHORITY,
        'live_acceptance': False, 'completion_claim_allowed': False,
        'broker_connected': False, 'symbol': symbol, 'currency': currency,
        'virtual_initial_cash': initial_cash, 'source_hash': source_hash,
        'config_hash': config_hash, 'config': frozen_config, 'fold_count': folds,
        'holdout_fraction': holdout_fraction,
        'selection_policy': 'PREDECLARED_CONFIG_NO_FITTING_OR_WINNER_SELECTION',
        'warmup_policy': 'TRAIN_TRAILING_BARS_ONLY_NO_ORDERS_OR_POSITIONS',
        'window_policy': 'EXPANDING_TRAIN_DISJOINT_TEST_FINAL_HOLDOUT_UNTOUCHED',
        'fold_windows': [{'train_end': tr[-1].timestamp.isoformat(),
                          'test_start': te[0].timestamp.isoformat(),
                          'test_end': te[-1].timestamp.isoformat()} for tr, te in specs],
        'final_holdout_start': holdout[0].timestamp.isoformat(),
        'aggregate_return': 'NOT_COMPUTED_SEPARATE_VIRTUAL_BALANCES',
    }
    # Persist config and partitions before examining any test result.
    (output_dir / 'walk_forward_manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (output_dir / 'source_bars.json').write_text(source_blob + '\n')

    def evaluate(training, evaluation, label, multiplier=1):
        class EquityRecordingEngine(NativeCurrencyReplayEngine):
            def _process_bar_step(self, bar, strategies):
                super()._process_bar_step(bar, strategies)
                self.equity_path.append(self.portfolio_manager.get_ledger(DecisionScope.SWING).equity)
        run_dir = output_dir / label
        run_dir.mkdir(exist_ok=False)
        store = NativeReplayEventStore(run_dir / 'events.sqlite', currency)
        costs = ExecutionCostConfig(slippage_bps=5.0 * multiplier,
                                   fee_rate_tw=0.001425 * multiplier,
                                   fee_rate_us=0.0005 * multiplier,
                                   min_fee_tw=20.0 * multiplier, min_fee_us=1.0 * multiplier)
        engine = EquityRecordingEngine(store, currency=currency, cost_config=costs,
                                       initial_cash_swing=initial_cash, initial_cash_intraday=0,
                                       reject_stale_bars=False, default_order_shares=1)
        engine.equity_path = [initial_cash]
        engine.bars_history[symbol] = list(training[-(slow+1):])
        summary = engine.run(evaluation, [FrozenMACrossover(frozen_config)])
        ledger = engine.portfolio_manager.get_ledger(DecisionScope.SWING)
        orders = {o.order_id: o for o in ledger.orders}
        if any(f.timestamp <= orders[f.order_id].created_at for f in ledger.fills):
            raise ValueError('SAME_BAR_OR_LOOKAHEAD_FILL')
        if any(f.currency != currency for f in ledger.fills):
            raise ValueError('REPLAY_FILL_CURRENCY_MISMATCH')
        peak, drawdown = initial_cash, 0.0
        for equity in engine.equity_path:
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak - equity) / peak * 100)
        report = {'bars': summary.total_bars, 'fills': summary.total_fills,
                  'orders': summary.total_orders, 'rejections': summary.total_rejections,
                  'deterministic': summary.is_deterministic, 'cost_multiplier': multiplier,
                  'terminal_nav': ledger.equity, 'cash': ledger.cash,
                  'reconstructed_nav': summary.reconstructed_swing.equity,
                  'net_return_pct': (ledger.equity / initial_cash - 1) * 100,
                  'max_drawdown_pct': drawdown, 'next_bar_all_fills_verified': True,
                  'equity_path': engine.equity_path}
        (run_dir / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        return report

    reports = [{'fold': i+1, 'train_count': len(tr), 'config_hash': config_hash,
                **manifest['fold_windows'][i], 'simulation': evaluate(tr, te, f'fold-{i+1}')}
               for i, (tr, te) in enumerate(specs)]
    final_holdout = {'test_start': holdout[0].timestamp.isoformat(),
                     'test_end': holdout[-1].timestamp.isoformat(),
                     **evaluate(development, holdout, 'final-holdout')}
    report = {**manifest, 'folds': reports, 'final_holdout': final_holdout,
              'cost_stress': evaluate(development, holdout, 'final-holdout-cost-stress', 2),
              'limitations': ['One frozen MA rule; no fitted or selected winner.',
                              'Separate virtual cash; no aggregate live return.',
                              'OHLC next-bar model is not real order-book execution.',
                              'One captured year is not seven/thirty-day live acceptance.']}
    if hashlib.sha256(json.dumps([b.model_dump(mode='json') for b in bars], sort_keys=True).encode()).hexdigest() != source_hash:
        raise ValueError('SOURCE_MUTATED_DURING_REPLAY')
    (output_dir / 'walk_forward_result.json').write_text(json.dumps(report, indent=2) + '\n')
    return report



def validation_selected_oos(
    bars: list[Bar],
    symbol: str,
    currency: str,
    initial_cash: float,
    candidate_configs: list[dict[str, Any]],
    output_dir: Path,
    *,
    train_fraction: float = 0.60,
    validation_fraction: float = 0.20,
    allow_test_only: bool = False,
    fit_inputs: list[dict[str, Any]] | None = None,
    selection_inputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Select on validation only, then evaluate untouched OOS exactly once.

    TEST_ONLY fixtures require explicit opt-in and remain permanently marked as
    non-live evidence. No holdout result is available to configuration selection.
    """
    import inspect
    if any(b.symbol != symbol for b in bars):
        raise ValueError("SOURCE_SYMBOL_MISMATCH")
    expected_currency = "TWD" if symbol.endswith((".TW", ".TWO")) else "USD"
    if currency != expected_currency:
        raise ValueError("SOURCE_SYMBOL_OR_NATIVE_CURRENCY_MISMATCH")
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("INVALID_PAPER_INITIAL_CASH")
    if len(bars) < 30:
        return {
            "status": "UNAVAILABLE",
            "reason": "INSUFFICIENT_HELDOUT_WINDOW",
            "live_approved": False,
            "completion_claim_allowed": False,
        }
    if any(a.timestamp >= b.timestamp for a, b in zip(bars, bars[1:])):
        raise ValueError("DUPLICATE_OR_NONCHRONOLOGICAL_BARS")
    if any(b.is_synthetic for b in bars):
        raise ValueError("SYNTHETIC_SOURCE_FORBIDDEN")
    if any(b.is_fixture for b in bars) and not allow_test_only:
        raise ValueError("TEST_ONLY_SOURCE_REQUIRES_EXPLICIT_OPT_IN")
    if not candidate_configs:
        raise ValueError("NO_CANDIDATE_CONFIGS")
    if not 0 < train_fraction < 1 or not 0 < validation_fraction < 1:
        raise ValueError("INVALID_OOS_SPLIT")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("INVALID_OOS_SPLIT")

    n = len(bars)
    train_end = int(n * train_fraction)
    validation_end = int(n * (train_fraction + validation_fraction))
    train = bars[:train_end]
    validation = bars[train_end:validation_end]
    holdout = bars[validation_end:]
    if not train or not validation or not holdout:
        return {
            "status": "UNAVAILABLE",
            "reason": "INSUFFICIENT_HELDOUT_WINDOW",
            "live_approved": False,
            "completion_claim_allowed": False,
        }

    allowed = {"fast", "slow", "fast_period", "slow_period"}
    normalized: list[dict[str, int]] = []
    for item in candidate_configs:
        if set(item) - allowed:
            raise ValueError("INVALID_FROZEN_CONFIG_KEYS")
        if ("fast" in item and "fast_period" in item) or ("slow" in item and "slow_period" in item):
            raise ValueError("INVALID_FROZEN_CONFIG_KEYS")
        fast = item.get("fast", item.get("fast_period", 5))
        slow = item.get("slow", item.get("slow_period", 20))
        if type(fast) is not int or type(slow) is not int or not 2 <= fast < slow <= 200:
            raise ValueError("INVALID_FROZEN_CONFIG")
        normalized.append({"fast": fast, "slow": slow})
    max_slow = max(item["slow"] for item in normalized)
    if min(len(train), len(validation), len(holdout)) < max_slow + 2:
        return {
            "status": "UNAVAILABLE",
            "reason": "INSUFFICIENT_HELDOUT_WINDOW",
            "live_approved": False,
            "completion_claim_allowed": False,
        }

    train_end_ts = train[-1].timestamp
    for item in fit_inputs or []:
        available_at = item.get("available_at")
        if not isinstance(available_at, datetime) or available_at.tzinfo is None:
            raise ValueError("FIT_INPUT_TIMESTAMP_REQUIRED")
        if available_at > train_end_ts:
            raise ValueError("FUTURE_TRAINING_INPUT_FORBIDDEN")

    validation_end_ts = validation[-1].timestamp
    for item in selection_inputs or []:
        available_at = item.get("available_at")
        if not isinstance(available_at, datetime):
            raise ValueError("SELECTION_INPUT_TIMESTAMP_REQUIRED")
        if available_at.tzinfo is None:
            raise ValueError("SELECTION_INPUT_TIMESTAMP_REQUIRED")
        if available_at > validation_end_ts:
            raise ValueError("FUTURE_SELECTION_INPUT_FORBIDDEN")

    source_tags = sorted({b.source for b in bars})
    if len(source_tags) != 1:
        raise ValueError("MIXED_SOURCE_TAGS_FORBIDDEN")
    source_blob = json.dumps([b.model_dump(mode="json") for b in bars], sort_keys=True)
    source_hash = hashlib.sha256(source_blob.encode()).hexdigest()
    strategy_source = inspect.getsource(FrozenMACrossover)
    strategy_code_hash = hashlib.sha256(strategy_source.encode()).hexdigest()
    costs = ExecutionCostConfig()

    def evaluate(config: dict[str, int], warmup: list[Bar], evaluation: list[Bar], label: str) -> dict[str, Any]:
        run_dir = Path(output_dir) / label
        run_dir.mkdir(parents=True, exist_ok=False)
        store = NativeReplayEventStore(run_dir / "events.sqlite", currency)
        engine = NativeCurrencyReplayEngine(
            store,
            currency=currency,
            cost_config=costs,
            initial_cash_swing=initial_cash,
            initial_cash_intraday=0,
            reject_stale_bars=False,
            default_order_shares=1,
        )
        engine.bars_history[symbol] = list(warmup[-(config["slow"] + 1):])
        result = engine.run(evaluation, [FrozenMACrossover(config)])
        ledger = engine.portfolio_manager.get_ledger(DecisionScope.SWING)
        return {
            "bar_count": len(evaluation),
            "first_timestamp": evaluation[0].timestamp.isoformat(),
            "last_timestamp": evaluation[-1].timestamp.isoformat(),
            "orders": result.total_orders,
            "fills": result.total_fills,
            "positions": {
                key: value.model_dump(mode="json") for key, value in ledger.positions.items()
            },
            "native_cash": ledger.cash,
            "native_nav": ledger.equity,
            "net_return_pct": (ledger.equity / initial_cash - 1) * 100,
            "deterministic": result.is_deterministic,
        }

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    candidate_rows = []
    for index, config in enumerate(normalized):
        train_result = evaluate(config, [], train, f"candidate-{index}-train")
        validation_result = evaluate(
            config, train, validation, f"candidate-{index}-validation"
        )
        candidate_rows.append({
            "config": config,
            "train": train_result,
            "validation": validation_result,
        })

    selected_index = max(
        range(len(candidate_rows)),
        key=lambda i: (
            candidate_rows[i]["validation"]["net_return_pct"],
            -i,
        ),
    )
    selected = candidate_rows[selected_index]
    frozen_config = dict(selected["config"])

    # Holdout is touched exactly once, only after the selection result is frozen.
    holdout_result = evaluate(
        frozen_config,
        train + validation,
        holdout,
        "final-holdout-oos",
    )
    report = {
        "status": "COMPLETE_TEST_ONLY" if any(b.is_fixture for b in bars) else "COMPLETE",
        "strategy_id": "frozen_ma_replay",
        "strategy_version": "1",
        "strategy_code_hash": strategy_code_hash,
        "data_source_tag": source_tags[0],
        "source_sha256": source_hash,
        "data_range": {
            "start": bars[0].timestamp.isoformat(),
            "end": bars[-1].timestamp.isoformat(),
        },
        "train_range": {
            "start": train[0].timestamp.isoformat(),
            "end": train[-1].timestamp.isoformat(),
        },
        "validation_range": {
            "start": validation[0].timestamp.isoformat(),
            "end": validation[-1].timestamp.isoformat(),
        },
        "oos_range": {
            "start": holdout[0].timestamp.isoformat(),
            "end": holdout[-1].timestamp.isoformat(),
        },
        "cost_assumptions": {
            "slippage_bps": costs.slippage_bps,
            "fee_rate_tw": costs.fee_rate_tw,
            "fee_rate_us": costs.fee_rate_us,
            "min_fee_tw": costs.min_fee_tw,
            "min_fee_us": costs.min_fee_us,
        },
        "candidate_results": candidate_rows,
        "selected_config": frozen_config,
        "selection_metric": "VALIDATION_NET_RETURN_ONLY",
        "selection_frozen_before_oos": True,
        "oos_evaluation_count": 1,
        "oos": holdout_result,
        "live_approved": False,
        "completion_claim_allowed": False,
        "evidence_scope": "TEST_ONLY_RETROSPECTIVE" if any(b.is_fixture for b in bars) else AUTHORITY,
    }
    (Path(output_dir) / "validation_selected_oos.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report
