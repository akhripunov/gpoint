#!/usr/bin/env python3
"""Persist the exact post-Verify GPoint Kamal runtime evidence."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from native_release_intent import ALL_COMPONENT_ROLES, ROLE_IDENTITY_GROUP, ROLE_REPOSITORIES

SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
ROLE_COMPONENTS = {role: component for component, roles in ALL_COMPONENT_ROLES.items()
                   for role in roles}


def docker_json(*args: str) -> object:
    result = subprocess.run(["docker", *args], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "sha", "app-source-sha", "version", "app-digest", "miniapp-digest",
                 "run-id", "attempt", "deployment-id"):
        result.add_argument(f"--{name}", required=True)
    result.add_argument("--eros-digest")
    result.add_argument("--mode", choices=("ordinary",), default="ordinary")
    for name in ("db-container-id", "redis-container-id", "prisma-head",
                 "prisma-migrations", "prisma-checksums", "baseline-prisma-head",
                 "baseline-prisma-migrations", "baseline-prisma-checksums",
                 "pending-prisma-migrations", "pending-prisma-checksums", "expected-role-identities-b64"):
        result.add_argument(f"--{name}")
    result.add_argument("--public-routes-verified", action="store_true")
    result.add_argument("--migration-compatibility-verified", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    if not SHA40.fullmatch(args.sha):
        raise SystemExit("candidate SHA must be 40 lowercase hex characters")
    if not SHA40.fullmatch(args.app_source_sha):
        raise SystemExit("application source SHA must be 40 lowercase hex characters")
    expected_role_plan = None
    if not SHA256.fullmatch(args.db_container_id or "") or not SHA256.fullmatch(args.redis_container_id or ""):
        raise SystemExit("ordinary manifest needs exact PostgreSQL and Redis container IDs")
    if not re.fullmatch(r"[0-9]{14}_[a-z0-9_]+", args.prisma_head or ""):
        raise SystemExit("ordinary manifest needs a verified Prisma migration head")
    migrations = (args.prisma_migrations or "").split(",")
    checksums = (args.prisma_checksums or "").split(",")
    if (not migrations or any(not re.fullmatch(r"[0-9]{14}_[a-z0-9_]+", value) for value in migrations)
            or len(migrations) != len(set(migrations)) or migrations[-1] != args.prisma_head):
        raise SystemExit("ordinary manifest needs the exact applied Prisma migration inventory")
    checksum_rows = {}
    for value in checksums:
        name, separator, checksum = value.partition("=")
        if (not separator or name in checksum_rows
                or not re.fullmatch(r"[0-9]{14}_[a-z0-9_]+", name)
                or not SHA256.fullmatch(checksum)):
            raise SystemExit("ordinary manifest Prisma checksums are malformed")
        checksum_rows[name] = checksum
    if set(checksum_rows) != set(migrations):
        raise SystemExit("ordinary manifest Prisma checksums do not match applied migrations")
    baseline_migrations = (args.baseline_prisma_migrations or "").split(",")
    baseline_checksums = (args.baseline_prisma_checksums or "").split(",")
    pending_migrations = (args.pending_prisma_migrations or "").split(",") if args.pending_prisma_migrations else []
    pending_checksums = (args.pending_prisma_checksums or "").split(",") if args.pending_prisma_checksums else []
    if (not baseline_migrations
            or any(not re.fullmatch(r"[0-9]{14}_[a-z0-9_]+", value) for value in baseline_migrations)
            or len(baseline_migrations) != len(set(baseline_migrations))
            or baseline_migrations[-1] != args.baseline_prisma_head):
        raise SystemExit("ordinary manifest Prisma baseline inventory is malformed")
    baseline_checksum_rows = {}
    for value in baseline_checksums:
        name, separator, checksum = value.partition("=")
        if (not separator or name in baseline_checksum_rows
                or not re.fullmatch(r"[0-9]{14}_[a-z0-9_]+", name)
                or not SHA256.fullmatch(checksum)):
            raise SystemExit("ordinary manifest Prisma baseline checksums are malformed")
        baseline_checksum_rows[name] = checksum
    pending_checksum_rows = {}
    for value in pending_checksums:
        name, separator, checksum = value.partition("=")
        if (not separator or name in pending_checksum_rows
                or not re.fullmatch(r"[0-9]{14}_[a-z0-9_]+", name)
                or not SHA256.fullmatch(checksum)):
            raise SystemExit("ordinary manifest pending Prisma checksums are malformed")
        pending_checksum_rows[name] = checksum
    if ([name for name in baseline_checksum_rows] != baseline_migrations
            or list(pending_checksum_rows) != pending_migrations
            or len(pending_migrations) != len(set(pending_migrations))):
        raise SystemExit("ordinary manifest migration plan order or checksum coverage is invalid")
    planned_migrations = baseline_migrations + pending_migrations
    planned_checksums = baseline_checksums + pending_checksums
    if (args.baseline_prisma_head != baseline_migrations[-1]
            or migrations != planned_migrations or checksums != planned_checksums
            or args.prisma_head != planned_migrations[-1]):
        raise SystemExit("ordinary manifest live migration transition differs from the exact approved plan")
    if not args.public_routes_verified or not args.migration_compatibility_verified:
        raise SystemExit("ordinary manifest needs verified public routes and live-schema compatibility")
    try:
        expected_role_plan = json.loads(base64.b64decode(args.expected_role_identities_b64 or "", validate=True))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise SystemExit("ordinary manifest expected component role inventory is malformed") from None
    expected_roles = expected_role_plan.get("roles") if isinstance(expected_role_plan, dict) else None
    if not isinstance(expected_roles, dict) or set(expected_roles) != set(ROLE_REPOSITORIES):
        raise SystemExit("ordinary manifest role inventory is incomplete")
    role_repositories = {role: ROLE_REPOSITORIES[role] for role in expected_roles}
    role_components = {role: ROLE_COMPONENTS[role] for role in expected_roles}
    scope = expected_role_plan.get("scope") if isinstance(expected_role_plan, dict) else None
    component_names = set(role_components.values())
    if scope == "FULL":
        updated_components = component_names
    elif scope == "NONE":
        updated_components = set()
    elif isinstance(scope, str):
        updated_components = set(scope.split("+"))
        if (not updated_components.issubset(component_names)
                or "+".join(sorted(updated_components)) != scope):
            updated_components = None
    else:
        updated_components = None
    if (not isinstance(expected_role_plan, dict)
            or expected_role_plan.get("schema") != 1 or expected_role_plan.get("candidateSha") != args.sha
            or updated_components is None or set(expected_role_plan.get("updatedComponents", [])) != updated_components
            or not isinstance(expected_roles, dict) or set(expected_roles) != set(role_repositories)):
        raise SystemExit("ordinary manifest component role plan does not match the exact candidate scope")
    candidate_digests = {
        "gpoint-core-api": "ghcr.io/akhripunov/gpoint-runtime@" + args.app_digest,
        "gpoint-core-worker": "ghcr.io/akhripunov/gpoint-runtime@" + args.app_digest,
        "gpoint-core-bot": "ghcr.io/akhripunov/gpoint-runtime@" + args.app_digest,
        "gpoint-miniapp-web": "ghcr.io/akhripunov/gpoint-miniapp@" + args.miniapp_digest,
        "gpoint-eros-pilot-engine": "ghcr.io/akhripunov/gpoint-eros@" + (args.eros_digest or ""),
    }
    has_eros = "gpoint-eros-pilot-engine" in expected_roles
    if has_eros and not OCI_DIGEST.fullmatch(args.eros_digest or ""):
        raise SystemExit("ordinary manifest needs the exact Eros Engine OCI digest")
    if not has_eros and args.eros_digest:
        raise SystemExit("ordinary manifest received an Eros digest without Eros roles")
    component_identities = {}
    for role, repository in role_repositories.items():
        row = expected_roles[role]
        component = role_components[role]
        version, source, repo_digest = row.get("version"), row.get("sourceSha"), row.get("repositoryDigest")
        baseline_image_id = row.get("baselineImageId")
        if (not re.fullmatch(r"[0-9a-f]{40}-[0-9]+-[0-9]+", version or "")
                or not SHA40.fullmatch(source or "")
                or not re.fullmatch(re.escape(repository) + r"@sha256:[0-9a-f]{64}", repo_digest or "")):
            raise SystemExit("ordinary manifest component role identity is malformed")
        if component in updated_components:
            expected_candidate_digest = candidate_digests[role]
            if (version != args.version or source != args.sha or repo_digest != expected_candidate_digest
                    or baseline_image_id is not None):
                raise SystemExit("ordinary manifest updated role differs from the exact candidate artifact")
        elif not re.fullmatch(r"sha256:[0-9a-f]{64}", baseline_image_id or ""):
            raise SystemExit("ordinary manifest untouched role lacks its exact baseline Docker image ID")
        identity = (version, source, repo_digest, baseline_image_id)
        identity_group = ROLE_IDENTITY_GROUP[role]
        if identity_group in component_identities and component_identities[identity_group] != identity:
            raise SystemExit("ordinary manifest shared component roles have inconsistent identities")
        component_identities[identity_group] = identity
    for field in ("run_id", "attempt", "deployment_id"):
        if not getattr(args, field).isdigit() or int(getattr(args, field)) <= 0:
            raise SystemExit(f"{field.replace('_', ' ')} must be a positive integer")
    if args.version != f"{args.sha}-{args.run_id}-{args.attempt}":
        raise SystemExit("attempt tag does not match the exact candidate SHA and workflow attempt")
    for field in ("app_digest", "miniapp_digest"):
        if not OCI_DIGEST.fullmatch(getattr(args, field)):
            raise SystemExit(f"{field.replace('_', ' ')} must be an immutable OCI digest")

    expected_services = [
        ("gpoint-core-api", "ghcr.io/akhripunov/gpoint-runtime", args.app_digest),
        ("gpoint-core-worker", "ghcr.io/akhripunov/gpoint-runtime", args.app_digest),
        ("gpoint-core-bot", "ghcr.io/akhripunov/gpoint-runtime", args.app_digest),
        ("gpoint-miniapp-web", "ghcr.io/akhripunov/gpoint-miniapp", args.miniapp_digest),
    ]
    if "gpoint-eros-pilot-engine" in expected_role_plan["roles"]:
        expected_services.extend([
            ("gpoint-eros-pilot-engine", "ghcr.io/akhripunov/gpoint-eros", args.eros_digest),
        ])
    runtime_services = []
    for prefix, repository, digest in expected_services:
        expected_role = expected_role_plan["roles"][prefix]
        role_version = expected_role["version"]
        role_source = expected_role["sourceSha"]
        expected_digest = expected_role["repositoryDigest"]
        expected_baseline_image_id = expected_role["baselineImageId"]
        name = f"{prefix}-{role_version}"
        container = docker_json("inspect", name)[0]
        image_id = container["Image"]
        image = docker_json("image", "inspect", image_id)[0]
        if expected_baseline_image_id is not None and image_id != expected_baseline_image_id:
            raise SystemExit(f"untouched runtime image ID changed before manifest: {name}")
        revision_label = ("com.gpoint.source.revision" if prefix == "gpoint-eros-pilot-engine"
                          else "org.opencontainers.image.revision")
        if (not container["State"]["Running"]
                or container["Config"]["Labels"].get(revision_label) != role_source):
            raise SystemExit(f"final runtime identity changed before manifest: {name}")
        if prefix in {"gpoint-core-api", "gpoint-core-worker", "gpoint-core-bot"}:
            process_sha = subprocess.run(
                ["docker", "exec", name, "node", "-p", "process.env.RELEASE_SHA || ''"],
                check=True, capture_output=True, text=True).stdout.strip()
            if process_sha != role_source:
                raise SystemExit(f"runtime RELEASE_SHA does not match application source: {name}")
        if expected_digest not in image.get("RepoDigests", []):
            raise SystemExit(f"final runtime digest changed before manifest: {name}")
        runtime_services.append({
            "name": name,
            "component": ROLE_COMPONENTS[prefix],
            "version": role_version,
            "containerId": container["Id"],
            "imageId": image_id,
            "repositoryDigest": expected_digest,
            "revision": role_source,
            "running": True,
            "healthStatus": container["State"].get("Health", {}).get("Status", "none"),
            "restartCount": container["RestartCount"],
        })

    if not shutil.which("systemctl"):
        raise SystemExit("cannot verify Caddy state without systemctl")
    caddy = subprocess.run(["systemctl", "is-active", "--quiet", "caddy"], check=False)
    if caddy.returncode == 0:
        raise SystemExit("Caddy must be inactive on ordinary Kamal production topology")
    if caddy.returncode != 3:
        raise SystemExit("could not prove Caddy is inactive")
    for name, expected_id in (("shared_postgres_1", args.db_container_id),
                              ("shared_redis_1", args.redis_container_id)):
        container = docker_json("inspect", name)[0]
        if container.get("Id") != expected_id or not container.get("State", {}).get("Running"):
            raise SystemExit(f"{name} changed or stopped during ordinary deployment")
    proxy = docker_json("inspect", "kamal-proxy")[0]
    if not proxy.get("State", {}).get("Running"):
        raise SystemExit("Kamal Proxy is not running")
    ports = proxy.get("NetworkSettings", {}).get("Ports", {})
    for port in ("80/tcp", "443/tcp"):
        bindings = ports.get(port) or []
        if not any(binding.get("HostPort") == port.split("/", 1)[0] for binding in bindings):
            raise SystemExit(f"Kamal Proxy does not own public port {port.split('/', 1)[0]}")
    stopped_compose = []
    for service in ("api", "worker", "bot"):
        result = subprocess.run(["docker", "ps", "-aq", "--filter",
                                 "label=com.docker.compose.project=compose", "--filter",
                                 f"label=com.docker.compose.service={service}"],
                                check=True, capture_output=True, text=True)
        container_ids = [value for value in result.stdout.splitlines() if value]
        if not container_ids:
            raise SystemExit(f"expected stopped legacy Compose service is missing: {service}")
        for container_id in container_ids:
            container = docker_json("inspect", container_id)[0]
            if container.get("State", {}).get("Running"):
                raise SystemExit(f"legacy Compose service must remain stopped: {service}")
            stopped_compose.append({"service": service, "containerId": container_id, "running": False})
    ordinary_preservation = {
        "postgresContainerId": args.db_container_id,
        "redisContainerId": args.redis_container_id,
        "caddy": "INACTIVE",
        "legacyCompose": "STOPPED",
        "legacyComposeContainers": stopped_compose,
        "databaseMigrationsAppliedByRelease": bool(pending_migrations),
        "migrationCompatibility": "PASS",
    }
    public_ingress = {"proxy": "kamal-proxy", "httpPort": 80, "httpsPort": 443,
                      "tls": "PASS", "routes": "PASS"}

    manifest = {
        "schema": "gpoint-kamal-production-manifest/v2",
        "operationMode": "ordinary",
        "result": "PASS",
        "verifiedAtUtc": datetime.now(timezone.utc).isoformat(),
        "candidate": {
            "gitSha": args.sha,
            "attemptTag": args.version,
            "appSourceSha": args.app_source_sha,
            "images": {
                "core": f"ghcr.io/akhripunov/gpoint-runtime@{args.app_digest}",
                "miniapp": f"ghcr.io/akhripunov/gpoint-miniapp@{args.miniapp_digest}",
                **({"eros": f"ghcr.io/akhripunov/gpoint-eros@{args.eros_digest}",
                }
                   if "gpoint-eros-pilot-engine" in expected_role_plan["roles"] else {}),
            },
        },
        "deploymentScope": {
            "classification": expected_role_plan["scope"],
            "updatedComponents": expected_role_plan["updatedComponents"],
        },
        "runtimeServices": runtime_services,
        "attempt": {"runId": args.run_id, "runAttempt": args.attempt,
                    "githubDeploymentId": args.deployment_id},
        "evidence": {
            "publicRoutes": "PASS", "migrationCompatibility": "PASS",
            "prismaHead": args.prisma_head,
        },
        "preservation": ordinary_preservation,
        "publicIngress": public_ingress,
        "migrationHeads": {
            "prismaHead": args.prisma_head,
            "prismaAppliedMigrations": migrations,
            "prismaAppliedChecksums": checksum_rows,
            "baselineUnchanged": {
                "prismaHead": args.baseline_prisma_head,
                "prismaMigrations": baseline_migrations,
                "prismaChecksums": baseline_checksum_rows,
            },
            "approvedPendingMigrations": [
                {"name": name, "checksum": digest} for name, digest in pending_checksum_rows.items()
            ],
            "liveBaselineUnchanged": not pending_migrations,
            "appliedExactlyApprovedPlan": True,
            "databaseWrites": bool(pending_migrations),
        },
        "verified": [
            "public-api-health", "public-media-route", "public-miniapp-routes",
            "exact-image-digests-and-git-sha", "service-health", "kamal-proxy-public-tls",
            "postgres-redis-preserved", "live-schema-compatibility-and-baseline-unchanged",
        ],
    }
    output_path = Path(args.output)
    fd, temporary = tempfile.mkstemp(prefix=".kamal-manifest-", dir=output_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(manifest, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.link(temporary, output_path)
        os.unlink(temporary)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
    print(f"GPOINT_KAMAL_MANIFEST_SHA256={digest} PATH={output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
