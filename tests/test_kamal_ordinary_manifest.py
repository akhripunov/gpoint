import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WRITER = ROOT / "scripts/write-kamal-production-manifest.py"
SHA = "a" * 40
APP = "sha256:" + "1" * 64
AGATA = "sha256:" + "2" * 64
MINIAPP = "sha256:" + "3" * 64
VERSION = f"{SHA}-42-2"
DB_ID = "d" * 64
REDIS_ID = "e" * 64


def setup_host_stubs(directory: Path, *, active_caddy=False):
    docker = directory / "docker"
    default_plan = expected_roles_b64()
    docker.write_text(f'''#!/usr/bin/env python3
import base64, json, os, sys
args=sys.argv[1:]
name=args[1] if args[0] == "inspect" else ""
roles=json.loads(base64.b64decode(os.environ.get("EXPECTED_ROLE_PLAN_B64", "{default_plan}")))["roles"]
def role_for(value):
 role=next((key for key, row in roles.items() if value.startswith(key + "-") or value == "image-" + key or row.get("baselineImageId") == value), None)
 if role is None: raise SystemExit("unknown role identity: " + value)
 return role, roles[role]
if args[0] == "inspect" and name == "kamal-proxy":
 print(json.dumps([{{"Id":"proxy-id","State":{{"Running":True}},"NetworkSettings":{{"Ports":{{"80/tcp":[{{"HostPort":"80"}}],"443/tcp":[{{"HostPort":"443"}}]}}}}}}]))
elif args[0] == "inspect" and name in ("shared_postgres_1", "shared_redis_1"):
 ident="{DB_ID}" if name == "shared_postgres_1" else "{REDIS_ID}"
 print(json.dumps([{{"Id":ident,"State":{{"Running":True}}}}]))
elif args[0] == "inspect" and name.startswith(("gsales-", "legacy-")):
 service=name.split("-", 1)[1]
 running=(name.startswith("legacy-") and __import__("os").environ.get("ACTIVE_COMPOSE_SERVICE") == service)
 print(json.dumps([{{"Id":name,"State":{{"Running":running}}}}]))
elif args[0] == "inspect" and name.startswith("gpoint-"):
 role,row=role_for(name)
 labels=dict(); labels["org.opencontainers.image.revision"]=row["sourceSha"]
 image_id=row.get("baselineImageId") or "image-"+role
 config=dict(Labels=labels,Env=["RELEASE_SHA="+row["sourceSha"]])
 state=dict(Running=True,Health=dict(Status="healthy"))
 print(json.dumps([dict(Id="container-"+name,Image=image_id,Config=config,State=state,RestartCount=0)]))
elif args[0] == "exec":
 print(role_for(args[1])[1]["sourceSha"])
elif args[0:2] == ["image", "inspect"]:
 row=role_for(args[2])[1]
 print(json.dumps(dict(debug=args[2], row=row)), file=sys.stderr)
 print(json.dumps([dict(RepoDigests=[row["repositoryDigest"]])]))
elif args[0:2] == ["ps", "-aq"]:
 if (len(args) != 6 or args[2] != "--filter"
     or args[3] != "label=com.docker.compose.project=compose"
     or args[4] != "--filter" or not args[5].startswith("label=com.docker.compose.service=")):
  raise SystemExit("invalid Docker filter argument contract")
 service=args[-1].split("=")[-1]
 prefix="legacy-" if service in ("api", "worker", "bot", "agata-api", "agata-memory-worker") else "gsales-"
 print(prefix+service)
else: raise SystemExit(2)
''')
    docker.chmod(0o755)
    systemctl = directory / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        + ("exit 0\n" if active_caddy else "exit 3\n")
    )
    systemctl.chmod(0o755)


def expected_roles_b64(scope="FULL"):
    roles = {
        "gpoint-core-api": ("CORE", "ghcr.io/akhripunov/gpoint-runtime", APP),
        "gpoint-core-worker": ("CORE", "ghcr.io/akhripunov/gpoint-runtime", APP),
        "gpoint-core-bot": ("CORE", "ghcr.io/akhripunov/gpoint-runtime", APP),
        "gpoint-agata-api": ("AGATA", "ghcr.io/akhripunov/gpoint-agata-runtime", AGATA),
        "gpoint-agata-memory-worker": ("AGATA", "ghcr.io/akhripunov/gpoint-agata-runtime", AGATA),
        "gpoint-miniapp-web": ("MINIAPP", "ghcr.io/akhripunov/gpoint-miniapp", MINIAPP),
    }
    components = {"CORE": ("gpoint-core-api", "gpoint-core-worker", "gpoint-core-bot"),
                  "AGATA": ("gpoint-agata-api", "gpoint-agata-memory-worker"),
                  "MINIAPP": ("gpoint-miniapp-web",)}
    updated = set(components) if scope == "FULL" else set(scope.split("+")) - {"NONE"}
    baseline = "b" * 40
    role_rows = {}
    for role, (component, repository, digest) in roles.items():
        role_rows[role] = ({"version": VERSION, "sourceSha": SHA,
                            "repositoryDigest": repository + "@" + digest,
                            "baselineImageId": None}
                           if component in updated else
                           {"version": baseline + "-40-1", "sourceSha": baseline,
                            "repositoryDigest": repository + "@sha256:" + ("4" if component == "CORE" else "6" if component == "AGATA" else "7") * 64,
                            "baselineImageId": "sha256:" + ("5" if component == "CORE" else "8" if component == "AGATA" else "9") * 64})
    value = {
        "schema": 1, "candidateSha": SHA, "scope": scope,
        "updatedComponents": sorted(updated), "roles": role_rows,
    }
    return base64.b64encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).decode()


