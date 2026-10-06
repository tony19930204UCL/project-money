from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Optional

from cio_market_lab.api.app import create_app
from cio_market_lab.engine.historical_fx import FxRateReceipt, FxReportingBlocked
from cio_market_lab.engine.paper_orders import PaperExperimentSettings
from cio_market_lab.engine.stage_d_observation import make_packet_observation_provider


VERIFY_ONLY = "VERIFY_ONLY"
AUTHENTICATED_PAPER = "AUTHENTICATED_PAPER"
CONFIG_NAME = "candidate_launcher_config.json"
RESULT_NAME = "candidate_result.json"
FX_NAME = "canonical_fx_receipts.json"


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)


def _receipt_payload(receipt: FxRateReceipt) -> dict[str, Any]:
    return {
        "pair": receipt.pair,
        "rate": str(receipt.rate),
        "observed_at": receipt.observed_at.isoformat(),
        "source": receipt.source,
        "source_url": receipt.source_url,
        "source_date": receipt.source_date.isoformat(),
        "provenance": receipt.provenance,
    }


def load_supplied_fx_receipts(path: Optional[Path], *, allow_test_only: bool) -> list[FxRateReceipt]:
    if path is None:
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = raw.get("receipts", raw) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError("FX_RECEIPTS_MUST_BE_A_LIST")
    receipts: list[FxRateReceipt] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("INVALID_FX_RECEIPT")
        value = dict(row)
        value["observed_at"] = datetime.fromisoformat(str(value["observed_at"]).replace("Z", "+00:00"))
        value["source_date"] = date.fromisoformat(str(value["source_date"]))
        receipt = FxRateReceipt(**value)
        if receipt.provenance == "TEST_ONLY" and not allow_test_only:
            raise ValueError("TEST_ONLY_FX_NOT_ALLOWED")
        receipts.append(receipt)
    return receipts


def _persist_exact_fx(runtime_dir: Path, receipts: Iterable[FxRateReceipt]) -> None:
    values = list(receipts)
    if not values:
        return
    _atomic_json(runtime_dir / FX_NAME, {"receipts": [_receipt_payload(r) for r in values]})


