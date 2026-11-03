#!/usr/bin/env python3
"""Fail-closed event and path routing for GPoint Native Release."""

from __future__ import annotations

import argparse
import base64
import json
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
SCHEMA_FILENAMES = frozenset({"schema.prisma"})
TEST_DIRECTORY_NAMES = frozenset({"test", "tests"})
PRISMA_MIGRATION = re.compile(
    r"^packages/db/prisma/migrations/[0-9]{14}_[a-z0-9_]+/migration\.sql$"
)
COMPONENT_ROLES = {
    "CORE": ("gpoint-core-api", "gpoint-core-worker", "gpoint-core-bot"),
    "AGATA": ("gpoint-agata-api", "gpoint-agata-memory-worker"),
    "MINIAPP": ("gpoint-miniapp-web",),
}
ROLE_REPOSITORIES = {
    "gpoint-core-api": "ghcr.io/akhripunov/gpoint-runtime",
    "gpoint-core-worker": "ghcr.io/akhripunov/gpoint-runtime",
    "gpoint-core-bot": "ghcr.io/akhripunov/gpoint-runtime",
    "gpoint-agata-api": "ghcr.io/akhripunov/gpoint-agata-runtime",
    "gpoint-agata-memory-worker": "ghcr.io/akhripunov/gpoint-agata-runtime",
    "gpoint-miniapp-web": "ghcr.io/akhripunov/gpoint-miniapp",
}
EXACT_SHA = re.compile(r"^[0-9a-f]{40}$")
EXACT_VERSION = re.compile(r"^[0-9a-f]{40}-[0-9]+-[0-9]+$")
EXACT_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
EXACT_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


def _path_kind(path: str) -> str:
    normalized = path.strip("/")
    parts = normalized.split("/")
    if not normalized or normalized.startswith("../") or "/../" in normalized:
        return "unknown"
    if normalized == "packages/db/prisma/schema.prisma":
        return "prisma_schema"
    if normalized.startswith("packages/db/prisma/migrations/"):
        return "prisma_migration" if PRISMA_MIGRATION.fullmatch(normalized) else "schema"
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


def classify_push(*, event_name: str, ref: str,
                  changed_paths: set[str] | frozenset[str]) -> str:
    if event_name != "push" or ref != "refs/heads/main":
        return "NO_DEPLOY"
    if not changed_paths:
        return "BLOCK"
    kinds = {_path_kind(path) for path in changed_paths}
    if "schema" in kinds or "unknown" in kinds:
        return "BLOCK"
    if "prisma_schema" in kinds and "prisma_migration" not in kinds:
        return "BLOCK"
    # Publishing to main authorizes the complete reviewed candidate. Docs,
    # release fixes and their app changes use the same Tests -> Build -> Deploy
    # run; no second manual run repeats CI just because a path is operational.
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


