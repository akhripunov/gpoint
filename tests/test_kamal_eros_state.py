import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/kamal_eros_state.py"


def base_facts(tmp_path: Path):
    containers = {
        "schema": "gpoint-vps-inventory/v1",
        "containers": [
            {"id": "a" * 64, "name": "shared_postgres_1", "imageId": "sha256:" + "b" * 64,
             "state": "running", "running": True, "health": "healthy", "restartPolicy": "unless-stopped",
             "composeProject": "compose", "composeService": "postgres", "owner": "compose:compose/postgres",
             "networks": {"shared_internal": {"networkId": "c" * 64}}},
            {"id": "d" * 64, "name": "compose-api-1", "imageId": "sha256:" + "e" * 64,
             "state": "running", "running": True, "health": "healthy", "restartPolicy": "unless-stopped",
             "composeProject": "compose", "composeService": "api", "owner": "compose:compose/api",
             "networks": {"shared_internal": {"networkId": "c" * 64}}},
        ],
        "resourceUsage": [
            {"containerId": "a" * 12, "containerName": "shared_postgres_1", "cpuPercent": 1.0,
             "memoryUsedBytes": 100, "memoryLimitBytes": 1000, "memoryPercent": 10.0, "pids": 1},
            {"containerId": "d" * 12, "containerName": "compose-api-1", "cpuPercent": 2.0,
             "memoryUsedBytes": 200, "memoryLimitBytes": 1000, "memoryPercent": 20.0, "pids": 2},
        ],
    }
    databases = {
        "schema": "gpoint-kamal-eros-database-facts/v1",
        "clusterCatalogScanComplete": True,
        "systemIdentifier": "7654321",
        "serverVersionNum": 160004,
        "databases": [
            {"name": "gpoint", "owner": "gpoint", "allowConnections": True, "acl": "gpoint=CTc/gpoint"},
            {"name": "postgres", "owner": "postgres", "allowConnections": True, "acl": "postgres=CTc/postgres"},
        ],
        "roles": [
            {"name": "postgres", "login": True, "superuser": True, "createDatabase": True,
             "createRole": True, "bypassRls": True},
            {"name": "gpoint", "login": True, "superuser": False, "createDatabase": False,
             "createRole": False, "bypassRls": False},
        ],
        "memberships": [],
        "gpointErosSchemaScanComplete": True,
        "gpointErosSchemas": [],
        "erosCatalog": None,
    }
    networks = {"schema": "gpoint-kamal-eros-networks/v1", "complete": True, "networks": []}
    paths = {}
    for name, data in (("containers", containers), ("databases", databases), ("networks", networks)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(data))
        paths[name] = path
    return containers, databases, networks, paths


def capture(tmp_path: Path, paths, output_name="snapshot.json"):
    return subprocess.run([
        sys.executable, str(HELPER), "capture",
        "--containers", str(paths["containers"]),
        "--databases", str(paths["databases"]),
        "--networks", str(paths["networks"]),
        "--output", str(tmp_path / output_name),
    ], capture_output=True, text=True)


def test_snapshot_proves_absence_and_does_not_copy_secret_fields(tmp_path):
    containers, _, _, paths = base_facts(tmp_path)
    containers["containers"][0]["environment"] = ["EROS_API_KEY=never-copy-this"]
    paths["containers"].write_text(json.dumps(containers))
    result = capture(tmp_path, paths)
    assert result.returncode == 0, result.stderr
    assert re.search(r"EROS_STATE_SHA256=[0-9a-f]{64}", result.stdout)
    snapshot = (tmp_path / "snapshot.json").read_text()
    assert "never-copy-this" not in snapshot
    facts = json.loads(snapshot)
    assert facts["erosAbsent"] is True
    assert facts["containers"] == []
    assert facts["database"] == {"eros": None, "roles": [], "memberships": [], "gpointErosSchemas": []}
    assert facts["networks"] == []
    assert (tmp_path / "snapshot.json").stat().st_mode & 0o777 == 0o600


def test_snapshot_fails_closed_if_any_eros_container_is_present(tmp_path):
    containers, _, _, paths = base_facts(tmp_path)
    containers["containers"].append({"id": "f" * 64, "name": "compose-eros-1", "imageId": "sha256:" + "1" * 64,
                                     "state": "exited", "running": False, "health": "unhealthy",
                                     "restartPolicy": "no", "composeProject": "compose", "composeService": "eros",
                                     "owner": "compose:compose/eros", "networks": {}})
    paths["containers"].write_text(json.dumps(containers))
    result = capture(tmp_path, paths)
    assert result.returncode == 2 and "Eros containers must be absent" in result.stderr

    containers, _, _, paths = base_facts(tmp_path)
    containers["containers"][1]["image"] = "ghcr.io/example/gpoint-eros-worker:tag"
    paths["containers"].write_text(json.dumps(containers))
    result = capture(tmp_path, paths, "snapshot-by-image.json")
    assert result.returncode == 2 and "Eros containers must be absent" in result.stderr


def test_snapshot_fails_closed_if_eros_database_or_role_is_present(tmp_path):
    _, databases, _, paths = base_facts(tmp_path)
    databases["databases"].append({"name": "eros", "owner": "postgres", "allowConnections": True, "acl": None})
    paths["databases"].write_text(json.dumps(databases))
    result = capture(tmp_path, paths)
    assert result.returncode == 2 and "Eros database must be absent" in result.stderr

    databases["databases"] = [row for row in databases["databases"] if row["name"] != "eros"]
    databases["roles"].append({"name": "eros_runtime", "login": True, "superuser": False,
                               "createDatabase": False, "createRole": False, "bypassRls": False})
    paths["databases"].write_text(json.dumps(databases))
    result = capture(tmp_path, paths, "snapshot-2.json")
    assert result.returncode == 2 and "Eros database roles must be absent" in result.stderr


