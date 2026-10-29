#!/usr/bin/env python3
"""Restore the exact pre-cutover GPoint runtime after a failed Kamal Apply.

This is intentionally attempt-scoped and fail-closed. It removes only routes
and containers that can be tied to the requested image digests and exact
Kamal version, then starts only the Compose containers recorded as running in
the verified baseline. It never changes PostgreSQL, networks, volumes, or
application data.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kamal_proxy_routes import parse_routes, validate_gpoint_routes


# Fixed production locations. Tests may replace these module constants while
# importing recover(); the command line never accepts alternate paths.
RELEASE_ROOT = Path("/opt/gpoint/release-logs")
BACKUP_ROOT = Path("/opt/gpoint/backups")
CADDYFILE = Path("/etc/caddy/Caddyfile")


class RecoveryBlocked(RuntimeError):
    pass


def command(argv: list[str], *, input_text: str | None = None, check: bool = True) -> str:
    result = subprocess.run(argv, input=input_text, text=True, capture_output=True, check=False, timeout=60)
    if check and result.returncode:
        raise RecoveryBlocked("a required recovery command failed")
    return result.stdout.strip()


def read_baseline(
    path: Path, expected_sha: str, sha: str, run_id: str, attempt: str,
    release_root: Path = Path("/opt/gpoint/release-logs"), owner_uid: int = 0,
) -> tuple[dict[str, str], dict[str, dict]]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise RecoveryBlocked("baseline checksum argument is invalid")
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_uid != owner_uid or path.stat().st_mode & 0o077:
            raise RecoveryBlocked("baseline evidence ownership or permissions are invalid")
        raw = path.read_bytes()
    except OSError as exc:
        raise RecoveryBlocked("baseline evidence is unavailable") from exc
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise RecoveryBlocked("baseline evidence checksum differs")
    values: dict[str, str] = {}
    try:
        for line in raw.decode("utf-8").splitlines():
            key, value = line.split("=", 1)
            if key in values:
                raise RecoveryBlocked("baseline evidence has duplicate keys")
            values[key] = value
    except (UnicodeDecodeError, ValueError) as exc:
        raise RecoveryBlocked("baseline evidence format is invalid") from exc
    if (values.get("BASELINE_SCHEMA") != "gpoint-kamal-cutover-baseline/v1"
            or values.get("CANDIDATE_SHA") != sha or values.get("RUN_ID") != run_id
            or values.get("ATTEMPT") != attempt or values.get("KAMAL_PROXY_BASELINE") != "ABSENT"):
        raise RecoveryBlocked("baseline identity is not this recovery attempt")
    if not re.fullmatch(r"[0-9]{10,12}", values.get("BASELINE_CAPTURED_AT_EPOCH", "")):
        raise RecoveryBlocked("baseline capture time is invalid")
    inventory_path = Path(values.get("INVENTORY_FILE", ""))
    inventory_sha = values.get("INVENTORY_SHA256", "")
    expected_inventory = release_root / sha / f"kamal-vps-inventory-{run_id}-{attempt}.json"
    if (str(inventory_path) != str(expected_inventory)
            or not re.fullmatch(r"[0-9a-f]{64}", inventory_sha)):
        raise RecoveryBlocked("baseline inventory identity is invalid")
    try:
        if (inventory_path.is_symlink() or not inventory_path.is_file()
                or inventory_path.stat().st_uid != owner_uid or inventory_path.stat().st_mode & 0o077):
            raise RecoveryBlocked("baseline inventory ownership or permissions are invalid")
        inventory_raw = inventory_path.read_bytes()
        inventory = json.loads(inventory_raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryBlocked("baseline inventory is unavailable") from exc
    if hashlib.sha256(inventory_raw).hexdigest() != inventory_sha or inventory.get("schema") != "gpoint-vps-inventory/v1":
        raise RecoveryBlocked("baseline inventory checksum or schema differs")
    rows = inventory.get("containers")
    if not isinstance(rows, list):
        raise RecoveryBlocked("baseline container inventory is incomplete")
    legacy: dict[str, dict] = {}
    expected = {"api", "worker", "bot", "agata-api", "agata-memory-worker"}
    stopped_gsales = {"gsales-api", "gsales-agata-api", "gsales-agata-memory-worker"}
    for row in rows:
        if row.get("composeProject") == "compose" and row.get("composeService") in expected:
            service = row["composeService"]
            if service in legacy or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("id", ""))):
                raise RecoveryBlocked("baseline legacy service identity is ambiguous")
            legacy[service] = row
    if set(legacy) != expected:
        raise RecoveryBlocked("baseline does not contain every legacy GPoint service")
    if any(row.get("running") is not True for row in legacy.values()):
        raise RecoveryBlocked("baseline legacy GPoint services were not all running")
    gsales_rows = [row for row in rows if row.get("composeProject") == "compose" and row.get("composeService") in stopped_gsales]
    if ({row["composeService"] for row in gsales_rows} != stopped_gsales
            or any(row.get("running") is not False for row in gsales_rows)
            or any(not re.fullmatch(r"[0-9a-f]{64}", str(row.get("id", ""))) for row in gsales_rows)):
        raise RecoveryBlocked("baseline stopped GSales inventory is incomplete")
    legacy.update({row["composeService"]: row for row in gsales_rows})
    return values, legacy


def inspect_json(identifier: str) -> dict:
    raw = command(["docker", "inspect", identifier])
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecoveryBlocked("Docker inspection result is invalid") from exc
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise RecoveryBlocked("Docker inspection identity is ambiguous")
    return rows[0]


def wait_container_ready(identifier: str, *, timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = inspect_json(identifier).get("State") or {}
        if not state.get("Running"):
            time.sleep(2)
            continue
        health = state.get("Health")
        if health is None or health.get("Status") == "healthy":
            return
        if health.get("Status") == "unhealthy":
            time.sleep(2)
            continue
        time.sleep(2)
    raise RecoveryBlocked("restored GPoint container did not become ready")


def recover(args: argparse.Namespace) -> None:
    sha = args.candidate_sha
    app_sha = args.app_source_sha
    version = args.version
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not re.fullmatch(r"[0-9a-f]{40}", app_sha):
        raise RecoveryBlocked("candidate or app source identity is invalid")
    if version != f"{sha}-{args.run_id}-{args.attempt}" or not re.fullmatch(r"[0-9]+", args.run_id) or not re.fullmatch(r"[0-9]+", args.attempt):
        raise RecoveryBlocked("Kamal version does not match this workflow attempt")
    digests = {"gpoint-core": args.app_digest, "gpoint-agata": args.agata_digest,
               "gpoint-miniapp": args.miniapp_digest}
    for digest in digests.values():
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise RecoveryBlocked("candidate image digest is invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.proxy_digest):
        raise RecoveryBlocked("Kamal Proxy digest is invalid")

    expected_baseline_path = str(RELEASE_ROOT / sha / f"kamal-cutover-baseline-{args.run_id}-{args.attempt}.txt")
    if args.baseline != expected_baseline_path:
        raise RecoveryBlocked("baseline path is not bound to this workflow attempt")
    baseline, legacy = read_baseline(Path(args.baseline), args.baseline_sha, sha, args.run_id,
                                     args.attempt, RELEASE_ROOT, os.getuid())
    if baseline.get("CURRENT_SHA") != app_sha:
        raise RecoveryBlocked("production app source differs from the recovery baseline")
    caddy_sha = baseline.get("CADDYFILE_SHA256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", caddy_sha):
        raise RecoveryBlocked("Caddy baseline checksum is invalid")
    if command(["sha256sum", str(CADDYFILE)]).split()[0] != caddy_sha:
        raise RecoveryBlocked("Caddy configuration changed after baseline")
    caddy_backup = BACKUP_ROOT / f"caddy-before-kamal-{sha}-{args.run_id}-{args.attempt}"
    checksum_file = Path(str(caddy_backup) + ".sha256")
    if (caddy_backup.is_symlink() or checksum_file.is_symlink()
            or not caddy_backup.is_file() or not checksum_file.is_file()
            or caddy_backup.stat().st_uid != os.getuid() or checksum_file.stat().st_uid != os.getuid()
            or caddy_backup.stat().st_mode & 0o077 or checksum_file.stat().st_mode & 0o077):
        raise RecoveryBlocked("Caddy recovery copy is missing")
    try:
        checksum_line = checksum_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise RecoveryBlocked("Caddy recovery checksum is unavailable") from exc
    if checksum_line != f"{caddy_sha}  {caddy_backup}\n" or hashlib.sha256(caddy_backup.read_bytes()).hexdigest() != caddy_sha:
        raise RecoveryBlocked("Caddy recovery copy does not match baseline")

    proxy_ids = [line for line in command(["docker", "ps", "-aq", "--no-trunc", "--filter", "name=^/kamal-proxy$"]).splitlines() if line]
    if len(proxy_ids) > 1:
        raise RecoveryBlocked("multiple Kamal Proxy containers are present")
    candidate_targets: dict[str, tuple[str, int]] = {}
    for service, role, port, repo_key in (
        ("gpoint-core", "api", 3000, "gpoint-core"),
        ("gpoint-miniapp", "web", 80, "gpoint-miniapp"),
    ):
        name = f"{service}-{role}-{version}"
        ids = [line for line in command(["docker", "ps", "-aq", "--no-trunc", "--filter", f"name=^/{name}$"]).splitlines() if line]
        if len(ids) > 1:
            raise RecoveryBlocked("candidate request container identity is ambiguous")
        if ids:
            row = inspect_json(ids[0])
            if row.get("Id") != ids[0] or row.get("Name", "").lstrip("/") != name:
                raise RecoveryBlocked("candidate request container name differs")
            config = row.get("Config") or {}
            labels = config.get("Labels") or {}
            if labels.get("org.opencontainers.image.revision") != app_sha:
                raise RecoveryBlocked("candidate request container source SHA differs")
            image_id = row.get("Image")
            image = inspect_json(image_id)
            expected_repo = {"gpoint-core": "ghcr.io/akhripunov/gpoint-runtime",
                             "gpoint-miniapp": "ghcr.io/akhripunov/gpoint-miniapp"}[repo_key]
            if (image.get("Id") != image_id or image.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision") != app_sha
                    or f"{expected_repo}@{digests[repo_key]}" not in (image.get("RepoDigests") or [])):
                raise RecoveryBlocked("candidate request image digest differs")
            candidate_targets[service] = (ids[0], port)

    # Validate the complete runtime inventory before any mutation. A late
    # identity mismatch must never strand ingress after the proxy was stopped.
    candidate_containers: list[str] = []
    for service, roles in (("gpoint-core", ("worker", "bot", "api")),
                           ("gpoint-agata", ("memory-worker", "api")),
                           ("gpoint-miniapp", ("web",))):
        repo = {"gpoint-core": "ghcr.io/akhripunov/gpoint-runtime",
                "gpoint-agata": "ghcr.io/akhripunov/gpoint-agata-runtime",
                "gpoint-miniapp": "ghcr.io/akhripunov/gpoint-miniapp"}[service]
        for role in roles:
            name = f"{service}-{role}-{version}"
            ids = [line for line in command(["docker", "ps", "-aq", "--no-trunc", "--filter", f"name=^/{name}$"]).splitlines() if line]
            if len(ids) > 1:
                raise RecoveryBlocked("candidate container identity is ambiguous")
            if not ids:
                continue
            row = inspect_json(ids[0])
            if row.get("Id") != ids[0] or row.get("Name", "").lstrip("/") != name:
                raise RecoveryBlocked("candidate container name differs")
            labels = (row.get("Config") or {}).get("Labels") or {}
            if labels.get("org.opencontainers.image.revision") != app_sha:
                raise RecoveryBlocked("candidate container source SHA differs")
            image_id = row.get("Image")
            image = inspect_json(image_id)
            if (image.get("Id") != image_id or image.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision") != app_sha
                    or f"{repo}@{digests[service]}" not in (image.get("RepoDigests") or [])):
                raise RecoveryBlocked("candidate container digest differs")
            candidate_containers.append(ids[0])

    for service in ("api", "worker", "bot", "agata-api", "agata-memory-worker"):
        baseline_row = legacy[service]
        cid = baseline_row["id"]
        current = inspect_json(cid)
        labels = (current.get("Config") or {}).get("Labels") or {}
        if (current.get("Id") != cid or current.get("Image") != baseline_row.get("imageId")
                or labels.get("com.docker.compose.project") != "compose"
                or labels.get("com.docker.compose.service") != service):
            raise RecoveryBlocked("legacy GPoint container no longer matches baseline")
    for service in ("gsales-api", "gsales-agata-api", "gsales-agata-memory-worker"):
        row = legacy[service]
        current = inspect_json(row["id"])
        if row.get("running") is not False:
            raise RecoveryBlocked("baseline GSales service was not stopped")
        else:
            labels = (current.get("Config") or {}).get("Labels") or {}
            if (current.get("Id") != row["id"] or labels.get("com.docker.compose.project") != "compose"
                    or labels.get("com.docker.compose.service") != service
                    or (current.get("State") or {}).get("Running")):
                raise RecoveryBlocked("stopped GSales service changed during recovery")

    recovery_errors: list[str] = []
    route_removal_failed = False
    proxy_running = False
    proxy_safely_stopped = not proxy_ids
    if proxy_ids:
        proxy_id = proxy_ids[0]
        try:
            proxy = inspect_json(proxy_id)
            proxy_running = bool((proxy.get("State") or {}).get("Running"))
            if proxy.get("Id") != proxy_id or proxy.get("Name", "").lstrip("/") != "kamal-proxy":
                raise RecoveryBlocked("Kamal Proxy identity differs")
            proxy_config = proxy.get("Config") or {}
            if proxy_config.get("Image") != "basecamp/kamal-proxy:v0.9.2":
                raise RecoveryBlocked("Kamal Proxy image reference is unexpected")
            proxy_image_id = proxy.get("Image")
            proxy_image = inspect_json(proxy_image_id)
            if (proxy_image.get("Id") != proxy_image_id
                    or not any(item.endswith("@" + args.proxy_digest) for item in (proxy_image.get("RepoDigests") or []))):
                raise RecoveryBlocked("Kamal Proxy immutable image digest differs from hosted build")
            created = datetime.fromisoformat(proxy["Created"].replace("Z", "+00:00"))
            if created.tzinfo is None or int(created.timestamp()) < int(baseline["BASELINE_CAPTURED_AT_EPOCH"]):
                raise RecoveryBlocked("Kamal Proxy predates this cutover baseline")
        except (KeyError, ValueError, TypeError) as exc:
            recovery_errors.append("Kamal Proxy creation time or identity is invalid")
        except RecoveryBlocked as exc:
            recovery_errors.append(str(exc))
        else:
            proxy_safely_stopped = not proxy_running
        if proxy_running:
            try:
                network_rows = json.loads(command(["docker", "network", "inspect", "kamal"]))
                if len(network_rows) != 1 or network_rows[0].get("Name") != "kamal":
                    raise RecoveryBlocked("Kamal network identity is unexpected")
                network_members = network_rows[0].get("Containers") or {}
                candidate_names = {
                    f"gpoint-core-{role}-{version}" for role in ("api", "worker", "bot")
                } | {
                    f"gpoint-agata-{role}-{version}" for role in ("api", "memory-worker")
                } | {f"gpoint-miniapp-web-{version}", "kamal-proxy"}
                for network_id, member in network_members.items():
                    if (not re.fullmatch(r"[0-9a-f]{64}", network_id)
                            or member.get("Name") not in candidate_names):
                        raise RecoveryBlocked("Kamal network contains an unrelated container")
                if proxy_id not in network_members:
                    raise RecoveryBlocked("Kamal Proxy is absent from its expected network")
                routes = parse_routes(command(["docker", "exec", proxy_id, "kamal-proxy", "list"]))
                services = validate_gpoint_routes(routes, candidate_targets)
                # We now have proof of exact proxy image, attempt ownership,
                # candidate-only network and validated routes. Only from here
                # may the proxy be retired, including route-removal failures.
                route_removal_failed = False
                for service in services:
                    try:
                        result = subprocess.run(["docker", "exec", proxy_id, "kamal-proxy", "remove", service], text=True, capture_output=True, check=False, timeout=60)
                    except (OSError, subprocess.SubprocessError):
                        route_removal_failed = True
                        break
                    if result.returncode:
                        route_removal_failed = True
                        break
                if not route_removal_failed:
                    try:
                        if parse_routes(command(["docker", "exec", proxy_id, "kamal-proxy", "list"])):
                            route_removal_failed = True
                    except (RecoveryBlocked, OSError, subprocess.SubprocessError):
                        # The initial route table was validated and contained
                        # candidate routes only, so stopping this exact proxy
                        # remains safe even if the post-remove readback fails.
                        route_removal_failed = True
                if route_removal_failed:
                    recovery_errors.append("Kamal Proxy route removal failed")
                try:
                    stop = subprocess.run(["docker", "stop", "--time", "30", proxy_id], text=True, capture_output=True, check=False, timeout=60)
                except subprocess.SubprocessError:
                    stop = subprocess.CompletedProcess(["docker", "stop", proxy_id], 1)
                if stop.returncode:
                    recovery_errors.append("Kamal Proxy graceful stop failed; attempting exact-ID kill")
                    try:
                        subprocess.run(["docker", "kill", proxy_id], text=True, capture_output=True, check=False, timeout=60)
                    except subprocess.SubprocessError:
                        recovery_errors.append("Kamal Proxy exact-ID kill command failed")
                proxy_after = inspect_json(proxy_id)
                if (proxy_after.get("Id") != proxy_id
                        or (proxy_after.get("State") or {}).get("Running")
                        or (proxy_after.get("Config") or {}).get("Image") != "basecamp/kamal-proxy:v0.9.2"):
                    recovery_errors.append("verified Kamal Proxy remains active")
                else:
                    proxy_safely_stopped = True
            except (RecoveryBlocked, json.JSONDecodeError, OSError, subprocess.SubprocessError) as exc:
                recovery_errors.append(str(exc) if isinstance(exc, RecoveryBlocked) else "Kamal Proxy route or ownership proof failed")

    # Stop only candidate containers whose exact versioned name, source SHA,
    # and immutable GHCR digest match this attempt. Retain them for diagnosis.
    if proxy_safely_stopped:
        for cid in candidate_containers:
            try:
                if (inspect_json(cid).get("State") or {}).get("Running"):
                    command(["docker", "stop", "--time", "30", cid])
                    if (inspect_json(cid).get("State") or {}).get("Running"):
                        recovery_errors.append("candidate container did not stop")
            except (RecoveryBlocked, OSError, subprocess.SubprocessError):
                recovery_errors.append("candidate container stop failed")
    elif proxy_running:
        recovery_errors.append("candidate containers retained because Proxy ownership/routes were not proven safe")

    for service in ("agata-api", "api", "agata-memory-worker", "bot", "worker"):
        baseline_row = legacy[service]
        cid = baseline_row["id"]
        try:
            current = inspect_json(cid)
            labels = (current.get("Config") or {}).get("Labels") or {}
            if (current.get("Id") != cid or current.get("Image") != baseline_row.get("imageId")
                    or labels.get("com.docker.compose.project") != "compose"
                    or labels.get("com.docker.compose.service") != service):
                recovery_errors.append(f"baseline {service} identity changed; start skipped")
                continue
            if not (current.get("State") or {}).get("Running"):
                command(["docker", "start", cid])
            wait_container_ready(cid)
        except (RecoveryBlocked, OSError, subprocess.SubprocessError):
            recovery_errors.append(f"baseline {service} did not become ready")

    caddy_ready = False
    try:
        caddy_active = subprocess.run(["systemctl", "is-active", "--quiet", "caddy"], check=False).returncode == 0
        if not caddy_active:
            command(["caddy", "validate", "--config", str(CADDYFILE), "--adapter", "caddyfile"])
            command(["systemctl", "start", "caddy"])
        caddy_ready = subprocess.run(["systemctl", "is-active", "--quiet", "caddy"], check=False).returncode == 0
        if not caddy_ready:
            recovery_errors.append("Caddy did not become active")
        if command(["sha256sum", str(CADDYFILE)]).split()[0] != caddy_sha:
            recovery_errors.append("Caddy configuration changed during recovery")
    except (RecoveryBlocked, OSError, subprocess.SubprocessError):
        recovery_errors.append("Caddy validation or start failed")
    ingress_ready = caddy_ready
    if caddy_ready:
        for path in ("https://gpoint.amiora.one/api/health/live", "https://gpoint.amiora.one/"):
            try:
                command(["curl", "--fail", "--silent", "--show-error", "--connect-timeout", "5", "--resolve",
                         "gpoint.amiora.one:443:127.0.0.1", path])
            except (RecoveryBlocked, OSError, subprocess.SubprocessError):
                ingress_ready = False
                recovery_errors.append("restored Caddy ingress probe failed")
    if recovery_errors:
        restored = proxy_safely_stopped and ingress_ready and not any("did not become ready" in e for e in recovery_errors)
        print("GPOINT_CUTOVER_RECOVERY=RESTORED_WITH_ERRORS" if restored else "GPOINT_CUTOVER_RECOVERY=PARTIAL")
        raise RecoveryBlocked("; ".join(dict.fromkeys(recovery_errors)))
    print("GPOINT_CUTOVER_RECOVERY=PASS")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--app-source-sha", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--baseline-sha", required=True)
    parser.add_argument("--app-digest", required=True)
    parser.add_argument("--agata-digest", required=True)
    parser.add_argument("--miniapp-digest", required=True)
    parser.add_argument("--proxy-digest", required=True)
    args = parser.parse_args()
    try:
        recover(args)
    except RecoveryBlocked as exc:
        print("GPOINT_CUTOVER_RECOVERY=BLOCKED")
        print(f"RECOVERY_REASON={exc}")
        return 1
    except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError):
        print("GPOINT_CUTOVER_RECOVERY=BLOCKED")
        print("RECOVERY_REASON=unexpected-safe-check-failure")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
