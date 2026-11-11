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


def _repo_accumulated(tmp_path):
    """Migration added in an earlier commit, code dependency only later."""
    repo = tmp_path / "repo_accum"
    (repo / "packages/db/prisma/migrations").mkdir(parents=True)
    (repo / "apps/api/src").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "apps/api/src/agata-worker-gateway.ts").write_text("// placeholder\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "base")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    migration_dir = repo / "packages/db/prisma/migrations" / MIGRATION
    migration_dir.mkdir(parents=True)
    (migration_dir / "migration.sql").write_text(
        'ALTER TABLE "chat_policy" ADD COLUMN "history_fences" TEXT;\n')
    # The dependent application line landed in the same earlier commit.
    (repo / "apps/api/src/agata-worker-gateway.ts").write_text(
        "const fences = row.history_fences;\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "migration earlier")
    # Later commit touches only a config file, exactly like the failing push.
    (repo / "config").mkdir(exist_ok=True)
    (repo / "config/deploy.eros.yml").write_text("a: b\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "config only")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    return repo, base, head


def test_accumulated_pending_migration_is_detected_from_an_older_base(tmp_path):
    # The real blocker: the migration landed on 7 Nov, the failing push on
    # 10 Nov only changed Eros config. With the last release as the base the
    # dependency is still visible before Build.
    repo, base, head = _repo_accumulated(tmp_path)
    result = _run(repo, base, head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"PENDING_SCHEMA_DEPENDENCY:{MIGRATION}" in result.stderr


def test_diff_only_base_misses_the_accumulated_migration(tmp_path):
    # Documents the limit that motivated using the last release as the base.
    repo, _, head = _repo_accumulated(tmp_path)
    previous = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD^"],
                              capture_output=True, text=True).stdout.strip()
    result = _run(repo, previous, head)
    assert result.returncode == 0
    assert "pending=none" in result.stdout


def test_workflow_dispatch_never_blocks_on_a_missing_base():
    import yaml
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/gpoint-native-release.yml").read_text())
    step = next(s for s in workflow["jobs"]["tests"]["steps"]
                if s.get("name") == "Detect pending schema dependencies before Build")
    script = step["run"]
    # Per-event base selection.
    assert 'pull_request) base="$PR_BASE_SHA"' in script
    assert "deployments?environment=production" in script
    assert 'base="$EVENT_BEFORE"' in script
    # A manual dispatch without a resolvable base reports SKIPPED instead of
    # failing closed on an unknown revision.
    assert 'SKIPPED reason=no_base_revision event=$EVENT_NAME' in script
    assert 'SKIPPED reason=base_not_in_checkout' in script
    assert "exit 0" in script
    # The live, database-backed parity gate is untouched.
    live = next(s for s in workflow["jobs"]["deploy"]["steps"]
                if s.get("name") == "Verify candidate compatibility with the live database baseline")
    assert "--prisma-current-head" in live["run"]
