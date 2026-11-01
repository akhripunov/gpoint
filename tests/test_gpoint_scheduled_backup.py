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


def test_scheduled_backup_uses_age_keygen_positional_identity_input():
    runner = (Path(__file__).resolve().parents[1] / "scripts/gpoint-scheduled-backup.py").read_text()
    assert '_run(["age-keygen", "-y", str(AGE_IDENTITY)], stage="age key identity")' in runner
    assert '"age-keygen", "-y", "-i"' not in runner


def test_migration_rehearsal_requires_existing_backup_and_never_refreshes_latest():
    runner = (Path(__file__).resolve().parents[1] / "scripts/gpoint-scheduled-backup.py").read_text()
    helper = (Path(__file__).resolve().parents[1] / "scripts/verify-gpoint-scheduled-backup-restore.sh").read_text()
    assert "GPOINT_MIGRATION_REHEARSAL" in runner
    assert "verify_freshness(latest_path" in runner
    rehearsal_branch = runner.split("if migration_rehearsal:", 1)[1].split("\n        else:", 1)[0]
    assert "_source_facts()" not in rehearsal_branch
    assert "existing_manifest" in rehearsal_branch
    assert "MEDIA_ROOT" not in rehearsal_branch
    assert "GPOINT_USE_EXISTING_BACKUP" in helper
    assert "GPOINT_MIGRATION_REHEARSAL=PASS" in helper
    assert "GPOINT_MIGRATION_REHEARSAL" in helper
    assert '"BASELINE_SCHEMA": ("gpoint-migration-rehearsal-baseline/v1" if migration_rehearsal' in runner
    rehearsal_finish = runner.index('print("GPOINT_MIGRATION_REHEARSAL=PASS")')
    normal_manifest_publish = runner.index("os.replace(pending_manifest, latest_path)")
    assert "return" in runner[rehearsal_finish:normal_manifest_publish]
    assert 'backup --sha "$candidate"' not in helper.split('if [[ "${GPOINT_USE_EXISTING_BACKUP:-0}" == "1" ]]', 1)[1].split("else", 1)[0]


def test_migration_rehearsal_can_consume_recent_release_backup_with_pinned_evidence():
    root = Path(__file__).resolve().parents[1]
    policy = (root / "scripts/gpoint-backup-policy.py").read_text()
    runner = (root / "scripts/gpoint-scheduled-backup.py").read_text()
    helper = (root / "scripts/verify-gpoint-scheduled-backup-restore.sh").read_text()
    assert "verify_release_backup_evidence" in policy
    assert '"RESTORED_DATABASE_BYTES", manifest["database"]["restored_database_bytes"]' in policy
    assert "GPOINT_MIGRATION_BACKUP_EVIDENCE" in runner
    assert "GPOINT_MIGRATION_BACKUP_EVIDENCE_SHA256" in runner
    rehearsal_branch = runner.split("if migration_rehearsal:", 1)[1].split("\n        else:", 1)[0]
    assert "verify_release_backup_evidence" in rehearsal_branch
    restore_branch = helper.split('if [[ "${GPOINT_MIGRATION_REHEARSAL:-0}" == "1" ]]', 1)[1].split("\nelse\n", 1)[0]
    assert "GPOINT_MIGRATION_BACKUP_EVIDENCE" in restore_branch
    assert "GPOINT_MIGRATION_BACKUP_EVIDENCE_SHA256" in restore_branch
    assert 'release-evidence' in restore_branch


def test_release_backup_evidence_checks_provenance_age_and_ciphertext_checksums(tmp_path):
    import datetime as dt

    backup_root = tmp_path / "backups"
    backup_root.mkdir(mode=0o700)
    base = backup_root / f"gpoint-pre-release-{'a' * 40}-41223019715-1"
    db_path = base.with_suffix(".dump.age")
    media_path = Path(str(base) + ".media.tar.age")
    db_path.write_bytes(b"encrypted database")
    media_path.write_bytes(b"encrypted media")
    db_path.chmod(0o600)
    media_path.chmod(0o600)
    release_logs = tmp_path / "release-logs"
    evidence_dir = release_logs / ("a" * 40)
    evidence_dir.mkdir(mode=0o700, parents=True)
    evidence = evidence_dir / "kamal-backup-restore-41223019715-1.txt"
    evidence.write_text("\n".join((
        "BACKUP_RESTORE_SCHEMA=gpoint-kamal-backup-restore/v1",
        f"BACKUP_PATH={db_path}",
        f"BACKUP_SHA256={hashlib.sha256(db_path.read_bytes()).hexdigest()}",
        f"BACKUP_BYTES={db_path.stat().st_size}",
        "RESTORED_TABLE_COUNT=74",
        "RESTORED_DATABASE_BYTES=12345",
        "RESTORED_PRISMA_HEAD=20261015130000_codex_telegram_challenge",
        "RESTORED_AGATA_HEAD=agata0011",
        "BACKUP_ENCRYPTION=AGE_X25519",
        "ISOLATED_RESTORE=PASS",
        "OFFSITE_RESTORE=PASS",
        f"MEDIA_BACKUP_PATH={media_path}",
        f"MEDIA_BACKUP_SHA256={hashlib.sha256(media_path.read_bytes()).hexdigest()}",
        f"MEDIA_BACKUP_BYTES={media_path.stat().st_size}",
        "MEDIA_FILE_COUNT=4",
        "MEDIA_DIRECTORY_COUNT=8",
        "MEDIA_TOTAL_BYTES=1024",
        "MEDIA_SOURCE_MANIFEST_SHA256=" + "b" * 64,
        "MEDIA_BACKUP_ENCRYPTION=AGE_X25519",
        "MEDIA_ISOLATED_RESTORE=PASS",
        "MEDIA_OFFSITE_RESTORE=PASS",
    )) + "\n", encoding="utf-8")
    evidence.chmod(0o600)
    evidence_sha = hashlib.sha256(evidence.read_bytes()).hexdigest()
    now = dt.datetime.now(dt.timezone.utc)
    normalized = gpoint_backup_policy.verify_release_backup_evidence(
        evidence, expected_sha256=evidence_sha, now=now, backup_root=backup_root,
        release_logs_root=release_logs)
    assert normalized["status"] == "PASS"
    assert normalized["database"]["sha256"] == hashlib.sha256(db_path.read_bytes()).hexdigest()
    assert normalized["media"]["manifest_sha256"] == "b" * 64

    db_path.write_bytes(b"changed ciphertext")
    with pytest.raises(gpoint_backup_policy.BackupPolicyError, match="checksum mismatch"):
        gpoint_backup_policy.verify_release_backup_evidence(
            evidence, expected_sha256=evidence_sha, now=now, backup_root=backup_root,
            release_logs_root=release_logs)


