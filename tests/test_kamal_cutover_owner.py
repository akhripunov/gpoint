import hashlib
import importlib.util
import json
import os
import subprocess
import shutil
import sys
import threading
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("kamal_cutover_owner", ROOT / "scripts/kamal_cutover_owner.py")
OWNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OWNER)
PACKAGE_SPEC = importlib.util.spec_from_file_location("package_kamal_cutover", ROOT / "scripts/package-kamal-cutover.py")
PACKAGE = importlib.util.module_from_spec(PACKAGE_SPEC)
PACKAGE_SPEC.loader.exec_module(PACKAGE)

SHA = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD^{commit}"], text=True).strip()
OTHER_SHA = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD^{commit}^"], text=True).strip()
RUN = "12345"
ATTEMPT = "1"
PHASES = ["pause", "retire", "requests", "routes", "consumers", "verify"]


def bundle(tmp_path, *, fail=None, slow=None, attempt=ATTEMPT, sha=SHA, git_sha=None, run_id=RUN):
    stage = tmp_path / "gpoint-kamal-owner" / f"{sha}-{run_id}-{attempt}"
    repo = stage / "repo"
    repo.mkdir(parents=True)
    (stage / "phases").mkdir()
    log = stage / "events.log"
    files = {}
    for name in PHASES:
        script = stage / "phases" / f"{name}.sh"
        code = f"#!/bin/bash\nset -eu\nprintf '%s\\n' '{name}:start' >> '{log}'\n"
        if slow == name:
            code += "sleep 0.35\n"
        code += f"printf '%s\\n' '{name}:end' >> '{log}'\n"
        if fail == name:
            code += "exit 17\n"
        script.write_text(code)
        script.chmod(0o700)
        files[f"phases/{name}.sh"] = hashlib.sha256(script.read_bytes()).hexdigest()
    for name in ("recover-pre", "recover-post"):
        script = stage / "phases" / f"{name}.sh"
        script.write_text(f"#!/bin/bash\nset -eu\nprintf '%s\\n' '{name}' >> '{log}'\n")
        script.chmod(0o700)
        files[f"phases/{name}.sh"] = hashlib.sha256(script.read_bytes()).hexdigest()
    source_sha = git_sha or sha
    if source_sha == SHA:
        git_entries = PACKAGE._git_workspace_entries(ROOT, source_sha)
    else:
        with tempfile.TemporaryDirectory(prefix="kamal-owner-git-fixture-") as temporary:
            checkout = Path(temporary) / "source"
            subprocess.run(["git", "clone", "--shared", "--quiet", "--no-checkout", str(ROOT), str(checkout)], check=True)
            subprocess.run(["git", "checkout", "--quiet", "--detach", source_sha], cwd=checkout, check=True)
            git_entries = PACKAGE._git_workspace_entries(checkout, source_sha)
    for name, (data, mode) in git_entries.items():
        path = stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        files[name] = hashlib.sha256(data).hexdigest()
    for directory in sorted((repo / ".git").rglob("*"), reverse=True):
        if directory.is_dir():
            directory.chmod(0o700)
    (repo / ".git").chmod(0o700)
    env_file = stage / "environment.json"
    env_file.write_text(json.dumps({"OWNER_TEST_VALUE": "private"}))
    env_file.chmod(0o600)
    files["environment.json"] = hashlib.sha256(env_file.read_bytes()).hexdigest()
    manifest = {"schema": "gpoint-kamal-owner/v1", "candidateSha": sha,
                "runId": run_id, "attempt": attempt, "files": files,
                "environmentFile": "environment.json"}
    manifest_path = stage / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    manifest_path.chmod(0o600)
    for directory in (stage.parent, stage):
        directory.chmod(0o700)
    return stage, hashlib.sha256(manifest_path.read_bytes()).hexdigest(), log


def run(stage, digest, tmp_path, uid=None):
    return OWNER.run_owner(stage, digest, owner_uid=os.getuid() if uid is None else uid,
                           lock_path=tmp_path / "owner.lock")


