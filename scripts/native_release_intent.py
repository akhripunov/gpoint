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
# Explicitly reviewed non-runtime inputs that do not change an application image.
KNOWN_NON_RUNTIME_FILES = frozenset({
    ".github/actionlint.yaml",
    ".kamal/hooks/post-app-boot",
    ".kamal/hooks/pre-app-boot",
    "AGENTS.md",
    "packages/db/prisma/production-approved-migrations.json",
    "scripts/check-kamal-configs.sh",
    "scripts/classify-release-prepush.py",
    "scripts/eros-native-database.py",
    "scripts/eros-production-secrets.py",
    "scripts/gpoint-backup-policy.py",
    "scripts/gpoint-scheduled-backup.py",
    "scripts/native_release_intent.py",
    "scripts/prepush.py",
    "scripts/prisma_migration_checksums.py",
    "scripts/prisma_migration_release_plan.py",
    "scripts/resolve_native_build_scope.py",
    "scripts/resolve_native_environment_deployment.py",
    "scripts/transition-agata-worker-v3.py",
    "scripts/verify-eros-runtime-labels.py",
    "scripts/verify-gpoint-scheduled-backup-restore.sh",
    "scripts/verify-kamal-migration-parity.py",
    "scripts/verify-kamal-production-baseline.py",
    "scripts/verify-kamal-role.sh",
    "scripts/verify-layer7-ollama.py",
    "scripts/verify_native_eros_image.py",
    "scripts/verify_native_rollback_evidence.py",
    "scripts/write-kamal-production-manifest.py",
    "services/gsales/tests/test_shared_worker_http.py",
    "systemd/gpoint-backup.service",
    "scripts/gpoint-backup-install.sh",
    "scripts/gpoint-backup-install-remote.sh",
})
COMPONENT_ROLES = {
    "CORE": ("gpoint-core-api", "gpoint-core-worker", "gpoint-core-bot"),
    "AGATA": ("gpoint-agata-api", "gpoint-agata-memory-worker"),
    "MINIAPP": ("gpoint-miniapp-web",),
}
EROS_ROLES = ("gpoint-eros-pilot-engine", "gpoint-eros-relay-relay")
EROS_COMPONENT_ROLES = {"EROS": EROS_ROLES[:1], "EROS_RELAY": EROS_ROLES[1:]}
ALL_COMPONENT_ROLES = {**COMPONENT_ROLES, **EROS_COMPONENT_ROLES}
COMPONENT_IMAGES = {component: component for component in ALL_COMPONENT_ROLES}
COMPONENT_IMAGES["EROS_RELAY"] = "CORE"
ROLE_REPOSITORIES = {
    "gpoint-core-api": "ghcr.io/akhripunov/gpoint-runtime",
    "gpoint-core-worker": "ghcr.io/akhripunov/gpoint-runtime",
    "gpoint-core-bot": "ghcr.io/akhripunov/gpoint-runtime",
    "gpoint-agata-api": "ghcr.io/akhripunov/gpoint-agata-runtime",
    "gpoint-agata-memory-worker": "ghcr.io/akhripunov/gpoint-agata-runtime",
    "gpoint-miniapp-web": "ghcr.io/akhripunov/gpoint-miniapp",
    "gpoint-eros-pilot-engine": "ghcr.io/akhripunov/gpoint-eros",
    # The Eros relay is a separate Kamal service using the GPoint runtime image.
    "gpoint-eros-relay-relay": "ghcr.io/akhripunov/gpoint-runtime",
}
ROLE_IDENTITY_GROUP = {
    **{role: "CORE" for role in COMPONENT_ROLES["CORE"]},
    **{role: "AGATA" for role in COMPONENT_ROLES["AGATA"]},
    **{role: "MINIAPP" for role in COMPONENT_ROLES["MINIAPP"]},
    "gpoint-eros-pilot-engine": "EROS_ENGINE",
    "gpoint-eros-relay-relay": "EROS_RELAY",
}
BASELINE_ROLES = set(ROLE_REPOSITORIES) - set(EROS_ROLES)
EXACT_SHA = re.compile(r"^[0-9a-f]{40}$")
EXACT_VERSION = re.compile(r"^[0-9a-f]{40}-[0-9]+-[0-9]+$")
EXACT_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
EROS_MODEL_CONFIG_PATHS = frozenset({
    "services/eros-engine/eros-pilot-model-config.toml",
    "services/eros-engine/eros-production-model-config.toml",
    "services/eros-engine/test-model-config.toml",
    "services/eros-engine/eros-layer7-staging-model-config.toml",
})
LEGACY_RELEASE_MARKERS = frozenset({
    "GPOINT-ARTIFACT-PREPARE",
    "GPOINT-LAYER7-API",
    "GPOINT-LAYER7-EROS",
})
EROS_UPSTREAM_PIN = "services/eros-engine/upstream.sha"
INFRASTRUCTURE_PREFIXES = (".github/", ".kamal/", "infra/production/", "scripts/", "systemd/")
PRODUCTION_KAMAL_CONFIGS = frozenset({
    "config/deploy.yml", "config/deploy.agata.yml", "config/deploy.eros-api.yml",
    "config/deploy.eros-relay.yml", "config/deploy.eros.yml", "config/deploy.miniapp.yml",
})


