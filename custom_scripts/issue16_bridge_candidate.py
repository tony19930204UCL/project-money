"""Issue #16 candidate-only host bridge invocation.

Source-only helper for Main-owned acceptance. It never deploys, changes cron or
providers, reads credentials, or performs broker/capital actions. Host modules,
route contracts, local position contracts and receipts are supplied explicitly
by Main at invocation time and are not uploaded by this script.
"""
from __future__ import annotations

import argparse
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cio_market_lab.engine.cio_session import CIOSessionHistory
from cio_market_lab.research.issue16_acceptance import HermesLocalInference, InferenceContract
from cio_market_lab.research.issue16_live_bridge import (
    LocalPositionReceiptBridge,
    OriginalResearchCallbackBridge,
    StageRoute,
)


def _now(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--now requires a timezone")
    return parsed.astimezone(timezone.utc)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_entrypoint(spec: str):
    if ":" not in spec:
        raise ValueError("--entrypoint must be module:function")
    module_name, function_name = spec.split(":", 1)
    module = importlib.import_module(module_name)
    entrypoint = getattr(module, function_name)
    if not callable(entrypoint):
        raise ValueError("entrypoint is not callable")
    return entrypoint


def _engine(raw: dict[str, Any]) -> HermesLocalInference:
    contract = InferenceContract.model_validate(raw)
    return HermesLocalInference(contract)


def _routes(path: Path) -> dict[str, StageRoute]:
    raw = _load_json(path)
    if not isinstance(raw, dict):
        raise ValueError("route config must be an object")
    routes: dict[str, StageRoute] = {}
    for stage in ("discovery", "commercial", "underwriting", "challenge"):
        value = raw.get(stage)
        if not isinstance(value, dict):
            raise ValueError(f"route config missing stage: {stage}")
        primary = value.get("primary")
        fallback = value.get("fallback")
        primary_model_family = value.get("primary_model_family")
        fallback_model_family = value.get("fallback_model_family")
        if not isinstance(primary, dict):
            raise ValueError(f"primary route missing for stage: {stage}")
        if not isinstance(primary_model_family, str) or not primary_model_family.strip():
            raise ValueError(f"primary_model_family missing for stage: {stage}")
        if isinstance(fallback, dict) and (
            not isinstance(fallback_model_family, str) or not fallback_model_family.strip()
        ):
            raise ValueError(f"fallback_model_family missing for stage: {stage}")
        routes[stage] = StageRoute(
            primary=_engine(primary),
            primary_model_family=primary_model_family.strip(),
            fallback=_engine(fallback) if isinstance(fallback, dict) else None,
            fallback_model_family=(
                fallback_model_family.strip()
                if isinstance(fallback_model_family, str) and fallback_model_family.strip()
                else None
            ),
        )
    return routes


def research(args: argparse.Namespace) -> int:
    bridge = OriginalResearchCallbackBridge(routes=_routes(args.routes))
    result = bridge.run_installed_run_case(
        _load_entrypoint(args.entrypoint),
        case_id=args.case_id,
        symbol=args.symbol,
        seed_urls=list(args.seed_url),
        directory=args.directory,
        max_attempts=args.max_attempts,
        now=_now(args.now),
    )
    result = {
        **result,
        "candidate_cli": True,
        "host_entrypoint_spec": args.entrypoint,
        "provider_model_identity_evidence": (
            "emitted by stage runtime_receipt/attempts when the installed host invokes callbacks"
        ),
        "live_acceptance_claimed": False,
    }
    print(json.dumps(result, sort_keys=True, default=str))
    # Preserve the original host terminal. Only READY_FOR_CIO is a successful
    # candidate CLI invocation; this remains source/synthetic evidence only.
    return 0 if result.get("status") == "READY_FOR_CIO" else 4


def monitor(args: argparse.Namespace) -> int:
    contracts = _load_json(args.contracts)
    quotes = _load_json(args.quotes)
    if not isinstance(contracts, list):
        raise ValueError("--contracts must contain a JSON list")
    if not isinstance(quotes, dict):
        raise ValueError("--quotes must contain a JSON object keyed by symbol")
    receipt = _load_json(args.receipt) if args.receipt else None

    def registry_loader():
        return {"contracts": contracts}

    def quote_provider(symbol: str):
        return quotes.get(symbol)

    def receipt_provider(expected):
        return receipt

    bridge = LocalPositionReceiptBridge(
        registry_loader=registry_loader,
        quote_provider=quote_provider,
        receipt_provider=receipt_provider,
        history=CIOSessionHistory(args.history_root, args.session_id),
        max_age_seconds=args.max_age_seconds,
    )
    result = bridge.evaluate(now=_now(args.now))
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "EVALUATED" else 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Issue #16 Main-owned candidate bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    research_parser = sub.add_parser("research")
    research_parser.add_argument("--symbol", required=True)
    research_parser.add_argument("--case-id", required=True)
    research_parser.add_argument("--seed-url", action="append", required=True)
    research_parser.add_argument("--directory", required=True)
    research_parser.add_argument("--routes", type=Path, required=True)
    research_parser.add_argument("--entrypoint", required=True)
    research_parser.add_argument("--max-attempts", type=int, default=2)
    research_parser.add_argument("--now")
    research_parser.set_defaults(func=research)

    monitor_parser = sub.add_parser("monitor")
    monitor_parser.add_argument("--contracts", type=Path, required=True)
    monitor_parser.add_argument("--quotes", type=Path, required=True)
    monitor_parser.add_argument("--receipt", type=Path)
    monitor_parser.add_argument("--history-root", type=Path, required=True)
    monitor_parser.add_argument("--session-id", default="issue16-host-monitor")
    monitor_parser.add_argument("--max-age-seconds", type=float, default=300)
    monitor_parser.add_argument("--now")
    monitor_parser.set_defaults(func=monitor)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
