"""Pending-migration/app-code dependency detection.

Generic payload words (`purpose`, `role`) must not fake a dependency. A real
dependency is admitted only when its pending migration is in the OWNER-approved
allowlist with the exact checksum; otherwise it stays fail-closed.
"""

import hashlib
import importlib.util
import json
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "migration_parity", ROOT / "scripts/verify-kamal-migration-parity.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

OLD_MIGRATION = "20260101000000_old"
NEW_MIGRATION = "20261105100000_gpoint_chat_policy_and_history_fences"
NEW_SQL = '''
CREATE TABLE IF NOT EXISTS "GpointChatConsent" (
  "id" TEXT PRIMARY KEY,
  "sourceTenantId" TEXT NOT NULL,
  "purpose" TEXT NOT NULL,
  "role" TEXT NOT NULL
);
'''.strip()


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo, text=True).strip()


def build_repo(tmp_path, candidate_source, *, allowlist=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    prisma = repo / "packages/db/prisma"
    old = prisma / "migrations" / OLD_MIGRATION
    old.mkdir(parents=True)
    (old / "migration.sql").write_text(
        'CREATE TABLE "GpointBase" ("id" TEXT PRIMARY KEY);\n', encoding="utf-8")
    agata = repo / "services/agata/migrations/versions"
    agata.mkdir(parents=True)
    (agata / "0001_init.py").write_text(textwrap.dedent('''\
        revision = "0001"
        down_revision = None

        def upgrade():
            pass

        def downgrade():
            pass
    '''), encoding="utf-8")
    (repo / "apps/api/src").mkdir(parents=True)
    (repo / "apps/api/src/consumer.ts").write_text("export const base = 1;\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")
    baseline = git(repo, "rev-parse", "HEAD")

    new = prisma / "migrations" / NEW_MIGRATION
    new.mkdir(parents=True)
    (new / "migration.sql").write_text(NEW_SQL + "\n", encoding="utf-8")
    digest = hashlib.sha256((new / "migration.sql").read_bytes()).hexdigest()
    if allowlist is not None:
        (prisma / "production-approved-migrations.json").write_text(json.dumps({
            "schema": "gpoint-prisma-production-apply/v1",
            "migrations": {NEW_MIGRATION: allowlist(digest) if callable(allowlist) else allowlist},
        }), encoding="utf-8")
    (repo / "apps/api/src/consumer.ts").write_text(candidate_source, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "candidate")
    candidate = git(repo, "rev-parse", "HEAD")
    return repo, baseline, candidate


def static_args(repo, baseline, candidate):
    return SimpleNamespace(
        repo_root=str(repo), baseline_source_sha=baseline, candidate_source_sha=candidate,
        prisma_directory=str(repo / "packages/db/prisma/migrations"),
        approved_migrations=str(repo / "packages/db/prisma/production-approved-migrations.json"))


def live_args(repo, baseline, candidate):
    return SimpleNamespace(
        repo_root=str(repo), baseline_source_sha=baseline, candidate_source_sha=candidate,
        prisma_directory=str(repo / "packages/db/prisma/migrations"),
        prisma_current_head=OLD_MIGRATION, agata_current_head="0001",
        agata_current_checksums="",
        agata_directory=str(repo / "services/agata/migrations/versions"),
        approved_migrations=str(repo / "packages/db/prisma/production-approved-migrations.json"))


def run_static(repo, baseline, candidate):
    return MODULE.verify_static_pending(static_args(repo, baseline, candidate))


def run_live(repo, baseline, candidate):
    args = live_args(repo, baseline, candidate)
    checksum = hashlib.sha256(
        (repo / "packages/db/prisma/migrations" / OLD_MIGRATION / "migration.sql").read_bytes()
    ).hexdigest()
    return MODULE.verify_compatible(args, [OLD_MIGRATION], {OLD_MIGRATION: checksum})


def both(repo, baseline, candidate):
    return run_static(repo, baseline, candidate), run_live(repo, baseline, candidate)


REAL_DEPENDENCY = "const row = { sourceTenantId: tenant };\n"


def test_unrelated_eros_purpose_is_not_a_schema_dependency(tmp_path):
    source = 'export function check(body: any) {\n  return body.purpose !== "eros_chat_completion";\n}\n'
    repo, baseline, candidate = build_repo(tmp_path, source)
    both(repo, baseline, candidate)


def test_unrelated_role_payload_is_not_a_schema_dependency(tmp_path):
    repo, baseline, candidate = build_repo(tmp_path, 'export const role = "assistant";\n')
    both(repo, baseline, candidate)


def test_table_and_generic_column_still_blocks_without_approval(tmp_path):
    source = 'await db.gpointChatConsent.update({ data: { purpose: value } });\n'
    repo, baseline, candidate = build_repo(tmp_path, source)
    with pytest.raises(ValueError, match="UNSAFE_SCHEMA_DEPENDENCY"):
        run_static(repo, baseline, candidate)
    with pytest.raises(ValueError, match="UNSAFE_SCHEMA_DEPENDENCY"):
        run_live(repo, baseline, candidate)


def test_approved_migration_admits_the_real_dependency(tmp_path):
    repo, baseline, candidate = build_repo(
        tmp_path, REAL_DEPENDENCY, allowlist=lambda digest: digest)
    both(repo, baseline, candidate)


def test_unapproved_migration_keeps_the_dependency_blocked(tmp_path):
    repo, baseline, candidate = build_repo(tmp_path, REAL_DEPENDENCY)
    with pytest.raises(ValueError, match="UNSAFE_SCHEMA_DEPENDENCY"):
        run_static(repo, baseline, candidate)
    with pytest.raises(ValueError, match="UNSAFE_SCHEMA_DEPENDENCY"):
        run_live(repo, baseline, candidate)


def test_checksum_mismatch_blocks_even_when_listed(tmp_path):
    repo, baseline, candidate = build_repo(
        tmp_path, REAL_DEPENDENCY, allowlist=lambda digest: "0" * 64)
    with pytest.raises(ValueError, match="APPROVED_MIGRATION_CHECKSUM_MISMATCH"):
        run_static(repo, baseline, candidate)
    with pytest.raises(ValueError, match="APPROVED_MIGRATION_CHECKSUM_MISMATCH"):
        run_live(repo, baseline, candidate)


def test_unknown_baseline_is_rejected(tmp_path):
    repo, baseline, candidate = build_repo(
        tmp_path, REAL_DEPENDENCY, allowlist=lambda digest: digest)
    args = static_args(repo, "", candidate)
    with pytest.raises(ValueError, match="baseline-source-sha"):
        MODULE.verify_static_pending(args)