def is_infrastructure_change(changed_paths: set[str] | frozenset[str]) -> bool:
    return any(path in PRODUCTION_KAMAL_CONFIGS | {".dockerignore"}
               or any(path.startswith(prefix) for prefix in INFRASTRUCTURE_PREFIXES)
               for path in changed_paths)


def push_deploy_authorized(*, mode: str, changed_paths: set[str] | frozenset[str],
                           cumulative_infrastructure_change: bool = False) -> bool:
    """Require explicit dispatch approval before production infrastructure Apply."""
    # Production topology and release-control changes require an explicit
    # workflow_dispatch through the existing protected production Environment.
    if cumulative_infrastructure_change or is_infrastructure_change(changed_paths):
        return False
    return mode == "DEPLOY"


def _path_kind(path: str) -> str:
    normalized = path.strip("/")
    parts = normalized.split("/")
    if not normalized or normalized.startswith("../") or "/../" in normalized:
        return "unknown"
    if normalized in EROS_MODEL_CONFIG_PATHS:
        return "ops"
    if normalized == "packages/db/prisma/schema.prisma":
        return "prisma_schema"
    if normalized.startswith("packages/db/prisma/migrations/"):
        return "prisma_migration" if PRISMA_MIGRATION.fullmatch(normalized) else "schema"
    if "migrations" in parts or Path(normalized).name in SCHEMA_FILENAMES:
        return "schema"
    if any(part in TEST_DIRECTORY_NAMES for part in parts):
        return "test"
    if normalized == ".dockerignore":
        return "ops"
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
                  changed_paths: set[str] | frozenset[str],
                  commit_message: str = "") -> str:
    if event_name != "push" or ref != "refs/heads/main":
        return "NO_DEPLOY"
    if not changed_paths:
        return "BLOCK"
    kinds = {_path_kind(path) for path in changed_paths}
    if "schema" in kinds or "unknown" in kinds:
        return "BLOCK"
    markers = set(commit_message.splitlines())
    if markers & LEGACY_RELEASE_MARKERS:
        # Retired staging and artifact markers cannot authorize production Apply.
        return "BLOCK"
    if EROS_UPSTREAM_PIN in changed_paths:
        return "BLOCK"
    # Prisma schema changes must reach hosted Tests, which compare the full
    # migration history with schema.prisma before Build. Production Apply still
    # requires an exact migration set in the approved migration manifest.
    # App and documentation commits use the ordinary Tests -> Build -> Deploy
    # route. Production topology and release-control changes are held for a
    # separately approved workflow_dispatch in the protected Environment.
    return "DEPLOY"


def classify_dispatch(*, ref: str, candidate_sha: str, github_sha: str, actor: str,
                      repository_owner: str, approve_production: bool,
                      rollback: bool = False,
                      rollback_eros: bool = False) -> tuple[str, bool]:
    if rollback_eros and not rollback:
        return "INVALID_ROLLBACK", False
    exact = bool(re.fullmatch(r"[0-9a-f]{40}", candidate_sha)) and candidate_sha == github_sha
    owner = bool(repository_owner) and actor.lower() == repository_owner.lower()
    if rollback:
        if ref == "refs/heads/main" and exact and owner and approve_production:
            # A rollback runs inside the protected Deploy job as a mode, so the
            # release pipeline must admit it exactly like a forward deploy.
            return "ROLLBACK", True
        return "INVALID_ROLLBACK", False
    if ref == "refs/heads/main" and exact and owner and approve_production:
        return "DEPLOY", True
    if ref == "refs/heads/main":
        return "INVALID_DEPLOY", False
    return "NO_DEPLOY", False


def changed_paths(before: str, sha: str, *, repo: Path = Path.cwd()) -> set[str]:
    return {path for _status, path in git_diff_entries(before, sha, repo=repo)}


