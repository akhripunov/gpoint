#!/usr/bin/env python3
"""Fail-closed event and path routing for GPoint Native Release."""

from __future__ import annotations

import argparse
import base64
import hashlib
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
RELEASE_AUTHORIZATION_RECORD = re.compile(
    r"^\.release-authorizations/[a-z0-9][a-z0-9-]*\.json$"
)
RELEASE_AUTHORIZATION_ADDENDUM = re.compile(
    r"^\.release-authorizations/addenda/[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*\.json$"
)
# Explicitly reviewed non-runtime inputs that do not change an application image.
KNOWN_NON_RUNTIME_FILES = frozenset({
    ".github/actionlint.yaml",
    ".kamal/hooks/post-app-boot",
    ".kamal/hooks/pre-app-boot",
    "AGENTS.md",
    ".github/filters/native-release.json",
    "packages/db/prisma/production-approved-migrations.json",
    "scripts/check-kamal-configs.sh",
    "scripts/classify-release-prepush.py",
    "scripts/eros-native-database.py",
    "scripts/eros-production-secrets.py",
    "scripts/gpoint-bot-bootstrap.sh",
    "scripts/gpoint-backup-policy.py",
    "scripts/gpoint-scheduled-backup.py",
    "scripts/native_release_intent.py",
    "scripts/prepush.py",
    "scripts/run-pinned-kamal.sh",
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
    "services/eros-engine/local-test-e2e.mjs",
    "services/eros-engine/local-e2e.test.mjs",
    "services/eros-engine/run-local-e2e.sh",
    "services/eros-engine/compose.release-test.yml",
    "services/eros-engine/local-test-api-entrypoint.mjs",
    "services/gsales/tests/test_shared_worker_http.py",
    "systemd/gpoint-backup.service",
    "scripts/gpoint-backup-install.sh",
    "scripts/gpoint-backup-install-remote.sh",
})
COMPONENT_ROLES = {
    "CORE": ("gpoint-core-api", "gpoint-core-worker", "gpoint-core-bot"),
    "MINIAPP": ("gpoint-miniapp-web",),
}
EROS_ROLES = ("gpoint-eros-pilot-engine",)
EROS_COMPONENT_ROLES = {"EROS": EROS_ROLES}
ALL_COMPONENT_ROLES = {**COMPONENT_ROLES, **EROS_COMPONENT_ROLES}
ROLE_REPOSITORIES = {
    "gpoint-core-api": "ghcr.io/akhripunov/gpoint-runtime",
    "gpoint-core-worker": "ghcr.io/akhripunov/gpoint-runtime",
    "gpoint-core-bot": "ghcr.io/akhripunov/gpoint-runtime",
    "gpoint-miniapp-web": "ghcr.io/akhripunov/gpoint-miniapp",
    "gpoint-eros-pilot-engine": "ghcr.io/akhripunov/gpoint-eros",
}
ROLE_IDENTITY_GROUP = {
    **{role: "CORE" for role in COMPONENT_ROLES["CORE"]},
    **{role: "MINIAPP" for role in COMPONENT_ROLES["MINIAPP"]},
    "gpoint-eros-pilot-engine": "EROS_ENGINE",
}
# Core Bot is optional until its production credentials and polling behavior
# are explicitly enabled. A missing Bot must not be bootstrapped by a release.
BASELINE_ROLES = set(ROLE_REPOSITORIES) - {"gpoint-core-bot"}
EXACT_SHA = re.compile(r"^[0-9a-f]{40}$")
EXACT_VERSION = re.compile(r"^[0-9a-f]{40}-[0-9]+-[0-9]+$")
EXACT_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
EROS_MODEL_CONFIG_PATHS = frozenset({
    "services/eros-engine/eros-pilot-model-config.toml",
    "services/eros-engine/eros-production-model-config.toml",
    "services/eros-engine/test-model-config.toml",
})
RETIRED_LAYER7_STAGING_PATHS = frozenset({
    ".github/actions/layer7-deploy/action.yml",
    ".github/workflows/gpoint-layer7-staging.yml",
    "config/deploy.layer7-staging.yml",
    "config/deploy.layer7-staging.miniapp.yml",
    "config/deploy.layer7-staging.eros-api.yml",
    "config/deploy.layer7-staging.eros.yml",
    "config/deploy.layer7-staging.eros-relay.yml",
    "infra/staging/layer7_known_hosts",
    "services/eros-engine/eros-layer7-staging-model-config.toml",
})
RETIRED_EROS_RELAY_PATHS = frozenset({
    "config/deploy.eros-relay.yml",
    "services/eros-engine/eros-pilot-embedding-relay.mjs",
    "services/eros-engine/eros-pilot-embedding-relay.test.mjs",
    "services/eros-engine/open-mac-ollama-tunnel.sh",
})
LEGACY_RELEASE_MARKERS = frozenset({
    "GPOINT-ARTIFACT-PREPARE",
    "GPOINT-LAYER7-API",
    "GPOINT-LAYER7-EROS",
})
EROS_UPSTREAM_PIN = "services/eros-engine/upstream.sha"
NATIVE_RELEASE_FILTERS = json.loads(
    (Path(__file__).resolve().parents[1] / ".github/filters/native-release.json").read_text()
)


