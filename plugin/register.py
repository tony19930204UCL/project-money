"""Hermes Desktop Server Plugin Registration Candidate for CIO Market Lab.

Provides the plugin manifest discovery, server lifecycle hooks, health probe,
and inspectable chat draft bridge registration for Hermes Desktop.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional
import urllib.error
import urllib.request

PLUGIN_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = PLUGIN_DIR / "plugin.json"


def load_manifest() -> Dict[str, Any]:
    """Loads and validates plugin.json manifest."""
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"Manifest not found: {MANIFEST_PATH}")
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    # Validate required fields for Hermes Desktop server plugin
    required = ["id", "name", "version", "plugin_type", "server", "ui", "safety"]
    for r in required:
        if r not in manifest:
            raise ValueError(f"Plugin manifest missing required field: {r}")

    # Ensure paper-only safety constraints
    safety = manifest.get("safety", {})
    if not safety.get("simulation_only") or not safety.get("paper_execution"):
        raise ValueError("Plugin must be configured strictly as simulation_only and paper_execution")
    if safety.get("broker_credentials") is not False:
        raise ValueError("Plugin must not allow broker credentials")
    if safety.get("autonomous_capital_decisions") is not False:
        raise ValueError("Plugin must strictly forbid autonomous capital decisions")

    return manifest


def check_server_health(host: str = "127.0.0.1", port: int = 8765) -> Dict[str, Any]:
    """Queries the running FastAPI engine's health endpoint."""
    url = f"http://{host}:{port}/api/health"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "HermesDesktop/PluginProbe"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {"online": True, "data": data}
    except Exception as ex:
        return {"online": False, "error": str(ex)}


def register(context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Registration hook called by Hermes Desktop plugin host."""
    manifest = load_manifest()
    server_conf = manifest["server"]
    host = server_conf.get("host", "127.0.0.1")
    port = server_conf.get("port", 8765)
    health = check_server_health(host=host, port=port)

    return {
        "status": "registered",
        "plugin_id": manifest["id"],
        "name": manifest["name"],
        "version": manifest["version"],
        "manifest": manifest,
        "server_status": "online" if health["online"] else "offline_or_pending",
        "health": health,
        "ui_entry": str((PLUGIN_DIR / manifest["ui"]["entry"]).resolve()),
        "safety": manifest["safety"],
    }


if __name__ == "__main__":
    import sys

    print("Checking CIO Market Lab plugin registration...")
    reg_info = register()
    print(json.dumps(reg_info, indent=2))
    sys.exit(0)
