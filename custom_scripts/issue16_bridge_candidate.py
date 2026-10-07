"""Issue #16 candidate-only host bridge invocation.

This script never deploys or edits cron/provider/broker state. It wires the
reviewed source bridge to caller-supplied host entrypoints/contracts and prints
sanitized result metadata for Main-owned acceptance.
"""
from __future__ import annotations

import argparse
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cio_market_lab.engine.cio_session import CIOSessionHistory
from cio_market_lab.research.issue16_acceptance import (
    AuthorizedStageInference,
    DeliveryReceiptConsumer,
    LocalPositionReceiptBridge,
    OriginalResearchEngineCallbackBridge,
    ReceiptAwarePositionConsumer,
    StageRouteContract,
)


def _now(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--now requires timezone")
    return parsed.astimezone(timezone.utc)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_entrypoint(spec: str):
    if ":" not in spec:
        raise ValueError("--entrypoint must be module:function")
    module_name,function_name=spec.split(":",1)
    module=importlib.import_module(module_name)
    entrypoint=getattr(module,function_name)
    if not callable(entrypoint):
        raise ValueError("entrypoint is not callable")
    return entrypoint


def research(args: argparse.Namespace) -> int:
    raw=_load_json(args.routes)
    if not isinstance(raw,dict):
        raise ValueError("route config must be an object")
    stages={}
    for stage in ("discovery","commercial","underwriting","challenge"):
        if stage not in raw:
            print(json.dumps({
                "status":"BLOCKED",
                "reason":f"STAGE_ROUTE_MISSING:{stage}",
                "live_acceptance_claimed":False,
            },sort_keys=True))
            return 4
        route=StageRouteContract.model_validate({"stage":stage,**raw[stage]})
        stages[stage]=AuthorizedStageInference(route)
    bridge=OriginalResearchEngineCallbackBridge(stage_inference=stages)
    result=bridge.run_original_entrypoint(
        _load_entrypoint(args.entrypoint),
        symbol=args.symbol,
        now=_now(args.now),
        reader=None,
    )
    print(json.dumps(result,sort_keys=True,default=str))
    return 0 if result.get("status")=="COMPLETED_PUBLIC_RESEARCH_CANDIDATE" else 4


def monitor(args: argparse.Namespace) -> int:
    contracts=_load_json(args.contracts)
    quotes=_load_json(args.quotes)
    if not isinstance(contracts,list) or not isinstance(quotes,dict):
        raise ValueError("contracts must be a list and quotes an object")
    history=CIOSessionHistory(args.history_root,args.session_id)
    bridge=LocalPositionReceiptBridge(
        ReceiptAwarePositionConsumer(),
        DeliveryReceiptConsumer(history),
    )
    result={
        "monitor":bridge.evaluate_contracts(
            contracts,quotes,now=_now(args.now),max_age_seconds=args.max_age_seconds
        ),
        "pending_replay":bridge.pending_replay(),
    }
    if args.expected:
        expected=_load_json(args.expected)
        if args.record_pending_contract:
            observation_identity=args.observation_identity or ""
            if not observation_identity:
                raise ValueError("--observation-identity required with --record-pending-contract")
            result["pending_record"]=bridge.record_pending(
                contract_id=args.record_pending_contract,
                expected=expected,
                observation_identity=observation_identity,
            )
        if args.receipt:
            result["receipt"]=bridge.consume_receipt(expected,_load_json(args.receipt))
            result["pending_replay_after_receipt"]=bridge.pending_replay()
    print(json.dumps(result,sort_keys=True,default=str))
    return 0


def main(argv: list[str] | None=None) -> int:
    parser=argparse.ArgumentParser(description="Issue #16 Main-owned candidate bridge")
    sub=parser.add_subparsers(dest="command",required=True)

    r=sub.add_parser("research")
    r.add_argument("--symbol",required=True)
    r.add_argument("--routes",type=Path,required=True)
    r.add_argument("--entrypoint",required=True)
    r.add_argument("--now")
    r.set_defaults(func=research)

    m=sub.add_parser("monitor")
    m.add_argument("--contracts",type=Path,required=True)
    m.add_argument("--quotes",type=Path,required=True)
    m.add_argument("--history-root",type=Path,required=True)
    m.add_argument("--session-id",default="issue16-host-monitor")
    m.add_argument("--now")
    m.add_argument("--max-age-seconds",type=float,default=300)
    m.add_argument("--expected",type=Path)
    m.add_argument("--receipt",type=Path)
    m.add_argument("--record-pending-contract")
    m.add_argument("--observation-identity")
    m.set_defaults(func=monitor)

    args=parser.parse_args(argv)
    return args.func(args)


if __name__=="__main__":
    raise SystemExit(main())
