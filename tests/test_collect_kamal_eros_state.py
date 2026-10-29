import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import collect_kamal_eros_state as collector


def raw_container(name, service, container_id, image_id, *, running=True, image=None):
    return {
        "Id": container_id,
        "Name": "/" + name,
        "Image": "sha256:" + image_id if not image_id.startswith("sha256:") else image_id,
        "Created": "2026-10-29T00:00:00Z",
        "Config": {"Image": image or "ghcr.io/example/" + service + ":tag",
                   "Env": ["DATABASE_URL=secret-value", "EROS_API_KEY=never-copy-this"],
                   "Labels": {"com.docker.compose.project": "compose",
                              "com.docker.compose.service": service}},
        "State": {"Status": "running" if running else "exited", "Running": running,
                  "Health": {"Status": "healthy"}},
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        "NetworkSettings": {"Networks": {"shared_internal": {
            "NetworkID": "c" * 64, "Aliases": ["private-alias"],
            "IPAddress": "172.20.0.4", "GlobalIPv6Address": ""}}},
        "Mounts": [],
    }


def valid_database_facts():
    return {
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


class FakeDocker:
    def __init__(self, *, eros_container=False, database=None, eros_network=False):
        ids = ["a" * 64, "b" * 64]
        first = raw_container("shared_postgres_1", "postgres", ids[0], "1" * 64, image="postgres:16")
        second = raw_container("compose-api-1", "api", ids[1], "2" * 64)
        if eros_container:
            second["Config"]["Image"] = "ghcr.io/acme/eros-api:latest"
        self.containers = [first, second]
        self.ids = ids
        self.database = database or valid_database_facts()
        self.eros_network = eros_network
        self.calls = []

    def __call__(self, command, input_text=None, *, stage):
        self.calls.append((command, input_text, stage))
        if command == ["docker", "ps", "-aq", "--no-trunc"]:
            return "\n".join(self.ids) + "\n"
        if command[:2] == ["docker", "inspect"]:
            assert set(command[2:]) == set(self.ids)
            return json.dumps(self.containers)
        if command == ["docker", "stats", "--no-stream", "--format", "{{json .}}"]:
            return "\n".join(json.dumps({
                "ID": cid[:12], "Name": name, "CPUPerc": "0.1%",
                "MemUsage": "1MiB / 10MiB", "MemPerc": "10%", "PIDs": "1",
            }) for cid, name in zip(self.ids, ("shared_postgres_1", "compose-api-1")))
        if command[:4] == ["docker", "exec", "-i", "shared_postgres_1"]:
            assert "-d gpoint" in command[-1]
            assert input_text is not None
            normalized = input_text.upper()
            assert normalized.startswith("\nBEGIN TRANSACTION READ ONLY;")
            assert "PG_CONTROL_SYSTEM()" in normalized
            assert not any(token in normalized for token in
                           ("\nINSERT ", "\nUPDATE ", "\nDELETE ", "\nCREATE ", "\nALTER ",
                            "\nDROP ", "\nGRANT ", "\nREVOKE ", "\nTRUNCATE "))
            return json.dumps(self.database) + "\n"
        if command == ["docker", "network", "ls", "--no-trunc", "--format", "{{json .}}"]:
            rows = [{"ID": "c" * 64, "Name": "shared_internal"},
                    {"ID": "d" * 64, "Name": "bridge"}]
            if self.eros_network:
                rows.append({"ID": "e" * 64, "Name": "gpoint_eros_api"})
            return "\n".join(json.dumps(row) for row in rows)
        if command[:3] == ["docker", "network", "inspect"]:
            return json.dumps([{"Id": command[3], "Name": "gpoint_eros_api", "Internal": True,
                                "Driver": "bridge", "IPAM": {"Config": []}}])
        raise AssertionError(f"unexpected collector command at {stage}: {command}")


def test_real_collector_uses_only_read_queries_and_projects_secrets_out():
    docker = FakeDocker()
    state = collector.collect_state(docker)
    assert state["erosAbsent"] is True
    assert state["containers"] == [] and state["networks"] == []
    assert "never-copy-this" not in json.dumps(state)
    assert "secret-value" not in json.dumps(state)
    assert "172.20.0.4" not in json.dumps(state)
    commands = [call[0] for call in docker.calls]
    assert commands[0] == ["docker", "ps", "-aq", "--no-trunc"]
    assert commands[-1] == ["docker", "network", "ls", "--no-trunc", "--format", "{{json .}}"]
    assert all(command[:2] in (["docker", "ps"], ["docker", "inspect"], ["docker", "stats"],
                              ["docker", "exec"], ["docker", "network"])
               for command in commands)


@pytest.mark.parametrize("condition, message", [
    ("container", "Eros containers must be absent"),
    ("database", "Eros database must be absent"),
    ("network", "Eros Docker networks must be absent"),
])
def test_real_collector_fails_closed_when_eros_resource_is_present(condition, message):
    if condition == "container":
        docker = FakeDocker(eros_container=True)
    elif condition == "database":
        facts = valid_database_facts()
        facts["databases"].append({"name": "eros", "owner": "postgres", "allowConnections": True, "acl": None})
        docker = FakeDocker(database=facts)
    else:
        docker = FakeDocker(eros_network=True)
    with pytest.raises(collector.kamal_eros_state.EvidenceError, match=message):
        collector.collect_state(docker)


def test_real_collector_fails_closed_on_read_only_query_failure_without_echoing_stderr(monkeypatch):
    class Failed:
        returncode = 1
        stdout = ""
        stderr = "secret value from a private Docker error"

    def fake_run(*args, **kwargs):
        return Failed()

    monkeypatch.setattr(collector.subprocess, "run", fake_run)
    with pytest.raises(collector.CollectionError) as error:
        collector.run_readonly(["docker", "exec"], stage="PostgreSQL catalog scan")
    assert "secret value" not in str(error.value)


def test_real_collector_requires_complete_direct_container_inventory():
    docker = FakeDocker()
    original = docker.ids
    docker.ids = ["a" * 64]
    with pytest.raises(collector.CollectionError, match="inspection is incomplete"):
        collector.collect_state(docker)
    docker.ids = original


def test_snapshot_writer_rejects_symlinked_parent_path(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(target, target_is_directory=True)
    state = collector.collect_state(FakeDocker())
    with pytest.raises(collector.CollectionError, match="symlink component"):
        collector.write_state(link / "snapshot.json", state)
    assert not (target / "snapshot.json").exists()


def test_read_only_database_query_has_completeness_markers_and_no_sensitive_role_columns():
    query = collector.DATABASE_QUERY.upper()
    assert "BEGIN TRANSACTION READ ONLY" in query and "COMMIT;" in query
    assert "CLUSTERCATALOGSCANCOMPLETE" in query
    assert "GPOINTEROSSCHEMASCANCOMPLETE" in query
    assert "ROLPASSWORD" not in query and "DATABASE_URL" not in query
