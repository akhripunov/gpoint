#!/usr/bin/env python3
"""Opt-in real Docker/SSH observer-death injection for the cutover owner.

Run only with --run-local-isolated. The disposable owner container mounts the
local Docker socket so it can mutate app fixtures, which are guarded by a
fresh random label and exact IDs. App fixtures use --network none and no mounts.
Building the SSH fixture fetches Debian package lists and openssh-server.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import secrets
import selectors
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OWNER_SOURCE = ROOT / "scripts/kamal_cutover_owner.py"
PACKAGE_SOURCE = ROOT / "scripts/package-kamal-cutover.py"
FIXTURE = ROOT / "tests/fixtures/kamal-owner-ssh"
DOCKERFILE = FIXTURE / "Dockerfile"
PYTHON_IMAGE = "sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704"
DOCKER_CLI_IMAGE = "sha256:9e79b879b177fbd72c34376a3dd3d91747da5db49e1f560f76aea59561471977"
PHASES = ("pause", "retire", "requests", "routes", "consumers", "verify")
LEGACY = ("api", "worker", "bot", "agata-api", "agata-memory-worker")
STAGE_ROOT = Path("/run/gpoint-kamal-owner")

PACKAGE_SPEC = importlib.util.spec_from_file_location("package_kamal_cutover", PACKAGE_SOURCE)
PACKAGE = importlib.util.module_from_spec(PACKAGE_SPEC)
assert PACKAGE_SPEC and PACKAGE_SPEC.loader
PACKAGE_SPEC.loader.exec_module(PACKAGE)
REVISION = subprocess.check_output(
    ["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD^{commit}"], text=True
).strip()


class HarnessError(RuntimeError):
    pass


def run(argv: list[str], *, check: bool = True, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False, timeout=timeout)
    if check and result.returncode:
        raise HarnessError(f"command failed ({result.returncode}): {argv[0]} {argv[1:]}\n{result.stderr.strip()}")
    return result


def docker(*args: str, check: bool = True, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return run(["docker", *args], check=check, timeout=timeout)


def require_image(ref: str, image_id: str) -> None:
    actual = docker("image", "inspect", ref, "--format", "{{.Id}}").stdout.strip()
    if actual != image_id:
        raise HarnessError(f"unexpected local image ID for {ref}: {actual}")


def docker_socket() -> Path:
    endpoint = docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}").stdout.strip()
    if not endpoint.startswith("unix://"):
        raise HarnessError("only a local Unix Docker context is supported")
    path = Path(endpoint.removeprefix("unix://")).resolve(strict=True)
    if not stat.S_ISSOCK(path.stat().st_mode):
        raise HarnessError("Docker endpoint is not a Unix socket")
    return path


def inspect(identifier: str) -> dict[str, Any]:
    rows = json.loads(docker("inspect", identifier).stdout)
    if not isinstance(rows, list) or len(rows) != 1:
        raise HarnessError("Docker returned an ambiguous container identity")
    return rows[0]


def make_app_fixtures(token: str) -> list[str]:
    ids: list[str] = []
    for role in LEGACY:
        name = f"gpoint-test-owner-{token}-{role}"
        cid = docker("run", "--detach", "--name", name,
                     "--label", f"gpoint-test-owner={token}",
                     "--label", f"gpoint-test-role=legacy-{role}",
                     "--network", "none", "--entrypoint", "python3", PYTHON_IMAGE,
                     "-c", "import time; time.sleep(3600)").stdout.strip()
        row = inspect(cid)
        labels = row["Config"].get("Labels") or {}
        if (row["Id"] != cid or row["Image"] != PYTHON_IMAGE
                or row["State"].get("Running") is not True
                or row["HostConfig"].get("NetworkMode") != "none" or row.get("Mounts") != []
                or labels.get("gpoint-test-owner") != token
                or labels.get("gpoint-test-role") != f"legacy-{role}"):
            raise HarnessError("created app fixture is not an isolated expected container")
        ids.append(cid)
    return ids


def write_bundle(root: Path, stage_name: str, token: str, ids: list[str],
                 *, fail_stop: int | None, post_handoff: bool, candidate_name: str) -> str:
    phase_dir, repo_dir = root / "phases", root / "repo"
    phase_dir.mkdir(parents=True)
    repo_dir.mkdir()
    git_entries = PACKAGE._git_workspace_entries(ROOT, REVISION)
    for relative, (contents, mode) in git_entries.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        path.chmod(mode)
    for path in sorted((repo_dir / ".git").rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o700)
    (repo_dir / ".git").chmod(0o700)
    qids = " ".join(shlex.quote(cid) for cid in ids)
    qpairs = " ".join(shlex.quote(f"{role}:{cid}") for role, cid in zip(LEGACY, ids))
    check = (
        "check_identity() { local id=$1 role=$2; "
        f"[[ $(docker inspect --format '{{{{ index .Config.Labels \"gpoint-test-owner\" }}}}' \"$id\") == {token} ]]; "
        "[[ $(docker inspect --format '{{ index .Config.Labels \"gpoint-test-role\" }}' \"$id\") == \"$role\" ]]; }\n"
    )
    pause = check + f"ids=({qids})\nroles=({' '.join('legacy-' + r for r in LEGACY)})\n"
    pause += "trap 'printf \"pause-process-exited\\n\"' EXIT\nprintf 'pause-started\\n'\n"
    pause += "for i in \"${!ids[@]}\"; do\n id=${ids[$i]}; check_identity \"$id\" \"${roles[$i]}\"\n"
    pause += " docker stop --time 0 \"$id\" >/dev/null\n printf 'legacy-stopped:%d:%s\\n' \"$((i+1))\" \"$id\"\n"
    if fail_stop is not None:
        pause += f" if (( i+1 == {fail_stop} )); then sleep 1; exit 17; fi\n"
    pause += "done\n"
    retire = "printf 'caddy-handoff-complete:test-fixture-only\\n'\n"
    requests = "printf 'requests-noop:test-fixture-only\\n'\n"
    if post_handoff:
        requests = (
            f"docker run --detach --name {shlex.quote(candidate_name)} "
            f"--label {shlex.quote('gpoint-test-owner=' + token)} --label gpoint-test-role=candidate "
            f"--network none --entrypoint python3 {PYTHON_IMAGE} -c 'import time; time.sleep(3600)' >/dev/null\n"
            f"printf 'candidate-started:{candidate_name}\\n'\ntrap 'printf \"requests-process-exited\\n\"' EXIT\nsleep 1\nexit 17\n"
        )
    sources = {
        "pause": pause,
        "retire": retire,
        "requests": requests,
        "routes": "printf 'routes-noop:test-fixture-only\\n'",
        "consumers": "printf 'consumers-noop:test-fixture-only\\n'",
        "verify": "printf 'verify-noop:test-fixture-only\\n'",
    }
    for phase, body in sources.items():
        path = phase_dir / f"{phase}.sh"
        path.write_text("#!/bin/bash\nset -euo pipefail\n" + body + "\n")
        path.chmod(0o700)
    pre = check + "printf 'recover-pre-invoked\\n'\n"
    pre += f"for pair in {qpairs}; do role=${{pair%%:*}}; id=${{pair#*:}}; check_identity \"$id\" \"legacy-$role\"; "
    pre += "if [[ $(docker inspect --format '{{.State.Running}}' \"$id\") != true ]]; then docker start \"$id\" >/dev/null; fi; "
    pre += "printf 'legacy-restored:%s:%s\\n' \"$role\" \"$id\"; done\n"
    post = check + "printf 'recover-post-invoked\\n'\n"
    post += f"candidate={shlex.quote(candidate_name)}; check_identity \"$candidate\" candidate; "
    post += "docker stop --time 0 \"$candidate\" >/dev/null; "
    post += "[[ $(docker inspect --format '{{.State.Running}}' \"$candidate\") == false ]]; printf 'candidate-stopped\\n'\n"
    post += f"for pair in {qpairs}; do role=${{pair%%:*}}; id=${{pair#*:}}; check_identity \"$id\" \"legacy-$role\"; "
    post += "if [[ $(docker inspect --format '{{.State.Running}}' \"$id\") != true ]]; then docker start \"$id\" >/dev/null; fi; "
    post += "printf 'legacy-restored:%s:%s\\n' \"$role\" \"$id\"; done\n"
    for name, body in (("recover-pre", pre), ("recover-post", post)):
        path = phase_dir / f"{name}.sh"
        path.write_text("#!/bin/bash\nset -euo pipefail\n" + body + "\n")
        path.chmod(0o700)
    env_bytes = (json.dumps({"OWNER_TEST_TOKEN": token}, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (root / "environment.json").write_bytes(env_bytes)
    (root / "environment.json").chmod(0o600)
    file_hashes = {f"phases/{p.name}": hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(phase_dir.iterdir())}
    file_hashes.update({relative: hashlib.sha256(contents).hexdigest()
                        for relative, (contents, _mode) in git_entries.items()})
    file_hashes["environment.json"] = hashlib.sha256(env_bytes).hexdigest()
    sha, run_id, attempt = stage_name.split("-")
    manifest = {"schema": "gpoint-kamal-owner/v1", "candidateSha": sha,
                "runId": run_id, "attempt": attempt, "files": file_hashes,
                "environmentFile": "environment.json"}
    raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (root / "manifest.json").write_bytes(raw)
    (root / "manifest.json").chmod(0o600)
    for path in (root, phase_dir, repo_dir):
        path.chmod(0o700)
    return hashlib.sha256(raw).hexdigest()


def docker_exec(server: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return docker("exec", server, *args, check=check)


def stage_in_container(server: str, root: Path, stage_name: str) -> str:
    stage = STAGE_ROOT / stage_name
    docker_exec(server, "mkdir", "-p", str(stage))
    docker_exec(server, "chmod", "0700", str(STAGE_ROOT), str(stage))
    docker("cp", str(root) + "/.", f"{server}:{stage}/")
    docker_exec(server, "chown", "-R", "0:0", str(stage))
    docker_exec(server, "chmod", "0700", str(stage), str(stage / "repo"), str(stage / "phases"))
    docker_exec(server, "chmod", "0600", str(stage / "manifest.json"), str(stage / "environment.json"))
    docker_exec(server, "chmod", "0700", *[str(stage / "phases" / f"{p}.sh") for p in (*PHASES, "recover-pre", "recover-post")])
    return str(stage)


def ssh_base(key: Path, known_hosts: Path, port: int) -> list[str]:
    return ["ssh", "-T", "-i", str(key), "-p", str(port),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=4"]


def wait_ssh(key: Path, known_hosts: Path, port: int) -> None:
    deadline = time.monotonic() + 20
    last_error = ""
    while time.monotonic() < deadline:
        result = run([*ssh_base(key, known_hosts, port), "root@127.0.0.1", "true"], check=False, timeout=6)
        if result.returncode == 0:
            return
        last_error = (result.stderr or result.stdout).strip()
        time.sleep(0.25)
    raise HarnessError(f"disposable localhost SSH fixture did not become ready: {last_error}")


def observer(key: Path, known_hosts: Path, port: int, stage: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen([*ssh_base(key, known_hosts, port), "root@127.0.0.1",
                             f"tail -n +1 -F {shlex.quote(stage + '/owner.log')}"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0)


def kill_observer_on(observer_process: subprocess.Popen[bytes], marker: str) -> None:
    if observer_process.stdout is None:
        raise HarnessError("SSH observer has no output stream")
    selector = selectors.DefaultSelector()
    selector.register(observer_process.stdout, selectors.EVENT_READ)
    found = False
    deadline = time.monotonic() + 45
    pending = b""
    try:
        while time.monotonic() < deadline:
            if observer_process.poll() is not None:
                raise HarnessError(f"SSH observer exited before observing {marker}")
            if not selector.select(timeout=0.2):
                continue
            pending += observer_process.stdout.read(4096)
            if marker.encode() in pending:
                found = True
                break
    finally:
        selector.close()
    if not found:
        raise HarnessError(f"SSH observer never saw owner event {marker}")
    observer_process.kill()
    observer_process.wait(timeout=5)
    if observer_process.returncode is None:
        raise HarnessError("SSH observer process did not die")


def status_result(server: str, stage: str) -> dict[str, Any] | None:
    result = docker_exec(server, "cat", f"{stage}/owner-result.json", check=False)
    if result.returncode:
        return None
    value = json.loads(result.stdout)
    return value if isinstance(value, dict) else None


def wait_result(server: str, stage: str) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = status_result(server, stage)
        if result and result.get("state") in ("RECOVERED", "COMPLETE", "BLOCKED"):
            return result
        time.sleep(0.1)
    log = docker_exec(server, "cat", f"{stage}/owner.log", check=False)
    raise HarnessError(f"owner failed to finish; log follows:\n{log.stdout}\n{log.stderr}")


def token_containers(token: str) -> list[str]:
    out = docker("ps", "--all", "--quiet", "--no-trunc", "--filter", f"label=gpoint-test-owner={token}").stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def assert_recovered_fixtures(token: str, ids: list[str], candidate_name: str | None) -> None:
    rows = [inspect(cid) for cid in token_containers(token)]
    for row in rows:
        labels = row["Config"].get("Labels") or {}
        name = row.get("Name", "").lstrip("/")
        if labels.get("gpoint-test-owner") != token or not name.startswith(f"gpoint-test-owner-{token}-"):
            raise HarnessError("test token selected a container outside its exact prefix")
        if row["HostConfig"].get("NetworkMode") != "none" or row.get("Mounts") != []:
            raise HarnessError("application test container has a network or a mount")
    expected_ids = set(ids)
    if candidate_name:
        candidate = inspect(candidate_name)
        expected_ids.add(candidate["Id"])
    if {row["Id"] for row in rows} != expected_ids:
        raise HarnessError("test token owns unexpected app container identities")
    by_role = {(row["Config"].get("Labels") or {}).get("gpoint-test-role"): row for row in rows}
    expected_roles = {f"legacy-{role}" for role in LEGACY}
    if candidate_name:
        expected_roles.add("candidate")
    if set(by_role) != expected_roles:
        raise HarnessError("test fixture consumer roles are ambiguous")
    for role, identifier in zip(LEGACY, ids):
        row = by_role[f"legacy-{role}"]
        if row["Id"] != identifier or row["Image"] != PYTHON_IMAGE or row["State"].get("Running") is not True:
            raise HarnessError(f"legacy {role} was not restored with its original ID and image")
    if candidate_name:
        row = by_role["candidate"]
        if row["Id"] != inspect(candidate_name)["Id"] or row["State"].get("Running") is not False:
            raise HarnessError("candidate consumer remains running")


def run_case(server: str, key: Path, known: Path, port: int, number: int,
             *, stop_number: int | None = None, post_handoff: bool = False) -> None:
    token = secrets.token_hex(12)
    candidate_name = f"gpoint-test-owner-{token}-candidate"
    stage_name = f"{REVISION}-{1700000000 + number}-1"
    app_ids: list[str] = []
    observer_process: subprocess.Popen[bytes] | None = None
    host_root: Path | None = None
    stage = str(STAGE_ROOT / stage_name)
    try:
        app_ids = make_app_fixtures(token)
        with tempfile.TemporaryDirectory(prefix="gpoint-owner-bundle-") as parent:
            host_root = Path(parent) / stage_name
            host_root.mkdir(mode=0o700)
            digest = write_bundle(host_root, stage_name, token, app_ids,
                                  fail_stop=None if post_handoff else stop_number,
                                  post_handoff=post_handoff, candidate_name=candidate_name)
            stage = stage_in_container(server, host_root, stage_name)
        validation = (
            "import importlib.util,pathlib; "
            "s=importlib.util.spec_from_file_location('owner','/opt/gpoint-test/kamal_cutover_owner.py'); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            f"m._load_bundle(pathlib.Path({stage!r}),{digest!r},0)"
        )
        checked = docker_exec(server, "python3", "-c", validation, check=False)
        if checked.returncode:
            raise HarnessError(f"disposable owner bundle validation failed: {checked.stderr.strip()}")
        owner_code = (
            "import importlib.util,pathlib,sys; "
            "s=importlib.util.spec_from_file_location('owner','/opt/gpoint-test/kamal_cutover_owner.py'); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            f"sys.exit(m.run_owner(pathlib.Path({stage!r}),{digest!r},owner_uid=0,"
            "lock_path=pathlib.Path('/tmp/gpoint-kamal-owner.lock')))"
        )
        detach = "nohup setsid python3 -c " + shlex.quote(owner_code)
        detach += f" >/run/gpoint-kamal-owner/{stage_name}/owner-launch.log 2>&1 </dev/null & echo $!"
        run([*ssh_base(key, known, port), "root@127.0.0.1", detach], timeout=15)
        observer_process = observer(key, known, port, stage)
        marker = f"candidate-started:{candidate_name}" if post_handoff else f"legacy-stopped:{stop_number}:"
        try:
            kill_observer_on(observer_process, marker)
        except HarnessError:
            for diagnostic in ("owner-launch.log", "owner.log", "owner-status.json"):
                output = docker_exec(server, "cat", f"{stage}/{diagnostic}", check=False)
                print(f"fixture {diagnostic}:\n{output.stdout}\n{output.stderr}", file=sys.stderr)
            raise
        observer_process = None
        result = wait_result(server, stage)
        expected_phase, recovery = ("requests", "post") if post_handoff else ("pause", "pre")
        if result.get("state") != "RECOVERED" or (result.get("phase"), result.get("recovery")) != (expected_phase, recovery):
            raise HarnessError(f"unexpected owner recovery result: {result}")
        log = docker_exec(server, "cat", f"{stage}/owner.log").stdout.splitlines()
        marker_name = "recover-post-invoked" if post_handoff else "recover-pre-invoked"
        if log.count(marker_name) != 1:
            raise HarnessError("recovery script did not run exactly once")
        if post_handoff:
            if log.index("candidate-stopped") > log.index(f"legacy-restored:api:{app_ids[0]}"):
                raise HarnessError("post-handoff recovery restored legacy before stopping candidate")
        elif f"legacy-restored:api:{app_ids[0]}" not in log:
            raise HarnessError("pre-handoff recovery did not restore the exact API ID")
        assert_recovered_fixtures(token, app_ids, candidate_name if post_handoff else None)
        print(f"PASS: observer killed after {marker}; detached owner completed {recovery} recovery", flush=True)
    finally:
        if observer_process is not None and observer_process.poll() is None:
            observer_process.kill()
            observer_process.wait(timeout=5)
        for cid in token_containers(token):
            row = inspect(cid)
            labels = row["Config"].get("Labels") or {}
            name = row.get("Name", "").lstrip("/")
            if labels.get("gpoint-test-owner") != token or not name.startswith(f"gpoint-test-owner-{token}-"):
                raise HarnessError("refusing cleanup outside random test-owned label/name scope")
            docker("rm", "--force", cid, check=False)


def launch_ssh_fixture(image_tag: str, fixture_token: str, socket_file: Path,
                       authorized_keys: Path) -> tuple[str, int]:
    name = f"gpoint-kamal-owner-ssh-{fixture_token}"
    server = docker("run", "--detach", "--name", name,
                    "--label", f"gpoint-owner-ssh-fixture={fixture_token}",
                    "--publish", "127.0.0.1::22",
                    "--mount", f"type=bind,src={socket_file},dst=/var/run/docker.sock",
                    image_tag).stdout.strip()
    labels = inspect(server)["Config"].get("Labels") or {}
    if labels.get("gpoint-owner-ssh-fixture") != fixture_token:
        raise HarnessError("server fixture label did not match generated token")
    docker("cp", str(authorized_keys), f"{server}:/root/.ssh/authorized_keys")
    docker_exec(server, "chown", "0:0", "/root/.ssh/authorized_keys")
    docker_exec(server, "chmod", "0600", "/root/.ssh/authorized_keys")
    port_lines = docker("port", server, "22/tcp").stdout.strip().splitlines()
    if len(port_lines) != 1 or not port_lines[0].startswith("127.0.0.1:"):
        raise HarnessError("SSH port is not published only on localhost")
    return server, int(port_lines[0].rsplit(":", 1)[1])


def run_test() -> None:
    fixture_token = secrets.token_hex(10)
    image_tag = f"gpoint-kamal-owner-ssh-test:{fixture_token}"
    server = ""
    with tempfile.TemporaryDirectory(prefix="gpoint-owner-ssh-") as temp_name:
        temp = Path(temp_name)
        key, public, known = temp / "observer", temp / "observer.pub", temp / "known_hosts"
        keygen = shutil.which("ssh-keygen")
        if not keygen:
            raise HarnessError("local ssh-keygen is required")
        run([keygen, "-q", "-t", "ed25519", "-N", "", "-C", "isolated-owner-observer", "-f", str(key)])
        key.chmod(0o600)
        fields = public.read_text(encoding="ascii").split()
        authorized = temp / "authorized_keys"
        authorized.write_text(f"restrict {fields[0]} {fields[1]} isolated-owner-observer\n", encoding="ascii")
        authorized.chmod(0o600)
        socket_file = docker_socket()
        print("Opt-in fixture build will fetch openssh-server from Debian; application images are cached and digest-pinned.", flush=True)
        require_image(PYTHON_IMAGE, PYTHON_IMAGE)
        require_image("docker:29-dind", DOCKER_CLI_IMAGE)
        docker("build", "--pull=false", "--network=default", "-t", image_tag,
               "-f", str(DOCKERFILE), str(FIXTURE), timeout=600)
        try:
            server, port = launch_ssh_fixture(image_tag, fixture_token, socket_file, authorized)
            docker_exec(server, "mkdir", "-p", "/opt/gpoint-test", "/run/gpoint-kamal-owner")
            docker("cp", str(OWNER_SOURCE), f"{server}:/opt/gpoint-test/kamal_cutover_owner.py")
            docker_exec(server, "chmod", "0700", "/opt/gpoint-test/kamal_cutover_owner.py")
            docker_exec(server, "docker", "version", "--format", "{{.Server.Version}}")
            hostkey = docker_exec(server, "cat", "/etc/ssh/ssh_host_ed25519_key.pub").stdout.split()
            if len(hostkey) < 2 or hostkey[0] != "ssh-ed25519":
                raise HarnessError("fixture host identity is not Ed25519")
            known.write_text(f"[127.0.0.1]:{port} {hostkey[0]} {hostkey[1]} isolated-fixture\n", encoding="ascii")
            known.chmod(0o600)
            try:
                wait_ssh(key, known, port)
            except HarnessError:
                print(docker("logs", server, check=False).stderr, file=sys.stderr)
                print(docker("logs", server, check=False).stdout, file=sys.stderr)
                raise
            for index, stop_number in enumerate((1, 2, 5), start=1):
                run_case(server, key, known, port, index, stop_number=stop_number)
            run_case(server, key, known, port, 4, post_handoff=True)
        finally:
            if server:
                labels = inspect(server)["Config"].get("Labels") or {}
                if labels.get("gpoint-owner-ssh-fixture") == fixture_token:
                    docker("rm", "--force", server, check=False)
            docker("image", "rm", image_tag, check=False)
    print("PASS: four real Docker + SSH observer-death recovery cases", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-local-isolated", action="store_true", required=True,
                        help="opt in to disposable local Docker and SSH fixtures")
    parser.parse_args()
    run_test()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HarnessError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"FAILED/BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2)
