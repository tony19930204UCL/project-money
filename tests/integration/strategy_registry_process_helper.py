from __future__ import annotations

import argparse
import json
from pathlib import Path

from cio_market_lab.api.app import create_app
from tests.browser.server_helper import TestOnlyMarketAdapter


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args()

    adapter = TestOnlyMarketAdapter()
    app = create_app(
        workspace_root=args.workspace_root,
        runtime_dir=args.runtime_dir,
        fixture_mode=False,
        is_read_only=True,
        market_adapter=adapter,
    )
    state = app.state.app_state
    rows = {}
    for reg in state.registry.list_all():
        rows[reg.id] = {
            "version": reg.version,
            "code_hash": reg.code_hash,
            "status": reg.status.value,
            "audit_log": reg.audit_log,
            "active_code_hash": state.registry._active_version.get(reg.id),
        }
    payload = {
        "registry": rows,
        "adapter": {
            "source_name": state.market_adapter.source_name,
            "runner_same_adapter": state.runner.market_adapter is state.market_adapter,
        },
        "provider": {
            "provider_id": state.cio_executor.provider_id,
            "model_id": state.cio_executor.model_id,
            "session_id": state.cio_executor.session_id,
            "runner_same_executor": state.runner.cio_executor is state.cio_executor,
        },
        "orders": [
            order.model_dump(mode="json") for order in state.paper_orders.all_orders()
        ],
        "fills": [
            fill.model_dump(mode="json")
            for bucket in state.portfolio_manager.get_all_portfolios().values()
            for fill in bucket.fills
        ],
    }
    print(json.dumps(payload, sort_keys=True))
    state.runner.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