def _path_filter_matches(path: str, patterns: list[str]) -> bool:
    """Match the deliberately small glob subset shared with dorny/paths-filter."""
    def compile_glob(pattern: str) -> re.Pattern[str]:
        parts: list[str] = []
        index = 0
        while index < len(pattern):
            char = pattern[index]
            if char == "*":
                if index + 1 < len(pattern) and pattern[index + 1] == "*":
                    parts.append(".*")
                    index += 2
                else:
                    parts.append("[^/]*")
                    index += 1
                continue
            if char == "?":
                parts.append("[^/]")
            else:
                parts.append(re.escape(char))
            index += 1
        return re.compile("^" + "".join(parts) + "$")

    included = any(compile_glob(pattern).fullmatch(path)
                   for pattern in patterns if not pattern.startswith("!"))
    if not included:
        return False
    return not any(compile_glob(pattern[1:]).fullmatch(path)
                   for pattern in patterns if pattern.startswith("!"))


def paths_match_filter(paths: set[str] | frozenset[str], filter_name: str) -> bool:
    patterns = NATIVE_RELEASE_FILTERS.get(filter_name)
    if not isinstance(patterns, list):
        raise ValueError("unknown canonical path filter")
    return any(_path_filter_matches(path, patterns) for path in paths)


def is_infrastructure_change(changed_paths: set[str] | frozenset[str]) -> bool:
    return paths_match_filter(changed_paths, "infrastructure")


def push_deploy_authorized(*, mode: str, changed_paths: set[str] | frozenset[str]) -> bool:
    """An owner-authorized valid main push enters the automatic release."""
    # Infrastructure classification may select extra CI checks, but must not
    # require a second manual dispatch after a permitted main push.
    return mode == "DEPLOY"


def _read_release_metadata(path: str) -> dict[str, object] | None:
    target = Path(__file__).resolve().parents[1] / path
    try:
        if target.is_symlink() or not target.is_file():
            return None
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _existing_release_authorization_record(path: str) -> bool:
    if RELEASE_AUTHORIZATION_RECORD.fullmatch(path) is None:
        return False
    record = _read_release_metadata(path)
    if record is None:
        return False
    authorization_id = Path(path).stem
    infrastructure = record.get("risk_contracts", {}).get("release_infrastructure") \
        if isinstance(record.get("risk_contracts"), dict) else None
    baseline = record.get("production_baseline_sha")
    return (
        record.get("schema_version") == 3
        and record.get("project") == "gpoint"
        and record.get("authorization_id") == authorization_id
        and record.get("authorization_mode") in {"TASK_SCOPED_AUTO", "EXACT_SHA"}
        and isinstance(baseline, str) and EXACT_SHA.fullmatch(baseline) is not None
        and isinstance(record.get("allowed_paths"), list)
        and isinstance(infrastructure, dict)
        and isinstance(infrastructure.get("allowed_paths"), list)
    )


def _commitment_matches(metadata: dict[str, object], field: str) -> bool:
    commitment = metadata.get(field)
    if not isinstance(commitment, str) or re.fullmatch(r"[0-9a-f]{64}", commitment) is None:
        return False
    payload = {key: value for key, value in metadata.items() if key != field}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest() == commitment


def _existing_release_authorization_addendum(path: str) -> bool:
    if RELEASE_AUTHORIZATION_ADDENDUM.fullmatch(path) is None:
        return False
    parts = path.split("/")
    authorization_id = parts[2]
    metadata = _read_release_metadata(path)
    if metadata is None:
        return False
    original_blob = metadata.get("original_authorization_record_blob_sha")
    return (
        _existing_release_authorization_record(
            f".release-authorizations/{authorization_id}.json")
        and metadata.get("schema_version") == 1
        and metadata.get("project") == "gpoint"
        and metadata.get("authorization_id") == authorization_id
        and metadata.get("amendment_id") == Path(path).stem
        and isinstance(original_blob, str) and re.fullmatch(r"[0-9a-f]{40}", original_blob)
        and _commitment_matches(metadata, "amendment_commitment_sha256")
        and isinstance(metadata.get("scope_additions"), list)
        and bool(metadata["scope_additions"])
    )


