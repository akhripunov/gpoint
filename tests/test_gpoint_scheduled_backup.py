import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

POLICY_PATH = Path(__file__).resolve().parents[1] / "scripts/gpoint-backup-policy.py"
SPEC = importlib.util.spec_from_file_location("gpoint_backup_policy_test", POLICY_PATH)
gpoint_backup_policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gpoint_backup_policy)
RUNNER_PATH = Path(__file__).resolve().parents[1] / "scripts/gpoint-scheduled-backup.py"
RUNNER_SPEC = importlib.util.spec_from_file_location("gpoint_scheduled_backup_test", RUNNER_PATH)
gpoint_scheduled_backup = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(gpoint_scheduled_backup)


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
            "bytes": path.stat().st_size,
            "encrypted": True, "integrity": "PASS", "isolated_restore": "PASS",
        }
    components["media"].update({"file_count": 4, "directory_count": 3,
                                "total_bytes": 100, "manifest_sha256": "c" * 64})
    manifest = {
        "schema": "gpoint-scheduled-backup/v1", "status": "PASS",
        "candidate_sha": "a" * 40, "run_id": "42", "attempt": "1",
        "completed_at": "2026-11-01T00:00:00+00:00",
        **components,
        "restore_evidence": {"path": str(evidence),
                             "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()},
    }
    prefix = "bunny:gpoint-backups/gpoint/scheduled/" + "a" * 40 + "/42-1"
    manifest["bunny"] = {
        "schema": "gpoint-bunny-backup/v1", "status": "PASS", "bucket": "gpoint-backups",
        "remote_root": "bunny:gpoint-backups", "snapshot_prefix": prefix,
        "snapshot": "a" * 40 + "/42-1",
        "database": {"object": prefix + "/gpoint-scheduled-database.age",
                     "sha256": components["database"]["sha256"], "bytes": components["database"]["bytes"],
                     "encrypted": True, "readback": "PASS", "isolated_restore": "PASS"},
        "media": {"object": prefix + "/gpoint-scheduled-media.age",
                  "sha256": components["media"]["sha256"], "bytes": components["media"]["bytes"],
                  "encrypted": True, "readback": "PASS", "isolated_restore": "PASS",
                  "source_manifest_object": prefix + "/media-source.json.age",
                  "source_manifest_sha256": components["media"]["manifest_sha256"],
                  "source_manifest_ciphertext_sha256": "d" * 64,
                  "source_manifest_readback": "PASS", "file_count": components["media"]["file_count"],
                  "directory_count": components["media"]["directory_count"],
                  "total_bytes": components["media"]["total_bytes"]},
        "restore": {"source": "BUNNY_READBACK", "status": "PASS",
                    "receipt_object": prefix + "/restore.txt",
                    "receipt_sha256": hashlib.sha256(b"ISOLATED_RESTORE=PASS\n").hexdigest(),
                    "receipt_readback": "PASS"},
        "manifest_object": prefix + "/manifest.json", "manifest_sha256": "e" * 64,
        "manifest_readback": "PASS",
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


def test_local_backup_freshness_requires_bunny_upload_readback_and_same_snapshot_restore(tmp_path, monkeypatch):
    manifest_path, _ = make_manifest(tmp_path, monkeypatch)
    value = json.loads(manifest_path.read_text())
    value["bunny"]["database"]["readback"] = "NOT_REQUESTED"
    manifest_path.write_text(json.dumps(value))
    manifest_path.chmod(0o600)
    with pytest.raises(gpoint_backup_policy.BackupPolicyError, match="read-back or restore evidence"):
        gpoint_backup_policy.verify_freshness(
            manifest_path, now=gpoint_backup_policy.datetime.fromisoformat("2026-11-01T01:00:00+00:00"))


def test_local_backup_freshness_rejects_bunny_objects_outside_gpoint_zone_or_snapshot(tmp_path, monkeypatch):
    manifest_path, _ = make_manifest(tmp_path, monkeypatch)
    value = json.loads(manifest_path.read_text())
    value["bunny"]["media"]["object"] = "bunny:other-zone/fake"
    manifest_path.write_text(json.dumps(value))
    manifest_path.chmod(0o600)
    with pytest.raises(gpoint_backup_policy.BackupPolicyError, match="outside the exact snapshot"):
        gpoint_backup_policy.verify_freshness(
            manifest_path, now=gpoint_backup_policy.datetime.fromisoformat("2026-11-01T01:00:00+00:00"))


def test_bunny_configuration_requires_existing_zone_and_configured_rclone_alias(monkeypatch, tmp_path):
    config = tmp_path / "rclone.conf"
    config.write_text("[bunny]\ntype = s3\nprovider = Other\nendpoint = https://ny-s3.storage.bunnycdn.com\naccess_key_id = gpoint-backups\nsecret_access_key = must-not-be-printed\n")
    config.chmod(0o600)
    destination = tmp_path / "gpoint-backup-offsite.conf"
    destination.write_text("GPOINT_BACKUP_BUNNY_REMOTE=bunny:gpoint-backups\n")
    destination.chmod(0o600)
    monkeypatch.setattr(gpoint_scheduled_backup, "OFFSITE_CONFIG", destination)
    monkeypatch.setattr(gpoint_scheduled_backup, "RCLONE_CONFIG", config)
    monkeypatch.setattr(gpoint_scheduled_backup, "_private_file", lambda path: None)
    monkeypatch.setattr(gpoint_scheduled_backup, "_run", lambda *args, **kwargs: "bunny:\n")
    monkeypatch.setenv("GPOINT_BACKUP_BUNNY_REMOTE", "bunny:gpoint-backups")
    assert gpoint_scheduled_backup._bunny_remote_root() == "bunny:gpoint-backups"
    monkeypatch.setenv("GPOINT_BACKUP_BUNNY_REMOTE", "bunny:other-zone")
    with pytest.raises(gpoint_scheduled_backup.ScheduledBackupError, match="does not match its protected configuration"):
        gpoint_scheduled_backup._bunny_remote_root()
    monkeypatch.setenv("GPOINT_BACKUP_BUNNY_REMOTE", "bunny:gpoint-backups")
    destination.write_text("GPOINT_BACKUP_BUNNY_REMOTE=bunny:other-zone\n")
    with pytest.raises(gpoint_scheduled_backup.ScheduledBackupError, match="invalid format"):
        gpoint_scheduled_backup._bunny_remote_root()


def test_bunny_receipt_is_immutable_snapshot_bound_and_read_back(tmp_path, monkeypatch):
    evidence_path = tmp_path / "restore.txt"
    evidence_path.write_text("OFFSITE_RESTORE=PASS\nMEDIA_OFFSITE_RESTORE=PASS\n")
    evidence = {
        "BUNNY_REMOTE_ROOT": "bunny:gpoint-backups",
        "BUNNY_SNAPSHOT_PREFIX": "bunny:gpoint-backups/gpoint/scheduled/" + "a" * 40 + "/42-1",
        "BUNNY_DATABASE_OBJECT": "bunny:gpoint-backups/gpoint/scheduled/" + "a" * 40 + "/42-1/db.dump.age",
        "BACKUP_SHA256": "b" * 64, "BACKUP_BYTES": "10", "BACKUP_ENCRYPTION": "AGE_X25519",
        "BUNNY_DATABASE_READBACK": "PASS", "OFFSITE_RESTORE": "PASS",
        "BUNNY_MEDIA_OBJECT": "bunny:gpoint-backups/gpoint/scheduled/" + "a" * 40 + "/42-1/media.tar.age",
        "MEDIA_BACKUP_SHA256": "c" * 64, "MEDIA_BACKUP_BYTES": "11", "MEDIA_BACKUP_ENCRYPTION": "AGE_X25519",
        "BUNNY_MEDIA_READBACK": "PASS", "MEDIA_OFFSITE_RESTORE": "PASS",
        "BUNNY_MEDIA_MANIFEST_OBJECT": "bunny:gpoint-backups/gpoint/scheduled/" + "a" * 40 + "/42-1/media-source.json.age",
        "MEDIA_SOURCE_MANIFEST_SHA256": "d" * 64,
        "BUNNY_MEDIA_MANIFEST_SHA256": "e" * 64,
        "BUNNY_MEDIA_MANIFEST_READBACK": "PASS",
        "MEDIA_FILE_COUNT": "2", "MEDIA_DIRECTORY_COUNT": "1", "MEDIA_TOTAL_BYTES": "64",
    }
    monkeypatch.setattr(gpoint_scheduled_backup, "BUNNY_TEMP_ROOT", tmp_path)
    uploads = []

    def readback(source, remote_object, target):
        target.write_bytes(source.read_bytes())
        uploads.append(remote_object)
        return hashlib.sha256(source.read_bytes()).hexdigest()

    monkeypatch.setattr(gpoint_scheduled_backup, "_bunny_upload_and_readback", readback)
    snapshot = gpoint_scheduled_backup._publish_bunny_receipt(
        "bunny:gpoint-backups", "a" * 40, "42", "1", evidence_path, evidence,
        "2026-11-01T00:00:00+00:00")
    assert snapshot["status"] == "PASS"
    assert snapshot["restore"]["source"] == "BUNNY_READBACK"
    assert snapshot["manifest_readback"] == "PASS"
    assert uploads == [
        "bunny:gpoint-backups/gpoint/scheduled/" + "a" * 40 + "/42-1/restore.txt",
        "bunny:gpoint-backups/gpoint/scheduled/" + "a" * 40 + "/42-1/manifest.json",
    ]


def test_scheduled_backup_uploads_then_restores_the_same_bunny_readback_objects():
    root = Path(__file__).resolve().parents[1]
    helper = (root / "scripts/verify-gpoint-scheduled-backup-restore.sh").read_text()
    runner = RUNNER_PATH.read_text()
    service = (root / "systemd/gpoint-backup.service").read_text()
    upload = helper.index('rclone copyto --immutable --config "$rclone_config" "$source" "$object"')
    readback = helper.index('rclone copyto --config "$rclone_config" "$object" "${bunny_readback_dir}/${name}"')
    database_restore = helper.index('age -d -i "$age_identity" "$restore_backup_file"')
    media_restore = helper.index('age -d -i "$age_identity" "$restore_media_backup_file"')
    assert upload < readback < database_restore < media_restore
    assert 'rclone check --download --one-way' in helper
    assert 'age -d -i "$age_identity" "${bunny_readback_dir}/media-source.json.age"' in helper
    assert 'BUNNY_DATABASE_READBACK=$bunny_database_readback' in helper
    assert 'OFFSITE_RESTORE=' in helper and 'MEDIA_OFFSITE_RESTORE=' in helper
    assert '_publish_bunny_receipt(' in runner
    assert 'manifest["bunny"] = _publish_bunny_receipt(' in runner
    assert 'EnvironmentFile=-/opt/gpoint/env/gpoint-backup-offsite.conf' in service
    assert 'def _retain_local(now: float)' in runner  # no unapproved retention switch


def test_migration_rehearsal_with_release_backup_evidence_does_not_require_bunny_config(monkeypatch):
    backup_evidence = "/opt/gpoint/release-logs/" + "a" * 40 + "/receipt.txt"
    monkeypatch.setenv("GPOINT_MIGRATION_BACKUP_EVIDENCE", backup_evidence)
    monkeypatch.setattr(
        gpoint_scheduled_backup, "_bunny_remote_root",
        lambda: pytest.fail("release evidence rehearsal must not load Bunny configuration"),
    )
    assert gpoint_scheduled_backup._backup_evidence_and_bunny_remote(migration_rehearsal=True) == (
        backup_evidence, "",
    )


def test_scheduled_backup_and_latest_snapshot_rehearsal_require_bunny_config(monkeypatch):
    monkeypatch.delenv("GPOINT_MIGRATION_BACKUP_EVIDENCE", raising=False)
    calls = []
    monkeypatch.setattr(
        gpoint_scheduled_backup, "_bunny_remote_root",
        lambda **kwargs: calls.append(kwargs) or "bunny:gpoint-backups",
    )
    assert gpoint_scheduled_backup._backup_evidence_and_bunny_remote(migration_rehearsal=False) == (
        "", "bunny:gpoint-backups",
    )
    assert gpoint_scheduled_backup._backup_evidence_and_bunny_remote(migration_rehearsal=True) == (
        "", "bunny:gpoint-backups",
    )
    assert calls == [
        {"require_systemd_environment": True},
        {"require_systemd_environment": False},
    ]


def test_latest_snapshot_rehearsal_reads_protected_bunny_destination_without_systemd_environment(monkeypatch, tmp_path):
    config = tmp_path / "rclone.conf"
    config.write_text(
        "[bunny]\ntype = s3\nprovider = Other\nendpoint = https://ny-s3.storage.bunnycdn.com\n"
        "access_key_id = gpoint-backups\nsecret_access_key = protected\n",
    )
    config.chmod(0o600)
    destination = tmp_path / "gpoint-backup-offsite.conf"
    destination.write_text("GPOINT_BACKUP_BUNNY_REMOTE=bunny:gpoint-backups\n")
    destination.chmod(0o600)
    monkeypatch.setattr(gpoint_scheduled_backup, "OFFSITE_CONFIG", destination)
    monkeypatch.setattr(gpoint_scheduled_backup, "RCLONE_CONFIG", config)
    monkeypatch.setattr(gpoint_scheduled_backup, "_private_file", lambda path: None)
    monkeypatch.setattr(gpoint_scheduled_backup, "_run", lambda *args, **kwargs: "bunny:\n")
    monkeypatch.delenv("GPOINT_BACKUP_BUNNY_REMOTE", raising=False)
    assert gpoint_scheduled_backup._backup_evidence_and_bunny_remote(migration_rehearsal=True) == (
        "", "bunny:gpoint-backups",
    )
    with pytest.raises(gpoint_scheduled_backup.ScheduledBackupError, match="does not match its protected configuration"):
        gpoint_scheduled_backup._bunny_remote_root()


def test_scheduled_backup_uses_age_keygen_positional_identity_input():
    runner = RUNNER_PATH.read_text()
    assert '_run(["age-keygen", "-y", str(AGE_IDENTITY)], stage="age key identity")' in runner
    assert '"age-keygen", "-y", "-i"' not in runner


def test_migration_rehearsal_requires_existing_backup_and_never_refreshes_latest():
    runner = RUNNER_PATH.read_text()
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
    runner = RUNNER_PATH.read_text()
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


def test_scheduled_manifest_keeps_ciphertext_sizes_separate_from_restore_sizes(tmp_path):
    import ast
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    runner = (root / "scripts/gpoint-scheduled-backup.py").read_text()
    node = next(n for n in ast.walk(ast.parse(runner))
                if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "manifest")
    evidence = {
        "BACKUP_PATH": "db.age", "BACKUP_SHA256": "a" * 64, "BACKUP_BYTES": "100",
        "RESTORED_TABLE_COUNT": "74", "RESTORED_DATABASE_BYTES": "900000",
        "RESTORED_PRISMA_HEAD": "20261015130000_codex_telegram_challenge", "RESTORED_AGATA_HEAD": "agata0011",
        "MEDIA_BACKUP_PATH": "media.age", "MEDIA_BACKUP_SHA256": "b" * 64, "MEDIA_BACKUP_BYTES": "7",
        "MEDIA_FILE_COUNT": "4", "MEDIA_DIRECTORY_COUNT": "8", "MEDIA_TOTAL_BYTES": "4000",
        "MEDIA_SOURCE_MANIFEST_SHA256": "c" * 64,
    }
    receipt = tmp_path / "restore.txt"
    receipt.write_text("verified restore")
    values = dict(evidence=evidence, completed_at="2026-11-01T15:00:00+00:00", candidate="d" * 40,
                  run_id="42", attempt="1", evidence_path=receipt,
                  _sha256=lambda path: hashlib.sha256(path.read_bytes()).hexdigest())
    exec(compile(ast.Module(body=[node], type_ignores=[]), "manifest", "exec"), values)
    manifest = values["manifest"]
    assert manifest["database"]["bytes"] == 100
    assert manifest["database"]["restored_database_bytes"] == 900000
    assert manifest["media"]["bytes"] == 7
    assert manifest["media"]["total_bytes"] == 4000
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(manifest))
    helper = (root / "scripts/verify-gpoint-scheduled-backup-restore.sh").read_text()
    media_code = "\n".join(helper.splitlines()[next(i for i,l in enumerate(helper.splitlines()) if "readarray -t media_manifest_facts" in l)+1:]).split("\nPY\n",1)[0]
    result = subprocess.run([sys.executable, "-", str(path)], input=media_code, text=True, capture_output=True, check=True)
    assert result.stdout.splitlines() == ["4", "8", "4000", "c" * 64]
    db_code = "\n".join(helper.splitlines()[next(i for i,l in enumerate(helper.splitlines()) if 'database_bytes="$(python3 - "$latest_manifest"' in l)+1:]).split("\nPY\n",1)[0]
    result = subprocess.run([sys.executable, "-", str(path)], input=db_code, text=True, capture_output=True, check=True)
    assert result.stdout.strip() == "900000"


def test_rehearsal_matches_verified_migration_privileges_without_runtime_bypass():
    helper = (Path(__file__).resolve().parents[1] / "scripts/verify-gpoint-scheduled-backup-restore.sh").read_text()
    assert 'GRANT CONNECT, CREATE ON DATABASE gpoint TO gpoint;' in helper
    assert 'AGATA_MIGRATOR_DATABASE_URL=postgresql+psycopg://gpoint_restore_admin:' in helper
    assert 'CREATE ROLE gpoint LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS' in helper
    assert 'runtime_role=gpoint' in helper
    assert 'GPOINT_MIGRATION_PRODUCTION_GRANTS_RLS=NOT_CHECKED' in (Path(__file__).resolve().parents[1] / "scripts/gpoint-scheduled-backup.py").read_text() or 'MIGRATION_PRODUCTION_GRANTS_RLS=NOT_CHECKED' in helper