def test_runs_phases_serially_and_only_verify_success_is_complete(tmp_path):
    stage, digest, log = bundle(tmp_path)
    assert run(stage, digest, tmp_path) == 0
    assert log.read_text().splitlines() == [f"{p}:{edge}" for p in PHASES for edge in ("start", "end")]
    result = json.loads((stage / "owner-result.json").read_text())
    assert (result["state"], result["phase"], result["candidateSha"]) == ("COMPLETE", "verify", SHA)
    assert (stage / "owner-result.json").stat().st_mode & 0o777 == 0o600


def test_pause_failure_runs_only_pre_recovery_after_child_exits(tmp_path):
    stage, digest, log = bundle(tmp_path, fail="pause")
    assert run(stage, digest, tmp_path) == 1
    assert log.read_text().splitlines() == ["pause:start", "pause:end", "recover-pre"]
    result = json.loads((stage / "owner-result.json").read_text())
    assert (result["state"], result["recovery"]) == ("RECOVERED", "pre")


def test_retirement_or_later_failure_runs_only_post_recovery_after_child_exits(tmp_path):
    for failing_phase in ("retire", "routes", "verify"):
        case = tmp_path / failing_phase
        case.mkdir()
        stage, digest, log = bundle(case, fail=failing_phase)
        assert run(stage, digest, case) == 1
        lines = log.read_text().splitlines()
        assert lines[-2:] == [f"{failing_phase}:end", "recover-post"]
        assert lines.count("recover-post") == 1 and "recover-pre" not in lines


def test_recovery_failure_is_blocked_and_nonzero(tmp_path):
    stage, digest, log = bundle(tmp_path, fail="pause")
    recovery = stage / "phases/recover-pre.sh"
    recovery.write_text("#!/bin/bash\nprintf 'recovery-failed\\n' >> '" + str(log) + "'\nexit 9\n")
    recovery.chmod(0o700)
    manifest = json.loads((stage / "manifest.json").read_text())
    manifest["files"]["phases/recover-pre.sh"] = hashlib.sha256(recovery.read_bytes()).hexdigest()
    raw = json.dumps(manifest, sort_keys=True).encode()
    (stage / "manifest.json").write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    assert run(stage, digest, tmp_path) == 2
    result = json.loads((stage / "owner-result.json").read_text())
    assert result["state"] == "BLOCKED"
    assert log.read_text().splitlines()[-1] == "recovery-failed"


def test_tampered_script_rejected_before_any_phase_mutation(tmp_path):
    stage, digest, log = bundle(tmp_path)
    (stage / "phases/pause.sh").write_text("#!/bin/bash\ntouch '" + str(log) + "'\n")
    assert run(stage, digest, tmp_path) == 2
    assert not log.exists()


def test_wrong_owner_identity_rejected_before_mutation(tmp_path):
    stage, digest, log = bundle(tmp_path)
    assert run(stage, digest, tmp_path, uid=os.getuid() + 1) == 2
    assert not log.exists()


def test_attempt_symlink_is_rejected(tmp_path):
    stage, digest, log = bundle(tmp_path)
    real = stage
    alias = stage.parent / "alias"
    alias.symlink_to(real, target_is_directory=True)
    assert run(alias, digest, tmp_path) == 2
    assert not log.exists()


def test_terminal_attempt_cannot_be_replayed(tmp_path):
    stage, digest, log = bundle(tmp_path)
    assert run(stage, digest, tmp_path) == 0
    count = len(log.read_text().splitlines())
    assert run(stage, digest, tmp_path) == 2
    assert len(log.read_text().splitlines()) == count


def test_missing_git_workspace_is_rejected_before_pause_phase(tmp_path):
    stage, digest, log = bundle(tmp_path)
    shutil.rmtree(stage / "repo" / ".git")
    assert run(stage, digest, tmp_path) == 2
    assert not log.exists(), "Compose pause must not run without a verified Git workspace"


def test_mismatched_git_head_is_rejected_before_pause_phase(tmp_path):
    stage, digest, log = bundle(tmp_path, git_sha=OTHER_SHA)
    assert run(stage, digest, tmp_path) == 2
    assert not log.exists(), "Compose pause must not run when Git HEAD differs from candidate"


