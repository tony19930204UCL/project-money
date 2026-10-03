from datetime import datetime, timedelta, timezone
import pytest
from cio_market_lab.domain.models import Bar, Market
from cio_market_lab.engine.walk_forward import chronological_windows, evaluate_walk_forward
from cio_market_lab.strategies.base import BaseStrategy

class NoSignalStrategy(BaseStrategy):
 def describe(self): return {"name":"no-signal"}
 def validate_config(self, config): return []
 def on_bar(self, context, bar): return []

def bars(n=10):
 t=datetime(2026,1,1,tzinfo=timezone.utc)
 return [Bar(symbol="XYZ",market=Market.US,timestamp=t+timedelta(days=i),observed_at=t+timedelta(days=i),open=10,high=11,low=9,close=10,volume=100,source="fixture",quality="historical") for i in range(n)]

def test_windows_are_chronological_disjoint_and_holdout_is_oos():
 w=chronological_windows(list(reversed(bars())))
 assert [x.name for x in w]==["TRAIN","VALIDATION","HOLDOUT_OOS"]
 assert [len(x.bars) for x in w]==[6,2,2]
 assert w[0].bars[-1].timestamp < w[1].bars[0].timestamp < w[2].bars[0].timestamp
 assert w[2].start_index == w[1].end_index

def test_runs_isolated_deterministic_retro_only():
 out=evaluate_walk_forward(bars(),NoSignalStrategy)
 assert out.evidence_scope=="RETROSPECTIVE_ONLY"
 assert [r["is_deterministic"] for r in out.results]==[True]*3
 assert sum(r["bar_count"] for r in out.results)==10

def test_invalid_empty_oos_rejected():
 with pytest.raises(ValueError): chronological_windows(bars(3),.8,.2)
