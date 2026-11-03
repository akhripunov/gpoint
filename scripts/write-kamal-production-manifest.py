#!/usr/bin/env python3
"""Persist the exact post-Verify GPoint Kamal runtime evidence."""

from __future__ import annotations

import argparse
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

SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def read_valid_no_eros_state(path: str, expected_sha: str) -> dict[str, object]:
    try:
        contents = Path(path).read_bytes()
        value = json.loads(contents)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise SystemExit("validated no-Eros state snapshot is unreadable") from None
    if hashlib.sha256(contents).hexdigest() != expected_sha:
        raise SystemExit("validated no-Eros state snapshot file hash changed")
    if not isinstance(value, dict):
        raise SystemExit("validated no-Eros state snapshot has invalid structure")
    state_sha = value.get("stateSha256")
    body = {key: item for key, item in value.items() if key != "stateSha256"}
    if (value.get("schema") != "gpoint-kamal-eros-state/v1" or value.get("erosAbsent") is not True
            or not isinstance(state_sha, str) or not SHA256.fullmatch(state_sha)
            or hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != state_sha):
        raise SystemExit("validated no-Eros state snapshot failed its content check")
    postgres = value.get("postgres")
    if (not isinstance(postgres, dict) or not isinstance(postgres.get("systemIdentifier"), str)
            or not postgres["systemIdentifier"].isdigit()
            or not isinstance(postgres.get("serverVersionNum"), int)
            or not SHA256.fullmatch(str(postgres.get("catalogAclSha256", "")))
            or value.get("database") != {"eros": None, "roles": [], "memberships": [], "gpointErosSchemas": []}
            or value.get("containers") != [] or value.get("networks") != []):
        raise SystemExit("validated no-Eros state snapshot is incomplete")
    return value


def docker_json(*args: str) -> object:
    result = subprocess.run(["docker", *args], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "sha", "app-source-sha", "version", "app-digest", "miniapp-digest",
                 "run-id", "attempt", "deployment-id"):
        result.add_argument(f"--{name}", required=True)
    result.add_argument("--agata-digest")
    result.add_argument("--mode", choices=("cutover", "ordinary"), default="cutover")
    for name in ("baseline-path", "baseline-sha",
                 "inventory-path", "inventory-sha", "backup-path", "backup-sha", "restore-path",
                 "restore-sha", "agata-candidate-head", "agata-live-head", "postgres-system-identifier",
                 "db-container-id", "redis-container-id", "no-eros-baseline-path",
                 "no-eros-baseline-sha", "no-eros-postflight-path", "no-eros-postflight-sha"):
        result.add_argument(f"--{name}")
    for name in ("media-backup-path", "media-backup-sha", "media-source-manifest-sha",
                 "media-restore-manifest-sha", "media-file-count", "media-directory-count",
                 "media-total-bytes", "media-backup-bytes", "prisma-head", "agata-head",
                 "prisma-migrations", "prisma-checksums", "baseline-prisma-head",
                 "baseline-prisma-migrations", "baseline-prisma-checksums", "baseline-agata-head"):
        result.add_argument(f"--{name}")
    result.add_argument("--public-routes-verified", action="store_true")
    result.add_argument("--migration-compatibility-verified", action="store_true")
    result.add_argument("--postgres-acl-fingerprint")
    return result


