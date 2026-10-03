from __future__ import annotations

import hashlib
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
from pydantic import BaseModel, ConfigDict, Field

from cio_market_lab.domain.models import StrategyStatus
from cio_market_lab.strategies.base import BaseStrategy

REQUIRED_MANIFEST_FIELDS = [
    "id",
    "version",
    "name",
    "markets",
    "horizons",
    "required_fields",
    "warmup_bars",
    "session_rules",
    "default_config",
    "risk_class",
    "author",
    "created_at",
]


class StrategyRegistration(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    version: str
    name: str
    manifest: Dict[str, Any]
    config: Dict[str, Any]
    code_hash: str
    status: StrategyStatus = StrategyStatus.CANDIDATE
    folder_path: str
    audit_log: List[Dict[str, Any]] = Field(default_factory=list)
    last_discovered_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class StrategyRegistry:
    """Manages discovery, validation, and candidate-only reload for strategies."""

    def __init__(self, strategies_dir: Path):
        self.strategies_dir = Path(strategies_dir)
        self._registrations: Dict[str, StrategyRegistration] = {}
        self._instances: Dict[str, BaseStrategy] = {}
        self._versions: Dict[str, Dict[str, tuple[StrategyRegistration, BaseStrategy]]] = {}
        self._active_version: Dict[str, str] = {}

    def get_registered(self, strategy_id: str) -> Optional[StrategyRegistration]:
        return self._registrations.get(strategy_id)

    def get_instance(self, strategy_id: str) -> Optional[BaseStrategy]:
        return self._instances.get(strategy_id)

    def list_all(self) -> List[StrategyRegistration]:
        return list(self._registrations.values())

    def _compute_hash(self, folder: Path) -> str:
        hasher = hashlib.sha256()
        files_to_hash = sorted(
            [f for f in folder.glob("**/*") if f.is_file() and not f.name.endswith(".pyc")]
        )
        for f in files_to_hash:
            hasher.update(f.name.encode())
            try:
                hasher.update(f.read_bytes())
            except Exception:
                pass
        return hasher.hexdigest()[:16]

    def _validate_manifest(self, manifest_data: Dict[str, Any]) -> List[str]:
        errors = []
        for field in REQUIRED_MANIFEST_FIELDS:
            if field not in manifest_data:
                errors.append(f"Missing required manifest field: '{field}'")
        return errors

    def _load_strategy_class(self, folder: Path, strat_id: str) -> type:
        py_file = folder / "strategy.py"
        if not py_file.exists():
            raise FileNotFoundError(f"strategy.py not found in {folder}")

        spec = importlib.util.spec_from_file_location(
            f"dynamic_strategy_{strat_id}", py_file
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load spec for {py_file}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        target_class = None
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseStrategy)
                and attr is not BaseStrategy
            ):
                target_class = attr
                break

        if target_class is None:
            raise TypeError(
                f"No subclass of BaseStrategy found in {py_file}"
            )
        return target_class

    def register_or_reload(self, folder: Path) -> StrategyRegistration:
        """Discovers or reloads a strategy directory. Always sets status to CANDIDATE if modified."""
        manifest_file = folder / "manifest.yaml"
        if not manifest_file.exists():
            raise FileNotFoundError(f"manifest.yaml not found in {folder}")

        with open(manifest_file, "r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f)

        errors = self._validate_manifest(manifest)
        if errors:
            raise ValueError(f"Manifest validation failed for {folder}: {', '.join(errors)}")

        strat_id = manifest["id"]
        code_hash = self._compute_hash(folder)
        existing = self._registrations.get(strat_id)

        # An unchanged or restored content hash identifies the exact validated
        # implementation. Do not reinstantiate and overwrite archived versions:
        # discovery alone must neither reset active state nor alter rollback.
        known = self._versions.get(strat_id, {}).get(code_hash)
        if known is not None:
            return known[0]

        # Load default config
        defaults_file = folder / "defaults.yaml"
        default_config = {}
        if defaults_file.exists():
            with open(defaults_file, "r", encoding="utf-8") as f:
                default_config = yaml.safe_load(f) or {}

        config = manifest.get("default_config", default_config)

        # Instantiate strategy
        strat_cls = self._load_strategy_class(folder, strat_id)
        instance = strat_cls(config=config)

        # Validate config with strategy logic
        validation_errs = instance.validate_config(config)
        if validation_errs:
            raise ValueError(f"Strategy {strat_id} config invalid: {validation_errs}")

        now = datetime.now(timezone.utc)
        audit_entry = {
            "timestamp": now.isoformat(),
            "action": "DISCOVERED" if not existing else "RELOADED_AS_CANDIDATE",
            "code_hash": code_hash,
            "version": manifest["version"],
            "by": "system",
        }

        # Candidate reload guarantee:
        # If code or config changed, it MUST NOT auto-activate. Status reverts to CANDIDATE!
        status = StrategyStatus.CANDIDATE
        audit_log = list(existing.audit_log) if existing else []
        audit_log.append(audit_entry)

        reg = StrategyRegistration(
            id=strat_id,
            version=manifest["version"],
            name=manifest["name"],
            manifest=manifest,
            config=config,
            code_hash=code_hash,
            status=status,
            folder_path=str(folder.resolve()),
            audit_log=audit_log,
            last_discovered_at=now,
        )

        # Keep each validated implementation immutable by version/hash. Reloading a
        # changed directory creates a candidate; it never silently replaces active code.
        self._versions.setdefault(strat_id, {})[code_hash] = (reg, instance)
        if existing is None or existing.status != StrategyStatus.PAPER_ACTIVE:
            self._registrations[strat_id] = reg
            self._instances[strat_id] = instance
        return reg


    def hot_swap_strategy(self, strategy_id: str, code_hash: str, authority: str = "Main CIO") -> StrategyRegistration:
        """Atomically promote a previously validated candidate; source objects are untouched."""
        if authority != "Main CIO":
            raise PermissionError("Only Main CIO may hot-swap a strategy")
        versions = self._versions.get(strategy_id, {})
        if code_hash not in versions:
            raise KeyError(f"Validated strategy version '{code_hash}' not found")
        candidate, instance = versions[code_hash]
        if candidate.status != StrategyStatus.CANDIDATE:
            raise ValueError("Hot-swap target must be a validated CANDIDATE")
        previous = self._active_version.get(strategy_id)
        if previous and previous in versions:
            versions[previous][0].status = StrategyStatus.PAUSED
        candidate.status = StrategyStatus.PAPER_ACTIVE
        candidate.audit_log.append({"timestamp": datetime.now(timezone.utc).isoformat(), "action": "HOT_SWAPPED", "authority": authority, "previous_code_hash": previous, "code_hash": code_hash})
        self._registrations[strategy_id] = candidate
        self._instances[strategy_id] = instance
        self._active_version[strategy_id] = code_hash
        return candidate

    def rollback_strategy(self, strategy_id: str, code_hash: str, authority: str = "Main CIO") -> StrategyRegistration:
        """Restore an exact previously validated version without mutating data adapters."""
        if authority != "Main CIO":
            raise PermissionError("Only Main CIO may roll back a strategy")
        versions = self._versions.get(strategy_id, {})
        if code_hash not in versions:
            raise KeyError(f"Rollback version '{code_hash}' not found")
        target, instance = versions[code_hash]
        previous = self._active_version.get(strategy_id)
        if previous and previous in versions:
            versions[previous][0].status = StrategyStatus.PAUSED
        target.status = StrategyStatus.PAPER_ACTIVE
        target.audit_log.append({"timestamp": datetime.now(timezone.utc).isoformat(), "action": "ROLLED_BACK", "authority": authority, "previous_code_hash": previous, "code_hash": code_hash})
        self._registrations[strategy_id] = target
        self._instances[strategy_id] = instance
        self._active_version[strategy_id] = code_hash
        return target

    def discover_all(self) -> List[StrategyRegistration]:
        """Scans strategies_dir and registers all valid strategies."""
        results = []
        if not self.strategies_dir.exists():
            return results

        for child in self.strategies_dir.iterdir():
            if child.is_dir() and (child / "manifest.yaml").exists():
                try:
                    reg = self.register_or_reload(child)
                    results.append(reg)
                except Exception as e:
                    # Log or record error
                    pass
        return results

    def activate_strategy(
        self, strategy_id: str, authority: str = "Main CIO"
    ) -> StrategyRegistration:
        """Explicit activation gate. Authority must be Main CIO."""
        reg = self._registrations.get(strategy_id)
        if not reg:
            raise KeyError(f"Strategy {strategy_id} not registered")

        if authority != "Main CIO":
            raise PermissionError("Only Main CIO may activate a strategy")
        if reg.code_hash not in self._versions.get(strategy_id, {}):
            self._versions.setdefault(strategy_id, {})[reg.code_hash] = (reg, self._instances[strategy_id])
        reg.status = StrategyStatus.PAPER_ACTIVE
        self._active_version[strategy_id] = reg.code_hash
        reg.audit_log.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": "ACTIVATED",
                "authority": authority,
                "version": reg.version,
                "code_hash": reg.code_hash,
            }
        )
        return reg

    def pause_strategy(
        self, strategy_id: str, authority: str = "Main CIO"
    ) -> StrategyRegistration:
        reg = self._registrations.get(strategy_id)
        if not reg:
            raise KeyError(f"Strategy {strategy_id} not registered")

        if authority != "Main CIO":
            raise PermissionError("Only Main CIO may pause a strategy")
        reg.status = StrategyStatus.PAUSED
        reg.audit_log.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": "PAUSED",
                "authority": authority,
                "version": reg.version,
            }
        )
        return reg
