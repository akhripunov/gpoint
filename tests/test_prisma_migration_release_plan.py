import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prisma_migration_release_plan", ROOT / "scripts/prisma_migration_release_plan.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def migration(root: Path, name: str, sql: str) -> str:
    path = root / name / "migration.sql"
    path.parent.mkdir(parents=True)
    path.write_text(sql, encoding="utf-8")
    return MODULE.sha256(path.read_bytes())


def test_no_pending_migrations_skips_apply_without_changing_live_inventory(tmp_path):
    directory = tmp_path / "migrations"
    first = "20261001000000_baseline"
    digest = migration(directory, first, "SELECT 1;\n")
    result = MODULE.build_release_plan(
        directory, [first], {first: digest}, {first: digest}
    )
    assert result["apply"] is False
    assert result["pending"] == []
    assert result["applied"] == [first]


def test_single_and_multiple_pending_migrations_require_exact_approved_checksums(tmp_path):
    directory = tmp_path / "migrations"
    first = "20261001000000_baseline"
    second = "20261002000000_add_queue"
    third = "20261003000000_add_status"
    first_sha = migration(directory, first, "SELECT 1;\n")
    second_sha = migration(directory, second, "CREATE TABLE queue(id integer);\n")
    third_sha = migration(directory, third, "ALTER TABLE queue ADD COLUMN status text;\n")
    applied = [first]
    checksums = {first: first_sha}

    single = MODULE.build_release_plan(
        directory, [first, second], {first: first_sha, second: second_sha},
        {second: second_sha, third: third_sha},
    )
    assert single["apply"] is True
    assert single["pending"] == [{"name": third, "sha256": third_sha}]

    multiple = MODULE.build_release_plan(
        directory, applied, checksums,
        {second: second_sha, third: third_sha},
    )
    assert multiple["apply"] is True
    assert multiple["pending"] == [
        {"name": second, "sha256": second_sha},
        {"name": third, "sha256": third_sha},
    ]


def test_unapproved_pending_migration_blocks_the_whole_apply(tmp_path):
    directory = tmp_path / "migrations"
    first = "20261001000000_baseline"
    second = "20261002000000_add_queue"
    first_sha = migration(directory, first, "SELECT 1;\n")
    migration(directory, second, "CREATE TABLE queue(id integer);\n")
    with pytest.raises(ValueError, match="pending migration is not approved"):
        MODULE.build_release_plan(directory, [first], {first: first_sha}, {})


def test_allowlist_cannot_authorize_a_changed_pending_migration(tmp_path):
    directory = tmp_path / "migrations"
    first = "20261001000000_baseline"
    second = "20261002000000_add_queue"
    first_sha = migration(directory, first, "SELECT 1;\n")
    migration(directory, second, "CREATE TABLE queue(id integer);\n")
    with pytest.raises(ValueError, match="allowlist checksum differs from candidate source"):
        MODULE.build_release_plan(directory, [first], {first: first_sha}, {second: "0" * 64})


def test_candidate_must_preserve_exact_applied_prefix_and_checksums(tmp_path):
    directory = tmp_path / "migrations"
    first = "20261001000000_baseline"
    second = "20261002000000_add_queue"
    first_sha = migration(directory, first, "SELECT 1;\n")
    second_sha = migration(directory, second, "CREATE TABLE queue(id integer);\n")
    with pytest.raises(ValueError, match="applied migration history is not a candidate prefix"):
        MODULE.build_release_plan(directory, [second], {second: second_sha}, {second: second_sha})
    with pytest.raises(ValueError, match="applied migration checksum differs"):
        MODULE.build_release_plan(directory, [first], {first: "0" * 64}, {second: second_sha})


def test_allowlist_must_be_well_formed_and_only_name_candidate_migrations(tmp_path):
    directory = tmp_path / "migrations"
    first = "20261001000000_baseline"
    first_sha = migration(directory, first, "SELECT 1;\n")
    with pytest.raises(ValueError, match="allowlist schema is invalid"):
        MODULE.validate_allowlist({"schema": "wrong", "migrations": {}}, {first: first_sha})
    with pytest.raises(ValueError, match="allowlist contains an unknown migration"):
        MODULE.validate_allowlist(
            {"schema": MODULE.ALLOWLIST_SCHEMA, "migrations": {"20261002000000_stray": "0" * 64}},
            {first: first_sha},
        )


def test_allowlist_file_is_parsed_as_data_not_code(tmp_path):
    path = tmp_path / "approved.json"
    path.write_text(json.dumps({"schema": MODULE.ALLOWLIST_SCHEMA, "migrations": {}}), encoding="utf-8")
    assert MODULE.load_allowlist(path) == {}
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="allowlist schema is invalid"):
        MODULE.load_allowlist(path)