def changed_paths(before: str, sha: str, *, repo: Path = Path.cwd()) -> set[str]:
    if (not re.fullmatch(r"[0-9a-f]{40}", before)
            or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or before == "0" * 40):
        raise ValueError("push range must contain exact nonzero commit SHAs")
    result = subprocess.run(
        ["git", "diff", "--name-status", "-z", "--no-renames", f"{before}..{sha}"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    entries = result.stdout.split("\0")
    if not entries or entries.pop() != "" or len(entries) % 2:
        raise ValueError("push diff path inventory is malformed")
    paths = set()
    for status, path in zip(entries[::2], entries[1::2]):
        if status.startswith("D") and (path.startswith("packages/db/prisma/") or "migrations" in Path(path).parts):
            raise ValueError("deleting or moving a migration/schema path is blocked")
        paths.add(path)
    if not paths:
        raise ValueError("push range contains no changed paths")
    return paths


def _validate_role_inventory(inventory: dict) -> dict[str, dict[str, str]]:
    if not isinstance(inventory, dict) or not isinstance(inventory.get("roles"), dict):
        raise ValueError("verified role inventory is missing")
    roles = inventory["roles"]
    if set(roles) != set(ROLE_REPOSITORIES):
        raise ValueError("verified role inventory has an unexpected role set")
    component_identities: dict[str, tuple[str, str, str]] = {}
    for role, repository in ROLE_REPOSITORIES.items():
        row = roles[role]
        if not isinstance(row, dict):
            raise ValueError("verified role inventory is malformed")
        version, source, image_id, repo_digest = (
            row.get("version"), row.get("source_sha"), row.get("image_id"), row.get("repo_digest")
        )
        if (not isinstance(version, str) or not EXACT_VERSION.fullmatch(version)
                or not isinstance(source, str) or not EXACT_SHA.fullmatch(source)
                or not isinstance(image_id, str) or not EXACT_IMAGE_ID.fullmatch(image_id)
                or not isinstance(repo_digest, str)
                or not re.fullmatch(re.escape(repository) + r"@sha256:[0-9a-f]{64}", repo_digest)):
            raise ValueError("verified role inventory contains an invalid runtime identity")
        identity = (version, source, repo_digest)
        if repository in component_identities and component_identities[repository] != identity:
            raise ValueError("shared image roles have different runtime identities")
        component_identities[repository] = identity
    return roles


def _component_path(path: str) -> str | None:
    if path == "apps/miniapp/index.html" or path.startswith(("apps/miniapp/src/", "apps/miniapp/public/")):
        return "MINIAPP"
    if (path.startswith("services/agata/app/") or path.startswith("services/agata/tests/")
            or path in {"services/agata/Dockerfile", "services/agata/pyproject.toml",
                        "services/agata/requirements.txt", "services/agata/requirements.lock"}):
        return "AGATA"
    if path.startswith(("apps/api/", "apps/worker/", "apps/bot/")):
        return "CORE"
    return None


def _safe_component_path(path: str) -> bool:
    return (isinstance(path, str) and bool(path) and not path.startswith(("/", "./", "../"))
            and "\\" not in path and "\x00" not in path and "\n" not in path
            and all(part not in ("", ".", "..") for part in path.split("/")))


def classify_component_scope(repo: Path, candidate: str, inventory: dict) -> dict[str, str | bool]:
    """Classify a verified runtime baseline to exact candidate diff; ambiguity is FULL."""
    if not EXACT_SHA.fullmatch(candidate or ""):
        raise ValueError("candidate SHA is invalid")
    roles = _validate_role_inventory(inventory)
    sources = {row["source_sha"] for row in roles.values()}
    result = {"scope": "FULL", "reason": "full_candidate", "deploy_core": True,
              "deploy_agata": True, "deploy_miniapp": True}
    if len(sources) != 1:
        result["reason"] = "mixed_component_baseline"
        return result
    baseline = next(iter(sources))
    if candidate == baseline:
        result["reason"] = "candidate_equals_baseline"
        return result
    subprocess.run(["git", "merge-base", "--is-ancestor", baseline, candidate],
                   cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    raw = subprocess.check_output(["git", "diff", "--name-status", "-z", "--no-renames",
                                   f"{baseline}..{candidate}", "--"], cwd=repo)
    parts = raw.decode("utf8").split("\x00")
    if not parts or parts.pop() != "" or len(parts) % 2:
        raise ValueError("component scope diff is malformed")
    entries = list(zip(parts[::2], parts[1::2]))
    if not entries:
        result["reason"] = "empty_candidate_diff"
        return result
    components = set()
    for status, path in entries:
        if status not in ("A", "M"):
            return result
        component = _component_path(path) if _safe_component_path(path) else None
        if component is None:
            return result
        components.add(component)
        tree = subprocess.check_output(["git", "ls-tree", candidate, "--", path],
                                       cwd=repo).decode()
        if not tree.startswith(("100644 blob ", "100755 blob ")):
            return result
    if len(components) != 1:
        result["reason"] = "multiple_or_unmapped_components"
        return result
    component = next(iter(components))
    result.update({"scope": component, "reason": "exact_single_component_diff",
                   "deploy_core": component in ("CORE",),
                   "deploy_agata": component in ("AGATA",),
                   "deploy_miniapp": component in ("MINIAPP",)})
    return result


def expected_runtime_inventory(scope: str, inventory: dict, candidate: str, version: str,
                               app_digest: str, agata_digest: str, miniapp_digest: str) -> dict:
    """Bind every verified role to either the untouched baseline or exact candidate artifact."""
    roles = _validate_role_inventory(inventory)
    if (scope not in (*COMPONENT_ROLES, "FULL") or not EXACT_SHA.fullmatch(candidate or "")
            or not EXACT_VERSION.fullmatch(version or "") or not version.startswith(candidate + "-")):
        raise ValueError("candidate component runtime plan is malformed")
    digests = {"CORE": app_digest, "AGATA": agata_digest, "MINIAPP": miniapp_digest}
    if any(not EXACT_DIGEST.fullmatch(value or "") for value in digests.values()):
        raise ValueError("candidate runtime image digest is invalid")
    updated = set(COMPONENT_ROLES) if scope == "FULL" else {scope}
    inventory_out = {}
    group_repo_digests = {
        "CORE": f"{ROLE_REPOSITORIES['gpoint-core-api']}@{app_digest}",
        "AGATA": f"{ROLE_REPOSITORIES['gpoint-agata-api']}@{agata_digest}",
        "MINIAPP": f"{ROLE_REPOSITORIES['gpoint-miniapp-web']}@{miniapp_digest}",
    }
    for component, component_roles in COMPONENT_ROLES.items():
        for role in component_roles:
            baseline = roles[role]
            inventory_out[role] = ({"version": version, "sourceSha": candidate,
                                    "repositoryDigest": group_repo_digests[component],
                                    "baselineImageId": None}
                                   if component in updated else
                                   {"version": baseline["version"], "sourceSha": baseline["source_sha"],
                                    "repositoryDigest": baseline["repo_digest"],
                                    "baselineImageId": baseline["image_id"]})
    return {"schema": 1, "candidateSha": candidate, "scope": scope,
            "updatedComponents": sorted(updated), "roles": inventory_out}


def expected_runtime_inventory_b64(*args, **kwargs) -> str:
    value = expected_runtime_inventory(*args, **kwargs)
    return base64.b64encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).decode()


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
    args = parser.parse_args()

    try:
        if args.event == "push":
            paths = changed_paths(args.before, args.sha)
            mode = classify_push(event_name=args.event, ref=args.ref, changed_paths=paths)
            deploy = mode == "DEPLOY"
        elif args.event == "workflow_dispatch":
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
    if mode == "BLOCK" or mode.startswith("INVALID_"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