def ordinary_args(output: Path):
    return [
        "--mode", "ordinary", "--output", str(output), "--sha", SHA,
        "--app-source-sha", SHA, "--version", VERSION,
        "--app-digest", APP, "--agata-digest", AGATA, "--miniapp-digest", MINIAPP,
        "--run-id", "42", "--attempt", "2", "--deployment-id", "987",
        "--db-container-id", DB_ID, "--redis-container-id", REDIS_ID,
        "--prisma-head", "20261022110000_eros_gpoint_message_state", "--agata-head", "agata0012",
        "--prisma-migrations", "20261015130000_baseline,20261022110000_eros_gpoint_message_state",
        "--prisma-checksums", "20261015130000_baseline=" + "1" * 64 + ",20261022110000_eros_gpoint_message_state=" + "2" * 64,
        "--baseline-prisma-head", "20261022110000_eros_gpoint_message_state",
        "--baseline-prisma-migrations", "20261015130000_baseline,20261022110000_eros_gpoint_message_state",
        "--baseline-prisma-checksums", "20261015130000_baseline=" + "1" * 64 + ",20261022110000_eros_gpoint_message_state=" + "2" * 64,
        "--baseline-agata-head", "agata0012",
        "--pending-prisma-migrations", "",
        "--pending-prisma-checksums", "",
        "--expected-role-identities-b64", expected_roles_b64(),
        "--public-routes-verified", "--migration-compatibility-verified",
    ]


def run_ordinary(tmp_path: Path, *, active_caddy=False, role_plan_b64=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    setup_host_stubs(bin_dir, active_caddy=active_caddy)
    output = tmp_path / "ordinary-manifest.json"
    args = ordinary_args(output)
    if role_plan_b64:
        args[args.index("--expected-role-identities-b64") + 1] = role_plan_b64
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "EXPECTED_ROLE_PLAN_B64": args[args.index("--expected-role-identities-b64") + 1]}
    result = subprocess.run([sys.executable, str(WRITER), *args],
                            capture_output=True, text=True, env=env)
    return result, output


def test_ordinary_manifest_binds_services_database_proxy_tls_and_routes(tmp_path):
    result, output = run_ordinary(tmp_path)
    assert result.returncode == 0, result.stderr
    raw = output.read_bytes()
    manifest = json.loads(raw)
    assert manifest["operationMode"] == "ordinary"
    assert manifest["candidate"]["gitSha"] == SHA
    assert manifest["candidate"]["appSourceSha"] == SHA
    assert len(manifest["runtimeServices"]) == 6
    assert {service["revision"] for service in manifest["runtimeServices"]} == {SHA}
    assert manifest["preservation"]["postgresContainerId"] == DB_ID
    assert manifest["preservation"]["redisContainerId"] == REDIS_ID
    assert manifest["preservation"]["caddy"] == "INACTIVE"
    assert manifest["preservation"]["legacyCompose"] == "STOPPED"
    assert len(manifest["preservation"]["legacyComposeContainers"]) == 5
    assert manifest["publicIngress"] == {"proxy": "kamal-proxy", "httpPort": 80, "httpsPort": 443,
                                         "tls": "PASS", "routes": "PASS"}
    assert manifest["preservation"]["gsales"] == "STOPPED"
    assert manifest["migrationHeads"]["prismaHead"] == "20261022110000_eros_gpoint_message_state"
    assert len(manifest["migrationHeads"]["prismaAppliedChecksums"]) == 2
    assert manifest["migrationHeads"]["liveBaselineUnchanged"] is True
    assert manifest["evidence"]["migrationCompatibility"] == "PASS"
    assert f"GPOINT_KAMAL_MANIFEST_SHA256={hashlib.sha256(raw).hexdigest()}" in result.stdout
    assert output.stat().st_mode & 0o777 == 0o600


def test_ordinary_manifest_accepts_canonical_joined_component_scopes(tmp_path):
    for index, scope in enumerate(("AGATA+CORE", "AGATA+MINIAPP", "CORE+MINIAPP",
                                   "AGATA+CORE+MINIAPP")):
        result, output = run_ordinary(tmp_path / str(index), role_plan_b64=expected_roles_b64(scope))
        assert result.returncode == 0, result.stderr
        manifest = json.loads(output.read_bytes())
        assert manifest["deploymentScope"] == {
            "classification": scope,
            "updatedComponents": sorted(scope.split("+")),
        }