def test_duplicate_owner_is_rejected_while_first_child_runs(tmp_path):
    stage, digest, log = bundle(tmp_path, slow="pause")
    lock = tmp_path / "owner.lock"
    outcome = []
    first = threading.Thread(target=lambda: outcome.append(OWNER.run_owner(stage, digest, os.getuid(), lock)))
    first.start()
    deadline = time.monotonic() + 2
    while not log.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert log.exists()
    assert OWNER.run_owner(stage, digest, os.getuid(), lock) == 2
    first.join(timeout=4)
    assert not first.is_alive()
    assert outcome == [0]


def test_cli_requires_fixed_production_stage_shape():
    with pytest.raises(SystemExit):
        OWNER.main(["--stage", "/tmp/arbitrary", "--manifest-sha", "a" * 64])


def test_bundle_files_require_expected_owner_and_non_writable_modes(tmp_path):
    stage, digest, log = bundle(tmp_path)
    phase = stage / "phases/pause.sh"
    phase.chmod(0o766)
    assert run(stage, digest, tmp_path) == 2
    assert not log.exists()


def test_manifest_and_environment_must_be_private_0600(tmp_path):
    stage, digest, log = bundle(tmp_path)
    (stage / "environment.json").chmod(0o640)
    assert run(stage, digest, tmp_path) == 2
    assert not log.exists()

def test_owner_crash_leaves_live_child_and_blocks_sibling_attempt(tmp_path):
    stage, digest, log = bundle(tmp_path)
    started = tmp_path / "child-started.marker"
    marker = tmp_path / "child-finished.marker"
    pause = stage / "phases/pause.sh"
    pause.write_text("#!/bin/bash\ntouch '" + str(started) + "'\nsleep 1\ntouch '" + str(marker) + "'\n")
    pause.chmod(0o700)
    manifest = json.loads((stage / "manifest.json").read_text())
    manifest["files"]["phases/pause.sh"] = hashlib.sha256(pause.read_bytes()).hexdigest()
    raw = json.dumps(manifest, sort_keys=True).encode()
    (stage / "manifest.json").write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    script = (
        "import importlib.util,sys; "
        f"p={str(ROOT / 'scripts/kamal_cutover_owner.py')!r}; "
        "s=importlib.util.spec_from_file_location('owner',p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        f"sys.exit(m.run_owner(__import__('pathlib').Path({str(stage)!r}), {digest!r}, __import__('os').getuid(), __import__('pathlib').Path({str(tmp_path / 'owner.lock')!r})))"
    )
    owner = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 3
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started.exists(), "owner must start the phase child before it is terminated"
    owner.kill()
    owner.wait(timeout=3)
    sibling, sibling_digest, _ = bundle(tmp_path, attempt="2")
    assert run(sibling, sibling_digest, tmp_path) == 2
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists(), "killing the owner must not kill or roll back its running child"
    assert json.loads((stage / "owner-status.json").read_text())["state"] == "RUNNING"


@pytest.mark.parametrize("sha,run_id", [(SHA, "12346"), (OTHER_SHA, RUN)])
def test_nonterminal_attempt_blocks_new_run_or_candidate(tmp_path, sha, run_id):
    prior, _, _ = bundle(tmp_path)
    (prior / "owner-status.json").write_text(json.dumps({
        "schema": "gpoint-kamal-owner/v1", "candidateSha": SHA, "runId": RUN,
        "attempt": ATTEMPT, "state": "RUNNING", "phase": "pause",
        "exitCode": None, "recovery": "none",
    }))
    (prior / "owner-status.json").chmod(0o600)
    sibling, sibling_digest, marker = bundle(tmp_path, sha=sha, run_id=run_id)
    assert run(sibling, sibling_digest, tmp_path) == 2
    assert not marker.exists()


def test_terminal_attempt_is_safe_history_for_new_candidate(tmp_path):
    prior, prior_digest, _ = bundle(tmp_path)
    assert run(prior, prior_digest, tmp_path) == 0
    sibling, sibling_digest, _ = bundle(tmp_path, sha=OTHER_SHA)
    assert run(sibling, sibling_digest, tmp_path) == 0
