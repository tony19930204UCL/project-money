"""TEST_ONLY persisted packet coverage; never uses network or inference."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from cio_market_lab.engine.cio_session import CIOSessionHistory, MaterialDeltaGate
from cio_market_lab.engine.stage_d_observation import (
    PacketUnavailableError,
    PersistedResearchPacketLoader,
    build_canonical_observation,
)

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def packet(**updates):
    value = {
        "research_id": "nvda-ir-note-test-001",
        "symbol": "NVDA",
        "market": "US",
        "source_url": "https://nvidianews.nvidia.com/news/example",
        "source_tier": "official_company_ir",
        "observed_at": (NOW - timedelta(minutes=15)).isoformat(),
        "verified": True,
        "verification_status": "verified",
        "verified_facts": ["TEST_ONLY fact; not market evidence"],
        "thesis": "TEST_ONLY thesis",
        "buy_zone": {"low": 100, "high": 105},
        "invalidation": "TEST_ONLY close below 90",
    }
    value.update(updates)
    return value


def test_packet_loader_and_canonical_identity_ignore_timestamp_noise(tmp_path, monkeypatch):
    monkeypatch.setenv("CIO_STAGE_D_OFFICIAL_HOSTS", "nvidianews.nvidia.com")
    p = packet(is_fixture=False, test_only=True)
    # Fixture-only files cannot become accepted research even when signed-looking.
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(p), encoding="utf-8")
    with pytest.raises(PacketUnavailableError, match="TEST_ONLY"):
        PersistedResearchPacketLoader(tmp_path).load("NVDA", NOW)

    p["test_only"] = True
    path.write_text(json.dumps(p), encoding="utf-8")
    loaded = PersistedResearchPacketLoader(tmp_path, allow_fixture=True).load("NVDA", NOW)
    obs1, ctx1 = build_canonical_observation(loaded, "NVDA", "stable-session")
    p["observed_at"] = (NOW - timedelta(minutes=2)).isoformat()
    path.write_text(json.dumps(p), encoding="utf-8")
    loaded2 = PersistedResearchPacketLoader(tmp_path, allow_fixture=True).load("NVDA", NOW)
    obs2, ctx2 = build_canonical_observation(loaded2, "NVDA", "stable-session")
    assert obs1["official_material_ids"] == obs2["official_material_ids"]
    assert ctx1.context_id == ctx2.context_id
    assert obs2["packet_observed_at"] != obs1["packet_observed_at"]


def test_missing_stale_unverified_wrong_host_and_symbol_fail_closed(tmp_path):
    loader = PersistedResearchPacketLoader(tmp_path)
    with pytest.raises(PacketUnavailableError, match="no persisted"):
        loader.load("NVDA", NOW)
    path = tmp_path / "packet.json"
    cases = [
        packet(observed_at=(NOW - timedelta(days=3)).isoformat()),
        packet(verified=False),
        packet(verification_status="unverified"),
        packet(source_url="https://notnvidianews.nvidia.com.attacker.example/a"),
        packet(symbol="AAPL"),
    ]
    for bad in cases:
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(PacketUnavailableError):
            loader.load("NVDA", NOW)


def test_material_gate_idle_restart_and_material_change_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("CIO_STAGE_D_OFFICIAL_HOSTS", "nvidianews.nvidia.com")
    p = packet(is_fixture=True, test_only=True)
    # This is deliberately marked TEST_ONLY in fact text and the file is used only
    # for deterministic engineering verification, never as a live acceptance.
    p["verified_facts"] = ["TEST_ONLY assertion for regression fixture"]
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(p), encoding="utf-8")
    loaded = PersistedResearchPacketLoader(tmp_path, allow_fixture=True).load("NVDA", NOW)
    obs, context = build_canonical_observation(loaded, "NVDA", "fixture-session")
    store = CIOSessionHistory(tmp_path / "sessions", "fixture-session")
    store.freeze(context)
    gate = MaterialDeltaGate(store)
    assert gate.evaluate(obs)["should_call"] is True
    gate.record_call(obs, {"success": True, "fixture": True})
    noisy = {**obs, "quote": 101.5, "timestamp": "noise", "packet_observed_at": "refreshed"}
    assert gate.evaluate(noisy)["status"] == "DUPLICATE"
    restarted = CIOSessionHistory(tmp_path / "sessions", "fixture-session")
    assert restarted.load_context().context_id == context.context_id
    assert len(restarted.history()) == 2
    changed = dict(obs, official_material_ids=["new-material-id"])
    assert MaterialDeltaGate(restarted).evaluate(changed)["should_call"] is True


def test_arbitrary_verified_packet_cannot_self_attest(tmp_path, monkeypatch):
    monkeypatch.setenv("CIO_STAGE_D_OFFICIAL_HOSTS", "nvidianews.nvidia.com")
    (tmp_path / "packet.json").write_text(json.dumps(packet()))
    with pytest.raises(PacketUnavailableError, match="independent trusted"):
        PersistedResearchPacketLoader(tmp_path).load("NVDA", NOW)
    with pytest.raises(PacketUnavailableError, match="outside"):
        PersistedResearchPacketLoader(tmp_path, trusted_manifest=tmp_path / "manifest.json")


def test_external_approval_binds_packet_and_raw_source(tmp_path, monkeypatch):
    import hashlib
    monkeypatch.setenv("CIO_STAGE_D_OFFICIAL_HOSTS", "nvidianews.nvidia.com")
    packets = tmp_path / "packets"; packets.mkdir()
    trust = tmp_path / "trust"; trust.mkdir()
    value = packet(source_excerpts=["TEST_ONLY captured official text"])
    source = trust / "source.txt"; source.write_text(value["source_excerpts"][0])
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    manifest = trust / "approved.json"
    manifest.write_text(json.dumps({"approved_packets":{digest:{"approved_by":"MAIN_CIO", "source_verified":True,
        "source_url":value["source_url"], "capture_file":"source.txt", "capture_sha256":hashlib.sha256(source.read_bytes()).hexdigest()}}}))
    path = packets / "packet.json"; path.write_text(json.dumps(value))
    loader = PersistedResearchPacketLoader(packets, trusted_manifest=manifest)
    assert loader.load("NVDA", NOW)["symbol"] == "NVDA"  # TEST_ONLY approval fixture, not real source acceptance.
    source.write_text("tampered")
    with pytest.raises(PacketUnavailableError, match="digest mismatch"):
        loader.load("NVDA", NOW)
    value["verified_facts"] = ["tampered claim"]
    path.write_text(json.dumps(value))
    with pytest.raises(PacketUnavailableError, match="not approved"):
        loader.load("NVDA", NOW)


def test_verified_provider_is_used_by_canonical_runner_across_restart(tmp_path, monkeypatch):
    import hashlib
    from tests.test_runtime_continuity import build_harness
    from tests.test_cio_gate_canonical import CountingExecutor
    from cio_market_lab.engine.stage_d_observation import make_packet_observation_provider
    packets=tmp_path/"packets";packets.mkdir()
    trust=tmp_path/"trust";trust.mkdir()
    now=datetime(2026,9,28,2,0,tzinfo=timezone.utc)
    value=packet(symbol="2330.TW",market="TW",source_url="https://mops.twse.com.tw/announcement",
                 observed_at=now.isoformat(),session_date="2026-09-28",source_excerpts=["TEST_ONLY official excerpt"])
    capture=trust/"capture.txt";capture.write_text(value["source_excerpts"][0])
    digest=hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",", ":"),ensure_ascii=False).encode()).hexdigest()
    manifest=trust/"approval.json"
    manifest.write_text(json.dumps({"approved_packets":{digest:{"approved_by":"MAIN_CIO","source_verified":True,
        "source_url":value["source_url"],"capture_file":"capture.txt","capture_sha256":hashlib.sha256(capture.read_bytes()).hexdigest()}}}))
    path=packets/"packet.json";path.write_text(json.dumps(value))
    executor=CountingExecutor(now)
    def make():
        runner,_,_,adapter=build_harness(tmp_path,[now])
        adapter.set_bar("2330.TW",now,995,1005,990,1000)
        runner.cio_executor=executor
        runner.material_gate_enabled=True
        runner.material_observation_provider=make_packet_observation_provider(packets,trusted_manifest=manifest)
        return runner,adapter
    def call(runner,adapter):
        return runner._run_cio_decision_path("fixture",runner.paper_orders.experiment_for("dynamic-desk"),"2330.TW",adapter.get_latest_bar("2330.TW"),{})
    runner,adapter=make();call(runner,adapter);call(runner,adapter)
    assert len(executor.requests)==1
    bundle=executor.requests[0].prior_lessons[-1]
    assert "2330.TW-2026-09-28" in bundle["frozen_context"]["context_id"]
    assert bundle["material_observation"]["verified_facts"]==value["verified_facts"]
    runner,adapter=make();call(runner,adapter)
    assert len(executor.requests)==1
    capture.write_text("tampered raw evidence")
    call(runner,adapter)
    assert len(executor.requests)==1
