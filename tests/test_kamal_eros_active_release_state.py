import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "kamal_eros_active_release_state", ROOT / "scripts/verify-kamal-eros-pilot-runtime.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def facts():
    user = "10000000-0000-4000-8000-000000000001"
    creator = "20000000-0000-4000-8000-000000000002"
    sha = "a" * 40
    marker = {"schema": MODULE.MARKER_SCHEMA, "status": "synthetic-pilot-ready", "sourceSha": sha,
              "imageVersion": f"{sha}-12-1", "testUserId": user, "testCreatorId": creator,
              "erosImage": "ghcr.io/akhripunov/gpoint-eros-pilot@sha256:" + "e" * 64,
              "pilotAppDigest": "sha256:" + "d" * 64, "backupSha256": "f" * 64}
    env = {"EROS_ENABLED": "true", "EROS_PRODUCTION_TEST_MODE": "true", "EROS_DATA_MODE": "synthetic",
           "EROS_PRODUCTION_TEST_USER_IDS": user, "EROS_PRODUCTION_TEST_CREATOR_IDS": creator,
           "DATABASE_URL": "postgresql://gpoint-api@shared-postgres/gpoint",
           "EROS_OLLAMA_EMBEDDING_URL": "http://172.31.254.1:11434/api/embed",
           "EROS_SERVICE_URL": "http://eros:8080",
           "EROS_OLLAMA_EMBEDDING_URL": "http://172.31.254.1:11434/api/embed"}
    api = {"name": "gpoint-core-api-" + "b" * 40 + "-13-1", "running": True, "health": "healthy",
           "env": env, "networks": {"gpoint_app": {"ip": "172.20.0.7"},
                                     "shared_internal": {"ip": "172.21.0.4"},
                                     "gpoint_eros_embeddings": {"ip": "172.31.254.3"}}}
    engine = {"id": "a" * 64, "name": "gpoint-eros-pilot-engine-" + marker["imageVersion"], "running": True,
              "health": "healthy", "revision": MODULE.EROS_UPSTREAM_SHA,
              "integrationRevision": sha, "patchSetSha256": "none", "repoDigests": [marker["erosImage"]],
              "networks": {"gpoint_app": {}, "gpoint_eros_data": {}}}
    relay = {"id": "b" * 64, "name": "gpoint-eros-relay-relay-" + marker["imageVersion"], "running": True,
             "revision": sha, "repoDigests": ["ghcr.io/akhripunov/gpoint-runtime@sha256:" + "d" * 64],
             "networkMode": "host"}
    postgres = {"id": "c" * 64, "running": True,
                "networks": {"gpoint_eros_data": {"aliases": ["gpoint-eros-db"]}},
                "database": {"name": "eros", "owner": "eros_ddl", "allowConnections": True,
                             "publicConnect": False},
                "roles": {"eros_ddl": {"login": False, "superuser": False, "createDatabase": False,
                                         "createRole": False, "bypassRls": False},
                          "eros_runtime": {"login": True, "superuser": False, "createDatabase": False,
                                           "createRole": False, "bypassRls": True},
                          "eros_migrator": {"login": True, "superuser": False, "createDatabase": False,
                                            "createRole": False, "bypassRls": False}},
                "runtimeConnect": True, "runtimeCreate": False, "migratorConnect": True,
                "migratorCreate": True, "foreignConnect": False, "memberships": [],
                "migrationCount": 4, "migrationFailures": 0, "migrationHead": "2026102001", "catalogSha256": "9" * 64}
    networks = {"gpoint_eros_data": {"id": "1" * 64, "driver": "bridge", "internal": True,
                                      "labels": {"com.gpoint.eros.managed": "true"}},
                "gpoint_eros_embeddings": {"id": "2" * 64, "driver": "bridge", "internal": True,
                                            "labels": {"com.gpoint.eros.managed": "true"},
                                            "subnet": "172.31.254.0/29", "gateway": "172.31.254.1"}}
    return marker, api, engine, relay, postgres, networks


def test_approved_synthetic_pilot_is_preserved_as_a_native_release_baseline():
    state = MODULE.validate(*facts())
    assert state["mode"] == "synthetic-eros-pilot"
    assert state["pilot"]["sourceSha"] == "a" * 40
    assert state["database"]["migrationHead"] == "2026102001"


@pytest.mark.parametrize("mutation", [
    lambda f: f[1]["env"].update(EROS_PRODUCTION_TEST_USER_IDS=""),
    lambda f: f[1]["env"].update(EROS_DATA_MODE="production"),
    lambda f: f[2].update(patchSetSha256="patched"),
    lambda f: f[3].update(networkMode="bridge"),
    lambda f: f[4].update(foreignConnect=True),
    lambda f: f[5]["gpoint_eros_embeddings"].update(internal=False),
])
def test_uncertain_or_widened_pilot_runtime_blocks_ordinary_release(mutation):
    values = facts()
    mutation(values)
    with pytest.raises(MODULE.RuntimeStateError):
        MODULE.validate(*values)


def test_empty_marker_or_unapproved_marker_status_cannot_activate_pilot():
    values = facts()
    values[0]["status"] = "apply-in-progress"
    with pytest.raises(MODULE.RuntimeStateError):
        MODULE.validate(*values)


def test_sqlx_failed_migration_blocks_ordinary_release():
    values = facts()
    values[4]["migrationFailures"] = 1
    with pytest.raises(MODULE.RuntimeStateError):
        MODULE.validate(*values)


def test_rolling_api_address_change_is_excluded_but_eros_container_replacement_changes_baseline():
    values = facts()
    before = MODULE.validate(*values)
    values[1]["networks"]["gpoint_eros_embeddings"]["ip"] = "172.31.254.4"
    assert MODULE.validate(*values)["sha256"] == before["sha256"]
    values[2]["id"] = "c" * 64
    assert MODULE.validate(*values)["sha256"] != before["sha256"]
