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
# ME-36 OWNER-approved removal of the parallel Mac installer and stale shared
# Worker test. This is an exact path exception; all other deletions still block.
KNOWN_RETIRED_NON_RUNTIME_FILES = frozenset({
    ".github/workflows/gpoint-deploy.yml",
    ".github/workflows/gpoint-first-kamal-cutover.yml",
    ".github/workflows/gpoint-linear-reconcile.yml",
    ".github/workflows/gpoint-prepare.yml",
    ".github/workflows/gpoint-recover-interrupted.yml",
    "scripts/automated-eros-pilot-release.mjs",
    "scripts/automated-release.mjs",
    "scripts/check-release-rehearsal.py",
    "scripts/collect_kamal_eros_state.py",
    "scripts/deployment-execution.mjs",
    "scripts/deployment-execution.test.mjs",
    "scripts/deployment-run-state.mjs",
    "scripts/deployment-run-state.test.mjs",
    "scripts/eros-postgres-acl-deploy.mjs",
    "scripts/eros-postgres-acl-deploy.test.mjs",
    "scripts/eros-production-deployment.mjs",
    "scripts/eros-production-deployment.test.mjs",
    "scripts/ghcr-receipt.mjs",
    "scripts/ghcr-receipt.test.mjs",
    "scripts/github-deployments.mjs",
    "scripts/github-deployments.test.mjs",
    "scripts/github_api_preflight.py",
    "scripts/gpoint-miniapp-static-manifest.py",
    "scripts/kamal-first-cutover/consumers.sh",
    "scripts/kamal-first-cutover/pause.sh",
    "scripts/kamal-first-cutover/recover-post.sh",
    "scripts/kamal-first-cutover/recover-pre.sh",
    "scripts/kamal-first-cutover/requests.sh",
    "scripts/kamal-first-cutover/retire.sh",
    "scripts/kamal-first-cutover/routes.sh",
    "scripts/kamal-first-cutover/verify.sh",
    "scripts/kamal_cutover_credentials.py",
    "scripts/kamal_cutover_owner.py",
    "scripts/kamal_eros_state.py",
    "scripts/kamal_pause_recovery_policy.py",
    "scripts/package-kamal-cutover.py",
    "scripts/pause-legacy-gpoint.sh",
    "scripts/prepare-release.py",
    "scripts/prepare_coalescing.py",
    "scripts/prepared_artifact_cache.py",
    "scripts/publish-release-linear.py",
    "scripts/publish_ghcr_images.py",
    "scripts/reconcile-deployment.mjs",
    "scripts/reconcile-deployment.test.mjs",
    "scripts/recover-interrupted-apply.mjs",
    "scripts/recover-interrupted-apply.test.mjs",
    "scripts/recover-kamal-cutover.py",
    "scripts/rehearse-release-bundle.mjs",
    "scripts/release-authorization.mjs",
    "scripts/release-authorization.test.mjs",
    "scripts/release-bundle.mjs",
    "scripts/release-bundle.test.mjs",
    "scripts/release-component-bundle-v4.mjs",
    "scripts/release-component-bundle-v4.test.mjs",
    "scripts/release-component-vps-proof-v4.mjs",
    "scripts/release-component-vps-proof-v4.test.mjs",
    "scripts/release-evidence.mjs",
    "scripts/release-evidence.test.mjs",
    "scripts/release-gpoint-vps-adapter.mjs",
    "scripts/release-gpoint.mjs",
    "scripts/release-gpoint.test.mjs",
    "scripts/release-gsales-provision.mjs",
    "scripts/release-gsales.mjs",
    "scripts/release-gsales.test.mjs",
    "scripts/release-lock.mjs",
    "scripts/release-migrations.mjs",
    "scripts/release-network-read.mjs",
    "scripts/release-network-read.test.mjs",
    "scripts/release-oci.mjs",
    "scripts/release-oci.test.mjs",
    "scripts/release-orchestrator.mjs",
    "scripts/release-orchestrator.test.mjs",
    "scripts/release-preflight-benchmark.py",
    "scripts/release-readiness.test.mjs",
    "scripts/release-runner-readiness.mjs",
    "scripts/release-runner-readiness.test.mjs",
    "scripts/release-static-baseline-v4.test.mjs",
    "scripts/release-static-publish-v4.test.mjs",
    "scripts/release-static-transfer-v4.mjs",
    "scripts/release-static-transfer-v4.test.mjs",
    "scripts/release-static-v4.mjs",
    "scripts/release-static-v4.test.mjs",
    "scripts/release-telemetry.py",
    "scripts/release_assets.py",
    "scripts/release_component_impact_v4.py",
    "scripts/release_component_manifest_v4.py",
    "scripts/release_impact_v3.py",
    "scripts/release_static_exchange_v4.py",
    "scripts/release_static_executor_v4.py",
    "scripts/release_static_migrations_v4.py",
    "scripts/release_static_prepare_v4.py",
    "scripts/release_ui_artifact_v4.py",
    "scripts/release_ui_switch_v4.py",
    "scripts/retire-gpoint-caddy.sh",
    "scripts/run-kamal-cutover-owner.sh",
    "scripts/sanitize-kamal-inventory.py",
    "scripts/sanitize-kamal-resources.py",
    "scripts/select-release-run.py",
    "scripts/select-release-run.test.py",
    "scripts/smoke_bot_image.py",
    "scripts/start_kamal_cutover_owner.py",
    "scripts/test_prepare_coalescing.py",
    "scripts/test_release_preflight_benchmark.py",
    "scripts/validate-task-authorization.mjs",
    "scripts/verify-gpoint-backup-restore.sh",
    "scripts/verify-interrupted-recovery-proof.mjs",
    "scripts/verify-loaded-images.py",
    "scripts/worker-runtime-guard.mjs",
    "scripts/worker-runtime-guard.test.mjs",
    "services/agata/worker/install_launch_agent.py",
    "services/gsales/tests/test_shared_worker_http.py",
    "tests/test_collect_kamal_eros_state.py",
    "tests/test_deploy_evidence_workflow.py",
    "tests/test_eros_pilot_artifact.py",
    "tests/test_eros_postgres_acl_workflow.py",
    "tests/test_gpoint_docker_cache_v3.py",
    "tests/test_gpoint_miniapp_static_manifest.py",
    "tests/test_gpoint_release_trigger_graph.py",
    "tests/test_interrupted_recovery_workflow.py",
    "tests/test_kamal_cutover_credentials.py",
    "tests/test_kamal_cutover_owner.py",
    "tests/test_kamal_cutover_package.py",
    "tests/test_kamal_eros_state.py",
    "tests/test_kamal_pause_recovery_policy.py",
    "tests/test_me38_recovery_immutable_rehearsal.py",
    "tests/test_prepare_release_migration_inventory.py",
    "tests/test_prepare_workflow_summary.py",
    "tests/test_prepared_artifact_cache.py",
    "tests/test_publish_ghcr_images.py",
    "tests/test_publish_ghcr_receipt.py",
    "tests/test_recover_kamal_cutover.py",
    "tests/test_release_assets.py",
    "tests/test_release_component_impact_v4.py",
    "tests/test_release_impact_v3.py",
    "tests/test_release_linear.py",
    "tests/test_release_static_publication_v4.py",
    "tests/test_start_kamal_cutover_owner.py",
    "tests/test_unified_release_v1.py",
})
KNOWN_NON_RUNTIME_FILES = frozenset({
    ".github/actionlint.yaml",
    ".kamal/hooks/post-app-boot",
    ".kamal/hooks/pre-app-boot",
    "AGENTS.md",
    "config/deploy.layer7-staging.eros-api.yml",
    "config/deploy.layer7-staging.eros-relay.yml",
    "config/deploy.layer7-staging.eros.yml",
    "config/deploy.layer7-staging.miniapp.yml",
    "config/deploy.layer7-staging.yml",
    "infra/staging/layer7_known_hosts",
    "packages/db/prisma/production-approved-migrations.json",
    "scripts/check-kamal-configs.sh",
    "scripts/classify-release-prepush.py",
    "scripts/eros-native-database.py",
    "scripts/eros-production-secrets.py",
    "scripts/gpoint-backup-policy.py",
    "scripts/gpoint-scheduled-backup.py",
    "scripts/layer7-transport.sh",
    "scripts/layer7_staging_database.py",
    "scripts/layer7_staging_env.py",
    "scripts/native_change_surface.py",
    "scripts/native_release_intent.py",
    "scripts/prepush.py",
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
    "scripts/verify_native_prepared_run.py",
    "scripts/verify_native_rollback_evidence.py",
    "scripts/write-kamal-production-manifest.py",
    "scripts/write_layer7_staging_env.py",
    "scripts/write_layer7_staging_eros_api_env.py",
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
ALL_COMPONENT_ROLES = {**COMPONENT_ROLES, "EROS": EROS_ROLES}
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
ARTIFACT_PREPARE_MARKER = "GPOINT-ARTIFACT-PREPARE"


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
                  changed_paths: set[str] | frozenset[str],
                  commit_message: str = "") -> str:
    if event_name != "push" or ref != "refs/heads/main":
        return "NO_DEPLOY"
    if not changed_paths:
        return "BLOCK"
    kinds = {_path_kind(path) for path in changed_paths}
    if "schema" in kinds or "unknown" in kinds:
        return "BLOCK"
    if re.search(rf"(?m)^{re.escape(ARTIFACT_PREPARE_MARKER)}$", commit_message):
        return "ARTIFACT_PREPARE"
    # Prisma schema changes must reach hosted Tests, which compare the full
    # migration history with schema.prisma before Build. Production Apply still
    # requires an exact migration set in the approved migration manifest.
    # Publishing to main authorizes the complete reviewed candidate. Docs,
    # release fixes and their app changes use the same Tests -> Build -> Deploy
    # run; no second manual run repeats CI just because a path is operational.
    return "DEPLOY"


def validate_prepared_image_digests(build_components: list[str],
                                    digests: dict[str, str]) -> dict[str, str]:
    """Require one immutable OCI digest for each, and only each, built component."""
    allowed = set(ALL_COMPONENT_ROLES)
    if (not isinstance(build_components, list) or not build_components
            or any(not isinstance(component, str) or component not in allowed
                   for component in build_components)
            or len(build_components) != len(set(build_components))):
        raise ValueError("prepared build component scope is invalid")
    if not isinstance(digests, dict) or set(digests) != set(build_components):
        raise ValueError("prepared image digest set does not match build component scope")
    if any(not isinstance(digest, str) or not EXACT_DIGEST.fullmatch(digest)
           for digest in digests.values()):
        raise ValueError("prepared image digest is not immutable")
    return {component: digests[component] for component in sorted(digests)}


def classify_dispatch(*, ref: str, candidate_sha: str, github_sha: str, actor: str,
                      repository_owner: str, migration_rehearsal: bool,
                      approve_production: bool,
                      backup_maintenance: bool = False,
                      rollback: bool = False,
                      rollback_eros: bool = False,
                      use_prepared_images: bool = False,
                      layer7_staging: bool = False,
                      approve_layer7_staging: bool = False) -> tuple[str, bool]:
    if approve_layer7_staging and not layer7_staging:
        return "INVALID_LAYER7_STAGING", False
    if layer7_staging:
        exact = bool(re.fullmatch(r"[0-9a-f]{40}", candidate_sha)) and candidate_sha == github_sha
        owner = bool(repository_owner) and actor.lower() == repository_owner.lower()
        if (ref == "refs/heads/main" and exact and owner and approve_layer7_staging
                and use_prepared_images and not approve_production and not migration_rehearsal
                and not backup_maintenance and not rollback and not rollback_eros):
            return "LAYER7_STAGING", False
        return "INVALID_LAYER7_STAGING", False
    if rollback_eros and not rollback:
        return "INVALID_ROLLBACK", False
    exact = bool(re.fullmatch(r"[0-9a-f]{40}", candidate_sha)) and candidate_sha == github_sha
    owner = bool(repository_owner) and actor.lower() == repository_owner.lower()
    if rollback and (migration_rehearsal or backup_maintenance or use_prepared_images):
        return "INVALID_ROLLBACK", False
    if migration_rehearsal and (backup_maintenance or use_prepared_images):
        return "INVALID_REHEARSAL", False
    if backup_maintenance and use_prepared_images:
        return "INVALID_BACKUP_MAINTENANCE", False
    if rollback:
        if ref == "refs/heads/main" and exact and owner and approve_production:
            # A rollback runs inside the protected Deploy job as a mode, so the
            # release pipeline must admit it exactly like a forward deploy.
            return "ROLLBACK", True
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
    if path == "apps/miniapp/index.html" or path.startswith(("apps/miniapp/src/", "apps/miniapp/public/")):
        return {"MINIAPP"}
    if path.startswith("services/agata/"):
        return {"AGATA"}
    if path.startswith(("services/eros-engine/", "config/deploy.eros")):
        return {"EROS"}
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
    required_sources = set(COMPONENT_ROLES)
    if (not isinstance(component_sources, dict)
            or set(component_sources) not in (required_sources, required_sources | {"EROS"})):
        raise ValueError("component production source inventory is incomplete")
    for component, source in component_sources.items():
        if not isinstance(source, str) or not EXACT_SHA.fullmatch(source):
            raise ValueError(f"{component} production source SHA is invalid")
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
        raw = subprocess.check_output(["git", "diff", "--name-status", "-z", "--no-renames",
                                       f"{baseline}..{candidate}", "--"], cwd=repo)
        parts = raw.decode("utf8").split("\x00")
        if not parts or parts.pop() != "" or len(parts) % 2:
            raise ValueError("component scope diff is malformed")
        entries = list(zip(parts[::2], parts[1::2]))
        paths = set()
        for status, path in entries:
            if status == "D" and path in KNOWN_RETIRED_NON_RUNTIME_FILES:
                prior = subprocess.check_output(["git", "ls-tree", baseline, "--", path], cwd=repo).decode()
                if not prior.startswith(("100644 blob ", "100755 blob ")):
                    raise ValueError("retired candidate path was not a regular file")
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
    runtime_scope = "+".join(sorted(components)) if components else "NONE"
    build_components = set(components)
    if "EROS" in components:
        # Eros API integration and relay share the CORE runtime artifact, but
        # their Kamal services remain independently scoped from Core roles.
        build_components.add("CORE")
    if core_migration_paths:
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
    eros_installed = "gpoint-eros-pilot-engine" in roles
    component_sources["EROS"] = (roles["gpoint-eros-pilot-engine"]["source_sha"] if eros_installed
                                  else roles["gpoint-core-api"]["source_sha"])
    plan = classify_component_build_plan(repo, candidate, component_sources)
    components = set(str(plan["deploy_scope"]).split("+")) - {"NONE"}
    result = {
        "scope": str(plan["deploy_scope"]),
        "reason": str(plan["reason"]),
        "deploy_core": "CORE" in components,
        "deploy_agata": "AGATA" in components,
        "deploy_miniapp": "MINIAPP" in components,
        "deploy_eros": "EROS" in components,
        "eros_bootstrap": "EROS" in components and not eros_installed,
    }
    return result


def expected_runtime_inventory(scope: str, inventory: dict, candidate: str, version: str,
                               app_digest: str, agata_digest: str, miniapp_digest: str,
                               *, eros_digest: str = "") -> dict:
    """Bind every verified role to either the untouched baseline or exact candidate artifact."""
    roles = _validate_role_inventory(inventory)
    scope_components = (set() if scope == "NONE" else
                        set(scope.split("+")) if isinstance(scope, str) else set())
    valid_components = set(COMPONENT_ROLES) | {"EROS"}
    if (scope != "NONE" and (not scope_components or not scope_components.issubset(valid_components)
                              or "+".join(sorted(scope_components)) != scope)
            or not EXACT_SHA.fullmatch(candidate or "")
            or not EXACT_VERSION.fullmatch(version or "") or not version.startswith(candidate + "-")):
        raise ValueError("candidate component runtime plan is malformed")
    updated = scope_components
    digests = {"CORE": app_digest, "AGATA": agata_digest, "MINIAPP": miniapp_digest,
               "EROS": eros_digest}
    for component, value in digests.items():
        if component in updated and not EXACT_DIGEST.fullmatch(value or ""):
            raise ValueError("updated candidate runtime image digest is invalid")
        if component not in updated and value and not EXACT_DIGEST.fullmatch(value):
            raise ValueError("baseline runtime image digest is invalid")
    if "EROS" in updated and not EXACT_DIGEST.fullmatch(app_digest or ""):
        raise ValueError("updated Eros relay runtime image digest is invalid")
    inventory_out = {}
    group_repo_digests = {
        "CORE": (f"{ROLE_REPOSITORIES['gpoint-core-api']}@{app_digest}" if app_digest else None),
        "AGATA": (f"{ROLE_REPOSITORIES['gpoint-agata-api']}@{agata_digest}" if agata_digest else None),
        "MINIAPP": (f"{ROLE_REPOSITORIES['gpoint-miniapp-web']}@{miniapp_digest}" if miniapp_digest else None),
        "EROS": (f"{ROLE_REPOSITORIES['gpoint-eros-pilot-engine']}@{eros_digest}" if eros_digest else None),
    }
    for component, component_roles in ALL_COMPONENT_ROLES.items():
        for role in component_roles:
            if role not in roles and not (component == "EROS" and component in updated):
                continue
            baseline = roles.get(role)
            role_component = component
            if component == "EROS" and role == "gpoint-eros-relay-relay":
                # Eros deploys a freshly built CORE runtime image to the relay
                # role only; the regular Core roles remain untouched on EROS-only scope.
                role_component = "CORE"
            if component in updated:
                repository_digest = group_repo_digests[role_component]
                if not repository_digest:
                    # For Eros API/relay, this is the no-Core-build case handled
                    # above; all other updated components require their digest.
                    raise ValueError(f"updated {component} runtime digest is required")
                inventory_out[role] = {"version": version, "sourceSha": candidate,
                                       "repositoryDigest": repository_digest,
                                       "baselineImageId": None}
            else:
                if baseline is None:
                    continue
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
    parser.add_argument("--migration-rehearsal", action="store_true")
    parser.add_argument("--backup-maintenance", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--rollback-eros", action="store_true")
    parser.add_argument("--use-prepared-images", action="store_true")
    parser.add_argument("--approve-production", action="store_true")
    parser.add_argument("--layer7-staging", action="store_true")
    parser.add_argument("--approve-layer7-staging", action="store_true")
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
                rollback_eros=args.rollback_eros,
                use_prepared_images=args.use_prepared_images,
                layer7_staging=args.layer7_staging,
                approve_layer7_staging=args.approve_layer7_staging,
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
