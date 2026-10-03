"""Candidate-only durable strategy snapshots; no broker/data-source mutation."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from cio_market_lab.strategies.registry import StrategyRegistry, StrategyRegistration


class DurableStrategyRegistry(StrategyRegistry):
    """Retain validated code and lifecycle pointers, restoring exact code on restart.

    The read-only observer neither discovers executable code nor writes snapshots.
    Mutable source changes always remain candidates until explicit CIO promotion.
    """
    def __init__(self, strategies_dir: Path, state_dir: Path, read_only: bool = False):
        super().__init__(strategies_dir)
        self.state_dir = Path(state_dir).resolve()
        self.read_only = read_only
        self._snapshots = {}
        self._restore()

    def _load_strategy_class(self, folder: Path, strat_id: str):
        # Compile retained source directly: importlib's source loader can create
        # __pycache__ even in an observer, violating the no-runtime-write contract.
        import types
        from cio_market_lab.strategies.base import BaseStrategy
        source = folder / 'strategy.py'
        module = types.ModuleType(f'dynamic_strategy_{strat_id}')
        module.__file__ = str(source)
        exec(compile(source.read_bytes(), str(source), 'exec'), module.__dict__)
        for name in sorted(module.__dict__):
            cls = module.__dict__[name]
            if isinstance(cls, type) and issubclass(cls, BaseStrategy) and cls is not BaseStrategy:
                return cls
        raise TypeError(f'No subclass of BaseStrategy found in {source}')

    def _owner(self):
        if self.read_only:
            raise PermissionError('Read-only registry cannot mutate lifecycle state')

    @contextmanager
    def _transaction(self):
        self._owner()
        registrations = dict(self._registrations)
        instances = dict(self._instances)
        versions = {sid: dict(v) for sid, v in self._versions.items()}
        active = dict(self._active_version)
        snapshots = dict(self._snapshots)
        metadata = [(reg, reg.model_copy(deep=True)) for vs in versions.values() for reg, _ in vs.values()]
        try:
            yield
            self._persist()
        except Exception:
            for reg, saved in metadata:
                reg.status = saved.status
                reg.audit_log = saved.audit_log
            self._registrations, self._instances = registrations, instances
            self._versions, self._active_version = versions, active
            self._snapshots = snapshots
            raise

    def _persist(self):
        self._owner()
        data = {'schema_version': 1, 'active': self._active_version,
                'selected': {sid: reg.code_hash for sid, reg in self._registrations.items()},
                'versions': {sid: {code: {'registration': reg.model_dump(mode='json'),
                    'snapshot': self._snapshots[(sid, code)]} for code, (reg, _) in versions.items()}
                    for sid, versions in self._versions.items()}}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.registry-', dir=self.state_dir)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(data, output, sort_keys=True, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.state_dir / 'registry.json')
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _restore(self):
        path = self.state_dir / 'registry.json'
        if not path.exists():
            return
        data = json.loads(path.read_text())
        if data.get('schema_version') != 1:
            raise ValueError('UNSUPPORTED_REGISTRY_SCHEMA')
        # Verify ALL snapshots before importing any executable plugin.
        validated = []
        for sid, versions in data['versions'].items():
            for code, entry in versions.items():
                folder = (self.state_dir / entry['snapshot']).resolve()
                if not folder.is_relative_to(self.state_dir) or folder == self.state_dir:
                    raise ValueError('INVALID_SNAPSHOT_PATH')
                if self._compute_hash(folder) != code:
                    raise ValueError('SNAPSHOT_HASH_MISMATCH')
                reg = StrategyRegistration.model_validate(entry['registration'])
                if reg.id != sid or reg.code_hash != code or Path(reg.folder_path).resolve() != folder:
                    raise ValueError('INVALID_REGISTRY_VERSION_METADATA')
                validated.append((sid, code, entry['snapshot'], folder, reg))
        for sid, code, snapshot, folder, reg in validated:
            cls = self._load_strategy_class(folder, sid)
            instance = cls(config=reg.config)
            if instance.validate_config(reg.config):
                raise ValueError('RESTORED_STRATEGY_CONFIG_INVALID')
            self._versions.setdefault(sid, {})[code] = (reg, instance)
            self._snapshots[(sid, code)] = snapshot
        for sid, code in data['selected'].items():
            reg, instance = self._versions[sid][code]
            self._registrations[sid], self._instances[sid] = reg, instance
        self._active_version = data['active']
        for sid, code in self._active_version.items():
            if data['selected'].get(sid) != code:
                raise ValueError('INVALID_ACTIVE_VERSION_POINTER')

    def register_or_reload(self, folder: Path):
        self._owner()
        folder = Path(folder).resolve()
        if any(p.is_symlink() for p in folder.rglob('*')):
            raise ValueError('STRATEGY_SOURCE_SYMLINK_NOT_ALLOWED')
        import yaml
        manifest = yaml.safe_load((folder / 'manifest.yaml').read_text())
        sid = manifest['id']
        code = self._compute_hash(folder)
        if code in self._versions.get(sid, {}):
            return self._versions[sid][code][0]
        name = hashlib.sha256(str(sid).encode()).hexdigest()[:16] + '-' + code
        snapshot = self.state_dir / 'versions' / name
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if not snapshot.exists():
            temp = Path(tempfile.mkdtemp(prefix='.snapshot-', dir=snapshot.parent))
            try:
                shutil.copytree(folder, temp, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                if self._compute_hash(temp) != code:
                    raise ValueError('STRATEGY_CHANGED_DURING_CAPTURE')
                os.rename(temp, snapshot)
            finally:
                if temp.exists():
                    shutil.rmtree(temp)
        if self._compute_hash(snapshot) != code:
            raise ValueError('SNAPSHOT_HASH_MISMATCH')
        with self._transaction():
            reg = super().register_or_reload(snapshot)
            self._snapshots[(sid, code)] = str(snapshot.relative_to(self.state_dir))
        return reg

    def activate_strategy(self, strategy_id, authority='Main CIO'):
        with self._transaction():
            reg = super().activate_strategy(strategy_id, authority)
        return reg

    def pause_strategy(self, strategy_id, authority='Main CIO'):
        with self._transaction():
            reg = super().pause_strategy(strategy_id, authority)
        return reg

    def hot_swap_strategy(self, strategy_id, code_hash, authority='Main CIO'):
        with self._transaction():
            reg = super().hot_swap_strategy(strategy_id, code_hash, authority)
        return reg

    def rollback_strategy(self, strategy_id, code_hash, authority='Main CIO'):
        with self._transaction():
            reg = super().rollback_strategy(strategy_id, code_hash, authority)
        return reg

    def discover_all(self):
        if self.read_only:
            return self.list_all()
        return super().discover_all()
