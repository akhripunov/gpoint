from pathlib import Path

import pytest

from scripts.prisma_migration_checksums import (
    build_inventory,
    verify_applied_migrations,
)


def test_inventory_is_complete_sorted_and_sha256_bound_to_source(tmp_path: Path):
    migrations = tmp_path / "migrations"
    for name, sql in (
        ("20261002000000_first", "SELECT 1;\n"),
        ("20261003000000_second", "SELECT 2;\n"),
    ):
        path = migrations / name / "migration.sql"
        path.parent.mkdir(parents=True)
        path.write_text(sql)

    assert build_inventory(migrations) == [
        ("20261002000000_first", "b4e0497804e46e0a0b0b8c31975b062152d551bac49c3c2e80932567b4085dcd"),
        ("20261003000000_second", "a41109d24069b4822ddc5f367b25d484dc7e839bff338ce7a3e5da641caacda0"),
    ]


def test_database_history_is_verified_against_source_manifest_not_image_files():
    source = [
        "20261002000000_first=" + "a" * 64,
        "20261003000000_second=" + "b" * 64,
        "20261004000000_pending=" + "c" * 64,
    ]
    applied = source[:2]

    assert verify_applied_migrations(source, applied, expected_count=2) == 2


@pytest.mark.parametrize(
    ("source", "applied"),
    [
        (["20261002000000_first=" + "a" * 64], ["20261002000000_first=" + "b" * 64]),
        (["20261002000000_first=" + "a" * 64], ["20261003000000_missing=" + "a" * 64]),
        (["20261002000000_first=" + "a" * 64] * 2, ["20261002000000_first=" + "a" * 64]),
    ],
)
def test_database_history_rejects_drift_unknown_and_duplicate_entries(source, applied):
    with pytest.raises(ValueError):
        verify_applied_migrations(source, applied, expected_count=1)


def test_rehearsal_verifies_old_history_from_exact_source_manifest():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/gpoint-native-release.yml").read_text()
    verifier = (root / "scripts/verify-gpoint-scheduled-backup-restore.sh").read_text()

    assert "prisma_migration_checksums.py write" in workflow
    assert "prisma_migration_checksums.py" in workflow
    assert "--manifest \"$GPOINT_PRISMA_SOURCE_CHECKSUM_MANIFEST\"" in verifier
    assert "--expected-count 18" in verifier
    assert 'docker run --rm --network none --entrypoint sha256sum "$app_image" \\\n+      "/app/packages/db/prisma/migrations/$migration_name/migration.sql"' not in verifier
