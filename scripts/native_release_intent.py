#!/usr/bin/env python3
"""Fail-closed event and path routing for GPoint Native Release."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


APP_PREFIXES = (
    "apps/",
    "services/agata/",
    "packages/agata-client/",
    "packages/contracts/",
    "packages/db/",
)
APP_ROOT_FILES = frozenset({
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
})
OPERATIONAL_PREFIXES = (
    ".agents/",
    ".codex/",
    ".github/",
    ".kamal/",
    "config/",
    "design/",
    "docs/",
    "infra/",
    "scripts/",
    "services/gsales/",
    "services/eros-engine/",
    "systemd/",
    "tests/",
    "tools/",
)
EROS_RUNTIME_PREFIXES = (
    "services/eros-engine/",
    "config/deploy.eros",
)
EROS_RUNTIME_FILES = frozenset({
    "scripts/eros-native-pilot-lifecycle.sh",
    "scripts/eros-pilot-mac-tunnel-service.sh",
    "scripts/eros-production-secrets.py",
    "scripts/eros_native_pilot_ops.py",
    "scripts/eros_native_release_compatibility.py",
    "scripts/verify-kamal-eros-pilot-runtime.py",
})
EROS_BOOTSTRAP_BASE_SHA = "35bfad2a42384adca55464b33cee72589a1247ad"
# Filled from the reviewed ME-36 bootstrap payload. The digest binds every
# blocked runtime file/tree entry to this one initial main-push range.
EROS_BOOTSTRAP_TREE_SHA256 = "d5a3afea6038f3e1970ebb0e77b0c544674e0062bcce00362bc9d46fe810d9b9"
EROS_BOOTSTRAP_PATHS = (
    "services/eros-engine/",
    # Hash the whole config tree so a newly added deploy.eros* path cannot
    # escape the exact initial payload fingerprint.
    "config/",
    *sorted(EROS_RUNTIME_FILES),
)
SCHEMA_FILENAMES = frozenset({"schema.prisma"})
TEST_DIRECTORY_NAMES = frozenset({"test", "tests"})
SYNTHETIC_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _path_kind(path: str) -> str:
    normalized = path.strip("/")
    parts = normalized.split("/")
    if not normalized or normalized.startswith("../") or "/../" in normalized:
        return "unknown"
    if "migrations" in parts or Path(normalized).name in SCHEMA_FILENAMES:
        return "schema"
    if any(part in TEST_DIRECTORY_NAMES for part in parts):
        return "test"
    if normalized.startswith("packages/gpoint-codex-mcp/"):
        return "ops"
    if normalized in APP_ROOT_FILES:
        return "app"
    if normalized.startswith(APP_PREFIXES):
        return "app"
    if normalized.startswith(OPERATIONAL_PREFIXES):
        return "ops"
    if normalized in {"AGENTS.md", "README.md", "pyproject.toml"}:
        return "ops"
    return "unknown"


def eros_runtime_tree_digest(sha: str, repo: Path = Path.cwd()) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("bootstrap candidate SHA is malformed")
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--full-tree", "-z", sha, "--", *EROS_BOOTSTRAP_PATHS],
        cwd=repo, check=True, capture_output=True,
    )
    if not result.stdout:
        raise ValueError("bootstrap runtime tree is empty")
    import hashlib
    return hashlib.sha256(result.stdout).hexdigest()


def is_eros_bootstrap_range(*, before: str, sha: str,
                            changed_paths: set[str] | frozenset[str],
                            repo: Path = Path.cwd()) -> bool:
    if before != EROS_BOOTSTRAP_BASE_SHA:
        return False
    has_eros_runtime_change = any(
        path.startswith(EROS_RUNTIME_PREFIXES) or path in EROS_RUNTIME_FILES
        for path in changed_paths
    )
    if not has_eros_runtime_change:
        return False
    return eros_runtime_tree_digest(sha, repo) == EROS_BOOTSTRAP_TREE_SHA256


def classify_push(*, event_name: str, ref: str,
                  changed_paths: set[str] | frozenset[str],
                  before: str = "", sha: str = "", repo: Path = Path.cwd()) -> str:
    if event_name != "push" or ref != "refs/heads/main":
        return "NO_DEPLOY"
    if not changed_paths:
        return "BLOCK"
    if any(path.startswith(EROS_RUNTIME_PREFIXES) or path in EROS_RUNTIME_FILES
           for path in changed_paths) and not is_eros_bootstrap_range(
               before=before, sha=sha, changed_paths=changed_paths, repo=repo):
        return "BLOCK"
    kinds = {_path_kind(path) for path in changed_paths}
    if "schema" in kinds or "unknown" in kinds:
        return "BLOCK"
    # An authorized main push is the existing Native release intent. The
    # workflow still runs Tests -> immutable Build/read-back -> Deploy -> Verify.
    return "DEPLOY"


def classify_dispatch(*, ref: str, candidate_sha: str, github_sha: str, actor: str,
                      repository_owner: str, migration_rehearsal: bool,
                      approve_production: bool,
                      backup_maintenance: bool = False,
                      rollback: bool = False) -> tuple[str, bool]:
    exact = bool(re.fullmatch(r"[0-9a-f]{40}", candidate_sha)) and candidate_sha == github_sha
    owner = bool(repository_owner) and actor.lower() == repository_owner.lower()
    if rollback and (migration_rehearsal or backup_maintenance):
        return "INVALID_ROLLBACK", False
    if migration_rehearsal and backup_maintenance:
        return "INVALID_REHEARSAL", False
    if rollback:
        if ref == "refs/heads/main" and exact and owner and approve_production:
            return "ROLLBACK", False
        return "INVALID_ROLLBACK", False
    if migration_rehearsal:
        if (ref == "refs/heads/main" and exact and owner and not approve_production):
            return "REHEARSAL", False
        return "INVALID_REHEARSAL", False
    if backup_maintenance:
        if (ref == "refs/heads/main" and exact and owner and approve_production):
            return "BACKUP_MAINTENANCE", False
        return "INVALID_BACKUP_MAINTENANCE", False
    if ref == "refs/heads/main" and exact and owner and approve_production:
        return "DEPLOY", True
    if ref == "refs/heads/main":
        return "INVALID_DEPLOY", False
    return "NO_DEPLOY", False


def _single_synthetic_id(value: str) -> bool:
    values = [item.strip() for item in value.split(",") if item.strip()]
    return len(values) == 1 and SYNTHETIC_UUID.fullmatch(values[0]) is not None


def classify_eros_pilot_dispatch(*, event_name: str, ref: str, candidate_sha: str,
                                 github_sha: str, actor: str, repository_owner: str,
                                 opt_in: bool, approve_apply: bool,
                                 environment_approved: bool, approve_production: bool,
                                 migration_rehearsal: bool, backup_maintenance: bool,
                                 rollback: bool, test_user_ids: str,
                                 test_creator_ids: str, cleanup_pilot: bool = False,
                                 approve_cleanup: bool = False) -> tuple[str, bool]:
    """Route a single-pair synthetic pilot without opening ordinary Deploy."""
    verify_branch = bool(re.fullmatch(r"refs/heads/codex/[A-Za-z0-9._/-]+", ref))
    eligible = (
        event_name == "workflow_dispatch" and (ref == "refs/heads/main" or verify_branch) and
        bool(re.fullmatch(r"[0-9a-f]{40}", candidate_sha)) and
        candidate_sha == github_sha and bool(repository_owner) and
        actor.lower() == repository_owner.lower() and opt_in is True and
        approve_production is False and migration_rehearsal is False and
        backup_maintenance is False and rollback is False and
        _single_synthetic_id(test_user_ids) and _single_synthetic_id(test_creator_ids)
    )
    if not eligible:
        return "INVALID_SYNTHETIC_PILOT", False
    if verify_branch:
        if approve_apply or environment_approved or cleanup_pilot or approve_cleanup:
            return "INVALID_SYNTHETIC_PILOT", False
        return "SYNTHETIC_PILOT_VERIFY_ONLY", False
    if cleanup_pilot:
        if approve_apply or (approve_cleanup and not environment_approved):
            return "INVALID_SYNTHETIC_PILOT_CLEANUP", False
        if not approve_cleanup:
            return ("INVALID_SYNTHETIC_PILOT_CLEANUP" if environment_approved
                    else "SYNTHETIC_PILOT_CLEANUP_REQUESTED"), False
        return "SYNTHETIC_PILOT_CLEANUP", False
    if approve_cleanup:
        return "INVALID_SYNTHETIC_PILOT_CLEANUP", False
    if not approve_apply:
        if environment_approved:
            return "INVALID_SYNTHETIC_PILOT", False
        return "SYNTHETIC_PILOT_READY", False
    if not environment_approved:
        return "SYNTHETIC_PILOT_APPLY_REQUESTED", False
    return "SYNTHETIC_PILOT_APPLY", False


def changed_paths(before: str, sha: str, *, repo: Path = Path.cwd()) -> set[str]:
    if (not re.fullmatch(r"[0-9a-f]{40}", before)
            or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or before == "0" * 40):
        raise ValueError("push range must contain exact nonzero commit SHAs")
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", f"{before}..{sha}"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    paths = set(filter(None, result.stdout.splitlines()))
    if not paths:
        raise ValueError("push range contains no changed paths")
    return paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--before", default="")
    parser.add_argument("--candidate-sha", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--repository-owner", default="")
    parser.add_argument("--migration-rehearsal", action="store_true")
    parser.add_argument("--backup-maintenance", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--approve-production", action="store_true")
    parser.add_argument("--synthetic-eros-pilot", action="store_true")
    parser.add_argument("--approve-synthetic-eros-pilot", action="store_true")
    parser.add_argument("--synthetic-eros-pilot-cleanup", action="store_true")
    parser.add_argument("--approve-synthetic-eros-pilot-cleanup", action="store_true")
    parser.add_argument("--pilot-environment-approved", action="store_true")
    args = parser.parse_args()

    pilot_apply = False
    pilot_cleanup = False
    try:
        if args.event == "push":
            paths = changed_paths(args.before, args.sha)
            mode = classify_push(event_name=args.event, ref=args.ref, changed_paths=paths,
                                 before=args.before, sha=args.sha)
            deploy = mode == "DEPLOY"
        elif args.event == "workflow_dispatch":
            if (args.synthetic_eros_pilot or args.approve_synthetic_eros_pilot
                    or args.synthetic_eros_pilot_cleanup or args.approve_synthetic_eros_pilot_cleanup):
                mode, deploy = classify_eros_pilot_dispatch(
                    event_name=args.event, ref=args.ref,
                    candidate_sha=args.candidate_sha, github_sha=args.sha,
                    actor=args.actor, repository_owner=args.repository_owner,
                    opt_in=args.synthetic_eros_pilot,
                    approve_apply=args.approve_synthetic_eros_pilot,
                    environment_approved=args.pilot_environment_approved,
                    approve_production=args.approve_production,
                    migration_rehearsal=args.migration_rehearsal,
                    backup_maintenance=args.backup_maintenance,
                    rollback=args.rollback,
                    test_user_ids=os.environ.get("EROS_PILOT_TEST_USER_ID", ""),
                    test_creator_ids=os.environ.get("EROS_PILOT_TEST_CREATOR_ID", ""),
                    cleanup_pilot=args.synthetic_eros_pilot_cleanup,
                    approve_cleanup=args.approve_synthetic_eros_pilot_cleanup,
                )
                pilot_apply = mode == "SYNTHETIC_PILOT_APPLY"
                pilot_cleanup = mode == "SYNTHETIC_PILOT_CLEANUP"
            else:
                mode, deploy = classify_dispatch(
                    ref=args.ref, candidate_sha=args.candidate_sha, github_sha=args.sha,
                    actor=args.actor, repository_owner=args.repository_owner,
                    migration_rehearsal=args.migration_rehearsal,
                    approve_production=args.approve_production,
                    backup_maintenance=args.backup_maintenance,
                    rollback=args.rollback,
                )
        else:
            mode, deploy = "NO_DEPLOY", False
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print("NATIVE_RELEASE_INTENT=BLOCK", file=sys.stderr)
        print(f"reason={str(error)[:180]}", file=sys.stderr)
        return 2

    print(f"mode={mode}")
    print(f"deploy={'true' if deploy else 'false'}")
    print(f"pilot_apply={'true' if pilot_apply else 'false'}")
    print(f"pilot_cleanup={'true' if pilot_cleanup else 'false'}")
    if mode == "BLOCK" or mode.startswith("INVALID_"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