def _persist_config(runtime_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path = runtime_dir / CONFIG_NAME
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        identity_keys = ("mode", "strategy_id", "session_id", "packet_root", "trusted_manifest")
        mismatches = [key for key in identity_keys if existing.get(key) != payload.get(key)]
        if mismatches:
            raise ValueError("CANDIDATE_CONFIG_IDENTITY_MISMATCH:" + ",".join(mismatches))
        return existing
    _atomic_json(path, payload)
    return payload


def read_candidate_result(runtime_dir: Path) -> dict[str, Any]:
    path = Path(runtime_dir) / RESULT_NAME
    if not path.exists():
        return {
            "status": "ABSENT",
            "paper_only": True,
            "broker_connected": False,
            "artifact_path": str(path),
        }
    return json.loads(path.read_text(encoding="utf-8"))


def run_candidate(
    *,
    workspace_root: Path,
    runtime_dir: Path,
    mode: str,
    settings: PaperExperimentSettings,
    packet_root: Optional[Path] = None,
    trusted_manifest: Optional[Path] = None,
    session_id: str = "project-money-main-cio",
    fx_receipts_path: Optional[Path] = None,
    market_adapter: Any = None,
    cio_executor: Any = None,
    now: Optional[datetime] = None,
    allow_test_only: bool = False,
) -> dict[str, Any]:
    """Portable source-only candidate launcher.

    VERIFY_ONLY never calls a model or runner cycle. AUTHENTICATED_PAPER binds the
    existing approved packet consumer, material gate and canonical runner. This
    function never connects a broker or manufactures approval/FX evidence.
    """
    if mode not in {VERIFY_ONLY, AUTHENTICATED_PAPER}:
        raise ValueError("INVALID_CANDIDATE_MODE")
    runtime_dir = Path(runtime_dir).resolve()
    workspace_root = Path(workspace_root).resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError("CANDIDATE_NOW_MUST_BE_TIMEZONE_AWARE")

    packet_root_value = str(Path(packet_root).resolve()) if packet_root is not None else None
    manifest_value = str(Path(trusted_manifest).resolve()) if trusted_manifest is not None else None
    config = _persist_config(runtime_dir, {
        "schema_version": 1,
        "mode": mode,
        "strategy_id": settings.strategy_id,
        "session_id": session_id,
        "packet_root": packet_root_value,
        "trusted_manifest": manifest_value,
        "paper_only": True,
        "broker_connected": False,
    })

    entry = {
        "schema_version": 1,
        "status": "STARTED",
        "mode": mode,
        "strategy_id": settings.strategy_id,
        "session_id": session_id,
        "entered_at": stamp.isoformat(),
        "completed_at": None,
        "paper_only": True,
        "broker_connected": False,
        "material_gate_enabled": mode == AUTHENTICATED_PAPER,
        "provider_invoked": False,
        "runner_invoked": False,
        "research_lineage": [],
        "fx": {"status": "MISSING", "receipts": []},
        "limitations": [],
    }
    result_path = runtime_dir / RESULT_NAME
    _atomic_json(result_path, entry)

    receipts = load_supplied_fx_receipts(fx_receipts_path, allow_test_only=allow_test_only)
    _persist_exact_fx(runtime_dir, receipts)

    app = create_app(
        workspace_root=workspace_root,
        runtime_dir=runtime_dir,
        fixture_mode=allow_test_only,
        is_read_only=False,
        market_adapter=market_adapter,
    )
    runner = app.state.app_state.runner
    runner._now_fn = lambda: stamp
    runner.cio_session_id = session_id
    runner.allow_fixture_quotes = allow_test_only

    if mode == VERIFY_ONLY:
        # Explicitly override any ambient environment opt-in. Verification-only
        # cannot accidentally become material-gated/model-calling operation.
        runner.material_gate_enabled = False
        runner.material_observation_provider = None
        runner.frozen_decision_context = None
        runner.set_cio_executor(None)
    else:
        if packet_root is None or (trusted_manifest is None and not allow_test_only):
            exit_payload = {
                **entry,
                "status": "WAITING_FOR_APPROVED_INPUT",
                "completed_at": stamp.isoformat(),
                "reason": "APPROVED_RESEARCH_CONTEXT_REQUIRED",
                "limitations": ["No approved packet root/trusted manifest supplied; runner cycle not invoked."],
            }
            _atomic_json(result_path, exit_payload)
            runner.shutdown()
            return exit_payload
        provider = make_packet_observation_provider(
            Path(packet_root),
            trusted_manifest=Path(trusted_manifest) if trusted_manifest is not None else None,
            allow_fixture=allow_test_only,
        )
        runner.material_observation_provider = provider
        runner.material_gate_enabled = True
        runner.frozen_decision_context = None
        if cio_executor is not None:
            runner.set_cio_executor(cio_executor)

    existing = runner.paper_orders.experiments.get(settings.strategy_id)
    if existing is None:
        runner.configure(settings)
    elif existing.model_dump(mode="json") != settings.model_dump(mode="json"):
        runner.shutdown()
        raise ValueError("PERSISTED_EXPERIMENT_SETTINGS_MISMATCH")

    nav = None
    fx_state: dict[str, Any]
    try:
        nav = runner.report_strategy_nav(
            settings.strategy_id,
            as_of=stamp,
            rate_receipts=receipts,
            allow_test_only=allow_test_only,
        )
        fx_state = {
            "status": "AVAILABLE",
            "receipts": [_receipt_payload(r) for r in receipts],
            "native_nav": nav.get("native_nav"),
            "native_currency": nav.get("native_currency"),
            "reporting_nav": nav.get("reporting_nav"),
            "reporting_currency": nav.get("reporting_currency"),
            "conversion_receipt": nav.get("conversion_receipt"),
        }
    except FxReportingBlocked as exc:
        ledger = runner.portfolio_manager.get_strategy_ledger(settings.strategy_id, settings.allowed_buckets[0])
        fx_state = {
            "status": "COMBINED_NAV_GAP",
            "receipts": [_receipt_payload(r) for r in receipts],
            "native_nav": ledger.equity,
            "native_currency": ledger.currency,
            "reporting_nav": None,
            "reporting_currency": settings.reporting_currency,
            "reason": str(exc),
        }

    if mode == VERIFY_ONLY:
        status = "WAITING_FOR_APPROVED_INPUT"
        reason = "VERIFICATION_ONLY_NO_MODEL_OR_RUNNER_CALL"
        if receipts:
            reason += "_APPROVED_RESEARCH_STILL_REQUIRED"
        exit_payload = {
            **entry,
            "status": status,
            "completed_at": stamp.isoformat(),
            "reason": reason,
            "fx": fx_state,
            "persisted_config": config,
            "limitations": [
                "Verification-only mode does not invoke the CIO executor, research provider, runner cycle, or broker.",
                "Approved research context is required for authenticated PAPER operation.",
            ],
        }
        _atomic_json(result_path, exit_payload)
        runner.shutdown()
        return exit_payload

    try:
        cycle = runner.run_one_cycle(settings.strategy_id, symbols=list(settings.universe))
        decisions = cycle.get("decisions", [])
        lineage = []
        for decision in decisions:
            inputs = decision.get("inputs") or {}
            frozen = inputs.get("frozen_decision_context")
            context_id = (
                frozen.get("context_id") if isinstance(frozen, dict)
                else inputs.get("frozen_context_id")
            )
            gate = inputs.get("material_delta_gate")
            if context_id:
                lineage.append({
                    "symbol": decision.get("symbol"),
                    "context_id": context_id,
                    "research_digest": frozen.get("research_digest") if isinstance(frozen, dict) else None,
                    "material_gate": gate,
                    "daily_plan_review": bool(inputs.get("daily_plan_review")),
                })
        if not lineage:
            status = "WAITING_FOR_APPROVED_INPUT"
            reason = (decisions[0].get("reason") if decisions else "APPROVED_CONTEXT_NOT_CONSUMED")
        else:
            status = (
                "COMPLETED_TEST_ONLY_PAPER_CANDIDATE"
                if allow_test_only else "COMPLETED_PAPER_CANDIDATE"
            )
            reason = "APPROVED_CONTEXT_CONSUMED_BY_CANONICAL_RUNNER"
        exit_payload = {
            **entry,
            "status": status,
            "completed_at": stamp.isoformat(),
            "reason": reason,
            "provider_invoked": bool(lineage) or any(
                (d.get("inputs") or {}).get("material_delta_gate") is not None for d in decisions
            ),
            "runner_invoked": True,
            "research_lineage": lineage,
            "cycle": cycle,
            "fx": fx_state,
            "persisted_config": config,
            "limitations": [
                "Source-only PAPER candidate; no broker connection or deployment claim.",
                "TEST_ONLY evidence remains TEST_ONLY and cannot establish live acceptance.",
            ],
        }
        _atomic_json(result_path, exit_payload)
        return exit_payload
    finally:
        runner.shutdown()