def main() -> int:
    args = parser().parse_args()
    if not SHA40.fullmatch(args.sha):
        raise SystemExit("candidate SHA must be 40 lowercase hex characters")
    if not SHA40.fullmatch(args.app_source_sha):
        raise SystemExit("application source SHA must be 40 lowercase hex characters")
    if args.mode == "cutover":
        required = ("baseline_path", "inventory_path", "backup_path", "restore_path",
                    "agata_candidate_head", "agata_live_head", "postgres_system_identifier",
                    "no_eros_baseline_path", "no_eros_postflight_path",
                    "postgres_acl_fingerprint",
                    "media_backup_path", "media_file_count", "media_directory_count", "media_total_bytes",
                    "media_backup_bytes")
        missing = [field.replace("_", " ") for field in required if not getattr(args, field)]
        if missing:
            raise SystemExit("cutover manifest is missing: " + ", ".join(missing))
        for field in ("baseline_sha", "inventory_sha", "backup_sha", "restore_sha",
                      "no_eros_baseline_sha", "no_eros_postflight_sha",
                      "media_backup_sha", "media_source_manifest_sha", "media_restore_manifest_sha"):
            if not SHA256.fullmatch(getattr(args, field) or ""):
                raise SystemExit(f"{field.replace('_', ' ')} must be a SHA-256 hex digest")
        if not SHA256.fullmatch(args.postgres_acl_fingerprint or ""):
            raise SystemExit("postgres ACL fingerprint must be a SHA-256 hex digest")
    else:
        if not SHA256.fullmatch(args.db_container_id or "") or not SHA256.fullmatch(args.redis_container_id or ""):
            raise SystemExit("ordinary manifest needs exact PostgreSQL and Redis container IDs")
        if not re.fullmatch(r"[0-9]{14}_[a-z0-9_]+", args.prisma_head or ""):
            raise SystemExit("ordinary manifest needs a verified Prisma migration head")
        if args.agata_digest:
            raise SystemExit("ordinary manifest cannot include retired AGATA runtime services")
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
        if (args.baseline_prisma_head != args.prisma_head
                or baseline_migrations != migrations or baseline_checksums != checksums):
            raise SystemExit("ordinary manifest live DB migration baseline changed during Deploy")
        if not args.public_routes_verified or not args.migration_compatibility_verified:
            raise SystemExit("ordinary manifest needs verified public routes and live-schema compatibility")
    for field in ("run_id", "attempt", "deployment_id"):
        if not getattr(args, field).isdigit() or int(getattr(args, field)) <= 0:
            raise SystemExit(f"{field.replace('_', ' ')} must be a positive integer")
    if args.version != f"{args.sha}-{args.run_id}-{args.attempt}":
        raise SystemExit("attempt tag does not match the exact candidate SHA and workflow attempt")
    if args.mode == "cutover":
        expected_state_name = f"kamal-no-eros-state-{args.run_id}-{args.attempt}"
        if Path(args.no_eros_baseline_path).name != expected_state_name + ".json":
            raise SystemExit("no-Eros baseline path does not match the exact cutover attempt")
        if (Path(args.no_eros_postflight_path).name != expected_state_name + "-postflight.json"
                or Path(args.no_eros_postflight_path).parent != Path(args.no_eros_baseline_path).parent):
            raise SystemExit("no-Eros postflight path does not match the exact cutover attempt")
        no_eros_baseline = read_valid_no_eros_state(args.no_eros_baseline_path, args.no_eros_baseline_sha)
        no_eros_postflight = read_valid_no_eros_state(args.no_eros_postflight_path, args.no_eros_postflight_sha)
        if no_eros_baseline != no_eros_postflight:
            raise SystemExit("no-Eros baseline and postflight state differ")
        if no_eros_baseline["postgres"]["systemIdentifier"] != args.postgres_system_identifier:
            raise SystemExit("no-Eros snapshot PostgreSQL identity differs from the production baseline")
        expected_media_path = (f"/opt/gpoint/backups/gpoint-pre-release-{args.sha}-"
                               f"{args.run_id}-{args.attempt}.media.tar.age")
        if args.media_backup_path != expected_media_path:
            raise SystemExit("media backup path does not match the exact cutover attempt")
        if args.media_source_manifest_sha != args.media_restore_manifest_sha:
            raise SystemExit("restored media manifest does not match the source tree")
        for field in ("media_file_count", "media_directory_count", "media_total_bytes", "media_backup_bytes"):
            value = getattr(args, field)
            if not value.isdigit():
                raise SystemExit(f"{field.replace('_', ' ')} must be a nonnegative integer")
        if int(args.media_backup_bytes) <= 0:
            raise SystemExit("media backup bytes must be positive")
    for field in ("app_digest", "miniapp_digest"):
        if not OCI_DIGEST.fullmatch(getattr(args, field)):
            raise SystemExit(f"{field.replace('_', ' ')} must be an immutable OCI digest")
    if args.agata_digest and not OCI_DIGEST.fullmatch(args.agata_digest):
        raise SystemExit("agata digest must be an immutable OCI digest")

    expected_services = [
        ("gpoint-core-api", "ghcr.io/akhripunov/gpoint-runtime", args.app_digest),
        ("gpoint-core-worker", "ghcr.io/akhripunov/gpoint-runtime", args.app_digest),
        ("gpoint-core-bot", "ghcr.io/akhripunov/gpoint-runtime", args.app_digest),
        ("gpoint-miniapp-web", "ghcr.io/akhripunov/gpoint-miniapp", args.miniapp_digest),
    ]
    if args.agata_digest:
        expected_services[3:3] = [
            ("gpoint-agata-api", "ghcr.io/akhripunov/gpoint-agata-runtime", args.agata_digest),
            ("gpoint-agata-memory-worker", "ghcr.io/akhripunov/gpoint-agata-runtime", args.agata_digest),
        ]
    runtime_services = []
    for prefix, repository, digest in expected_services:
        name = f"{prefix}-{args.version}"
        container = docker_json("inspect", name)[0]
        image_id = container["Image"]
        image = docker_json("image", "inspect", image_id)[0]
        repo_digest = f"{repository}@{digest}"
        if (not container["State"]["Running"]
                or container["Config"]["Labels"].get("org.opencontainers.image.revision") != args.app_source_sha):
            raise SystemExit(f"final runtime identity changed before manifest: {name}")
        if prefix != "gpoint-miniapp-web":
            if prefix.startswith("gpoint-agata-"):
                process_sha = subprocess.run(
                    ["docker", "exec", name, "python", "-c",
                     "import os; print(os.environ.get('RELEASE_SHA', ''))"],
                    check=True, capture_output=True, text=True).stdout.strip()
            else:
                process_sha = subprocess.run(
                    ["docker", "exec", name, "node", "-p", "process.env.RELEASE_SHA || ''"],
                    check=True, capture_output=True, text=True).stdout.strip()
            if process_sha != args.app_source_sha:
                raise SystemExit(f"runtime RELEASE_SHA does not match application source: {name}")
        if repo_digest not in image.get("RepoDigests", []):
            raise SystemExit(f"final runtime digest changed before manifest: {name}")
        runtime_services.append({
            "name": name,
            "containerId": container["Id"],
            "imageId": image_id,
            "repositoryDigest": repo_digest,
            "revision": args.app_source_sha,
            "running": True,
            "healthStatus": container["State"].get("Health", {}).get("Status", "none"),
            "restartCount": container["RestartCount"],
        })

    ordinary_preservation = None
    public_ingress = None
    if args.mode == "ordinary":
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
        for service in ("api", "worker", "bot", "agata-api", "agata-memory-worker"):
            result = subprocess.run(["docker", "ps", "-aq", "--filter",
                                     "label=com.docker.compose.project=compose", "--filter",
                                     f"label=com.docker.compose.service={service}"],
                                    check=True, capture_output=True, text=True)
            container_ids = [value for value in result.stdout.splitlines() if value]
            if not container_ids:
                if service in ("agata-api", "agata-memory-worker"):
                    continue
                raise SystemExit(f"expected stopped legacy Compose service is missing: {service}")
            for container_id in container_ids:
                container = docker_json("inspect", container_id)[0]
                if container.get("State", {}).get("Running"):
                    raise SystemExit(f"legacy Compose service must remain stopped: {service}")
                stopped_compose.append({"service": service, "containerId": container_id, "running": False})
        stopped_gsales = []
        for service in ("gsales-api", "gsales-agata-api", "gsales-agata-memory-worker"):
            result = subprocess.run(["docker", "ps", "-aq", "--filter",
                                     "label=com.docker.compose.project=compose",
                                     "--filter", f"label=com.docker.compose.service={service}"],
                                    check=True, capture_output=True, text=True)
            container_ids = [value for value in result.stdout.splitlines() if value]
            if not container_ids:
                raise SystemExit(f"expected stopped GSales service is missing: {service}")
            for container_id in container_ids:
                container = docker_json("inspect", container_id)[0]
                if container.get("State", {}).get("Running"):
                    raise SystemExit(f"GSales service must remain stopped: {service}")
                stopped_gsales.append({"service": service, "containerId": container_id, "running": False})
        ordinary_preservation = {
            "postgresContainerId": args.db_container_id,
            "redisContainerId": args.redis_container_id,
            "caddy": "INACTIVE",
            "legacyCompose": "STOPPED",
            "legacyComposeContainers": stopped_compose,
            "gsales": "STOPPED",
            "gsalesContainers": stopped_gsales,
            "databaseMigrationsAppliedByRelease": False,
            "migrationCompatibility": "PASS",
        }
        public_ingress = {"proxy": "kamal-proxy", "httpPort": 80, "httpsPort": 443,
                          "tls": "PASS", "routes": "PASS"}

    manifest = {
        "schema": "gpoint-kamal-production-manifest/v2",
        "operationMode": args.mode,
        "result": "PASS",
        "verifiedAtUtc": datetime.now(timezone.utc).isoformat(),
        "candidate": {
            "gitSha": args.sha,
            "attemptTag": args.version,
            "appSourceSha": args.app_source_sha,
            "images": {
                "core": f"ghcr.io/akhripunov/gpoint-runtime@{args.app_digest}",
                "miniapp": f"ghcr.io/akhripunov/gpoint-miniapp@{args.miniapp_digest}",
            },
        },
        "runtimeServices": runtime_services,
        "attempt": {"runId": args.run_id, "runAttempt": args.attempt,
                    "githubDeploymentId": args.deployment_id},
        "evidence": ({
            "baseline": {"path": args.baseline_path, "sha256": args.baseline_sha},
            "noErosBaseline": {"path": args.no_eros_baseline_path, "sha256": args.no_eros_baseline_sha,
                               "result": "PASS"},
            "noErosPostflight": {"path": args.no_eros_postflight_path, "sha256": args.no_eros_postflight_sha,
                                 "result": "PASS"},
            "inventory": {"path": args.inventory_path, "sha256": args.inventory_sha},
            "encryptedBackup": {"path": args.backup_path, "sha256": args.backup_sha,
                                "encryption": "age-x25519", "offsiteRestore": "PASS"},
            "isolatedRestore": {"path": args.restore_path, "sha256": args.restore_sha,
                                "result": "PASS"},
            "mediaBackup": {"path": args.media_backup_path, "sha256": args.media_backup_sha,
                            "bytes": int(args.media_backup_bytes), "encryption": "age-x25519", "offsiteRestore": "PASS",
                            "fileCount": int(args.media_file_count),
                            "directoryCount": int(args.media_directory_count),
                            "totalBytes": int(args.media_total_bytes),
                            "sourceManifestSha256": args.media_source_manifest_sha,
                            "restoredManifestSha256": args.media_restore_manifest_sha},
            "mediaRestore": {"result": "PASS", "manifestSha256": args.media_restore_manifest_sha},
        } if args.mode == "cutover" else {
            "publicRoutes": "PASS", "migrationCompatibility": "PASS",
        "prismaHead": args.prisma_head,
        }),
        "preservation": ({
            "postgresSystemIdentifier": args.postgres_system_identifier,
            "postgresAclFingerprint": args.postgres_acl_fingerprint,
            "postgresCatalogAclSha256": no_eros_postflight["postgres"]["catalogAclSha256"],
            "erosAbsent": "PASS",
            "persistentMediaBackupAndRestore": "PASS",
            "gsales": "STOPPED",
            "databaseMigrationsAppliedByCutover": False,
        } if args.mode == "cutover" else ordinary_preservation),
        "publicIngress": public_ingress,
        "migrationHeads": ({
            "agataCandidate": args.agata_candidate_head,
            "agataLive": args.agata_live_head, "agataParity": "PASS",
            "prismaStatus": "UP_TO_DATE",
        } if args.mode == "cutover" else {
            "prismaHead": args.prisma_head,
            "prismaAppliedMigrations": migrations,
            "prismaAppliedChecksums": checksum_rows,
            "baselineUnchanged": {
                "prismaHead": args.baseline_prisma_head,
                "prismaMigrations": baseline_migrations,
                "prismaChecksums": dict(value.split("=", 1) for value in baseline_checksums),
            },
            "liveBaselineUnchanged": True,
            "databaseWrites": False,
        }),
        "verified": ([
            "public-api-health", "public-miniapp-routes", "exact-image-digests-and-git-sha",
            "service-health", "media-read-write", "worker-bot-singletons",
            "media-backup-offsite-readback-and-isolated-restore",
            "postgres-acl-no-eros-caddy-preservation", "migration-heads", "gsales-stopped",
        ] if args.mode == "cutover" else [
            "public-api-health", "public-media-route", "public-miniapp-routes",
            "exact-image-digests-and-git-sha", "service-health", "kamal-proxy-public-tls",
            "postgres-redis-preserved", "live-schema-compatibility-and-baseline-unchanged", "gsales-stopped",
        ]),
    }
    if args.agata_digest:
        manifest["candidate"]["images"]["agata"] = f"ghcr.io/akhripunov/gpoint-agata-runtime@{args.agata_digest}"
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
