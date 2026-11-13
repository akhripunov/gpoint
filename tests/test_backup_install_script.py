"""Executable tests for the standalone backup installation procedure.

The remote half is executed for real against an isolated ROOT: http:// ssh is a
stub that runs scripts/gpoint-backup-install-remote.sh locally, so idempotent
reconciliation, evidence checks and systemd handling are all exercised without a
production host.
"""

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/gpoint-backup-install.sh"
REMOTE = ROOT / "scripts/gpoint-backup-install-remote.sh"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo, text=True).strip()


def write_manifest(path: Path, completed_at: str, status: str = "PASS") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": status, "completed_at": completed_at}), encoding="utf-8")


@pytest.fixture()
def workspace(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "scripts").mkdir()
    (repo / "systemd").mkdir()
    for name in ("gpoint-scheduled-backup.py", "gpoint-backup-policy.py",
                 "gpoint-media-tree.py", "agata-production-migration.py"):
        (repo / "scripts" / name).write_text("import sys\n\nsys.exit(0)\n", encoding="utf-8")
    (repo / "scripts" / "verify-gpoint-scheduled-backup-restore.sh").write_text(
        "#!/bin/sh\nexit 0\n", encoding="utf-8")
    for name in ("gpoint-backup.service", "gpoint-backup.timer"):
        (repo / "systemd" / name).write_text("[Unit]\n", encoding="utf-8")
    shutil.copy(SCRIPT, repo / "scripts" / "gpoint-backup-install.sh")
    shutil.copy(REMOTE, repo / "scripts" / "gpoint-backup-install-remote.sh")
    (repo / "scripts" / "gpoint-backup-install.sh").chmod(0o755)
    (repo / "scripts" / "gpoint-backup-install-remote.sh").chmod(0o755)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")
    sha = git(repo, "rev-parse", "HEAD")
    bare = tmp_path / "origin.git"
    subprocess.check_output(["git", "init", "-q", "--bare", str(bare)])
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")

    root = tmp_path / "root"
    manifest = root / "opt/gpoint/backups/scheduled/latest.json"
    write_manifest(manifest, "2020-01-01T00:00:00+00:00")
    agekey = root / "opt/gpoint/env/gpoint-backup.agekey"
    agekey.parent.mkdir(parents=True)
    agekey.write_text("AGE-SECRET-KEY-TEST\n", encoding="utf-8")
    agekey.chmod(0o600)
    (root / "etc/systemd/system").mkdir(parents=True)
    (root / "run").mkdir(parents=True)

    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    env = {
        "TEST_ROOT": str(root), "TEST_LOG": str(log), "TEST_MANIFEST": str(manifest),
        "TEST_UID": str(os.getuid()),
    }
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        echo "systemctl $*" >> "{log}"
        case "$1" in
          daemon-reload|is-active) exit 0 ;;
          enable) exit 0 ;;
          show) echo inactive ; exit 0 ;;
          is-enabled) echo enabled ; exit 0 ;;
          start)
            [[ -n "${{SYSTEMCTL_FAIL_START:-}}" ]] && exit 1
            [[ -n "${{MANIFEST_STALE:-}}" ]] && exit 0
            counter="$(cat "$TEST_MANIFEST.counter" 2>/dev/null || echo 0)"
            counter=$((counter + 1))
            echo "$counter" > "$TEST_MANIFEST.counter"
            printf '{{"status":"PASS","completed_at":"%s","run":%s}}\n' \
              "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$counter" > "$TEST_MANIFEST"
            exit 0 ;;
        esac
        exit 0
    """), encoding="utf-8")
    systemctl.chmod(0o755)

    ssh = bin_dir / "ssh"
    ssh.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        echo "ssh $*" >> "{log}"
        stdin="$(mktemp)"
        cat > "$stdin"
        is_install=false
        for token in "$@"; do [[ "$token" == ROOT=* ]] && is_install=true; done
        if [[ "$is_install" != true ]]; then
          # setup call: create the stage under the isolated root
          for token in "$@"; do
            if [[ "$token" == /run/gpoint-backup-install-* ]]; then
              mkdir -p "$TEST_ROOT$token"
            fi
          done
          rm -f "$stdin"
          exit 0
        fi
        stage=""; release=""; archive_sha=""; run_id=""; attempt=""
        for token in "$@"; do
          case "$token" in
            STAGE=*) stage="${{token#*=}}" ;;
            RELEASE=*) release="${{token#*=}}" ;;
            ARCHIVE_SHA=*) archive_sha="${{token#*=}}" ;;
            RUN_ID=*) run_id="${{token#*=}}" ;;
            ATTEMPT=*) attempt="${{token#*=}}" ;;
          esac
        done
        export ROOT="$TEST_ROOT"
        export SYSTEMCTL="{bin_dir}/systemctl"
        export STAGE="$TEST_ROOT$stage"
        export RELEASE="$TEST_ROOT$release"
        export ARCHIVE_SHA="$archive_sha"
        export RUN_ID="$run_id"
        export ATTEMPT="$attempt"
        export EXPECTED_KEY_OWNER="$TEST_UID"
        export SYSTEMCTL_FAIL_START="${{SYSTEMCTL_FAIL_START:-}}"
        export MANIFEST_PATH="$TEST_MANIFEST"
        bash "$stdin"
        status=$?
        rm -f "$stdin"
        exit $status
    """), encoding="utf-8")
    ssh.chmod(0o755)

    scp = bin_dir / "scp"
    scp.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        echo "scp $*" >> "{log}"
        [[ -n "${{SCP_FAIL:-}}" ]] && exit 1
        source_file="$2"
        eval "target=\${{$#}}"
        remote_path="${{target#*:}}"
        mkdir -p "$TEST_ROOT$(dirname "$remote_path")"
        cp "$source_file" "$TEST_ROOT$remote_path"
    """), encoding="utf-8")
    scp.chmod(0o755)
    env["TEST_HOME"] = str(home)
    return repo, sha, bin_dir, log, root, manifest, env


def run(workspace, sha, bin_dir, env, **overrides):
    repo = workspace[0]
    base = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": env["TEST_HOME"],
        "CANDIDATE_SHA": sha,
        "GPOINT_PRODUCTION_HOST": "gpoint.test",
        "RUN_ID": "1700000000",
        "ATTEMPT": "1",
        **env,
    }
    base.update(overrides)
    for key in [k for k, v in base.items() if v is None]:
        base.pop(key)
    return subprocess.run(
        ["bash", "scripts/gpoint-backup-install.sh"], cwd=repo, text=True,
        capture_output=True, env=base)


def test_success_executes_the_real_remote_script(workspace):
    repo, sha, bin_dir, log, root, manifest, env = workspace
    result = run(workspace, sha, bin_dir, env, APPROVE_PRODUCTION="true")
    assert result.returncode == 0, result.stderr
    assert "GPOINT_BACKUP_INSTALL=PASS" in result.stdout
    release = root / "opt/gpoint/backup-releases" / sha
    assert (release / "PAYLOAD.sha256").is_file()
    assert (root / "opt/gpoint/gpoint-backup-current").is_symlink()
    assert (root / "etc/systemd/system/gpoint-backup.timer").is_file()
    assert "systemctl enable --now gpoint-backup.timer" in log.read_text()
    assert "GPOINT_VPS_BACKUP_TIMER=PASS" in result.stdout
    assert json.loads(manifest.read_text())["completed_at"] > "2020"


def test_repeated_run_reuses_the_installed_release(workspace):
    repo, sha, bin_dir, log, root, manifest, env = workspace
    first = run(workspace, sha, bin_dir, env, APPROVE_PRODUCTION="true")
    assert first.returncode == 0, first.stderr
    stamp = root / "opt/gpoint/backup-releases" / sha / "PAYLOAD.sha256"
    stamp.write_text(stamp.read_text() + "-marker")
    second = run(workspace, sha, bin_dir, env, APPROVE_PRODUCTION="true", RUN_ID="1700000002")
    assert second.returncode != 0
    assert "differs from this payload" in second.stderr
    assert stamp.read_text().endswith("-marker")


def test_conflicting_existing_release_is_never_overwritten(workspace):
    repo, sha, bin_dir, log, root, manifest, env = workspace
    first = run(workspace, sha, bin_dir, env, APPROVE_PRODUCTION="true")
    assert first.returncode == 0, first.stderr
    stamp = root / "opt/gpoint/backup-releases" / sha / "PAYLOAD.sha256"
    original = stamp.read_text()
    (root / "opt/gpoint/backup-releases" / sha / "keep.me").write_text("recovery", encoding="utf-8")
    second = run(workspace, sha, bin_dir, env, APPROVE_PRODUCTION="true", RUN_ID="1700000003")
    assert second.returncode == 0, second.stderr
    assert stamp.read_text() == original
    assert (root / "opt/gpoint/backup-releases" / sha / "keep.me").read_text() == "recovery"


def test_missing_owner_approval_blocks_before_any_remote_call(workspace):
    repo, sha, bin_dir, log, root, manifest, env = workspace
    result = run(workspace, sha, bin_dir, env)
    assert result.returncode != 0
    assert not log.exists() or log.read_text() == ""


def test_wrong_sha_blocks_before_any_remote_call(workspace):
    repo, sha, bin_dir, log, root, manifest, env = workspace
    result = run(workspace, sha, bin_dir, env, APPROVE_PRODUCTION="true", CANDIDATE_SHA="0" * 40)
    assert result.returncode != 0
    assert not log.exists() or log.read_text() == ""


def test_transfer_failure_stops_before_install(workspace):
    repo, sha, bin_dir, log, root, manifest, env = workspace
    result = run(workspace, sha, bin_dir, env, APPROVE_PRODUCTION="true", SCP_FAIL="1")
    assert result.returncode != 0
    assert log.read_text().count("ssh ") == 1


def test_remote_install_failure_is_reported(workspace):
    repo, sha, bin_dir, log, root, manifest, env = workspace
    result = run(workspace, sha, bin_dir, env, APPROVE_PRODUCTION="true", SYSTEMCTL_FAIL_START="1")
    assert result.returncode != 0
    assert "GPOINT_BACKUP_INSTALL=PASS" not in result.stdout


def test_backup_predating_the_installation_is_rejected(workspace):
    repo, sha, bin_dir, log, root, manifest, env = workspace
    # systemctl start succeeds but produces no new snapshot: the old manifest stays
    result = run(workspace, sha, bin_dir, env, APPROVE_PRODUCTION="true", MANIFEST_STALE="1")
    assert result.returncode != 0
    assert "backup manifest did not change" in result.stderr
