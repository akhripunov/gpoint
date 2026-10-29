import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
WRITER = ROOT / "scripts/write-kamal-production-manifest.py"
SHA = "a" * 40
APP_SOURCE_SHA = "b" * 40
APP = "sha256:" + "1" * 64
AGATA = "sha256:" + "2" * 64
MINIAPP = "sha256:" + "3" * 64
VERSION = f"{SHA}-42-2"
MEDIA_BACKUP = "/opt/gpoint/backups/gpoint-pre-release-" + SHA + "-42-2.media.tar.age"
MEDIA_SHA = "9" * 64


def install_docker_stub(directory: Path, *, revision: str = APP_SOURCE_SHA,
                        release_sha: str = APP_SOURCE_SHA, wrong_digest: bool = False):
    docker = directory / "docker"
    docker.write_text(f'''#!/usr/bin/env python3
import json, sys
args=sys.argv[1:]
if args[0] == "inspect":
 name=args[1]
 print(json.dumps([{{"Id":"container-"+name,"Image":"image-"+name,
  "Config":{{"Labels":{{"org.opencontainers.image.revision":"{revision}"}},"Env":["RELEASE_SHA={release_sha}"]}},
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


def args_for(output: Path, *, version=VERSION):
    state_base = output.parent / f"kamal-no-eros-state-42-2"
    state = {
        "schema": "gpoint-kamal-eros-state/v1", "erosAbsent": True,
        "postgres": {"systemIdentifier": "123456", "serverVersionNum": 160004,
                     "catalogAclSha256": "8" * 64},
        "database": {"eros": None, "roles": [], "memberships": [], "gpointErosSchemas": []},
        "containers": [], "networks": [],
    }
    state["stateSha256"] = hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    baseline_state = state_base.with_suffix(".json")
    postflight_state = state_base.parent / (state_base.name + "-postflight.json")
    for path in (baseline_state, postflight_state):
        path.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n")
    baseline_file_sha = hashlib.sha256(baseline_state.read_bytes()).hexdigest()
    postflight_file_sha = hashlib.sha256(postflight_state.read_bytes()).hexdigest()
    return [
        "--output", str(output), "--sha", SHA, "--app-source-sha", APP_SOURCE_SHA, "--version", version,
        "--app-digest", APP, "--agata-digest", AGATA, "--miniapp-digest", MINIAPP,
        "--run-id", "42", "--attempt", "2", "--deployment-id", "987",
        "--baseline-path", "/opt/gpoint/baseline", "--baseline-sha", "4" * 64,
        "--inventory-path", "/opt/gpoint/inventory", "--inventory-sha", "5" * 64,
        "--backup-path", "/opt/gpoint/backup.dump.age", "--backup-sha", "6" * 64,
        "--restore-path", "/opt/gpoint/restore-evidence", "--restore-sha", "7" * 64,
        "--agata-candidate-head", "agata0012", "--agata-live-head", "agata0012",
        "--postgres-system-identifier", "123456",
        "--no-eros-baseline-path", str(baseline_state), "--no-eros-baseline-sha", baseline_file_sha,
        "--no-eros-postflight-path", str(postflight_state), "--no-eros-postflight-sha", postflight_file_sha,
        "--postgres-acl-fingerprint", "7" * 64,
        "--media-backup-path", MEDIA_BACKUP, "--media-backup-sha", MEDIA_SHA,
        "--media-source-manifest-sha", "a" * 64, "--media-restore-manifest-sha", "a" * 64,
        "--media-file-count", "2", "--media-directory-count", "1",
        "--media-total-bytes", "4096", "--media-backup-bytes", "4200",
    ]


def run_writer(tmp_path: Path, *, wrong_digest: bool = False, revision: str = APP_SOURCE_SHA,
               release_sha: str = APP_SOURCE_SHA, version=VERSION):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    install_docker_stub(bin_dir, revision=revision, release_sha=release_sha, wrong_digest=wrong_digest)
    output = tmp_path / "kamal-production-42-2.json"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return subprocess.run([sys.executable, str(WRITER), *args_for(output, version=version)], capture_output=True,
                          text=True, env=env), output


def test_manifest_binds_exact_attempt_evidence_and_six_running_containers(tmp_path):
    result, output = run_writer(tmp_path)
    assert result.returncode == 0, result.stderr
    manifest_bytes = output.read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["schema"] == "gpoint-kamal-production-manifest/v2"
    assert manifest["candidate"]["gitSha"] == SHA
    assert manifest["candidate"]["appSourceSha"] == APP_SOURCE_SHA
    assert manifest["attempt"] == {"runId": "42", "runAttempt": "2", "githubDeploymentId": "987"}
    assert len(manifest["runtimeServices"]) == 6
    assert all(row["running"] and row["revision"] == APP_SOURCE_SHA and row["healthStatus"] == "healthy"
               for row in manifest["runtimeServices"])
    assert manifest["runtimeServices"][0]["repositoryDigest"].endswith(APP)
    assert manifest["preservation"]["databaseMigrationsAppliedByCutover"] is False
    assert manifest["preservation"]["erosAbsent"] == "PASS"
    assert manifest["preservation"]["postgresAclFingerprint"] == "7" * 64
    assert manifest["preservation"]["postgresCatalogAclSha256"] == "8" * 64
    assert manifest["evidence"]["noErosBaseline"]["result"] == "PASS"
    assert manifest["evidence"]["noErosPostflight"]["result"] == "PASS"
    assert manifest["preservation"]["persistentMediaBackupAndRestore"] == "PASS"
    assert manifest["evidence"]["mediaBackup"] == {
        "path": MEDIA_BACKUP, "sha256": MEDIA_SHA, "bytes": 4200,
        "encryption": "age-x25519", "offsiteRestore": "PASS",
        "fileCount": 2, "directoryCount": 1, "totalBytes": 4096,
        "sourceManifestSha256": "a" * 64, "restoredManifestSha256": "a" * 64,
    }
    assert manifest["evidence"]["mediaRestore"]["result"] == "PASS"
    assert f"GPOINT_KAMAL_MANIFEST_SHA256={hashlib.sha256(manifest_bytes).hexdigest()}" in result.stdout
    assert output.stat().st_mode & 0o777 == 0o600


def test_manifest_refuses_container_digest_mismatch_without_creating_output(tmp_path):
    result, output = run_writer(tmp_path, wrong_digest=True)
    assert result.returncode != 0
    assert "digest changed" in result.stderr
    assert not output.exists()


def test_manifest_refuses_container_revision_mismatch_without_creating_output(tmp_path):
    result, output = run_writer(tmp_path, revision="c" * 40)
    assert result.returncode != 0
    assert "identity changed" in result.stderr
    assert not output.exists()


def test_manifest_refuses_runtime_release_sha_mismatch_without_creating_output(tmp_path):
    result, output = run_writer(tmp_path, release_sha=SHA)
    assert result.returncode != 0
    assert "runtime RELEASE_SHA does not match application source" in result.stderr
    assert not output.exists()


def test_manifest_rejects_a_postflight_snapshot_that_does_not_prove_eros_absence(tmp_path):
    output = tmp_path / "kamal-production-42-2.json"
    args = args_for(output)
    snapshot_path = Path(args[args.index("--no-eros-postflight-path") + 1])
    snapshot = json.loads(snapshot_path.read_text())
    snapshot["erosAbsent"] = False
    body = {key: value for key, value in snapshot.items() if key != "stateSha256"}
    snapshot["stateSha256"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    snapshot_path.write_text(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n")
    args[args.index("--no-eros-postflight-sha") + 1] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    install_docker_stub(bin_dir)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = subprocess.run([sys.executable, str(WRITER), *args], capture_output=True, text=True, env=env)
    assert result.returncode != 0
    assert "failed its content check" in result.stderr
    assert not output.exists()


def test_manifest_rejects_attempt_tag_that_does_not_bind_candidate_run_and_attempt(tmp_path):
    result, output = run_writer(tmp_path, version=f"{SHA}-41-1")
    assert result.returncode != 0
    assert "attempt tag does not match" in result.stderr
    assert not output.exists()


def test_manifest_refuses_media_restore_manifest_mismatch(tmp_path):
    args = args_for(tmp_path / "kamal-production-42-2.json")
    args[args.index("--media-restore-manifest-sha") + 1] = "b" * 64
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_docker_stub(bin_dir)
    output = tmp_path / "kamal-production-42-2.json"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = subprocess.run([sys.executable, str(WRITER), *args], capture_output=True, text=True, env=env)
    assert result.returncode != 0
    assert "restored media manifest does not match" in result.stderr
    assert not output.exists()


def test_manifest_is_attempt_immutable_and_never_overwrites_existing_file(tmp_path):
    result, output = run_writer(tmp_path)
    assert result.returncode == 0, result.stderr
    before = output.read_bytes()
    repeat, _ = run_writer(tmp_path)
    assert repeat.returncode != 0
    assert output.read_bytes() == before
