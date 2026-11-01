import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

POLICY_PATH = Path(__file__).resolve().parents[1] / "scripts/gpoint-backup-policy.py"
SPEC = importlib.util.spec_from_file_location("gpoint_backup_policy_test", POLICY_PATH)
gpoint_backup_policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gpoint_backup_policy)


def make_manifest(tmp_path: Path, monkeypatch):
    root = tmp_path / "scheduled"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(gpoint_backup_policy, "SCHEDULED_ROOT", root)
    run_dir = root / "runs" / ("a" * 40 + "-42-1")
    run_dir.mkdir(mode=0o700, parents=True)
    evidence = run_dir / "restore.txt"
    evidence.write_text("ISOLATED_RESTORE=PASS\n", encoding="utf-8")
    evidence.chmod(0o600)
    components = {}
    for name in ("database", "media"):
        path = root / f"gpoint-scheduled-{name}.age"
        path.write_bytes((name + "-encrypted").encode())
        path.chmod(0o600)
        components[name] = {
            "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "encrypted": True, "integrity": "PASS", "isolated_restore": "PASS",
        }
    manifest = {
        "schema": "gpoint-scheduled-backup/v1", "status": "PASS",
        "completed_at": "2026-11-01T00:00:00+00:00",
        **components,
        "restore_evidence": {"path": str(evidence),
                             "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()},
    }
    manifest_path = root / "latest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)
    return manifest_path, components["database"]["path"]


def test_local_backup_freshness_requires_encrypted_checksums_and_restore_proof(tmp_path, monkeypatch):
    manifest, _ = make_manifest(tmp_path, monkeypatch)
    result = gpoint_backup_policy.verify_freshness(
        manifest, now=gpoint_backup_policy.datetime.fromisoformat("2026-11-01T01:00:00+00:00"))
    assert result["status"] == "PASS"


def test_local_backup_freshness_rejects_tampered_backup_bytes(tmp_path, monkeypatch):
    manifest, database_path = make_manifest(tmp_path, monkeypatch)
    Path(database_path).write_bytes(b"tampered")
    with pytest.raises(gpoint_backup_policy.BackupPolicyError, match="checksum mismatch"):
        gpoint_backup_policy.verify_freshness(
            manifest, now=gpoint_backup_policy.datetime.fromisoformat("2026-11-01T01:00:00+00:00"))
