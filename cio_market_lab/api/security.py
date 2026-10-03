"""Security and port boundary helpers for CIO Market Lab API."""
from __future__ import annotations

import os
import sys
from typing import Any, List, Optional, Union
from fastapi import HTTPException, Request


def is_read_only_role(
    port: Optional[Union[int, str]] = None,
    argv: Optional[List[str]] = None,
) -> bool:
    """Determine if process or port is in read-only mode."""
    # 1. Explicit CIO_ROLE environment variable
    role_env = os.environ.get("CIO_ROLE", "").strip().lower()
    if role_env in ("read_only", "readonly", "observer"):
        return True
    if role_env in ("owner", "writer", "primary"):
        return False

    # 2. CIO_AUTONOMOUS_RUNNER_OWNER environment variable
    env_owner = os.environ.get("CIO_AUTONOMOUS_RUNNER_OWNER")
    if env_owner == "0":
        return True
    if env_owner == "1":
        return False

    # 3. Port environment variable (PORT or CIO_PORT)
    port_env = str(port or os.environ.get("PORT") or os.environ.get("CIO_PORT") or "").strip()
    if port_env == "8765":
        return True
    if port_env == "21322":
        return False

    # 4. Check sys.argv for --port
    args = argv if argv is not None else sys.argv
    for i, arg in enumerate(args):
        if arg == "--port" and i + 1 < len(args):
            p = args[i + 1].strip()
            if p == "8765":
                return True
            if p == "21322":
                return False
        elif arg.startswith("--port="):
            p = arg.split("=", 1)[1].strip()
            if p == "8765":
                return True
            if p == "21322":
                return False

    # 5. Default when port/role cannot be established: writer/owner (not read-only)
    return False


def is_owner_port(request: Optional[Request] = None) -> bool:
    """Determine if current request or environment is running on the owner port (21322)."""
    # 1. Explicit negative owner flag
    env_owner = os.environ.get("CIO_AUTONOMOUS_RUNNER_OWNER")
    if env_owner == "0":
        return False

    # 2. Explicit read-only role
    role_env = os.environ.get("CIO_ROLE", "").strip().lower()
    if role_env in ("read_only", "readonly", "observer"):
        return False

    # 3. Explicit read-only port env
    port_env = str(os.environ.get("PORT") or os.environ.get("CIO_PORT") or "").strip()
    if port_env == "8765":
        return False

    # 4. If request is provided, inspect request URL port
    if request is not None:
        try:
            if request.url.port == 8765:
                return False
            if request.url.port == 21322:
                if hasattr(request, "app"):
                    app_state = getattr(request.app.state, "app_state", None)
                    if app_state is not None and getattr(app_state, "is_read_only", False):
                        return False
                    if getattr(request.app.state, "is_read_only", False):
                        return False
                return True
        except Exception:
            pass

    # 5. Check if app instance is explicitly read-only
    if request is not None and hasattr(request, "app"):
        app_state = getattr(request.app.state, "app_state", None)
        if app_state is not None and getattr(app_state, "is_read_only", False):
            return False
        if getattr(request.app.state, "is_read_only", False):
            return False

    # 6. Check if port env is owner port 21322
    if port_env == "21322":
        return True

    # 7. Check if explicit owner role or owner flag
    if env_owner == "1" or role_env in ("owner", "writer", "primary"):
        return True

    # 8. Check sys.argv for --port
    for i, arg in enumerate(sys.argv):
        if arg == "--port" and i + 1 < len(sys.argv):
            p = sys.argv[i + 1].strip()
            if p == "21322":
                return True
            if p == "8765":
                return False
        elif arg.startswith("--port="):
            p = arg.split("=", 1)[1].strip()
            if p == "21322":
                return True
            if p == "8765":
                return False

    # 9. Default: Unknown or no port defaults to read-only (mutations blocked)
    return False


def assert_owner_port(request: Optional[Request] = None, resource: str = "Mutations") -> None:
    """Raise 403 HTTPException if request is not on owner port 21322."""
    if not is_owner_port(request):
        raise HTTPException(
            status_code=403,
            detail=f"MUTATION_FORBIDDEN_ON_READONLY_PORT: Port 8765 is read-only. {resource} are permitted only on owner port 21322.",
        )