def test_migration_restore_path_never_reads_live_database_or_kamal_api():
    helper = (Path(__file__).resolve().parents[1] / "scripts/verify-gpoint-scheduled-backup-restore.sh").read_text()
    assert helper.count('if [[ "${GPOINT_MIGRATION_REHEARSAL:-0}" != "1" ]]') >= 2
    assert 'runtime_role=gpoint' in helper
    assert 'REASSIGN OWNED BY gpoint_restore_admin TO gpoint' in helper
    assert 'shared_internal' not in helper
    assert 'production.env' not in helper
    rehearsal_baseline = helper.split('if [[ "${GPOINT_MIGRATION_REHEARSAL:-0}" == "1" ]]', 1)[1].split('\nelse\n', 1)[0]
    assert 'docker exec "$postgres"' not in rehearsal_baseline
    assert 'docker inspect "$api_container_id"' not in rehearsal_baseline
    assert '/opt/gpoint/media' not in rehearsal_baseline


def test_isolated_restore_reassignment_uses_a_non_pinned_admin_role():
    helper = (Path(__file__).resolve().parents[1] / "scripts/verify-gpoint-scheduled-backup-restore.sh").read_text()
    assert "--env POSTGRES_USER=postgres" in helper
    assert "CREATE ROLE gpoint_restore_admin LOGIN SUPERUSER" in helper
    assert 'pg_isready -q -U postgres -d gpoint' in helper
    assert 'REASSIGN OWNED BY gpoint_restore_admin TO gpoint' in helper


def test_isolated_restore_compares_container_image_id_to_pinned_image_content_id():
    helper = (Path(__file__).resolve().parents[1] / "scripts/verify-gpoint-scheduled-backup-restore.sh").read_text()
    assert 'restore_image_id="$(docker image inspect "$postgres_image" --format \'{{.Id}}\')"' in helper
    assert '"$restore_container_id $restore_image_id none"' in helper
    assert '"$restore_container_id $postgres_image none"' not in helper


def test_restore_evidence_records_verified_database_and_media_integrity():
    helper = (Path(__file__).resolve().parents[1] / "scripts/verify-gpoint-scheduled-backup-restore.sh").read_text()
    evidence = helper.split('cat > "$evidence" <<EOF', 1)[1].split("\nEOF", 1)[0]
    assert "BACKUP_INTEGRITY=PASS" in evidence
    assert "MEDIA_BACKUP_INTEGRITY=PASS" in evidence


def test_normal_backup_initializes_postgres_image_before_capacity_or_restore():
    helper = (Path(__file__).resolve().parents[1] / "scripts/verify-gpoint-scheduled-backup-restore.sh").read_text()
    # Both normal and rehearsal paths must derive the exact image from their
    # hash-verified baseline, before the shared capacity/restore code uses it.
    shared = helper.split("# Reserve enough space", 1)[1]
    assignment = shared.index('postgres_image="$(baseline_value POSTGRES_IMAGE_ID)"')
    assert assignment < shared.index('docker image inspect "$postgres_image"')


def test_restore_role_dump_preserves_grants_and_skips_only_existing_bootstrap_role(tmp_path):
    import re
    import subprocess

    helper = (Path(__file__).resolve().parents[1] / "scripts/verify-gpoint-scheduled-backup-restore.sh").read_text()
    match = re.search(r"(?ms)^restore_role_dump\(\) \{\n.*?^\}", helper)
    assert match, "restore must handle the pinned postgres bootstrap role explicitly"
    sql = "CREATE ROLE postgres;\nALTER ROLE postgres WITH SUPERUSER;\nCREATE ROLE gpoint;\nCREATE ROLE postgres_reader;\nGRANT postgres_reader TO gpoint;\n"
    roles = tmp_path / "roles.sql"
    roles.write_text(sql)
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", match.group() + '\nrestore_role_dump "$1"', "test", str(roles)], text=True, capture_output=True, check=True)
    assert result.stdout == sql.replace("CREATE ROLE postgres;\n", "")
    assert 'restore_role_dump "$roles_file" |' in helper
    assert 'psql -Xq -v ON_ERROR_STOP=1 -U gpoint_restore_admin' in helper
