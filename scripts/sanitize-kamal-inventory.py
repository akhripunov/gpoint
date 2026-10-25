#!/usr/bin/env python3
"""Emit the production container facts needed for a Kamal cutover audit.

Docker inspect contains command arguments and environment variables. This
projection deliberately keeps only operational identity, state, ports,
networks, mounts, restart policy, and Compose ownership labels.
"""
import json
import sys


def sanitize(container):
    config = container.get("Config") or {}
    labels = config.get("Labels") or {}
    state = container.get("State") or {}
    health = state.get("Health") or {}
    restart = (container.get("HostConfig") or {}).get("RestartPolicy") or {}
    network_settings = container.get("NetworkSettings") or {}
    networks = {}
    for name, value in sorted((network_settings.get("Networks") or {}).items()):
        networks[name] = {
            "networkId": value.get("NetworkID"),
            "aliases": sorted(value.get("Aliases") or []),
            "ipAddress": value.get("IPAddress"),
            "globalIPv6Address": value.get("GlobalIPv6Address"),
        }
    ports = {}
    for port, bindings in sorted((network_settings.get("Ports") or {}).items()):
        ports[port] = sorted(
            ({"hostIp": item.get("HostIp"), "hostPort": item.get("HostPort")} for item in (bindings or [])),
            key=lambda item: (item["hostIp"] or "", item["hostPort"] or ""),
        )
    mounts = []
    for mount in container.get("Mounts") or []:
        mounts.append({
            "type": mount.get("Type"),
            "name": mount.get("Name"),
            "source": mount.get("Source"),
            "destination": mount.get("Destination"),
            "readWrite": mount.get("RW"),
        })
    mounts.sort(key=lambda item: (item["destination"] or "", item["source"] or ""))
    project = labels.get("com.docker.compose.project")
    service = labels.get("com.docker.compose.service")
    return {
        "id": container.get("Id"),
        "name": (container.get("Name") or "").lstrip("/"),
        "created": container.get("Created"),
        "image": config.get("Image"),
        "imageId": container.get("Image"),
        "state": state.get("Status"),
        "running": bool(state.get("Running")),
        "health": health.get("Status"),
        "restartPolicy": restart.get("Name"),
        "composeProject": project,
        "composeService": service,
        "owner": f"compose:{project}/{service}" if project and service else "unattributed",
        "ports": ports,
        "networks": networks,
        "mounts": mounts,
    }


def main():
    try:
        raw = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise SystemExit("Docker inspection JSON is invalid") from None
    if not isinstance(raw, list) or not raw:
        raise SystemExit("Docker inspection must contain at least one container")
    if any(not isinstance(item, dict) or not item.get("Id") or not item.get("Config") for item in raw):
        raise SystemExit("Docker inspection container identity is incomplete")
    print(json.dumps({
        "schema": "gpoint-vps-inventory/v1",
        "containers": sorted((sanitize(item) for item in raw), key=lambda item: item["name"]),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
