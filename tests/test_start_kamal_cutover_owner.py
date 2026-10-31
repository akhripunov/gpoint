import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("start_kamal_cutover_owner", ROOT / "scripts/start_kamal_cutover_owner.py")
STARTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STARTER)
PACKAGE_SPEC = importlib.util.spec_from_file_location("package_kamal_cutover", ROOT / "scripts/package-kamal-cutover.py")
PACKAGE = importlib.util.module_from_spec(PACKAGE_SPEC)
assert PACKAGE_SPEC and PACKAGE_SPEC.loader
PACKAGE_SPEC.loader.exec_module(PACKAGE)
OWNER_BYTES = (ROOT / "scripts/kamal_cutover_owner.py").read_bytes()
CREDENTIAL_BYTES = (ROOT / "scripts/kamal_cutover_credentials.py").read_bytes()
SHA = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD^{commit}"], text=True).strip()
RUN = "12345"
ATTEMPT = "1"


def make_stage(tmp_path, *, extra=(), replace_owner=None, bootstrap=False):
    stage = tmp_path / "gpoint-kamal-owner" / f"{SHA}-{RUN}-{ATTEMPT}"
    workspace_root = tmp_path / "exec-workspaces"
    stage.mkdir(parents=True)
    stage.chmod(0o700)
    if bootstrap:
        (stage / "start.py").write_text("# trusted by the caller\n")
        (stage / "start.py").chmod(0o600)
    values = {
        "GITHUB_ACTOR": "ci-user", "KAMAL_REGISTRY_PASSWORD": "super-secret",
        "HOME": str(stage / "home"), "RUNNER_TEMP": str(stage / "tmp"),
        "GITHUB_WORKSPACE": str(workspace_root / stage.name / "repo"),
        "GPOINT_KAMAL_SECRET_FILE": str(stage / "repo/.kamal/secrets"),
        "GITHUB_STEP_SUMMARY": str(stage / "github-summary"),
        "GITHUB_OUTPUT": str(stage / "github-output"),
    }
    files = {}
    entries = {
        "kamal_cutover_owner.py": replace_owner if replace_owner is not None else OWNER_BYTES,
        "kamal_cutover_credentials.py": CREDENTIAL_BYTES,
        "environment.json": json.dumps(values, sort_keys=True).encode(),
        "repo/.kamal/secrets": b"private-config",
        "repo/.kamal/hooks/pre-app-boot": b"#!/bin/sh\nexit 0\n",
    }
    entries.update({name: data for name, (data, _mode) in PACKAGE._git_workspace_entries(ROOT, SHA).items()})
    for phase in ("pause", "retire", "requests", "routes", "consumers", "verify", "recover-pre", "recover-post"):
        entries[f"phases/{phase}.sh"] = b"#!/bin/bash\nexit 0\n"
    entries.update(dict(extra))
    for name, data in entries.items():
        files[name] = hashlib.sha256(data).hexdigest()
    manifest = {"schema": "gpoint-kamal-owner/v1", "candidateSha": SHA,
                "runId": RUN, "attempt": ATTEMPT, "environmentFile": "environment.json", "files": files}
    entries["manifest.json"] = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    archive = stage / "bundle.tar"
    with tarfile.open(archive, "w") as tar:
        for name, data in sorted(entries.items()):
            item = tarfile.TarInfo(name)
            item.size = len(data)
            item.mode = 0o700 if name.startswith("repo/.kamal/hooks/") else 0o600
            item.uid = item.gid = 0
            tar.addfile(item, io.BytesIO(data))
    archive.chmod(0o600)
    return stage, archive


def hashes(archive):
    with tarfile.open(archive, "r:") as tar:
        manifest = tar.extractfile("manifest.json").read()
    return (hashlib.sha256(archive.read_bytes()).hexdigest(), hashlib.sha256(manifest).hexdigest(),
            hashlib.sha256(OWNER_BYTES).hexdigest(), hashlib.sha256(CREDENTIAL_BYTES).hexdigest())


