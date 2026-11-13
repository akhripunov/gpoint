"""Executable tests for the standalone backup installation procedure.

The procedure runs on the operator workstation, not inside GitHub Actions, so it
must not depend on GITHUB_* variables. These tests execute the real script with
stub ssh/scp in an isolated repository and assert both the happy path and the
fail-closed paths. No production host is contacted.
"""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/gpoint-backup-install.sh"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo, text=True).strip()


@pytest.fixture()
def workspace(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "scripts").mkdir()
    (repo / "systemd").mkdir()
    for name in ("gpoint-scheduled-backup.py", "gpoint-backup-policy.py",
                 "gpoint-media-tree.py", "agata-production-migration.py",
                 "verify-gpoint-scheduled-backup-restore.sh"):
        (repo / "scripts" / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for name in ("gpoint-backup.service", "gpoint-backup.timer"):
        (repo / "systemd" / name).write_text("[Unit]\n", encoding="utf-8")
    shutil.copy(SCRIPT, repo / "scripts" / "gpoint-backup-install.sh")
    (repo / "scripts" / "gpoint-backup-install.sh").chmod(0o755)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")
    sha = git(repo, "rev-parse", "HEAD")

    bare = tmp_path / "origin.git"
    subprocess.check_output(["git", "init", "-q", "--bare", str(bare)])
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    ssh = bin_dir / "ssh"
    ssh.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        echo "ssh $*" >> "{log}"
        count=$(grep -c '^ssh ' "{log}")
        cat >/dev/null
        [[ -n "${{SSH_FAIL_AT:-}}" && "$count" = "$SSH_FAIL_AT" ]] && exit 1
        exit 0
    """), encoding="utf-8")
    scp = bin_dir / "scp"
    scp.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        echo "scp $*" >> "{log}"
        [[ -n "${{SCP_FAIL:-}}" ]] && exit 1
        exit 0
    """), encoding="utf-8")
    for item in (ssh, scp):
        item.chmod(0o755)
    return repo, sha, bin_dir, log


def run(workspace, sha, bin_dir, **env):
    repo, _, _, _ = workspace
    base = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(repo),
        "CANDIDATE_SHA": sha,
        "GPOINT_PRODUCTION_HOST": "gpoint.test",
        "RUN_ID": "1700000000",
        "ATTEMPT": "1",
    }
    base.update(env)
    for key in [k for k, v in base.items() if v is None]:
        base.pop(key)
    return subprocess.run(
        ["bash", "scripts/gpoint-backup-install.sh"], cwd=repo, text=True,
        capture_output=True, env=base)


def test_success_without_any_github_variable(workspace):
    repo, sha, bin_dir, log = workspace
    result = run(workspace, sha, bin_dir, APPROVE_PRODUCTION="true")
    assert result.returncode == 0, result.stderr
    assert "GPOINT_BACKUP_INSTALL=PASS" in result.stdout
    calls = log.read_text()
    assert calls.count("ssh ") == 2
    assert calls.count("scp ") == 1
    assert "/run/gpoint-backup-install-1700000000-1" in calls


def test_missing_owner_approval_blocks_before_any_remote_call(workspace):
    repo, sha, bin_dir, log = workspace
    result = run(workspace, sha, bin_dir)
    assert result.returncode != 0
    assert not log.exists() or log.read_text() == ""


def test_wrong_sha_blocks_before_any_remote_call(workspace):
    repo, sha, bin_dir, log = workspace
    result = run(workspace, sha, bin_dir, APPROVE_PRODUCTION="true",
                 CANDIDATE_SHA="0" * 40)
    assert result.returncode != 0
    assert not log.exists() or log.read_text() == ""


def test_dirty_worktree_blocks(workspace):
    repo, sha, bin_dir, log = workspace
    (repo / "dirty.txt").write_text("x\n", encoding="utf-8")
    result = run(workspace, sha, bin_dir, APPROVE_PRODUCTION="true")
    assert result.returncode != 0
    assert not log.exists() or log.read_text() == ""


def test_transfer_failure_stops_before_install(workspace):
    repo, sha, bin_dir, log = workspace
    result = run(workspace, sha, bin_dir, APPROVE_PRODUCTION="true", SCP_FAIL="1")
    assert result.returncode != 0
    assert log.read_text().count("ssh ") == 1


def test_remote_install_failure_is_reported(workspace):
    repo, sha, bin_dir, log = workspace
    result = run(workspace, sha, bin_dir, APPROVE_PRODUCTION="true", SSH_FAIL_AT="2")
    assert result.returncode != 0
    assert "GPOINT_BACKUP_INSTALL=PASS" not in result.stdout


def test_rerun_after_partial_failure_succeeds_with_a_fresh_run_id(workspace):
    repo, sha, bin_dir, log = workspace
    failed = run(workspace, sha, bin_dir, APPROVE_PRODUCTION="true", SSH_FAIL_AT="2")
    assert failed.returncode != 0
    retried = run(workspace, sha, bin_dir, APPROVE_PRODUCTION="true", RUN_ID="1700000001")
    assert retried.returncode == 0, retried.stderr
    calls = log.read_text()
    assert "/run/gpoint-backup-install-1700000001-1" in calls


def test_procedure_proves_the_first_backup_belongs_to_this_install():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'test /opt/gpoint/backups/scheduled/latest.json -nt "$stage"' in text
    assert 'test /opt/gpoint/backups/scheduled/latest.json -nt /opt/gpoint/gpoint-backup-current' in text
    assert "GPOINT_BACKUP_INSTALL=PASS" in text
