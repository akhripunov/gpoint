"""The pending-migration/app-code dependency is detectable before Build."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify-kamal-migration-parity.py"
MIGRATION = "20261105100000_gpoint_chat_policy_and_history_fences"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path, code_line):
    repo = tmp_path / "repo"
    (repo / "packages/db/prisma/migrations").mkdir(parents=True)
    (repo / "apps/api/src").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "apps/api/src/agata-worker-gateway.ts").write_text("// placeholder\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    baseline = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()

    migration_dir = repo / "packages/db/prisma/migrations" / MIGRATION
    migration_dir.mkdir(parents=True)
    (migration_dir / "migration.sql").write_text(
        'ALTER TABLE "chat_policy" ADD COLUMN "history_fences" TEXT;\n')
    (repo / "apps/api/src/agata-worker-gateway.ts").write_text(code_line + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "candidate")
    candidate = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                               capture_output=True, text=True).stdout.strip()
    return repo, baseline, candidate


def _run(repo, baseline, candidate):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--static-pending",
         "--repo-root", str(repo),
         "--baseline-source-sha", baseline, "--candidate-source-sha", candidate,
         "--prisma-directory", "packages/db/prisma/migrations"],
        capture_output=True, text=True)


def test_pending_migration_dependency_blocks_before_build(tmp_path):
    repo, baseline, candidate = _repo(
        tmp_path, "const fences = row.history_fences;")
    result = _run(repo, baseline, candidate)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "GPOINT_KAMAL_SCHEMA_DEPENDENCY=BLOCKED" in result.stderr
    assert f"PENDING_SCHEMA_DEPENDENCY:{MIGRATION}" in result.stderr


def test_pending_migration_without_code_dependency_passes(tmp_path):
    repo, baseline, candidate = _repo(tmp_path, "const unrelated = 1;")
    result = _run(repo, baseline, candidate)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "GPOINT_KAMAL_SCHEMA_DEPENDENCY=PASS" in result.stdout
    assert MIGRATION in result.stdout