def git_diff_entries(before: str, sha: str, *, repo: Path = Path.cwd()) -> list[tuple[str, str]]:
    """Return the complete exact commit-range file inventory using Git's NUL format."""
    if (not re.fullmatch(r"[0-9a-f]{40}", before)
            or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or before == "0" * 40):
        raise ValueError("push range must contain exact nonzero commit SHAs")
    for revision in (before, sha):
        resolved = subprocess.run(["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
                                  cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
        if resolved != revision:
            raise ValueError("push range revision does not resolve to its exact commit SHA")
    result = subprocess.run(
        ["git", "diff", "--name-status", "-z", "--no-renames", f"{before}..{sha}"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    entries = result.stdout.split("\0")
    if not entries or entries.pop() != "" or len(entries) % 2:
        raise ValueError("push diff path inventory is malformed")
    parsed: list[tuple[str, str]] = []
    seen: set[str] = set()
    for status, path in zip(entries[::2], entries[1::2]):
        if status not in {"A", "M", "D"} or not path or path in seen:
            raise ValueError("push diff path inventory contains an unsupported or duplicate entry")
        seen.add(path)
        if status.startswith("D") and (path.startswith("packages/db/prisma/") or "migrations" in Path(path).parts):
            raise ValueError("deleting or moving a migration/schema path is blocked")
        parsed.append((status, path))
    if not parsed:
        raise ValueError("push range contains no changed paths")
    return parsed


def _validate_role_inventory(inventory: dict) -> dict[str, dict[str, str]]:
    if not isinstance(inventory, dict) or not isinstance(inventory.get("roles"), dict):
        raise ValueError("verified role inventory is missing")
    roles = inventory["roles"]
    if set(roles) not in (set(ROLE_REPOSITORIES), BASELINE_ROLES):
        raise ValueError("verified role inventory has an unexpected role set")
    repository_identities: dict[str, tuple[str, str]] = {}
    for role, repository in ROLE_REPOSITORIES.items():
        if role not in roles:
            continue
        row = roles[role]
        if not isinstance(row, dict):
            raise ValueError("verified role inventory is malformed")
        name, version, source, image_id, repo_digest = (
            row.get("name"), row.get("version"), row.get("source_sha"), row.get("image_id"), row.get("repo_digest")
        )
        if (not isinstance(version, str) or not EXACT_VERSION.fullmatch(version)
                or name != f"{role}-{version}"
                or not isinstance(source, str) or not EXACT_SHA.fullmatch(source)
                or not isinstance(image_id, str) or not EXACT_DIGEST.fullmatch(image_id)
                or not isinstance(repo_digest, str)
                or not re.fullmatch(re.escape(repository) + r"@sha256:[0-9a-f]{64}", repo_digest)):
            raise ValueError("verified role inventory contains an invalid runtime identity")
        # One image repository may serve independently-versioned Kamal services.
        # The image bytes/digest must agree, while each service's version/source
        # identity remains role-specific.
        identity = (image_id, repo_digest)
        group = ROLE_IDENTITY_GROUP[role]
        if group in repository_identities and repository_identities[group] != identity:
            raise ValueError("shared image roles have different image identities")
        repository_identities[group] = identity
    return roles


def _component_paths(path: str) -> set[str]:
    if (PRISMA_MIGRATION.fullmatch(path)
            or path == "packages/db/prisma/production-approved-migrations.json"):
        return set()
    if path == "packages/db/prisma/schema.prisma":
        return {"CORE"}
    if path in {
        "config/deploy.yml",
        "config/deploy.eros-api.yml",
    }:
        return {"CORE"}
    if path == "apps/miniapp/index.html" or path.startswith(("apps/miniapp/src/", "apps/miniapp/public/")):
        return {"MINIAPP"}
    if path.startswith("services/agata/"):
        return {"AGATA"}
    if path in EROS_MODEL_CONFIG_PATHS:
        return set()
    if path.startswith("config/deploy.layer7-staging."):
        return set()
    if path in {"services/eros-engine/eros-pilot-embedding-relay.mjs", "config/deploy.eros-relay.yml"}:
        return {"EROS_RELAY"}
    if path.startswith(("services/eros-engine/", "config/deploy.eros")):
        return {"EROS"}
    if path in {"infra/production/Gpoint.Dockerfile", "infra/production/nginx-miniapp.conf"}:
        return {"CORE", "MINIAPP"} if path.endswith("Gpoint.Dockerfile") else {"MINIAPP"}
    if path == ".dockerignore":
        return {"CORE", "MINIAPP"}
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
            or path in EROS_MODEL_CONFIG_PATHS
            or path.startswith("config/deploy.layer7-staging.")
            or path in KNOWN_NON_RUNTIME_FILES
            or PRISMA_MIGRATION.fullmatch(path) is not None
            or path.startswith("tests/")
            or (path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")))
            or (path.startswith(".github/actions/") and path.endswith((".yml", ".yaml")))
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
            or set(component_sources) not in (set(COMPONENT_ROLES), set(ALL_COMPONENT_ROLES))):
        raise ValueError("component production source inventory is incomplete")
    for component, source in component_sources.items():
        if not isinstance(source, str) or not EXACT_SHA.fullmatch(source):
            raise ValueError(f"{component} production source SHA is invalid")
    eros_installed = "EROS" in component_sources
    if not eros_installed:
        component_sources = {**component_sources, **dict.fromkeys(EROS_COMPONENT_ROLES, component_sources["CORE"])}
    paths_by_baseline: dict[str, set[str]] = {}
    core_migration_paths: set[str] = set()
    core_source = component_sources["CORE"]
    for baseline in component_sources.values():
        if baseline == candidate or baseline in paths_by_baseline:
            continue
        ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", baseline, candidate],
                                  cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ancestor.returncode:
            raise ValueError("component production source is not an ancestor of the candidate")
        entries = git_diff_entries(baseline, candidate, repo=repo)
        paths = set()
        for status, path in entries:
            if status == "D":
                prior = subprocess.check_output(["git", "ls-tree", baseline, "--", path], cwd=repo).decode()
                if not prior.startswith(("100644 blob ", "100755 blob ")):
                    raise ValueError("deleted candidate path was not a prior regular file")
                if path == "infra/staging/layer7_known_hosts":
                    continue
                deleted_components = _component_paths(path)
                retired_release_script = path.startswith("scripts/")
                if not deleted_components and (_known_non_runtime_path(path) or retired_release_script):
                    continue
                if not deleted_components:
                    raise ValueError("deleted or unmapped candidate path needs explicit scope approval")
                paths.update(deleted_components)
                continue
            if status not in ("A", "M"):
                raise ValueError("deleted or renamed candidate path needs explicit scope approval")
            if not _safe_component_path(path):
                raise ValueError("unmapped or unsafe candidate path needs explicit scope approval")
            tree = subprocess.check_output(["git", "ls-tree", candidate, "--", path],
                                           cwd=repo).decode()
            if not tree.startswith(("100644 blob ", "100755 blob ")):
                raise ValueError("candidate path is not a regular file and needs explicit scope approval")
            changed_components = _component_paths(path)
            if eros_installed and path == "infra/production/Gpoint.Dockerfile":
                changed_components.add("EROS_RELAY")
            if baseline == core_source and PRISMA_MIGRATION.fullmatch(path):
                core_migration_paths.add(path)
            if not changed_components:
                if _known_non_runtime_path(path):
                    continue
                raise ValueError("unmapped or unsafe candidate path needs explicit scope approval")
            paths.update(changed_components)
        paths_by_baseline[baseline] = paths

    components = {
        component for component, baseline in component_sources.items()
        if baseline != candidate and component in paths_by_baseline[baseline]
    }
    if not components and all(source == candidate for source in component_sources.values()):
        raise ValueError("candidate equals production baseline; no changed component scope")
    # Historical runtime changes may appear when a lagging component SHA is
    # compared with the candidate. Only paths owned by that component count;
    # a newer component baseline has already absorbed its own changes. Unknown
    # paths still fail closed above.
    if not eros_installed and components.intersection(EROS_COMPONENT_ROLES):
        components.update(EROS_COMPONENT_ROLES)
    runtime_scope = "+".join(sorted(components)) if components else "NONE"
    build_components = {COMPONENT_IMAGES[component] for component in components}
    if core_migration_paths:
        build_components.add("CORE")
    return {
        "deploy_scope": runtime_scope,
        "build_components": sorted(build_components),
        "reason": "exact_component_diff" if components else "no_runtime_changes",
    }


def installed_component_sources(inventory: dict) -> dict[str, str]:
    roles = _validate_role_inventory(inventory)
    return {component: roles[names[0]]["source_sha"]
            for component, names in ALL_COMPONENT_ROLES.items() if names[0] in roles}


def classify_component_scope(repo: Path, candidate: str, inventory: dict) -> dict[str, str | bool]:
    """Classify the exact candidate diff; never broaden an ambiguous scope."""
    component_sources = installed_component_sources(inventory)
    plan = classify_component_build_plan(repo, candidate, component_sources)
    components = set(str(plan["deploy_scope"]).split("+")) - {"NONE"}
    result = {
        "scope": str(plan["deploy_scope"]),
        "reason": str(plan["reason"]),
        "deploy_core": "CORE" in components,
        "deploy_agata": "AGATA" in components,
        "deploy_miniapp": "MINIAPP" in components,
        "deploy_eros": "EROS" in components,
        "deploy_eros_relay": "EROS_RELAY" in components,
        "eros_bootstrap": "EROS" in components and "EROS" not in component_sources,
    }
    return result


def expected_runtime_inventory(scope: str, inventory: dict, candidate: str, version: str,
                               app_digest: str, agata_digest: str, miniapp_digest: str,
                               *, eros_digest: str = "", eros_relay_digest: str = "") -> dict:
    """Bind every verified role to either the untouched baseline or exact candidate artifact."""
    roles = _validate_role_inventory(inventory)
    scope_components = (set() if scope == "NONE" else
                        set(scope.split("+")) if isinstance(scope, str) else set())
    valid_components = set(ALL_COMPONENT_ROLES)
    if (scope != "NONE" and (not scope_components or not scope_components.issubset(valid_components)
                              or "+".join(sorted(scope_components)) != scope)
            or not EXACT_SHA.fullmatch(candidate or "")
            or not EXACT_VERSION.fullmatch(version or "") or not version.startswith(candidate + "-")):
        raise ValueError("candidate component runtime plan is malformed")
    updated = scope_components
    digests = {"CORE": app_digest, "AGATA": agata_digest, "MINIAPP": miniapp_digest,
               "EROS": eros_digest, "EROS_RELAY": eros_relay_digest}
    for component, value in digests.items():
        if component in updated and not EXACT_DIGEST.fullmatch(value or ""):
            raise ValueError("updated candidate runtime image digest is invalid")
        if component not in updated and value and not EXACT_DIGEST.fullmatch(value):
            raise ValueError("baseline runtime image digest is invalid")
    if (EROS_ROLES[0] not in roles and updated.intersection(EROS_COMPONENT_ROLES)
            and not EROS_COMPONENT_ROLES.keys() <= updated):
        raise ValueError("first Eros deploy requires both engine and relay")
    inventory_out = {}
    for component, component_roles in ALL_COMPONENT_ROLES.items():
        for role in component_roles:
            baseline = roles.get(role)
            if component in updated:
                repository_digest = f"{ROLE_REPOSITORIES[role]}@{digests[component]}"
                inventory_out[role] = {"version": version, "sourceSha": candidate,
                                       "repositoryDigest": repository_digest,
                                       "baselineImageId": None}
            elif baseline is not None:
                inventory_out[role] = {"version": baseline["version"], "sourceSha": baseline["source_sha"],
                                       "repositoryDigest": baseline["repo_digest"],
                                       "baselineImageId": baseline["image_id"]}
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
    parser.add_argument("--commit-message", default="")
    parser.add_argument("--candidate-sha", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--triggering-actor", default="")
    parser.add_argument("--repository-owner", default="")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--rollback-eros", action="store_true")
    parser.add_argument("--approve-production", action="store_true")
    parser.add_argument("--infrastructure-baseline-sha", default="")
    args = parser.parse_args()

    try:
        if args.event == "push":
            if args.ref == "refs/heads/main":
                owner = args.repository_owner.lower()
                if (not owner or args.actor.lower() != owner
                        or args.triggering_actor.lower() != owner):
                    raise ValueError("main push actor and triggering actor must match the repository owner")
            paths = changed_paths(args.before, args.sha)
            mode = classify_push(event_name=args.event, ref=args.ref, changed_paths=paths,
                                 commit_message=args.commit_message)
            cumulative_infrastructure_change = False
            if args.ref == "refs/heads/main":
                if not EXACT_SHA.fullmatch(args.infrastructure_baseline_sha or ""):
                    # Without a verified production baseline the accumulated
                    # infrastructure diff is unknown, so automatic Apply stops.
                    cumulative_infrastructure_change = True
                else:
                    cumulative_paths = changed_paths(args.infrastructure_baseline_sha, args.sha)
                    cumulative_infrastructure_change = is_infrastructure_change(cumulative_paths)
            deploy = push_deploy_authorized(
                mode=mode, changed_paths=paths,
                cumulative_infrastructure_change=cumulative_infrastructure_change,
            )
        elif args.event == "workflow_dispatch":
            owner = args.repository_owner.lower()
            if not owner or args.triggering_actor.lower() != owner:
                raise ValueError("workflow dispatch triggering actor must match the repository owner")
            mode, deploy = classify_dispatch(
                ref=args.ref, candidate_sha=args.candidate_sha, github_sha=args.sha,
                actor=args.actor, repository_owner=args.repository_owner,
                approve_production=args.approve_production,
                rollback=args.rollback,
                rollback_eros=args.rollback_eros,
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
