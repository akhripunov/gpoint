"""Regression coverage for the Kamal post-app-boot network attachment hook.

`ssh` joins the remote command and its arguments with single spaces and no
quoting, so an empty argument disappears. Eros roles deliberately attach no
networks, which used to shift every later positional parameter and abort the
hook with `$6: unbound variable` after the container had already booted.
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = "a" * 40
APP_SOURCE = "b" * 40
VERSION = f"{CANDIDATE}-42495781430-1"
HOST = "95.133.236.59"


def _run_hook(tmp_path, service, roles=""):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_file = tmp_path / "ssh-args"
    stdin_file = tmp_path / "ssh-stdin"
    fake = bin_dir / "ssh"
    fake.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" >> '{args_file}'\n"
        f"cat >> '{stdin_file}'\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    key = tmp_path / "key"
    key.write_text("k")
    key.chmod(0o600)
    known = tmp_path / "known"
    known.write_text("h")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GPOINT_KAMAL_SERVICE": service,
        "KAMAL_VERSION": VERSION,
        "GPOINT_COMMIT_SHA": CANDIDATE,
        "GPOINT_APP_SOURCE_SHA": APP_SOURCE,
        "KAMAL_HOSTS": HOST,
        "KAMAL_ROLES": roles,
        "GPOINT_SSH_KEY": str(key),
        "GPOINT_KNOWN_HOSTS_FILE": str(known),
        "GPOINT_KAMAL_TARGET_HOST": HOST,
    }
    result = subprocess.run(
        ["sh", str(ROOT / ".kamal/hooks/post-app-boot")],
        env=env, capture_output=True, text=True, input="",
    )
    args = args_file.read_text().splitlines() if args_file.exists() else []
    stdin = stdin_file.read_text() if stdin_file.exists() else ""
    return result, args, stdin


def test_eros_role_keeps_positional_arguments_when_no_network_is_attached(tmp_path):
    result, args, stdin = _run_hook(tmp_path, "gpoint-eros-pilot")
    assert result.returncode == 0, result.stderr
    assert args[-6:] == ["gpoint-eros-pilot", "engine", VERSION, "-", CANDIDATE, APP_SOURCE]
    assert 'if [ "$networks" = "-" ]; then networks=""; fi' in stdin


def test_core_role_still_passes_its_network_list(tmp_path):
    result, args, stdin = _run_hook(tmp_path, "gpoint-core", roles="api")
    assert result.returncode == 0, result.stderr
    assert args[-6:] == [
        "gpoint-core", "api", VERSION,
        "gpoint_app:api,shared_internal:", CANDIDATE, APP_SOURCE,
    ]
    assert 'if [ "$networks" = "-" ]; then networks=""; fi' in stdin
