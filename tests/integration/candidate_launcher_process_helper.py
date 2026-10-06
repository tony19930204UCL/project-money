from __future__ import annotations
import argparse, json
from datetime import datetime
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

from cio_market_lab.engine.candidate_launcher import AUTHENTICATED_PAPER, run_candidate
from cio_market_lab.engine.paper_orders import PaperExperimentSettings
from tests.integration.research_learning_chain_helper import ChainMarketAdapter, TestOnlyDailyExecutor

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--workspace-root",required=True)
    p.add_argument("--runtime-dir",required=True)
    p.add_argument("--packet-root",required=True)
    p.add_argument("--now",required=True)
    args=p.parse_args()
    now=datetime.fromisoformat(args.now)
    settings=PaperExperimentSettings(
        strategy_id="TEST_ONLY_CANDIDATE",
        enabled=True,
        market="US",
        base_currency="USD",
        reporting_currency="TWD",
        initial_cash=100000.0,
        universe=["MSFT"],
    )
    result=run_candidate(
        workspace_root=Path(args.workspace_root),
        runtime_dir=Path(args.runtime_dir),
        mode=AUTHENTICATED_PAPER,
        settings=settings,
        packet_root=Path(args.packet_root),
        trusted_manifest=None,
        session_id="TEST_ONLY_CHAIN_SESSION",
        market_adapter=ChainMarketAdapter(now),
        cio_executor=TestOnlyDailyExecutor(now,"TEST_ONLY_CANDIDATE_CASE_RESTART"),
        now=now,
        allow_test_only=True,
    )
    print(json.dumps(result,sort_keys=True))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
