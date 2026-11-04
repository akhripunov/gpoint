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
KNOWN_NON_RUNTIME_FILES = frozenset({
    "AGENTS.md",
    "packages/db/prisma/production-approved-migrations.json",
    "scripts/classify-release-prepush.py",
    "scripts/check-kamal-configs.sh",
    "scripts/gpoint-backup-policy.py",
    "scripts/gpoint-scheduled-backup.py",
    "scripts/native_release_intent.py",
    "scripts/prisma_migration_release_plan.py",
    "scripts/resolve_native_environment_deployment.py",
    "scripts/resolve_native_build_scope.py",
    "scripts/verify-gpoint-scheduled-backup-restore.sh",
    "scripts/verify-kamal-migration-parity.py",
    "scripts/verify-kamal-production-baseline.py",
    "scripts/verify_native_rollback_evidence.py",
    "scripts/write-kamal-production-manifest.py",
    "systemd/gpoint-backup.service",
})
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
    # Prisma schema changes must reach hosted Tests, which compare the full
    # migration history with schema.prisma before Build. Production Apply still
    # requires an exact migration set in the approved migration manifest.
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
                or not isinstance(image_id, str) or not EXACT_DIGEST.fullmatch(image_id)
                or not isinstance(repo_digest, str)
                or not re.fullmatch(re.escape(repository) + r"@sha256:[0-9a-f]{64}", repo_digest)):
            raise ValueError("verified role inventory contains an invalid runtime identity")
        identity = (version, source, repo_digest)
        if repository in component_identities and component_identities[repository] != identity:
            raise ValueError("shared image roles have different runtime identities")
        component_identities[repository] = identity
    return roles


def _component_paths(path: str) -> set[str]:
    if (PRISMA_MIGRATION.fullmatch(path)
            or path == "packages/db/prisma/production-approved-migrations.json"):
        return set()
    if path == "packages/db/prisma/schema.prisma":
        return {"CORE"}
    if path == "apps/miniapp/index.html" or path.startswith(("apps/miniapp/src/", "apps/miniapp/public/")):
        return {"MINIAPP"}
    if path.startswith("services/agata/"):
        return {"AGATA"}
    if path in {"infra/production/Gpoint.Dockerfile", "infra/production/nginx-miniapp.conf"}:
        return {"CORE", "MINIAPP"} if path.endswith("Gpoint.Dockerfile") else {"MINIAPP"}
    if path in APP_ROOT_FILES or path in {"pnpm-lock.yaml", "pnpm-workspace.yaml", "tsconfig.base.json"}:
        return {"CORE", "MINIAPP"}
    if path.startswith("apps/"):
        return {"CORE"}
    if path.startswith("packages/") and not path.startswith("packages/gpoint-codex-mcp/"):
        return {"CORE", "MINIAPP"}
    return set()


def _non_runtime_documentation_path(path: str) -> bool:
    return (path == "README.md"
            or (path.startswith("docs/") and path.endswith((".md", ".mdx"))))


