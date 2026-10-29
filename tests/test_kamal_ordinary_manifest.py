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
    docker.write_text(f'''#!/usr/bin/env python3
import json, sys
args=sys.argv[1:]
name=args[1] if args[0] == "inspect" else ""
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
 print(json.dumps([{{"Id":"container-"+name,"Image":"image-"+name,
  "Config":{{"Labels":{{"org.opencontainers.image.revision":"{SHA}"}},"Env":["RELEASE_SHA={SHA}"]}},
  "State":{{"Running":True,"Health":{{"Status":"healthy"}}}},"RestartCount":0}}]))
elif args[0:2] == ["image", "inspect"]:
 name=args[2].removeprefix("image-")
 if name.startswith("gpoint-core-"): repo="ghcr.io/akhripunov/gpoint-runtime@{APP}"
 elif name.startswith("gpoint-agata-"): repo="ghcr.io/akhripunov/gpoint-agata-runtime@{AGATA}"
 else: repo="ghcr.io/akhripunov/gpoint-miniapp@{MINIAPP}"
 print(json.dumps([{{"RepoDigests":[repo]}}]))
elif args[0:2] == ["ps", "-aq"]:
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


def ordinary_args(output: Path):
    return [
        "--mode", "ordinary", "--output", str(output), "--sha", SHA,
        "--app-source-sha", SHA, "--version", VERSION,
        "--app-digest", APP, "--agata-digest", AGATA, "--miniapp-digest", MINIAPP,
        "--run-id", "42", "--attempt", "2", "--deployment-id", "987",
        "--db-container-id", DB_ID, "--redis-container-id", REDIS_ID,
        "--public-routes-verified", "--migration-parity-verified",
    ]


def run_ordinary(tmp_path: Path, *, active_caddy=False):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    setup_host_stubs(bin_dir, active_caddy=active_caddy)
    output = tmp_path / "ordinary-manifest.json"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = subprocess.run([sys.executable, str(WRITER), *ordinary_args(output)],
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
    assert f"GPOINT_KAMAL_MANIFEST_SHA256={hashlib.sha256(raw).hexdigest()}" in result.stdout
    assert output.stat().st_mode & 0o777 == 0o600


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
