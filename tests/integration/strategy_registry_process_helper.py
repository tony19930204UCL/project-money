from __future__ import annotations

import argparse
import json
from pathlib import Path

from cio_market_lab.strategies.durable_registry import DurableStrategyRegistry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategies-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    registry = DurableStrategyRegistry(args.strategies_dir, args.state_dir)
    rows = {}
    for reg in registry.list_all():
        rows[reg.id] = {
            "version": reg.version,
            "code_hash": reg.code_hash,
            "status": reg.status.value,
            "audit_log": reg.audit_log,
            "active_code_hash": registry._active_version.get(reg.id),
        }
    print(json.dumps(rows, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