def _path_kind(path: str) -> str:
    normalized = path.strip("/")
    parts = normalized.split("/")
    if not normalized or normalized.startswith("../") or "/../" in normalized:
        return "unknown"
    if normalized.startswith(".release-authorizations/"):
        if (_existing_release_authorization_record(normalized)
                or _existing_release_authorization_addendum(normalized)):
            return "ops"
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
    # A changed Eros upstream pin is validated by Build and the exact
    # synthetic Eros E2E in protected Deploy preflight when Eros is selected.
    # Prisma schema changes must reach hosted Tests, which compare the full
    # migration history with schema.prisma before Build. Production Apply still
    # requires an exact migration set in the approved migration manifest.
    # A permitted main push uses the same Tests -> Build -> Deploy route
    # for application, operational, and infrastructure changes.
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
    required = {"gpoint-core-api", "gpoint-core-worker", "gpoint-miniapp-web"}
    if (not set(roles).issubset(ROLE_REPOSITORIES) or not required.issubset(roles)):
        raise ValueError("verified role inventory has an unexpected role set")
    missing = set(ROLE_REPOSITORIES) - set(roles)
    declared_missing = inventory.get("missing_roles")
    if declared_missing is not None and (
            not isinstance(declared_missing, list)
            or len(declared_missing) != len(set(declared_missing))
            or set(declared_missing) != missing):
        raise ValueError("verified role inventory missing-role declaration is inconsistent")
    image_identities_by_source: dict[tuple[str, str], tuple[str, str]] = {}
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
                or not version.startswith(source + "-")
                or not isinstance(image_id, str) or not EXACT_DIGEST.fullmatch(image_id)
                or not isinstance(repo_digest, str)
                or not re.fullmatch(re.escape(repository) + r"@sha256:[0-9a-f]{64}", repo_digest)):
            raise ValueError("verified role inventory contains an invalid runtime identity")
        group = ROLE_IDENTITY_GROUP[role]
        key = (group, source)
        identity = (image_id, repo_digest)
        if key in image_identities_by_source and image_identities_by_source[key] != identity:
            raise ValueError("shared image roles with the same source SHA have different image identities")
        image_identities_by_source[key] = identity
    return roles


def mixed_component_baselines(inventory: dict) -> set[str]:
    """Return components whose enabled roles have independently confirmed releases."""
    roles = _validate_role_inventory(inventory)
    identities: dict[str, set[tuple[str, str, str, str]]] = {}
    for role, row in roles.items():
        component = ROLE_IDENTITY_GROUP[role]
        if component == "EROS_ENGINE":
            component = "EROS"
        identities.setdefault(component, set()).add((row["version"], row["source_sha"],
                                                       row["image_id"], row["repo_digest"]))
    return {component for component, values in identities.items() if len(values) > 1}


def _component_paths(path: str) -> set[str]:
    if (PRISMA_MIGRATION.fullmatch(path)
            or path == "packages/db/prisma/production-approved-migrations.json"):
        return set()
    # Component ownership comes only from the shared dorny filter definition.
    # Schema/approval exceptions above are migration policy, not another map.
    names = {"core": "CORE", "miniapp": "MINIAPP", "eros_engine": "EROS"}
    return {component for filter_name, component in names.items()
            if _path_filter_matches(path, NATIVE_RELEASE_FILTERS[filter_name])}


def _non_runtime_documentation_path(path: str) -> bool:
    return (path == "README.md"
            or (path.startswith("docs/") and path.endswith((".md", ".mdx"))))


