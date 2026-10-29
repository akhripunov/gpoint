#!/usr/bin/env python3
"""Collect complete read-only Docker/PostgreSQL evidence for the no-Eros gate.

Raw Docker inspection (which includes container environments) stays in memory
and is projected through the existing sanitizer before any snapshot is written.
PostgreSQL is queried in an enforced READ ONLY transaction. No production
service, role, database, network, or filesystem state is changed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import kamal_eros_state
def _load_sanitizer(name: str, filename: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("sanitizer module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sanitize_kamal_inventory = _load_sanitizer("_gpoint_sanitize_inventory", "sanitize-kamal-inventory.py")
sanitize_kamal_resources = _load_sanitizer("_gpoint_sanitize_resources", "sanitize-kamal-resources.py")


ID64 = re.compile(r"^[0-9a-f]{64}$")
DATABASE_QUERY = r"""
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '15000';
SELECT json_build_object(
  'schema','gpoint-kamal-eros-database-facts/v1',
  'clusterCatalogScanComplete',true,
  'systemIdentifier',(SELECT system_identifier::text FROM pg_control_system()),
  'serverVersionNum',current_setting('server_version_num')::integer,
  'databases',COALESCE((SELECT json_agg(json_build_object(
    'name',datname,'owner',pg_get_userbyid(datdba),'allowConnections',datallowconn,'acl',datacl::text
  ) ORDER BY datname) FROM pg_database),'[]'::json),
  'roles',COALESCE((SELECT json_agg(json_build_object(
    'name',rolname,'login',rolcanlogin,'superuser',rolsuper,'createDatabase',rolcreatedb,
    'createRole',rolcreaterole,'bypassRls',rolbypassrls
  ) ORDER BY rolname) FROM pg_roles),'[]'::json),
  'memberships',COALESCE((SELECT json_agg(json_build_object(
    'role',gr.rolname,'member',m.rolname,'adminOption',am.admin_option
  ) ORDER BY gr.rolname,m.rolname)
  FROM pg_auth_members am JOIN pg_roles gr ON gr.oid=am.roleid
  JOIN pg_roles m ON m.oid=am.member),'[]'::json),
  'gpointErosSchemaScanComplete',true,
  'gpointErosSchemas',COALESCE((SELECT json_agg(json_build_object(
    'name',nspname,'owner',pg_get_userbyid(nspowner),'acl',nspacl::text
  ) ORDER BY nspname) FROM pg_namespace WHERE lower(nspname) LIKE '%eros%'),'[]'::json),
  'erosCatalog',NULL
)::text;
COMMIT;
"""


class CollectionError(RuntimeError):
    pass


def run_readonly(command: list[str], input_text: str | None = None, *, stage: str) -> str:
    try:
        result = subprocess.run(command, input=input_text, capture_output=True, text=True, check=False)
    except OSError:
        raise CollectionError(f"{stage} command unavailable") from None
    if result.returncode != 0:
        # Docker stderr can contain untrusted/private details; never forward it.
        raise CollectionError(f"{stage} command failed")
    return result.stdout


def _json_object(text: str, message: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, UnicodeError):
        raise CollectionError(message) from None
    if not isinstance(value, dict):
        raise CollectionError(message)
    return value


def _json_array(text: str, message: str) -> list[Any]:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, UnicodeError):
        raise CollectionError(message) from None
    if not isinstance(value, list):
        raise CollectionError(message)
    return value


def _dump_ids(stdout: str) -> list[str]:
    ids = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not ids or any(not ID64.fullmatch(item) for item in ids) or len(ids) != len(set(ids)):
        raise CollectionError("complete Docker container ID inventory is invalid")
    return ids


def _network_rows(runner: Callable[..., str]) -> dict[str, Any]:
    listing = runner(["docker", "network", "ls", "--no-trunc", "--format", "{{json .}}"],
                     stage="Docker network inventory")
    names: set[str] = set()
    eros_ids: list[str] = []
    for line in listing.splitlines():
        if not line.strip():
            continue
        item = _json_object(line, "Docker network inventory JSON is invalid")
        name, network_id = item.get("Name"), item.get("ID")
        if not isinstance(name, str) or not name or not isinstance(network_id, str) or not ID64.fullmatch(network_id):
            raise CollectionError("Docker network inventory identity is incomplete")
        if name in names:
            raise CollectionError("Docker network inventory has duplicate names")
        names.add(name)
        if "eros" in name.casefold():
            eros_ids.append(network_id)

    networks = []
    for network_id in eros_ids:
        raw = _json_array(runner(["docker", "network", "inspect", network_id],
                                 stage="Docker Eros network inspection"),
                          "Docker Eros network inspection JSON is invalid")
        if len(raw) != 1 or not isinstance(raw[0], dict):
            raise CollectionError("Docker Eros network inspection identity is invalid")
        row = raw[0]
        ipam_config = ((row.get("IPAM") or {}).get("Config") or [])
        if (row.get("Id") != network_id or not isinstance(row.get("Name"), str)
                or not isinstance(row.get("Internal"), bool) or not isinstance(row.get("Driver"), str)
                or not isinstance(ipam_config, list)):
            raise CollectionError("Docker Eros network configuration is incomplete")
        ipam_hash = hashlib.sha256(json.dumps(ipam_config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        networks.append({"name": row["Name"], "id": row["Id"], "internal": row["Internal"],
                         "driver": row["Driver"], "ipam": [{"sha256": ipam_hash}]})
    return {"schema": "gpoint-kamal-eros-networks/v1", "complete": True, "networks": networks}


def collect_facts(runner: Callable[..., str] = run_readonly) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    ids = _dump_ids(runner(["docker", "ps", "-aq", "--no-trunc"], stage="Docker container inventory"))
    inspect_text = runner(["docker", "inspect", *ids], stage="Docker container inspection")
    raw_containers = _json_array(inspect_text, "Docker container inspection JSON is invalid")
    if len(raw_containers) != len(ids) or any(not isinstance(row, dict) for row in raw_containers):
        raise CollectionError("Docker container inspection is incomplete")
    inspected_ids = [row.get("Id") for row in raw_containers]
    if set(inspected_ids) != set(ids) or len(inspected_ids) != len(ids):
        raise CollectionError("Docker container inspection does not match complete ID inventory")

    stats_text = runner(["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                        stage="Docker resource inventory")
    try:
        raw_stats = [json.loads(line) for line in stats_text.splitlines() if line.strip()]
        resources = sanitize_kamal_resources.project(raw_stats)
        containers = [sanitize_kamal_inventory.sanitize(row) for row in raw_containers]
    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError, KeyError):
        raise CollectionError("sanitized Docker inventory is incomplete") from None
    inventory = {"schema": "gpoint-vps-inventory/v1",
                 "containers": sorted(containers, key=lambda row: row["name"]),
                 "resourceUsage": resources}

    database_text = runner(
        ["docker", "exec", "-i", "shared_postgres_1", "sh", "-ec",
         'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d gpoint'],
        input_text=DATABASE_QUERY,
        stage="read-only PostgreSQL catalog scan",
    )
    lines = [line.strip() for line in database_text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise CollectionError("read-only PostgreSQL catalog result is incomplete")
    database_facts = _json_object(lines[0], "read-only PostgreSQL catalog JSON is invalid")

    network_facts = _network_rows(runner)
    return inventory, database_facts, network_facts


def collect_state(runner: Callable[..., str] = run_readonly) -> dict[str, Any]:
    inventory, database_facts, network_facts = collect_facts(runner)
    return kamal_eros_state.canonical_snapshot(inventory, database_facts, network_facts)


def write_state(output: Path, state: dict[str, Any]) -> None:
    output = Path(os.path.abspath(output))
    current = Path(output.anchor)
    for component in output.parts[1:-1]:
        current /= component
        try:
            if current.is_symlink():
                raise CollectionError("snapshot output path contains a symlink component")
        except OSError:
            raise CollectionError("snapshot output path is unavailable") from None
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.exists() or output.is_symlink():
        raise CollectionError("snapshot output already exists")
    try:
        with output.open("x", encoding="utf-8") as target:
            target.write(json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n")
        output.chmod(0o600)
    except OSError:
        raise CollectionError("snapshot output could not be written") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path,
                        help="new private path for the verified no-Eros snapshot")
    args = parser.parse_args()
    try:
        os.umask(0o077)
        state = collect_state()
        write_state(args.output, state)
    except (CollectionError, kamal_eros_state.EvidenceError) as error:
        # EvidenceError messages are fixed allowlisted facts; never print command stderr.
        print(f"EROS_STATE_COLLECT=BLOCKED:{error}", file=sys.stderr)
        return 2
    print(f"EROS_STATE_COLLECT=PASS\nEROS_STATE_SHA256={state['stateSha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
