from pathlib import Path
import shutil
import yaml
import pytest
from cio_market_lab.strategies.registry import StrategyRegistry


def test_candidate_reload_hot_swap_rollback_preserves_source(tmp_path):
    source = Path(__file__).resolve().parents[1] / "strategies" / "opening_range_breakout"
    strategy_dir = tmp_path / "orb"
    shutil.copytree(source, strategy_dir)
    registry = StrategyRegistry(tmp_path)
    v1 = registry.register_or_reload(strategy_dir)
    active1 = registry.activate_strategy(v1.id)
    original_adapter = object()
    source_before = original_adapter
    old_instance = registry.get_instance(v1.id)
    manifest_path = strategy_dir / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["version"] = "9.9.9"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    v2 = registry.register_or_reload(strategy_dir)
    assert v2.status.value == "CANDIDATE"
    assert registry.get_instance(v1.id) is old_instance
    assert registry.get_registered(v1.id).code_hash == v1.code_hash
    registry.hot_swap_strategy(v1.id, v2.code_hash)
    assert registry.get_registered(v1.id).code_hash == v2.code_hash
    assert registry.get_instance(v1.id) is registry._versions[v1.id][v2.code_hash][1]
    registry.rollback_strategy(v1.id, v1.code_hash)
    assert registry.get_registered(v1.id).code_hash == v1.code_hash
    assert registry.get_instance(v1.id) is old_instance
    assert original_adapter is source_before
    actions = [x["action"] for x in registry.get_registered(v1.id).audit_log]
    assert "ROLLED_BACK" in actions


def test_hot_swap_and_rollback_require_main_cio(tmp_path):
    source = Path(__file__).resolve().parents[1] / "strategies" / "opening_range_breakout"
    strategy_dir = tmp_path / "orb"
    shutil.copytree(source, strategy_dir)
    registry = StrategyRegistry(tmp_path)
    reg = registry.register_or_reload(strategy_dir)
    with pytest.raises(PermissionError):
        registry.hot_swap_strategy(reg.id, reg.code_hash, authority="worker")
    with pytest.raises(PermissionError):
        registry.rollback_strategy(reg.id, reg.code_hash, authority="worker")