def _known_non_runtime_path(path: str) -> bool:
    """Recognize only verified release/test metadata outside application images."""
    if path in RETIRED_LAYER7_STAGING_PATHS:
        return False
    if path in RETIRED_EROS_RELAY_PATHS:
        return True
    if path.startswith(("services/agata/", "packages/agata-client/", "config/deploy.agata.yml")):
        return True
    return (_non_runtime_documentation_path(path)
            or path == "config/deploy.ollama.yml"
            or _existing_release_authorization_record(path)
            or _existing_release_authorization_addendum(path)
            or path in EROS_MODEL_CONFIG_PATHS
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


def classify_component_changes(repo: Path, candidate: str,
                              component_sources: dict[str, str]) -> dict[str, object]:
    """Classify cumulative runtime changes against installed component sources."""
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
                if path in RETIRED_LAYER7_STAGING_PATHS:
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
    # Historical runtime changes may appear when a lagging component SHA is
    # compared with the candidate. Only paths owned by that component count;
    # a newer component baseline has already absorbed its own changes. Unknown
    # paths still fail closed above.
    if not eros_installed and components.intersection(EROS_COMPONENT_ROLES):
        components.update(EROS_COMPONENT_ROLES)
    return {
        "scope": "+".join(sorted(components)) if components else "NONE",
        "reason": "exact_component_diff" if components else "no_runtime_changes",
    }


def installed_component_sources(inventory: dict) -> dict[str, str]:
    roles = _validate_role_inventory(inventory)
    sources = {component: roles[names[0]]["source_sha"]
               for component, names in ALL_COMPONENT_ROLES.items() if names[0] in roles}
    # Missing services have no independent deployed SHA. Use the live Core
    # source only as a comparison floor; classify_component_scope below forces
    # their owning component into the deploy scope regardless of the diff.
    return sources


def classify_component_scope(repo: Path, candidate: str, inventory: dict) -> dict[str, str | bool]:
    """Classify the exact candidate diff; never broaden an ambiguous scope."""
    component_sources = installed_component_sources(inventory)
    plan = classify_component_changes(repo, candidate, component_sources)
    components = set(str(plan["scope"]).split("+")) - {"NONE"}
    mixed = mixed_component_baselines(inventory)
    if "CORE" in mixed:
        core_source = component_sources["CORE"]
        for role in ALL_COMPONENT_ROLES["CORE"]:
            row = inventory["roles"].get(role)
            if row is None or row["source_sha"] in {core_source, candidate}:
                continue
            ancestor = subprocess.run(
                ["git", "merge-base", "--is-ancestor", row["source_sha"], candidate],
                cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if ancestor.returncode:
                raise ValueError(f"{role} production source is not an ancestor of the candidate")
    components.update(mixed)
    # The production topology has five required runtime roles.
    missing_required = BASELINE_ROLES - set(inventory["roles"])
    for component, roles in ALL_COMPONENT_ROLES.items():
        if missing_required.intersection(roles):
            components.add(component)
    planned_components = set(str(plan["scope"]).split("+")) - {"NONE"}
    forced = components != planned_components
    scope_name = "+".join(sorted(components)) if components else "NONE"
    result = {
        "scope": scope_name,
        "reason": ("mixed_component_baseline" if mixed else
                   "missing_runtime_roles" if forced else str(plan["reason"])),
        "deploy_core": "CORE" in components,
        "deploy_miniapp": "MINIAPP" in components,
        "deploy_eros": "EROS" in components,
        "eros_bootstrap": "EROS" in components and "EROS" not in component_sources,
    }
    return result


def expected_runtime_inventory(scope: str, inventory: dict, candidate: str, version: str,
                               app_digest: str, miniapp_digest: str,
                               *, eros_digest: str = "") -> dict:
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
    if "CORE" in mixed_component_baselines(inventory) and "CORE" not in updated:
        raise ValueError("mixed CORE baseline requires a full Core rollout")
    digests = {"CORE": app_digest, "MINIAPP": miniapp_digest, "EROS": eros_digest}
    for component, value in digests.items():
        if component in updated and not EXACT_DIGEST.fullmatch(value or ""):
            raise ValueError("updated candidate runtime image digest is invalid")
        if component not in updated and value and not EXACT_DIGEST.fullmatch(value):
            raise ValueError("baseline runtime image digest is invalid")
    inventory_out = {}
    for component, component_roles in ALL_COMPONENT_ROLES.items():
        for role in component_roles:
            baseline = roles.get(role)
            if component in updated:
                if role == "gpoint-core-bot" and baseline is None:
                    continue
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
    args = parser.parse_args()

    try:
        if args.event == "push":
            if args.ref == "refs/heads/main":
                owner = args.repository_owner.lower()
                if not owner or args.actor.lower() != owner or args.triggering_actor.lower() != owner:
                    raise ValueError("main push and triggering actor must match the repository owner")
            paths = changed_paths(args.before, args.sha)
            mode = classify_push(event_name=args.event, ref=args.ref, changed_paths=paths,
                                 commit_message=args.commit_message)
            deploy = push_deploy_authorized(mode=mode, changed_paths=paths)
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
