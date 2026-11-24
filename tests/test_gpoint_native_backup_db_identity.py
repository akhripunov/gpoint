"""Native GPoint backup must dump the current database, never legacy AGATA/gpoint."""
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
RESTORE_SCRIPT = ROOT / "scripts/verify-gpoint-scheduled-backup-restore.sh"


def _native_dump_stage() -> str:
    source = RESTORE_SCRIPT.read_text(encoding="utf-8")
    marker = "  # Back up the exact production database already attested by this release's"
    return source.split(marker, 1)[1].split("  encrypted_name=", 1)[0]


def _run_native_dump(tmp_path: Path, *, database: str = "gpoint_staging",
                     dump_fails: bool = False, prisma_ledger: bool = True,
                     empty_dump: bool = False):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$FAKE_DOCKER_LOG"\n'
        'if [ "$3" = sh ] && [ "$4" = -ec ]; then\n'
        '  case "$5" in\n'
        '    *pg_dump*) [ "$FAKE_DUMP_FAIL" = 0 ] || exit 15\n'
        '              [ "$FAKE_EMPTY_DUMP" = 1 ] || printf "synthetic-only-archive"; exit 0 ;;\n'
        '  esac\n'
        'fi\n'
        'if [ "$4" = pg_restore ] && [ "$5" = --list ]; then\n'
        '  cat >/dev/null\n'
        '  if [ "$FAKE_PRISMA_LEDGER" = 1 ]; then\n'
        '    echo "42; 0 12345 TABLE DATA public _prisma_migrations postgres"\n'
        '  else\n'
        '    echo "42; 0 12345 TABLE DATA public unrelated postgres"\n'
        '  fi\n'
        '  exit 0\n'
        'fi\n'
        'exit 30\n', encoding="utf-8")
    docker.chmod(0o700)
    sha = bindir / "sha256sum"
    sha.write_text('#!/bin/sh\necho "' + "a" * 64 + '  $1"\n', encoding="utf-8")
    sha.chmod(0o700)

    script = (
        "set -euo pipefail\n"
        "candidate=" + "b" * 40 + "\n"
        "run_id=42\nattempt=1\n"
        "postgres=shared_postgres_1\n"
        "database_name=" + database + "\n"
        'plaintext_dir="$FAKE_PLAINTEXT_DIR"\n'
        + _native_dump_stage()
    )
    env = {
        **os.environ,
        "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
        "FAKE_PLAINTEXT_DIR": str(tmp_path),
        "FAKE_DOCKER_LOG": str(tmp_path / "calls.log"),
        "FAKE_DUMP_FAIL": "1" if dump_fails else "0",
        "FAKE_EMPTY_DUMP": "1" if empty_dump else "0",
        "FAKE_PRISMA_LEDGER": "1" if prisma_ledger else "0",
    }
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                            text=True, timeout=15)
    log = tmp_path / "calls.log"
    return result, log.read_text() if log.exists() else ""


def test_release_backup_targets_only_verified_production_database(tmp_path):
    result, calls = _run_native_dump(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "pg_dump" in calls
    assert "gpoint_staging" in calls
    assert " -d gpoint " not in calls
    assert "pg_restore --list" in calls
    assert (tmp_path / ("gpoint-" + "b" * 40 + "-42-1.dump")).stat().st_size > 0


def test_release_backup_rejects_legacy_database_before_dump(tmp_path):
    result, calls = _run_native_dump(tmp_path, database="gpoint")
    assert result.returncode != 0
    assert calls == ""
    assert "DATABASE_IDENTITY_MISMATCH" in result.stderr


@pytest.mark.parametrize("failure,ledger,diagnostic", [
    (True, True, "GPOINT_RELEASE_BACKUP_FAILED=PG_DUMP"),
    (False, False, "GPOINT_RELEASE_BACKUP_FAILED=PRISMA_LEDGER_MISSING"),
])
def test_release_backup_fails_closed_before_encrypting_invalid_dump(
        tmp_path, failure, ledger, diagnostic):
    result, calls = _run_native_dump(tmp_path, dump_fails=failure, prisma_ledger=ledger)
    assert result.returncode != 0
    assert diagnostic in result.stderr
    assert "pg_dump" in calls


def test_release_backup_rejects_empty_archive_without_attempting_restore(tmp_path):
    result, calls = _run_native_dump(tmp_path, empty_dump=True)
    assert result.returncode != 0
    assert "GPOINT_RELEASE_BACKUP_FAILED=EMPTY_OR_UNSAFE_DUMP" in result.stderr
    assert "pg_restore" not in calls


def test_native_backup_does_not_invoke_legacy_agata_migration_helper():
    stage = _native_dump_stage()
    assert 'python3 "$backup_helper" backup' not in stage
    assert 'pg_dump -U "$POSTGRES_USER" -d "$1" --format=custom --no-owner' in stage
    assert "PG_RESTORE_LIST" in stage
    assert "PRISMA_LEDGER_MISSING" in stage


def test_eros_sqlx_backup_and_deploy_logic_not_modified_by_gpoint_db_fix():
    workflow = (ROOT / ".github/workflows/gpoint-release.yml").read_text(encoding="utf-8")
    assert "Rehearse exact Eros SQLx transition against protected backup restore" in workflow
    assert "Verify Eros SQLx ledger after the candidate boots" in workflow
    assert "scripts/eros-native-database.py" in workflow
