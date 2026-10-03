"""Regression test suite for single-path research inlet producer/consumer integration.

Verifies:
1. POST /api/research/intake with genuine public structured evidence -> persisted to canonical
   research inbox -> consumed by /api/paper/cio/learning-context -> survives service restart.
2. Negative test: Test fixtures and synthetic data are strictly rejected and never enter model facts.
3. Negative test: Stale (> 7 days) and future-dated evidence are strictly rejected.
4. Raw unverified documents are staged to raw adapter but rejected from canonical learning context
   (never auto-certified into verified facts).
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from cio_market_lab.api.app import create_app
from cio_market_lab.domain.models import Bar, Market
from cio_market_lab.engine.autonomous_runner import AutonomousPaperRunner, DurableQuoteSnapshot


class MockMarketAdapter:
    def __init__(self):
        self.last_fetch_mode = "test_mock"
        self.last_error = None

    def get_bars(self, symbol, timeframe="1D", limit=80):
        now = datetime.now(timezone.utc)
        return [
            Bar(
                symbol=symbol,
                timestamp=now - timedelta(days=1),
                observed_at=now,
                open=100.0,
                high=105.0,
                low=99.0,
                close=104.0,
                volume=1000,
                source="test",
                quality="good",
            )
        ]

    def timeframe_metadata(self, timeframe):
        return {"timeframe": timeframe, "interval": "1d"}


def test_positive_post_structured_evidence_to_learning_context_and_restart(tmp_path: Path):
    """POST verified public structured evidence persists in canonical inbox and survives restart."""
    workspace_root = Path(__file__).resolve().parent.parent
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir = runtime_dir / "research_inbox"

    app = create_app(
        workspace_root=workspace_root,
        runtime_dir=runtime_dir,
        is_read_only=False,
        market_adapter=MockMarketAdapter(),
        fixture_mode=False,
    )
    client = TestClient(app, base_url="http://localhost:21322")

    now = datetime.now(timezone.utc)
    observed_str = (now - timedelta(minutes=10)).isoformat()

    nvda_payload = {
        "research_id": "nvda-repurchase-20260928-official",
        "symbol": "NVDA",
        "source_url": "https://nvidianews.nvidia.com/news/nvidia-announces-a-150-billion-share-repurchase-authorization-increase",
        "source_tier": "official_company_ir",
        "observed_at": observed_str,
        "published_at": "2026-09-28",
        "is_fixture": False,
        "verification_status": "verified",
        "verified_facts": [
            "NVIDIA announced a USD150 billion increase to its share repurchase authorization.",
            "Company reports total remaining authorization of USD235 billion.",
            "Company expects execution through fiscal year 2028.",
        ],
        "research_scope": "event_input_only_not_order",
        "limitations": [
            "No execution pace guaranteed.",
            "No valuation or price evidence in this release; no buy inference from authorization alone.",
        ],
        "raw_metadata": {
            "verification": "Official company press release verified HTTP 200",
        },
    }

    # 1. POST producer
    resp = client.post("/api/research/intake", json=nvda_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["verified"] is True
    assert data["verification_reason"] == "VERIFIED"

    # Verify persisted to canonical research inbox on disk
    persisted_file = inbox_dir / "nvda-repurchase-20260928-official.json"
    assert persisted_file.exists()

    # 2. Verify /api/paper/cio/learning-context returns it
    ctx_resp = client.get("/api/paper/cio/learning-context?symbols=NVDA,TSLA")
    assert ctx_resp.status_code == 200
    ctx = ctx_resp.json()

    assert len(ctx["verified_research"]) == 1
    ev = ctx["verified_research"][0]
    assert ev["research_id"] == "nvda-repurchase-20260928-official"
    assert ev["symbol"] == "NVDA"
    assert ev["source_tier"] == "official_company_ir"
    assert len(ev["verified_facts"]) == 3
    assert ev["is_fixture"] is False

    # TSLA has no verified research -> explicit research gap
    research_gaps = [g for g in ctx["research_gaps"] if g.get("gap_status") == "EXPLICIT_RESEARCH_GAP"]
    assert len(research_gaps) == 1
    assert research_gaps[0]["symbol"] == "TSLA"
    assert research_gaps[0]["gap_status"] == "EXPLICIT_RESEARCH_GAP"

    # 3. SERVICE RESTART: Instantiate a new app using the same runtime directory
    app_restarted = create_app(
        workspace_root=workspace_root,
        runtime_dir=runtime_dir,
        is_read_only=False,
        market_adapter=MockMarketAdapter(),
        fixture_mode=False,
    )
    client_restarted = TestClient(app_restarted, base_url="http://localhost:21322")

    # After restart, the canonical reader scans the inbox and loads the persisted evidence
    restarted_ctx_resp = client_restarted.get("/api/paper/cio/learning-context?symbols=NVDA,TSLA")
    assert restarted_ctx_resp.status_code == 200
    restarted_ctx = restarted_ctx_resp.json()

    assert len(restarted_ctx["verified_research"]) == 1
    assert restarted_ctx["verified_research"][0]["research_id"] == "nvda-repurchase-20260928-official"
    assert restarted_ctx["verified_research"][0]["symbol"] == "NVDA"
    restarted_research_gaps = [g for g in restarted_ctx["research_gaps"] if g.get("gap_status") == "EXPLICIT_RESEARCH_GAP"]
    assert len(restarted_research_gaps) == 1
    assert restarted_research_gaps[0]["symbol"] == "TSLA"

    # /api/research/inbox also returns the verified item
    inbox_resp = client_restarted.get("/api/research/inbox")
    assert inbox_resp.status_code == 200
    inbox_items = inbox_resp.json()
    assert any(i.get("research_id") == "nvda-repurchase-20260928-official" for i in inbox_items)


def test_negative_post_fixture_and_synthetic_evidence_rejected(tmp_path: Path):
    """POSTing test fixture or synthetic evidence is strictly rejected from canonical facts."""
    workspace_root = Path(__file__).resolve().parent.parent
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir = runtime_dir / "research_inbox"

    app = create_app(
        workspace_root=workspace_root,
        runtime_dir=runtime_dir,
        is_read_only=False,
        market_adapter=MockMarketAdapter(),
        fixture_mode=False,
    )
    client = TestClient(app, base_url="http://localhost:21322")

    now = datetime.now(timezone.utc)

    fixture_payload = {
        "research_id": "fixture-nvda-announcement",
        "symbol": "NVDA",
        "source_url": "https://example.com/mock-ir",
        "source_tier": "official_company_ir",
        "observed_at": now.isoformat(),
        "is_fixture": True,  # Labeled fixture
        "verification_status": "verified",
        "verified_facts": ["Synthetic revenue increase"],
    }

    resp = client.post("/api/research/intake", json=fixture_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["verified"] is False
    assert "REJECTED_FIXTURE" in data["verification_reason"]

    # Must NOT be persisted in inbox directory
    assert not (inbox_dir / "fixture-nvda-announcement.json").exists()

    # Must NOT enter model learning context facts
    ctx_resp = client.get("/api/paper/cio/learning-context?symbols=NVDA")
    assert ctx_resp.status_code == 200
    ctx = ctx_resp.json()
    assert len(ctx["verified_research"]) == 0
    research_gaps = [g for g in ctx["research_gaps"] if g.get("gap_status") == "EXPLICIT_RESEARCH_GAP"]
    assert len(research_gaps) == 1
    assert research_gaps[0]["symbol"] == "NVDA"
    assert research_gaps[0]["gap_status"] == "EXPLICIT_RESEARCH_GAP"

    # Appears in rejected research audit log
    rej_resp = client.get("/api/research/rejected")
    assert rej_resp.status_code == 200
    rej = rej_resp.json()
    assert any("REJECTED_FIXTURE" in r.get("reason", "") for r in rej)


def test_negative_post_stale_and_future_evidence_rejected(tmp_path: Path):
    """POSTing stale or future-dated evidence is strictly rejected."""
    workspace_root = Path(__file__).resolve().parent.parent
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    app = create_app(
        workspace_root=workspace_root,
        runtime_dir=runtime_dir,
        is_read_only=False,
        market_adapter=MockMarketAdapter(),
        fixture_mode=False,
    )
    client = TestClient(app, base_url="http://localhost:21322")

    now = datetime.now(timezone.utc)

    # Stale evidence: 30 days old
    stale_payload = {
        "research_id": "stale-evidence-001",
        "symbol": "2330.TW",
        "source_url": "https://mops.twse.com.tw/old_notice",
        "source_tier": "regulatory_filing",
        "observed_at": (now - timedelta(days=30)).isoformat(),
        "is_fixture": False,
        "verification_status": "verified",
        "verified_facts": ["Outdated filing claim"],
    }
    resp = client.post("/api/research/intake", json=stale_payload)
    assert resp.status_code == 200
    assert resp.json()["verified"] is False
    assert "REJECTED_STALE_EVIDENCE" in resp.json()["verification_reason"]

    # Future evidence: 2 days in the future
    future_payload = {
        "research_id": "future-evidence-001",
        "symbol": "2330.TW",
        "source_url": "https://mops.twse.com.tw/future_notice",
        "source_tier": "regulatory_filing",
        "observed_at": (now + timedelta(days=2)).isoformat(),
        "is_fixture": False,
        "verification_status": "verified",
        "verified_facts": ["Future announcement claim"],
    }
    resp_future = client.post("/api/research/intake", json=future_payload)
    assert resp_future.status_code == 200
    assert resp_future.json()["verified"] is False
    assert "REJECTED_FUTURE_TIMESTAMP" in resp_future.json()["verification_reason"]

    # Neither enters model learning context
    ctx_resp = client.get("/api/paper/cio/learning-context?symbols=2330.TW")
    assert ctx_resp.status_code == 200
    assert len(ctx_resp.json()["verified_research"]) == 0


def test_raw_unverified_document_separation_never_auto_certified(tmp_path: Path):
    """Raw unverified documents intake into adapter but are NEVER auto-certified into model facts."""
    workspace_root = Path(__file__).resolve().parent.parent
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir = runtime_dir / "research_inbox"

    app = create_app(
        workspace_root=workspace_root,
        runtime_dir=runtime_dir,
        is_read_only=False,
        market_adapter=MockMarketAdapter(),
        fixture_mode=False,
    )
    client = TestClient(app, base_url="http://localhost:21322")

    raw_unverified = {
        "url": "https://mops.twse.com.tw/sample_unverified",
        "title": "Raw unverified press snippet",
        "claims": ["Unconfirmed rumor about packaging line"],
        "related_symbols": ["2330.TW"],
        "status": "unverified",
        "source_mode": "manual_intake",
    }

    resp = client.post("/api/research/intake", json=raw_unverified)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    # Crucial: NOT verified
    assert data["verified"] is False
    assert "REJECTED_UNVERIFIED_NARRATIVE" in data["verification_reason"]

    # Not persisted in canonical inbox
    assert len(list(inbox_dir.glob("*.json"))) == 0

    # Never enters learning context facts
    ctx_resp = client.get("/api/paper/cio/learning-context?symbols=2330.TW")
    assert ctx_resp.status_code == 200
    ctx = ctx_resp.json()
    assert len(ctx["verified_research"]) == 0
    assert len(ctx["research_gaps"]) == 1
    assert ctx["research_gaps"][0]["symbol"] == "2330.TW"


def test_negative_path_traversal_rejected_no_write_outside_inbox(tmp_path: Path):
    """Negative test: Path traversal attempts in research_id are rejected with no write outside inbox."""
    workspace_root = Path(__file__).resolve().parent.parent
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir = runtime_dir / "research_inbox"

    app = create_app(
        workspace_root=workspace_root,
        runtime_dir=runtime_dir,
        is_read_only=False,
        market_adapter=MockMarketAdapter(),
        fixture_mode=False,
    )
    client = TestClient(app, base_url="http://localhost:21322")
    now = datetime.now(timezone.utc)

    # Various path traversal attack patterns
    traversal_ids = [
        "../../traversal_escape",
        "../outside_inbox",
        "subdir/nested_traversal",
        "..\\windows_traversal",
        "/absolute/root/traversal",
    ]

    for attack_id in traversal_ids:
        payload = {
            "research_id": attack_id,
            "symbol": "NVDA",
            "source_url": "https://nvidianews.nvidia.com/news/valid-url",
            "source_tier": "official_company_ir",
            "observed_at": now.isoformat(),
            "is_fixture": False,
            "verification_status": "verified",
            "verified_facts": ["Legitimate statement body with malicious id"],
        }
        resp = client.post("/api/research/intake", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["verified"] is False
        assert "REJECTED_PATH_TRAVERSAL" in data["verification_reason"]

    # PROVE NO WRITE OUTSIDE INBOX:
    # 1. No files written to runtime root or tmp_path root
    assert not (runtime_dir / "traversal_escape.json").exists()
    assert not (runtime_dir / "outside_inbox.json").exists()
    assert not (tmp_path / "traversal_escape.json").exists()
    assert not (tmp_path / "outside_inbox.json").exists()
    # 2. Entire tmp_path tree has zero files matching any attack name
    assert list(tmp_path.rglob("*traversal*")) == []
    assert list(tmp_path.rglob("*outside*")) == []
    # 3. Canonical inbox directory contains zero files
    if inbox_dir.exists():
        assert len(list(inbox_dir.glob("*"))) == 0

    # Rejection audit trail contains REJECTED_PATH_TRAVERSAL
    rej_resp = client.get("/api/research/rejected")
    assert rej_resp.status_code == 200
    assert any("REJECTED_PATH_TRAVERSAL" in r.get("reason", "") for r in rej_resp.json())

    # Learning context has no verified research
    ctx_resp = client.get("/api/paper/cio/learning-context?symbols=NVDA")
    assert ctx_resp.status_code == 200
    assert len(ctx_resp.json()["verified_research"]) == 0
    assert any(g.get("gap_status") == "EXPLICIT_RESEARCH_GAP" and g.get("symbol") == "NVDA" for g in ctx_resp.json()["research_gaps"])


def test_negative_persistence_failure_explicit_error_and_no_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Negative test: Persistence errors return explicit failure, do not stage unpersisted evidence, and leave no corrupted files."""
    workspace_root = Path(__file__).resolve().parent.parent
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir = runtime_dir / "research_inbox"

    app = create_app(
        workspace_root=workspace_root,
        runtime_dir=runtime_dir,
        is_read_only=False,
        market_adapter=MockMarketAdapter(),
        fixture_mode=False,
    )
    client = TestClient(app, base_url="http://localhost:21322")
    now = datetime.now(timezone.utc)

    # Monkeypatch tempfile.NamedTemporaryFile to simulate atomic write failure (e.g. disk full / EIO)
    def mock_named_temporary_file(*args, **kwargs):
        raise OSError("Simulated disk I/O error during atomic research write")

    monkeypatch.setattr("tempfile.NamedTemporaryFile", mock_named_temporary_file)

    payload = {
        "research_id": "nvda-official-fail-disk",
        "symbol": "NVDA",
        "source_url": "https://nvidianews.nvidia.com/news/nvidia-announces-a-150-billion-share-repurchase-authorization-increase",
        "source_tier": "official_company_ir",
        "observed_at": (now - timedelta(minutes=5)).isoformat(),
        "is_fixture": False,
        "verification_status": "verified",
        "verified_facts": ["Legitimate statement body"],
    }

    resp = client.post("/api/research/intake", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    # 1. Explicit failure returned
    assert data["verified"] is False
    assert "PERSISTENCE_FAILURE" in data["verification_reason"]
    assert "Simulated disk I/O error" in data["verification_reason"]

    # 2. Stage ONLY after successful persistence: verify unpersisted item is NOT staged in reader
    reader = app.state.app_state.runner.research_reader
    assert "nvda-official-fail-disk" not in reader._staged_evidence

    # 3. Model learning context does NOT receive the failed evidence
    ctx_resp = client.get("/api/paper/cio/learning-context?symbols=NVDA")
    assert ctx_resp.status_code == 200
    assert len(ctx_resp.json()["verified_research"]) == 0
    assert any(g.get("gap_status") == "EXPLICIT_RESEARCH_GAP" and g.get("symbol") == "NVDA" for g in ctx_resp.json()["research_gaps"])

    # 4. Reader inbox does not list it
    inbox_resp = client.get("/api/research/inbox")
    assert inbox_resp.status_code == 200
    assert not any(i.get("research_id") == "nvda-official-fail-disk" for i in inbox_resp.json())

    # 5. Rejected records audit trail records the failure
    rej_resp = client.get("/api/research/rejected")
    assert rej_resp.status_code == 200
    assert any("PERSISTENCE_FAILURE" in r.get("reason", "") for r in rej_resp.json())

    # 6. Prove no corrupted destination file or leftover temp files in inbox
    assert not (inbox_dir / "nvda-official-fail-disk.json").exists()
    if inbox_dir.exists():
        assert len(list(inbox_dir.glob("*.tmp"))) == 0
        assert len(list(inbox_dir.glob(".tmp_*"))) == 0
