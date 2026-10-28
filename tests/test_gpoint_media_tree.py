import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts/gpoint-media-tree.py"


def run_tree(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
    )


def test_media_tree_manifest_round_trips_files_modes_and_empty_directories(tmp_path: Path):
    source = tmp_path / "media"
    restore = tmp_path / "restore"
    (source / "nested" / "empty").mkdir(parents=True)
    restore.mkdir()
    (source / "nested" / "asset.bin").write_bytes(b"synthetic media payload\x00\xff")
    (source / "nested" / "asset.bin").chmod(0o640)
    (restore / "nested").mkdir()
    (restore / "nested" / "empty").mkdir()
    (restore / "nested" / "asset.bin").write_bytes(b"synthetic media payload\x00\xff")
    (restore / "nested" / "asset.bin").chmod(0o640)

    before_manifest = tmp_path / "before.json"
    restored_manifest = tmp_path / "restored.json"
    before = run_tree("manifest", "--root", str(source), "--output", str(before_manifest))
    restored = run_tree("manifest", "--root", str(restore), "--output", str(restored_manifest))

    assert before.returncode == 0, before.stderr
    assert restored.returncode == 0, restored.stderr
    assert json.loads(before_manifest.read_text()) == json.loads(restored_manifest.read_text())
    assert "GPOINT_MEDIA_MANIFEST=PASS" in before.stdout


def test_media_tree_manifest_rejects_changed_or_missing_content(tmp_path: Path):
    source = tmp_path / "media"
    restore = tmp_path / "restore"
    source.mkdir()
    restore.mkdir()
    (source / "one.bin").write_bytes(b"original")
    (restore / "one.bin").write_bytes(b"changed")
    manifest = tmp_path / "before.json"
    created = run_tree("manifest", "--root", str(source), "--output", str(manifest))
    assert created.returncode == 0, created.stderr

    compared = run_tree("compare", "--manifest", str(manifest), "--root", str(restore))

    assert compared.returncode != 0
    assert "media tree does not match" in compared.stderr.lower()


def test_media_tree_manifest_includes_root_directory_metadata(tmp_path: Path):
    source = tmp_path / "media"
    restore = tmp_path / "restore"
    source.mkdir()
    restore.mkdir()
    source.chmod(0o750)
    restore.chmod(0o700)
    manifest = tmp_path / "before.json"

    created = run_tree("manifest", "--root", str(source), "--output", str(manifest))
    compared = run_tree("compare", "--manifest", str(manifest), "--root", str(restore))

    assert created.returncode == 0, created.stderr
    assert compared.returncode != 0
    assert "media tree does not match" in compared.stderr.lower()


def test_media_tree_manifest_fails_closed_on_symlinks_and_special_files(tmp_path: Path):
    source = tmp_path / "media"
    source.mkdir()
    target = tmp_path / "outside"
    target.write_text("do not follow")
    (source / "link").symlink_to(target)

    result = run_tree("manifest", "--root", str(source), "--output", str(tmp_path / "manifest.json"))

    assert result.returncode != 0
    assert "unsupported media filesystem entry" in result.stderr.lower()


def test_media_manifest_file_is_private(tmp_path: Path):
    source = tmp_path / "media"
    source.mkdir()
    destination = tmp_path / "manifest.json"

    result = run_tree("manifest", "--root", str(source), "--output", str(destination))

    assert result.returncode == 0, result.stderr
    assert os.stat(destination).st_mode & 0o777 == 0o600


def test_first_cutover_backs_up_and_restores_media_before_production_mutation():
    workflow = (ROOT / ".github/workflows/gpoint-first-kamal-cutover.yml").read_text()
    helper = (ROOT / "scripts/verify-gpoint-backup-restore.sh").read_text()
    manifest_writer = (ROOT / "scripts/write-kamal-production-manifest.py").read_text()
    backup_step = "name: Verify fresh PostgreSQL and media backup with isolated restore"
    assert backup_step in workflow
    assert workflow.index(backup_step) < workflow.index("name: Create exact-SHA in-progress GitHub Deployment")
    assert workflow.index(backup_step) < workflow.index("name: Pause legacy GPoint services after restore gates")
    assert "/opt/gpoint/media" in helper
    assert "tar --create" in helper and "tar --extract" in helper
    assert "--acls" in helper and "--xattrs" in helper
    assert "age --recipient" in helper and "rclone check --download" in helper
    assert "GPOINT_MEDIA_TREE_MATCH=PASS" in helper
    assert "GPOINT_MEDIA_BACKUP=PASS" in helper
    assert "media_backup_sha256" in workflow and "MEDIA_BACKUP_SHA" in workflow
    assert "mediaBackup" in manifest_writer and "mediaRestore" in manifest_writer
    assert "media-backup-offsite-readback-and-isolated-restore" in manifest_writer
