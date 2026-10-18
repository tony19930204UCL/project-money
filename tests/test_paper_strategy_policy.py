from types import SimpleNamespace

from cio_market_lab.engine.paper_strategy_policy import ExitPolicy, SizingPolicy
from cio_market_lab.engine.paper_runner import Momentum
from cio_market_lab.engine.paper_trading_loop import PaperAccount


def test_sizing_uses_equity_fraction_not_legacy_share_count():
    policy = SizingPolicy(target_pct=0.10, max_pct=0.20)
    assert policy.quantity(1_000_000, 2_500) == 40
    assert policy.quantity(30_000, 200) == 15


def test_sizing_does_not_add_when_target_is_already_reached():
    policy = SizingPolicy(target_pct=0.10, max_pct=0.20)
    assert policy.quantity(100_000, 100, current_qty=100) == 0


def test_exit_policy_learns_time_and_session_end_limbs():
    policy = ExitPolicy(max_hold_ticks=3, force_flat_at_session_end=True)
    assert policy.reason(100, 100, 100, 3) == "time_stop"
    assert policy.reason(100, 100, 100, 1, session_end=True) == "tw_daytrade_session_end"
    learned = policy.learn_hold_ticks(
        [{"hold_ticks": 10, "pnl": 1}, {"hold_ticks": 20, "pnl": 2},
         {"hold_ticks": 30, "pnl": 3}, {"hold_ticks": 30, "pnl": 3},
         {"hold_ticks": 30, "pnl": 3}],
        candidates=(10, 30), min_samples=3)
    assert learned.max_hold_ticks == 30


def test_runner_momentum_sizes_from_marked_equity():
    strategy = Momentum("AAA", market="US", target_pct=0.10, max_position_pct=0.20)
    account = PaperAccount(cash=10_000, marks={})
    for _ in range(strategy.LOOKBACK):
        assert strategy.signals({"AAA": SimpleNamespace(c=100)}, account) == []
    signals = strategy.signals({"AAA": SimpleNamespace(c=101)}, account)
    assert signals[0].size == 9
