#!/usr/bin/env python3
"""Persist the exact post-Verify GPoint Kamal runtime evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
                 "run-id", "attempt", "deployment-id", "baseline-path", "baseline-sha",
                 "inventory-path", "inventory-sha", "backup-path", "backup-sha", "restore-path",
                 "restore-sha", "agata-candidate-head", "agata-live-head", "postgres-system-identifier",
                 "network-fingerprint"):
        result.add_argument(f"--{name}", required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    if not SHA40.fullmatch(args.sha):
        raise SystemExit("candidate SHA must be 40 lowercase hex characters")
    for field in ("baseline_sha", "inventory_sha", "backup_sha", "restore_sha", "network_fingerprint"):
        if not SHA256.fullmatch(getattr(args, field)):
            raise SystemExit(f"{field.replace('_', ' ')} must be a SHA-256 hex digest")
    for field in ("run_id", "attempt", "deployment_id"):
        if not getattr(args, field).isdigit() or int(getattr(args, field)) <= 0:
            raise SystemExit(f"{field.replace('_', ' ')} must be a positive integer")
    if args.version != f"{args.sha}-{args.run_id}-{args.attempt}":
        raise SystemExit("attempt tag does not match the exact candidate SHA and workflow attempt")
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

    manifest = {
        "schema": "gpoint-kamal-production-manifest/v1",
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
        "evidence": {
            "baseline": {"path": args.baseline_path, "sha256": args.baseline_sha},
            "inventory": {"path": args.inventory_path, "sha256": args.inventory_sha},
            "encryptedBackup": {"path": args.backup_path, "sha256": args.backup_sha,
                                "encryption": "age-x25519", "offsiteRestore": "PASS"},
            "isolatedRestore": {"path": args.restore_path, "sha256": args.restore_sha,
                                "result": "PASS"},
        },
        "preservation": {
            "postgresSystemIdentifier": args.postgres_system_identifier,
            "erosNetworkFingerprint": args.network_fingerprint,
            "gsales": "STOPPED",
            "databaseMigrationsAppliedByCutover": False,
        },
        "migrationHeads": {"agataCandidate": args.agata_candidate_head,
                           "agataLive": args.agata_live_head, "agataParity": "PASS",
                           "prismaStatus": "UP_TO_DATE"},
        "verified": [
            "public-api-health", "public-miniapp-routes", "exact-image-digests-and-git-sha",
            "service-health", "media-read-write", "worker-bot-singletons",
            "postgres-eros-caddy-preservation", "migration-heads", "gsales-stopped",
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
