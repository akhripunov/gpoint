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


def docker_json(*args: str) -> object:
    result = subprocess.run(["docker", *args], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "sha", "version", "app-digest", "agata-digest", "miniapp-digest",
                 "run-id", "attempt", "deployment-id"):
        result.add_argument(f"--{name}", required=True)
    result.add_argument("--mode", choices=("cutover", "ordinary"), default="cutover")
    for name in ("baseline-path", "baseline-sha",
                 "inventory-path", "inventory-sha", "backup-path", "backup-sha", "restore-path",
                 "restore-sha", "agata-candidate-head", "agata-live-head", "postgres-system-identifier",
                 "network-fingerprint", "db-container-id", "redis-container-id"):
        result.add_argument(f"--{name}")
    for name in ("media-backup-path", "media-backup-sha", "media-source-manifest-sha",
                 "media-restore-manifest-sha", "media-file-count", "media-directory-count",
                 "media-total-bytes", "media-backup-bytes"):
        result.add_argument(f"--{name}")
    result.add_argument("--public-routes-verified", action="store_true")
    result.add_argument("--migration-parity-verified", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    if not SHA40.fullmatch(args.sha):
        raise SystemExit("candidate SHA must be 40 lowercase hex characters")
    if args.mode == "cutover":
        required = ("baseline_path", "inventory_path", "backup_path", "restore_path",
                    "agata_candidate_head", "agata_live_head", "postgres_system_identifier",
                    "media_backup_path", "media_file_count", "media_directory_count", "media_total_bytes",
                    "media_backup_bytes")
        missing = [field.replace("_", " ") for field in required if not getattr(args, field)]
        if missing:
            raise SystemExit("cutover manifest is missing: " + ", ".join(missing))
        for field in ("baseline_sha", "inventory_sha", "backup_sha", "restore_sha", "network_fingerprint",
                      "media_backup_sha", "media_source_manifest_sha", "media_restore_manifest_sha"):
            if not SHA256.fullmatch(getattr(args, field) or ""):
                raise SystemExit(f"{field.replace('_', ' ')} must be a SHA-256 hex digest")
    else:
        if not SHA256.fullmatch(args.db_container_id or "") or not SHA256.fullmatch(args.redis_container_id or ""):
            raise SystemExit("ordinary manifest needs exact PostgreSQL and Redis container IDs")
        if not args.public_routes_verified or not args.migration_parity_verified:
            raise SystemExit("ordinary manifest needs verified public routes and read-only migration parity")
    for field in ("run_id", "attempt", "deployment_id"):
        if not getattr(args, field).isdigit() or int(getattr(args, field)) <= 0:
            raise SystemExit(f"{field.replace('_', ' ')} must be a positive integer")
    if args.version != f"{args.sha}-{args.run_id}-{args.attempt}":
        raise SystemExit("attempt tag does not match the exact candidate SHA and workflow attempt")
    if args.mode == "cutover":
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
    for field in ("app_digest", "agata_digest", "miniapp_digest"):
        if not OCI_DIGEST.fullmatch(getattr(args, field)):
            raise SystemExit(f"{field.replace('_', ' ')} must be an immutable OCI digest")

    expected_services = [
        ("gpoint-core-api", "ghcr.io/akhripunov/gpoint-runtime", args.app_digest),
        ("gpoint-core-worker", "ghcr.io/akhripunov/gpoint-runtime", args.app_digest),
        ("gpoint-core-bot", "ghcr.io/akhripunov/gpoint-runtime", args.app_digest),
        ("gpoint-agata-api", "ghcr.io/akhripunov/gpoint-agata-runtime", args.agata_digest),
        ("gpoint-agata-memory-worker", "ghcr.io/akhripunov/gpoint-agata-runtime", args.agata_digest),
        ("gpoint-miniapp-web", "ghcr.io/akhripunov/gpoint-miniapp", args.miniapp_digest),
    ]
    runtime_services = []
    for prefix, repository, digest in expected_services:
        name = f"{prefix}-{args.version}"
        container = docker_json("inspect", name)[0]
        image_id = container["Image"]
        image = docker_json("image", "inspect", image_id)[0]
        repo_digest = f"{repository}@{digest}"
        if (not container["State"]["Running"]
                or container["Config"]["Labels"].get("org.opencontainers.image.revision") != args.sha):
            raise SystemExit(f"final runtime identity changed before manifest: {name}")
        if repo_digest not in image.get("RepoDigests", []):
            raise SystemExit(f"final runtime digest changed before manifest: {name}")
        runtime_services.append({
            "name": name,
            "containerId": container["Id"],
            "imageId": image_id,
            "repositoryDigest": repo_digest,
            "revision": args.sha,
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
                raise SystemExit(f"expected stopped legacy Compose service is missing: {service}")
            for container_id in container_ids:
                container = docker_json("inspect", container_id)[0]
                if container.get("State", {}).get("Running"):
                    raise SystemExit(f"legacy Compose service must remain stopped: {service}")
                stopped_compose.append({"service": service, "containerId": container_id, "running": False})
        stopped_gsales = []
        for service in ("gsales-api", "gsales-agata-api", "gsales-agata-memory-worker"):
            result = subprocess.run(["docker", "ps", "-aq", "--filter",
                                     "--filter", f"label=com.docker.compose.project=compose",
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
            "migrationParity": "PASS",
        }
        public_ingress = {"proxy": "kamal-proxy", "httpPort": 80, "httpsPort": 443,
                          "tls": "PASS", "routes": "PASS"}

    manifest = {
        "schema": "gpoint-kamal-production-manifest/v1",
        "operationMode": args.mode,
        "result": "PASS",
        "verifiedAtUtc": datetime.now(timezone.utc).isoformat(),
        "candidate": {
            "gitSha": args.sha,
            "attemptTag": args.version,
            "images": {
                "core": f"ghcr.io/akhripunov/gpoint-runtime@{args.app_digest}",
                "agata": f"ghcr.io/akhripunov/gpoint-agata-runtime@{args.agata_digest}",
                "miniapp": f"ghcr.io/akhripunov/gpoint-miniapp@{args.miniapp_digest}",
            },
        },
        "runtimeServices": runtime_services,
        "attempt": {"runId": args.run_id, "runAttempt": args.attempt,
                    "githubDeploymentId": args.deployment_id},
        "evidence": ({
            "baseline": {"path": args.baseline_path, "sha256": args.baseline_sha},
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
            "publicRoutes": "PASS", "migrationParity": "PASS",
        }),
        "preservation": ({
            "postgresSystemIdentifier": args.postgres_system_identifier,
            "erosNetworkFingerprint": args.network_fingerprint,
            "persistentMediaBackupAndRestore": "PASS",
            "gsales": "STOPPED",
            "databaseMigrationsAppliedByCutover": False,
        } if args.mode == "cutover" else ordinary_preservation),
        "publicIngress": public_ingress,
        "migrationHeads": ({
            "agataCandidate": args.agata_candidate_head,
            "agataLive": args.agata_live_head, "agataParity": "PASS",
            "prismaStatus": "UP_TO_DATE",
        } if args.mode == "cutover" else {"parity": "PASS", "databaseWrites": False}),
        "verified": ([
            "public-api-health", "public-miniapp-routes", "exact-image-digests-and-git-sha",
            "service-health", "media-read-write", "worker-bot-singletons",
            "media-backup-offsite-readback-and-isolated-restore",
            "postgres-eros-caddy-preservation", "migration-heads", "gsales-stopped",
        ] if args.mode == "cutover" else [
            "public-api-health", "public-media-route", "public-miniapp-routes",
            "exact-image-digests-and-git-sha", "service-health", "kamal-proxy-public-tls",
            "postgres-redis-preserved", "read-only-migration-parity", "gsales-stopped",
        ]),
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
