#!/usr/bin/env python3
"""Build and compare a secret-free proof that Eros is absent before cutover.

This helper only reads sanitized JSON facts captured by a caller. It never
contacts Docker/PostgreSQL and never starts, stops, or changes production state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


SNAPSHOT_SCHEMA = "gpoint-kamal-eros-state/v1"
DATABASE_SCHEMA = "gpoint-kamal-eros-database-facts/v1"
NETWORK_SCHEMA = "gpoint-kamal-eros-networks/v1"
INVENTORY_SCHEMA = "gpoint-vps-inventory/v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
BOOL_ROLE_FIELDS = ("login", "superuser", "createDatabase", "createRole", "bypassRls")


class EvidenceError(ValueError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise EvidenceError("evidence JSON is unreadable") from None
    if not isinstance(value, dict):
        raise EvidenceError("evidence JSON root must be an object")
    return value


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def _contains_eros(*values: Any) -> bool:
    return any(isinstance(value, str) and "eros" in value.casefold() for value in values)


def canonical_snapshot(containers: dict[str, Any], databases: dict[str, Any],
                       networks: dict[str, Any]) -> dict[str, Any]:
    require(containers.get("schema") == INVENTORY_SCHEMA, "container inventory schema mismatch")
    require(databases.get("schema") == DATABASE_SCHEMA, "database facts schema mismatch")
    require(networks.get("schema") == NETWORK_SCHEMA, "network facts schema mismatch")

    rows = containers.get("containers")
    resources = containers.get("resourceUsage")
    require(isinstance(rows, list) and rows, "complete container inventory is required")
    require(isinstance(resources, list) and resources, "complete running-container resource sample is required")
    names: dict[str, dict[str, Any]] = {}
    ids: set[str] = set()
    for row in rows:
        require(isinstance(row, dict), "container inventory row is invalid")
        name, identity, image_id = row.get("name"), row.get("id"), row.get("imageId")
        require(isinstance(name, str) and name and HEX64.fullmatch(str(identity or "")),
                "container identity is incomplete")
        require(name not in names and identity not in ids, "duplicate container identity")
        require(isinstance(image_id, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", image_id),
                "container image identity is incomplete")
        require(isinstance(row.get("state"), str) and isinstance(row.get("running"), bool),
                "container runtime state is incomplete")
        require(row.get("health") is None or isinstance(row.get("health"), str),
                "container health facts are invalid")
        require(row.get("restartPolicy") is None or isinstance(row.get("restartPolicy"), str),
                "container restart policy is invalid")
        require(row.get("image") is None or isinstance(row.get("image"), str),
                "container image reference is invalid")
        for field in ("composeProject", "composeService", "owner"):
            require(row.get(field) is None or isinstance(row.get(field), str),
                    "container ownership facts are invalid")
        raw_networks = row.get("networks")
        require(isinstance(raw_networks, dict), "container network inventory is incomplete")
        for network_name, network in raw_networks.items():
            require(isinstance(network_name, str) and network_name and isinstance(network, dict),
                    "container network row is invalid")
            network_id = network.get("networkId")
            require(network_id is None or HEX64.fullmatch(str(network_id)),
                    "container network identity is invalid")
        names[name] = row
        ids.add(identity)

    resource_names: set[str] = set()
    running_names = {name for name, row in names.items() if row["running"]}
    for sample in resources:
        require(isinstance(sample, dict), "running-container resource row is invalid")
        name, short_id = sample.get("containerName"), sample.get("containerId")
        require(isinstance(name, str) and name in names and
                isinstance(short_id, str) and re.fullmatch(r"[0-9a-f]{12,64}", short_id),
                "running-container resource identity is incomplete")
        require(names[name]["id"].startswith(short_id), "resource sample does not match container ID")
        require(name not in resource_names, "duplicate running-container resource row")
        for field in ("cpuPercent", "memoryUsedBytes", "memoryLimitBytes", "memoryPercent", "pids"):
            value = sample.get(field)
            require(isinstance(value, (int, float)) and value >= 0,
                    "running-container resource facts are incomplete")
        resource_names.add(name)
    require(resource_names == running_names, "resource sample does not cover complete running inventory")

    postgres = names.get("shared_postgres_1")
    require(postgres is not None and postgres["running"], "expected running PostgreSQL container is missing")
    gpoint_apps = [row for row in rows if
                   (row.get("composeProject") == "compose" and
                    row.get("composeService") in {"api", "agata-api"}) or
                   str(row.get("name", "")).startswith(("gpoint-core-api-", "gpoint-agata-api-"))]
    require(bool(gpoint_apps) and any(row.get("running") for row in gpoint_apps),
            "expected running GPoint application container is missing")

    eros_containers = []
    for row in rows:
        if _contains_eros(row.get("name"), row.get("composeProject"),
                          row.get("composeService"), row.get("owner"), row.get("image")):
            eros_containers.append(row.get("name"))
    require(not eros_containers, "Eros containers must be absent")

    db_system_id = databases.get("systemIdentifier")
    server_version = databases.get("serverVersionNum")
    require(databases.get("clusterCatalogScanComplete") is True,
            "complete PostgreSQL catalog scan is required")
    require(isinstance(db_system_id, str) and db_system_id.isdigit(),
            "PostgreSQL system identifier is incomplete")
    require(isinstance(server_version, int) and server_version > 0,
            "PostgreSQL server version is incomplete")
    db_rows = databases.get("databases")
    require(isinstance(db_rows, list) and db_rows, "complete database inventory is required")
    db_names: set[str] = set()
    for row in db_rows:
        require(isinstance(row, dict), "database inventory row is invalid")
        name, owner, acl = row.get("name"), row.get("owner"), row.get("acl")
        require(isinstance(name, str) and name and isinstance(owner, str) and owner,
                "database identity is incomplete")
        require(isinstance(row.get("allowConnections"), bool), "database connection policy is incomplete")
        require(acl is None or isinstance(acl, str), "database ACL facts are invalid")
        require(name not in db_names, "duplicate database inventory row")
        db_names.add(name)
    require({"gpoint", "postgres"}.issubset(db_names), "expected GPoint/PostgreSQL databases are missing")
    require(not any(_contains_eros(name) for name in db_names), "Eros database must be absent")
    require(databases.get("erosCatalog") is None, "Eros database catalog facts exist but the database is absent")

    role_rows = databases.get("roles")
    require(isinstance(role_rows, list) and role_rows, "complete PostgreSQL role inventory is required")
    roles_by_name: dict[str, dict[str, Any]] = {}
    for row in role_rows:
        require(isinstance(row, dict), "PostgreSQL role inventory row is invalid")
        name = row.get("name")
        require(isinstance(name, str) and name and name not in roles_by_name,
                "PostgreSQL role identity is invalid")
        require(all(isinstance(row.get(field), bool) for field in BOOL_ROLE_FIELDS),
                "PostgreSQL role security facts are incomplete")
        roles_by_name[name] = {"name": name, **{field: row[field] for field in BOOL_ROLE_FIELDS}}
    require(not any(_contains_eros(name) for name in roles_by_name), "Eros database roles must be absent")
    require(bool(roles_by_name), "PostgreSQL role inventory is empty")

    membership_rows = databases.get("memberships")
    require(isinstance(membership_rows, list), "complete PostgreSQL role membership inventory is required")
    seen_edges: set[tuple[str, str]] = set()
    for row in membership_rows:
        require(isinstance(row, dict), "PostgreSQL membership row is invalid")
        role, member, admin = row.get("role"), row.get("member"), row.get("adminOption")
        require(isinstance(role, str) and role in roles_by_name and
                isinstance(member, str) and member in roles_by_name and isinstance(admin, bool),
                "PostgreSQL role membership facts are incomplete")
        edge = (role, member)
        require(edge not in seen_edges, "duplicate role membership row")
        seen_edges.add(edge)
        require(not _contains_eros(role, member), "Eros role memberships must be absent")

    require(databases.get("gpointErosSchemaScanComplete") is True,
            "complete GPoint Eros schema/ACL scan is required")
    gpoint_eros_schemas = databases.get("gpointErosSchemas")
    require(isinstance(gpoint_eros_schemas, list), "GPoint Eros schema inventory is incomplete")
    for row in gpoint_eros_schemas:
        require(isinstance(row, dict), "GPoint Eros schema row is invalid")
        name, owner, acl = row.get("name"), row.get("owner"), row.get("acl")
        require(isinstance(name, str) and _contains_eros(name) and
                isinstance(owner, str) and owner and "acl" in row and (acl is None or isinstance(acl, str)),
                "GPoint Eros schema ACL evidence is incomplete")
    require(not gpoint_eros_schemas, "Eros schemas in the GPoint database must be absent")

    catalog_identity = {
        "databases": db_rows,
        "roles": role_rows,
        "memberships": membership_rows,
        "gpointErosSchemas": gpoint_eros_schemas,
    }
    catalog_acl_sha = hashlib.sha256(
        json.dumps(catalog_identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    network_rows = networks.get("networks")
    require(networks.get("complete") is True and isinstance(network_rows, list),
            "complete Docker network inventory is required")
    eros_networks = []
    seen_network_names: set[str] = set()
    seen_network_ids: set[str] = set()
    for row in network_rows:
        require(isinstance(row, dict), "network inventory row is invalid")
        name, network_id = row.get("name"), row.get("id")
        require(isinstance(name, str) and _contains_eros(name),
                "network inventory contains a non-Eros network")
        require(HEX64.fullmatch(str(network_id or "")), "Eros network identity is incomplete")
        require(name not in seen_network_names and network_id not in seen_network_ids,
                "duplicate Eros network identity")
        require(isinstance(row.get("internal"), bool) and isinstance(row.get("driver"), str),
                "Eros network configuration is incomplete")
        require(isinstance(row.get("ipam"), list), "Eros network IPAM facts are incomplete")
        seen_network_names.add(name)
        seen_network_ids.add(network_id)
        eros_networks.append(name)
    require(not eros_networks, "Eros Docker networks must be absent")

    state = {
        "schema": SNAPSHOT_SCHEMA,
        "erosAbsent": True,
        "postgres": {"systemIdentifier": db_system_id, "serverVersionNum": server_version,
                     "catalogAclSha256": catalog_acl_sha},
        "database": {"eros": None, "roles": [], "memberships": [], "gpointErosSchemas": []},
        "containers": [],
        "networks": [],
    }
    state_bytes = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    state["stateSha256"] = hashlib.sha256(state_bytes).hexdigest()
    return state


def validate_snapshot(value: dict[str, Any]) -> None:
    require(value.get("schema") == SNAPSHOT_SCHEMA, "snapshot schema mismatch")
    claimed = value.get("stateSha256")
    require(isinstance(claimed, str) and HEX64.fullmatch(claimed), "snapshot hash is invalid")
    body = {key: item for key, item in value.items() if key != "stateSha256"}
    actual = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    require(actual == claimed, "snapshot hash mismatch")


def write_snapshot(args: argparse.Namespace) -> int:
    state = canonical_snapshot(read_json(args.containers), read_json(args.databases), read_json(args.networks))
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as target:
        target.write(encoded)
    args.output.chmod(0o600)
    print(f"EROS_STATE_SNAPSHOT=PASS\nEROS_STATE_SHA256={state['stateSha256']}")
    return 0


def compare_snapshots(args: argparse.Namespace) -> int:
    before, after = read_json(args.before), read_json(args.after)
    validate_snapshot(before)
    validate_snapshot(after)
    if before != after:
        print("EROS_STATE_MATCH=FAIL", file=sys.stderr)
        return 1
    print(f"EROS_STATE_MATCH=PASS\nEROS_STATE_SHA256={before['stateSha256']}")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--containers", required=True, type=Path,
                         help="sanitized gpoint-vps-inventory/v1 JSON")
    capture.add_argument("--databases", required=True, type=Path,
                         help="read-only gpoint-kamal-eros-database-facts/v1 JSON")
    capture.add_argument("--networks", required=True, type=Path,
                         help="read-only gpoint-kamal-eros-networks/v1 JSON; empty list is valid")
    capture.add_argument("--output", required=True, type=Path)
    compare = commands.add_parser("compare")
    compare.add_argument("--before", required=True, type=Path)
    compare.add_argument("--after", required=True, type=Path)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return write_snapshot(args) if args.command == "capture" else compare_snapshots(args)
    except EvidenceError as error:
        print(f"EROS_STATE_SNAPSHOT=BLOCKED:{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
