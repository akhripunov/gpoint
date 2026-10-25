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


def install_docker_stub(directory: Path, *, revision: str = SHA, wrong_digest: bool = False):
    docker = directory / "docker"
    docker.write_text(f'''#!/usr/bin/env python3
import json, sys
args=sys.argv[1:]
if args[0] == "inspect":
 name=args[1]
 print(json.dumps([{{"Id":"container-"+name,"Image":"image-"+name,
  "Config":{{"Labels":{{"org.opencontainers.image.revision":"{revision}"}}}},
  "State":{{"Running":True,"Health":{{"Status":"healthy"}}}},"RestartCount":0}}]))
elif args[0:2] == ["image", "inspect"]:
 name=args[2].removeprefix("image-")
 if name.startswith("gpoint-core-"): repo="ghcr.io/akhripunov/gpoint-runtime@{APP}"
 elif name.startswith("gpoint-agata-"): repo="ghcr.io/akhripunov/gpoint-agata-runtime@{AGATA}"
 else: repo="ghcr.io/akhripunov/gpoint-miniapp@{MINIAPP}"
 if {wrong_digest!r} and name == "gpoint-core-api-{VERSION}": repo += "-wrong"
 print(json.dumps([{{"RepoDigests":[repo]}}]))
else: raise SystemExit(2)
''')
    docker.chmod(0o755)
    return docker


def args_for(output: Path):
    return [
        "--output", str(output), "--sha", SHA, "--version", VERSION,
        "--app-digest", APP, "--agata-digest", AGATA, "--miniapp-digest", MINIAPP,
        "--run-id", "42", "--attempt", "2", "--deployment-id", "987",
        "--baseline-path", "/opt/gpoint/baseline", "--baseline-sha", "4" * 64,
        "--inventory-path", "/opt/gpoint/inventory", "--inventory-sha", "5" * 64,
        "--backup-path", "/opt/gpoint/backup.dump.age", "--backup-sha", "6" * 64,
        "--restore-path", "/opt/gpoint/restore-evidence", "--restore-sha", "7" * 64,
        "--agata-candidate-head", "agata0012", "--agata-live-head", "agata0012",
        "--postgres-system-identifier", "123456", "--network-fingerprint", "8" * 64,
    ]


def run_writer(tmp_path: Path, *, wrong_digest: bool = False, revision: str = SHA):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    install_docker_stub(bin_dir, revision=revision, wrong_digest=wrong_digest)
    output = tmp_path / "kamal-production-42-2.json"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return subprocess.run([sys.executable, str(WRITER), *args_for(output)], capture_output=True,
                          text=True, env=env), output


def test_manifest_binds_exact_attempt_evidence_and_six_running_containers(tmp_path):
    result, output = run_writer(tmp_path)
    assert result.returncode == 0, result.stderr
    manifest_bytes = output.read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["schema"] == "gpoint-kamal-production-manifest/v1"
    assert manifest["candidate"]["gitSha"] == SHA
    assert manifest["attempt"] == {"runId": "42", "runAttempt": "2", "githubDeploymentId": "987"}
    assert len(manifest["runtimeServices"]) == 6
    assert all(row["running"] and row["revision"] == SHA and row["healthStatus"] == "healthy"
               for row in manifest["runtimeServices"])
    assert manifest["runtimeServices"][0]["repositoryDigest"].endswith(APP)
    assert manifest["preservation"]["databaseMigrationsAppliedByCutover"] is False
    assert f"GPOINT_KAMAL_MANIFEST_SHA256={hashlib.sha256(manifest_bytes).hexdigest()}" in result.stdout
    assert output.stat().st_mode & 0o777 == 0o600


def test_manifest_refuses_container_digest_mismatch_without_creating_output(tmp_path):
    result, output = run_writer(tmp_path, wrong_digest=True)
    assert result.returncode != 0
    assert "digest changed" in result.stderr
    assert not output.exists()


def test_manifest_refuses_container_revision_mismatch_without_creating_output(tmp_path):
    result, output = run_writer(tmp_path, revision="b" * 40)
    assert result.returncode != 0
    assert "identity changed" in result.stderr
    assert not output.exists()


def test_manifest_is_attempt_immutable_and_never_overwrites_existing_file(tmp_path):
    result, output = run_writer(tmp_path)
    assert result.returncode == 0, result.stderr
    before = output.read_bytes()
    repeat, _ = run_writer(tmp_path)
    assert repeat.returncode != 0
    assert output.read_bytes() == before