class Harness:
    def __init__(self, stage, active="active\nrunning\n"):
        self.stage = stage
        self.calls = []
        self.active = active
        self.credentials_ready = False

    def install(self, module, stage):
        assert "KAMAL_REGISTRY_PASSWORD" not in os.environ
        self.credentials_ready = True
        (stage / "home/.ssh").mkdir(parents=True, mode=0o700)

    def cleanup(self, module, stage):
        self.credentials_ready = False

    def runner(self, argv, **kwargs):
        argv = list(map(str, argv))
        assert "KAMAL_REGISTRY_PASSWORD" not in kwargs.get("env", {})
        self.calls.append(argv)
        if argv[0].endswith("systemctl"):
            if any("LoadState" in value for value in argv):
                return subprocess.CompletedProcess(argv, 0, stdout="not-found\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout=self.active, stderr="")
        if "systemd-run" in argv[0]:
            assert self.credentials_ready
            workspace = self.stage.parent.parent / "exec-workspaces" / self.stage.name / "repo"
            assert workspace.is_dir()
            assert subprocess.check_output(
                ["git", "-C", str(workspace), "rev-parse", "HEAD^{commit}"], text=True
            ).strip() == SHA
            assert (workspace / ".kamal/secrets").read_bytes() == b""
            config = self.stage / "home/.docker/config.json"
            assert config.stat().st_mode & 0o777 == 0o600
            assert json.loads(config.read_text())["auths"]["ghcr.io"]["auth"]
            assert (self.stage / "github-summary").stat().st_mode & 0o777 == 0o600
            assert (self.stage / "github-output").stat().st_mode & 0o777 == 0o600
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(argv)


def manifest_sha_from_archive(archive):
    with tarfile.open(archive, "r:") as tar:
        manifest = tar.extractfile("manifest.json").read()
    return hashlib.sha256(manifest).hexdigest()


def start(stage, archive, digest, owner_sha, credential_sha, harness):
    return STARTER.start(stage, archive, digest, manifest_sha_from_archive(archive),
                         owner_sha, credential_sha, runner=harness.runner,
                         systemd_run_path="/usr/bin/systemd-run", systemctl_path="/usr/bin/systemctl",
                         owner_uid=os.getuid(), credential_install=harness.install,
                         credential_cleanup=harness.cleanup,
                         execution_workspace_root=stage.parent.parent / "exec-workspaces")


def test_verified_bundle_installs_credentials_before_single_exact_systemd_dispatch(tmp_path):
    stage, archive = make_stage(tmp_path)
    digest, manifest_sha, owner_sha, credential_sha = hashes(archive)
    harness = Harness(stage)
    manifest_sha = manifest_sha_from_archive(archive)
    assert start(stage, archive, digest, owner_sha, credential_sha, harness) == "STARTED"
    dispatch = next(argv for argv in harness.calls if argv[0].endswith("systemd-run"))
    assert dispatch[:2] == ["/usr/bin/systemd-run", "--unit=gpoint-kamal-owner-12345-1"]
    assert "--service-type=exec" in dispatch
    assert "--property=Restart=no" in dispatch
    assert "--property=KillMode=control-group" in dispatch
    assert "--property=UMask=0077" in dispatch
    assert any(value.startswith("--property=ExecStopPost=") and "kamal_cutover_credentials.py remove --stage " in value for value in dispatch)
    assert dispatch[-7:] == ["/usr/bin/python3", "-B", str(stage / "kamal_cutover_owner.py"),
                             "--stage", str(stage), "--manifest-sha", manifest_sha]
    assert len([argv for argv in harness.calls if argv[0].endswith("systemd-run")]) == 1
    assert not archive.exists()
    assert not any("super-secret" in arg for argv in harness.calls for arg in argv)
    workspace = stage.parent.parent / "exec-workspaces" / stage.name / "repo"
    assert subprocess.check_output(["git", "-C", str(workspace), "rev-parse", "HEAD^{commit}"], text=True).strip() == SHA
    assert (workspace / ".kamal/hooks/pre-app-boot").stat().st_mode & 0o111
    assert (workspace / ".kamal/secrets").read_bytes() == b""
    assert (stage / "repo/.kamal/secrets").read_bytes() == b"private-config"


def test_noexec_execution_workspace_fails_before_systemd_dispatch(tmp_path, monkeypatch):
    stage, archive = make_stage(tmp_path)
    digest, _, owner_sha, credential_sha = hashes(archive)
    harness = Harness(stage)
    original_run = STARTER.subprocess.run

    def noexec_probe(argv, **kwargs):
        if argv and str(argv[0]).endswith(".exec-probe"):
            return subprocess.CompletedProcess(argv, 126, stdout="", stderr="Permission denied")
        return original_run(argv, **kwargs)

    monkeypatch.setattr(STARTER.subprocess, "run", noexec_probe)
    with pytest.raises(STARTER.OwnerStartError, match="filesystem is noexec"):
        start(stage, archive, digest, owner_sha, credential_sha, harness)
    assert not harness.credentials_ready
    assert not any(argv[0].endswith("systemd-run") for argv in harness.calls)
    assert not (stage.parent.parent / "exec-workspaces" / stage.name).exists()


def test_tar_digest_mismatch_fails_before_import_credentials_or_systemd(tmp_path):
    stage, archive = make_stage(tmp_path)
    _, _, owner_sha, credential_sha = hashes(archive)
    harness = Harness(stage)
    with pytest.raises(STARTER.OwnerStartError):
        start(stage, archive, "0" * 64, owner_sha, credential_sha, harness)
    assert harness.calls == [] and not harness.credentials_ready
    assert not (stage / "manifest.json").exists()
    assert not archive.exists()


@pytest.mark.parametrize("entry_kind", ["traversal", "symlink", "hardlink"])
def test_unsafe_tar_entries_rejected_before_credentials_and_dispatch(tmp_path, entry_kind):
    stage, archive = make_stage(tmp_path)
    with tarfile.open(archive, "a") as tar:
        item = tarfile.TarInfo("../escape")
        if entry_kind == "traversal":
            item.size = 1
            tar.addfile(item, io.BytesIO(b"x"))
        elif entry_kind == "symlink":
            item.type = tarfile.SYMTYPE
            item.linkname = "manifest.json"
            tar.addfile(item)
        else:
            item.type = tarfile.LNKTYPE
            item.linkname = "manifest.json"
            tar.addfile(item)
    archive.chmod(0o600)
    digest, manifest_sha, owner_sha, credential_sha = hashes(archive)
    harness = Harness(stage)
    with pytest.raises(STARTER.OwnerStartError):
        start(stage, archive, digest, owner_sha, credential_sha, harness)
    assert harness.calls == [] and not harness.credentials_ready


@pytest.mark.parametrize("which", ["owner", "credentials"])
def test_trusted_helper_hash_mismatch_rejected_before_import_or_dispatch(tmp_path, which):
    stage, archive = make_stage(tmp_path)
    digest, manifest_sha, owner_sha, credential_sha = hashes(archive)
    harness = Harness(stage)
    if which == "owner":
        owner_sha = "0" * 64
    else:
        credential_sha = "0" * 64
    with pytest.raises(STARTER.OwnerStartError):
        start(stage, archive, digest, owner_sha, credential_sha, harness)
    assert harness.calls == [] and not harness.credentials_ready
    assert not (stage / "owner-imported.marker").exists()


def test_unknown_post_dispatch_state_is_not_cleaned_or_retried(tmp_path):
    stage, archive = make_stage(tmp_path)
    digest, manifest_sha, owner_sha, credential_sha = hashes(archive)
    harness = Harness(stage, active="inactive\ndead\n")
    assert start(stage, archive, digest, owner_sha, credential_sha, harness) == "UNKNOWN"
    assert harness.credentials_ready
    assert len([argv for argv in harness.calls if argv[0].endswith("systemd-run")]) == 1
    assert not archive.exists()
    assert not any("super-secret" in arg for argv in harness.calls for arg in argv)


@pytest.mark.parametrize("which", ["owner", "credentials"])
def test_mismatched_helper_payload_is_never_executed(tmp_path, which):
    marker_name = "owner-imported.marker" if which == "owner" else "credentials-imported.marker"
    payload = f"from pathlib import Path\nPath(__file__).with_name({marker_name!r}).touch()\n".encode()
    stage, archive = make_stage(tmp_path, replace_owner=payload if which == "owner" else None,
                                extra=() if which == "owner" else (("kamal_cutover_credentials.py", payload),))
    digest, _, owner_sha, credential_sha = hashes(archive)
    harness = Harness(stage)
    with pytest.raises(STARTER.OwnerStartError):
        start(stage, archive, digest, owner_sha, credential_sha, harness)
    assert not (stage / marker_name).exists()
    assert harness.calls == [] and not harness.credentials_ready


def test_existing_unit_blocks_before_credentials_or_dispatch(tmp_path):
    stage, archive = make_stage(tmp_path)
    digest, _, owner_sha, credential_sha = hashes(archive)
    harness = Harness(stage)
    base_runner = harness.runner
    def existing_unit(argv, **kwargs):
        if any("LoadState" in value for value in argv):
            return subprocess.CompletedProcess(argv, 0, stdout="loaded\n", stderr="")
        return base_runner(argv, **kwargs)
    harness.runner = existing_unit
    with pytest.raises(STARTER.OwnerStartError, match="already exists"):
        start(stage, archive, digest, owner_sha, credential_sha, harness)
    assert not harness.credentials_ready
    assert not any(argv[0].endswith("systemd-run") for argv in harness.calls)


def test_systemd_terminal_state_requires_attempt_bound_owner_status(tmp_path):
    stage, archive = make_stage(tmp_path)
    digest, _, owner_sha, credential_sha = hashes(archive)
    harness = Harness(stage, active="inactive\ndead\n")
    def runner(argv, **kwargs):
        if "systemd-run" in argv[0]:
            assert harness.credentials_ready
            (stage / "owner-status.json").write_text(json.dumps({
                "schema": "gpoint-kamal-owner/v1", "candidateSha": "b" * 40,
                "runId": RUN, "attempt": ATTEMPT, "state": "COMPLETE",
            }))
            (stage / "owner-status.json").chmod(0o600)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return harness.runner(argv, **kwargs)
    result = STARTER.start(stage, archive, digest, manifest_sha_from_archive(archive),
                           owner_sha, credential_sha, runner=runner, owner_uid=os.getuid(),
                           credential_install=harness.install, credential_cleanup=harness.cleanup,
                           execution_workspace_root=stage.parent.parent / "exec-workspaces")
    assert result == "UNKNOWN"
    assert harness.credentials_ready


def test_cli_rejects_stage_outside_fixed_production_root():
    with pytest.raises(SystemExit):
        STARTER.main(["--stage", "/tmp/not-production", "--archive-sha", "a" * 64,
                      "--manifest-sha", "b" * 64, "--owner-sha", "c" * 64,
                      "--credentials-sha", "d" * 64])


def test_pre_dispatch_setup_failure_cleans_installed_credentials(tmp_path, monkeypatch):
    stage, archive = make_stage(tmp_path)
    digest, _, owner_sha, credential_sha = hashes(archive)
    harness = Harness(stage)
    def fail_config(*args, **kwargs):
        raise OSError("injected config write failure")
    monkeypatch.setattr(STARTER, "_install_registry_config", fail_config)
    with pytest.raises(STARTER.OwnerStartError):
        start(stage, archive, digest, owner_sha, credential_sha, harness)
    assert not harness.credentials_ready
    assert not any(argv[0].endswith("systemd-run") for argv in harness.calls)
    assert not (stage / "environment.json").exists()
    assert not (stage / ".registry-auth.pending").exists()
    assert not (stage.parent.parent / "exec-workspaces" / stage.name).exists()


def test_preverified_start_script_can_coexist_with_bundle_archive(tmp_path):
    stage, archive = make_stage(tmp_path, bootstrap=True)
    digest, _, owner_sha, credential_sha = hashes(archive)
    harness = Harness(stage)
    assert start(stage, archive, digest, owner_sha, credential_sha, harness) == "STARTED"
    assert (stage / "start.py").read_text() == "# trusted by the caller\n"