def test_snapshot_fails_closed_on_eros_schema_even_with_acl_evidence(tmp_path):
    _, databases, _, paths = base_facts(tmp_path)
    databases["gpointErosSchemas"] = [{"name": "eros", "owner": "gpoint", "acl": "gpoint=UC/gpoint"}]
    paths["databases"].write_text(json.dumps(databases))
    result = capture(tmp_path, paths)
    assert result.returncode == 2 and "Eros schemas in the GPoint database must be absent" in result.stderr

    databases["gpointErosSchemas"] = [{"name": "eros", "owner": "gpoint"}]
    paths["databases"].write_text(json.dumps(databases))
    result = capture(tmp_path, paths, "snapshot-incomplete-acl.json")
    assert result.returncode == 2 and "GPoint Eros schema ACL evidence is incomplete" in result.stderr


def test_snapshot_fails_closed_on_eros_network(tmp_path):
    _, _, networks, paths = base_facts(tmp_path)
    networks["networks"] = [{"name": "gpoint_eros_api", "id": "f" * 64,
                             "internal": True, "driver": "bridge", "ipam": []}]
    paths["networks"].write_text(json.dumps(networks))
    result = capture(tmp_path, paths)
    assert result.returncode == 2 and "Eros Docker networks must be absent" in result.stderr


def test_snapshot_requires_complete_container_database_schema_and_network_evidence(tmp_path):
    containers, databases, networks, paths = base_facts(tmp_path)
    containers["resourceUsage"].pop()
    paths["containers"].write_text(json.dumps(containers))
    result = capture(tmp_path, paths)
    assert result.returncode == 2 and "resource sample does not cover complete" in result.stderr

    containers, databases, networks, paths = base_facts(tmp_path)
    containers["containers"] = [row for row in containers["containers"] if row["name"] != "shared_postgres_1"]
    containers["resourceUsage"] = [row for row in containers["resourceUsage"]
                                   if row["containerName"] != "shared_postgres_1"]
    paths["containers"].write_text(json.dumps(containers))
    result = capture(tmp_path, paths, "snapshot-2.json")
    assert result.returncode == 2 and "expected running PostgreSQL container" in result.stderr

    containers, databases, networks, paths = base_facts(tmp_path)
    databases["clusterCatalogScanComplete"] = False
    paths["databases"].write_text(json.dumps(databases))
    result = capture(tmp_path, paths, "snapshot-3.json")
    assert result.returncode == 2 and "complete PostgreSQL catalog scan" in result.stderr

    containers, databases, networks, paths = base_facts(tmp_path)
    databases["gpointErosSchemaScanComplete"] = False
    paths["databases"].write_text(json.dumps(databases))
    result = capture(tmp_path, paths, "snapshot-4.json")
    assert result.returncode == 2 and "complete GPoint Eros schema/ACL scan" in result.stderr

    containers, databases, networks, paths = base_facts(tmp_path)
    networks["complete"] = False
    paths["networks"].write_text(json.dumps(networks))
    result = capture(tmp_path, paths, "snapshot-5.json")
    assert result.returncode == 2 and "complete Docker network inventory" in result.stderr


def test_compare_requires_valid_hash_and_detects_drift(tmp_path):
    _, _, _, paths = base_facts(tmp_path)
    assert capture(tmp_path, paths, "before.json").returncode == 0
    assert capture(tmp_path, paths, "after.json").returncode == 0
    same = subprocess.run([sys.executable, str(HELPER), "compare", "--before", str(tmp_path / "before.json"),
                           "--after", str(tmp_path / "after.json")], capture_output=True, text=True)
    assert same.returncode == 0 and "EROS_STATE_MATCH=PASS" in same.stdout

    _, databases, _, paths = base_facts(tmp_path)
    databases["systemIdentifier"] = "7654322"
    paths["databases"].write_text(json.dumps(databases))
    assert capture(tmp_path, paths, "changed.json").returncode == 0
    changed = subprocess.run([sys.executable, str(HELPER), "compare", "--before", str(tmp_path / "before.json"),
                              "--after", str(tmp_path / "changed.json")], capture_output=True, text=True)
    assert changed.returncode == 1 and "EROS_STATE_MATCH=FAIL" in changed.stderr

    _, databases, _, paths = base_facts(tmp_path)
    databases["databases"][0]["acl"] = "gpoint=Tc/gpoint"
    paths["databases"].write_text(json.dumps(databases))
    assert capture(tmp_path, paths, "acl-changed.json").returncode == 0
    acl_changed = subprocess.run([sys.executable, str(HELPER), "compare", "--before", str(tmp_path / "before.json"),
                                  "--after", str(tmp_path / "acl-changed.json")], capture_output=True, text=True)
    assert acl_changed.returncode == 1 and "EROS_STATE_MATCH=FAIL" in acl_changed.stderr

    tampered = json.loads((tmp_path / "before.json").read_text())
    tampered["networks"].append({"name": "gpoint_eros_new", "id": "1" * 64})
    (tmp_path / "tampered.json").write_text(json.dumps(tampered))
    invalid = subprocess.run([sys.executable, str(HELPER), "compare", "--before", str(tmp_path / "tampered.json"),
                              "--after", str(tmp_path / "after.json")], capture_output=True, text=True)
    assert invalid.returncode == 2 and "snapshot hash mismatch" in invalid.stderr