def _known_non_runtime_path(path: str) -> bool:
    """Recognize only verified release/test metadata outside application images."""
    return (_non_runtime_documentation_path(path)
            or path in KNOWN_NON_RUNTIME_FILES
            or PRISMA_MIGRATION.fullmatch(path) is not None
            or path.startswith("tests/")
            or (path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")))
            or (path.startswith(".codex/roles/") and path.endswith(".md")))


def _safe_component_path(path: str) -> bool:
    return (isinstance(path, str) and bool(path) and not path.startswith(("/", "./", "../"))
            and "\\" not in path and "\x00" not in path and "\n" not in path
            and all(part not in ("", ".", "..") for part in path.split("/")))


def classify_component_build_plan(repo: Path, candidate: str,
                                  component_sources: dict[str, str]) -> dict[str, object]:
    """Classify runtime updates and image-only Prisma migration build impact."""
    if not EXACT_SHA.fullmatch(candidate or ""):
        raise ValueError("candidate SHA is invalid")
    if (not isinstance(component_sources, dict)
            or set(component_sources) != set(COMPONENT_ROLES)):
        raise ValueError("component production source inventory is incomplete")
    for component, source in component_sources.items():
        if not isinstance(source, str) or not EXACT_SHA.fullmatch(source):
            raise ValueError(f"{component} production source SHA is invalid")
    paths_by_baseline: dict[str, set[str]] = {}
    all_paths: set[str] = set()
    non_runtime_only = True
    for baseline in component_sources.values():
        if baseline == candidate or baseline in paths_by_baseline:
            continue
        ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", baseline, candidate],
                                  cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ancestor.returncode:
            raise ValueError("component production source is not an ancestor of the candidate")
        raw = subprocess.check_output(["git", "diff", "--name-status", "-z", "--no-renames",
                                       f"{baseline}..{candidate}", "--"], cwd=repo)
        parts = raw.decode("utf8").split("\x00")
        if not parts or parts.pop() != "" or len(parts) % 2:
            raise ValueError("component scope diff is malformed")
        entries = list(zip(parts[::2], parts[1::2]))
        paths = set()
        for status, path in entries:
            if status not in ("A", "M"):
                raise ValueError("deleted or renamed candidate path needs explicit scope approval")
            if not _safe_component_path(path):
                raise ValueError("unmapped or unsafe candidate path needs explicit scope approval")
            tree = subprocess.check_output(["git", "ls-tree", candidate, "--", path],
                                           cwd=repo).decode()
            if not tree.startswith(("100644 blob ", "100755 blob ")):
                raise ValueError("candidate path is not a regular file and needs explicit scope approval")
            changed_components = _component_paths(path)
            all_paths.add(path)
            if not changed_components:
                if _known_non_runtime_path(path):
                    continue
                raise ValueError("unmapped or unsafe candidate path needs explicit scope approval")
            non_runtime_only = False
            paths.update(changed_components)
        paths_by_baseline[baseline] = paths

    components = {
        component for component, baseline in component_sources.items()
        if baseline != candidate and component in paths_by_baseline[baseline]
    }
    if not components:
        if all(source == candidate for source in component_sources.values()):
            raise ValueError("candidate equals production baseline; no changed component scope")
        if not non_runtime_only:
            raise ValueError("candidate diff contains no changed runtime component")
    runtime_scope = "+".join(sorted(components)) if components else "NONE"
    build_components = set(components)
    if any(PRISMA_MIGRATION.fullmatch(path) for path in all_paths):
        build_components.add("CORE")
    return {
        "deploy_scope": runtime_scope,
        "build_components": sorted(build_components),
        "reason": "exact_component_diff" if components else "no_runtime_changes",
    }


def classify_component_scope(repo: Path, candidate: str, inventory: dict) -> dict[str, str | bool]:
    """Classify the exact candidate diff; never broaden an ambiguous scope."""
    roles = _validate_role_inventory(inventory)
    component_sources = {
        component: roles[component_roles[0]]["source_sha"]
        for component, component_roles in COMPONENT_ROLES.items()
    }
    plan = classify_component_build_plan(repo, candidate, component_sources)
    components = set(str(plan["deploy_scope"]).split("+")) - {"NONE"}
    return {
        "scope": str(plan["deploy_scope"]),
        "reason": str(plan["reason"]),
        "deploy_core": "CORE" in components,
        "deploy_agata": "AGATA" in components,
        "deploy_miniapp": "MINIAPP" in components,
    }


def expected_runtime_inventory(scope: str, inventory: dict, candidate: str, version: str,
                               app_digest: str, agata_digest: str, miniapp_digest: str) -> dict:
    """Bind every verified role to either the untouched baseline or exact candidate artifact."""
    roles = _validate_role_inventory(inventory)
    scope_components = (set() if scope == "NONE" else
                        set(scope.split("+")) if isinstance(scope, str) else set())
    if (scope != "NONE" and (not scope_components or not scope_components.issubset(COMPONENT_ROLES)
                              or "+".join(sorted(scope_components)) != scope)
            or not EXACT_SHA.fullmatch(candidate or "")
            or not EXACT_VERSION.fullmatch(version or "") or not version.startswith(candidate + "-")):
        raise ValueError("candidate component runtime plan is malformed")
    updated = scope_components
    digests = {"CORE": app_digest, "AGATA": agata_digest, "MINIAPP": miniapp_digest}
    for component, value in digests.items():
        if component in updated and not EXACT_DIGEST.fullmatch(value or ""):
            raise ValueError("updated candidate runtime image digest is invalid")
        if component not in updated and value and not EXACT_DIGEST.fullmatch(value):
            raise ValueError("baseline runtime image digest is invalid")
    inventory_out = {}
    group_repo_digests = {
        "CORE": (f"{ROLE_REPOSITORIES['gpoint-core-api']}@{app_digest}" if app_digest else None),
        "AGATA": (f"{ROLE_REPOSITORIES['gpoint-agata-api']}@{agata_digest}" if agata_digest else None),
        "MINIAPP": (f"{ROLE_REPOSITORIES['gpoint-miniapp-web']}@{miniapp_digest}" if miniapp_digest else None),
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
    parser.add_argument("--triggering-actor", default="")
    parser.add_argument("--repository-owner", default="")
    parser.add_argument("--migration-rehearsal", action="store_true")
    parser.add_argument("--backup-maintenance", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--approve-production", action="store_true")
    args = parser.parse_args()

    try:
        if args.event == "push":
            if args.ref == "refs/heads/main":
                owner = args.repository_owner.lower()
                if (not owner or args.actor.lower() != owner
                        or args.triggering_actor.lower() != owner):
                    raise ValueError("main push actor and triggering actor must match the repository owner")
            paths = changed_paths(args.before, args.sha)
            mode = classify_push(event_name=args.event, ref=args.ref, changed_paths=paths)
            deploy = mode == "DEPLOY"
        elif args.event == "workflow_dispatch":
            owner = args.repository_owner.lower()
            if not owner or args.triggering_actor.lower() != owner:
                raise ValueError("workflow dispatch triggering actor must match the repository owner")
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