def test_ordinary_manifest_accepts_documentation_only_no_runtime_scope(tmp_path):
    result, output = run_ordinary(tmp_path, role_plan_b64=expected_roles_b64("NONE"))
    assert result.returncode == 0, result.stderr
    manifest = json.loads(output.read_bytes())
    assert manifest["deploymentScope"] == {"classification": "NONE", "updatedComponents": []}
    assert {service["revision"] for service in manifest["runtimeServices"]} == {"b" * 40}


def test_ordinary_manifest_refuses_active_caddy_without_writing_output(tmp_path):
    result, output = run_ordinary(tmp_path, active_caddy=True)
    assert result.returncode != 0
    assert "Caddy must be inactive" in result.stderr
    assert not output.exists()


def test_ordinary_manifest_refuses_running_legacy_compose_worker(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    setup_host_stubs(bin_dir)
    output = tmp_path / "ordinary-manifest.json"
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
           "ACTIVE_COMPOSE_SERVICE": "worker"}
    result = subprocess.run([sys.executable, str(WRITER), *ordinary_args(output)],
                            capture_output=True, text=True, env=env)
    assert result.returncode != 0
    assert "legacy Compose service must remain stopped: worker" in result.stderr
    assert not output.exists()


def test_ordinary_manifest_refuses_database_migration_baseline_drift(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    setup_host_stubs(bin_dir)
    output = tmp_path / "ordinary-manifest.json"
    args = ordinary_args(output)
    index = args.index("--baseline-agata-head") + 1
    args[index] = "agata0011"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = subprocess.run([sys.executable, str(WRITER), *args], capture_output=True, text=True, env=env)
    assert result.returncode != 0
    assert "AGATA head changed" in result.stderr
    assert not output.exists()


def test_ordinary_manifest_records_exact_approved_migration_append(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    setup_host_stubs(bin_dir)
    output = tmp_path / "ordinary-migration-manifest.json"
    args = ordinary_args(output)
    baseline = "20261015130000_baseline,20261022110000_eros_gpoint_message_state"
    baseline_checksums = "20261015130000_baseline=" + "1" * 64 + ",20261022110000_eros_gpoint_message_state=" + "2" * 64
    pending = "20261023120000_safe_extension"
    pending_checksum = "3" * 64
    args[args.index("--prisma-head") + 1] = pending
    args[args.index("--prisma-migrations") + 1] = baseline + "," + pending
    args[args.index("--prisma-checksums") + 1] = baseline_checksums + "," + pending + "=" + pending_checksum
    args[args.index("--pending-prisma-migrations") + 1] = pending
    args[args.index("--pending-prisma-checksums") + 1] = pending + "=" + pending_checksum
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = subprocess.run([sys.executable, str(WRITER), *args], capture_output=True, text=True, env=env)

    assert result.returncode == 0, result.stderr
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["migrationHeads"]["liveBaselineUnchanged"] is False
    assert manifest["migrationHeads"]["databaseWrites"] is True
    assert manifest["migrationHeads"]["appliedExactlyApprovedPlan"] is True
    assert manifest["migrationHeads"]["approvedPendingMigrations"] == [{"name": pending, "checksum": pending_checksum}]
    assert manifest["preservation"]["databaseMigrationsAppliedByRelease"] is True


def test_ordinary_manifest_rejects_unplanned_or_reordered_migration_append(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    setup_host_stubs(bin_dir)
    output = tmp_path / "ordinary-migration-manifest.json"
    args = ordinary_args(output)
    pending = "20261023120000_safe_extension"
    actual_pending = "20261024120000_unplanned"
    args[args.index("--prisma-head") + 1] = actual_pending
    args[args.index("--prisma-migrations") + 1] += "," + actual_pending
    args[args.index("--prisma-checksums") + 1] += "," + actual_pending + "=" + "3" * 64
    args[args.index("--pending-prisma-migrations") + 1] = pending
    args[args.index("--pending-prisma-checksums") + 1] = pending + "=" + "4" * 64
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = subprocess.run([sys.executable, str(WRITER), *args], capture_output=True, text=True, env=env)

    assert result.returncode != 0
    assert "migration transition differs" in result.stderr
    assert not output.exists()


def test_ordinary_manifest_verifies_untouched_component_identity_in_selective_release(tmp_path):
    result, output = run_ordinary(tmp_path, role_plan_b64=expected_roles_b64("MINIAPP"))

    assert result.returncode == 0, result.stderr
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["deploymentScope"] == {"classification": "MINIAPP", "updatedComponents": ["MINIAPP"]}
    services = {row["name"].split("-" + row["version"], 1)[0]: row for row in manifest["runtimeServices"]}
    assert services["gpoint-core-api"]["revision"] == "b" * 40
    assert services["gpoint-core-api"]["version"] == "b" * 40 + "-40-1"
    assert services["gpoint-core-api"]["repositoryDigest"].endswith("sha256:" + "4" * 64)
    assert services["gpoint-miniapp-web"]["revision"] == SHA
    assert services["gpoint-miniapp-web"]["version"] == VERSION
