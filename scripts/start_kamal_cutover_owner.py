#!/usr/bin/env python3
"""Validate and dispatch one immutable Kamal cutover owner service."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

RUN_ROOT = Path("/run/gpoint-kamal-owner")
OWNER_NAME = "kamal_cutover_owner.py"
CREDENTIALS_NAME = "kamal_cutover_credentials.py"
AUTHORIZED_KEYS = Path("/root/.ssh/authorized_keys")
HOST_KEY = Path("/etc/ssh/ssh_host_ed25519_key.pub")
PYTHON = "/usr/bin/python3"
BOOTSTRAP_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C"}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_RE = re.compile(r"^[0-9]{1,20}$")
ATTEMPT_RE = re.compile(r"^[1-9][0-9]{0,5}$")
SCHEMA = "gpoint-kamal-owner/v1"


class OwnerStartError(Exception):
    pass


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _safe_member(name: str) -> tuple[str, ...]:
    if "\\" in name or "\x00" in name:
        raise OwnerStartError("unsafe archive member")
    if name.startswith("/"):
        raise OwnerStartError("unsafe archive member")
    parts = tuple(name.split("/"))
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise OwnerStartError("unsafe archive member")
    return parts


def _check_stage(stage: Path, archive: Path, owner_uid: int) -> None:
    if os.geteuid() != owner_uid:
        raise OwnerStartError("effective identity mismatch")
    try:
        info = stage.lstat()
        parent = stage.parent.lstat()
        bundle = archive.lstat()
    except OSError as exc:
        raise OwnerStartError("private stage unavailable") from exc
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) & 0o077):
        raise OwnerStartError("stage ownership or permissions invalid")
    if (stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != owner_uid or stat.S_IMODE(parent.st_mode) & 0o022):
        raise OwnerStartError("stage parent ownership or permissions invalid")
    if (archive != stage / "bundle.tar" or stat.S_ISLNK(bundle.st_mode)
            or not stat.S_ISREG(bundle.st_mode) or bundle.st_uid != owner_uid
            or stat.S_IMODE(bundle.st_mode) != 0o600):
        raise OwnerStartError("archive must be private stage/bundle.tar")
    try:
        names = {entry.name for entry in stage.iterdir()}
    except OSError as exc:
        raise OwnerStartError("stage unavailable") from exc
    if not names.issubset({"bundle.tar", "start.py"}) or "bundle.tar" not in names:
        raise OwnerStartError("stage must contain the bundle and optional verified start.py")
    bootstrap = stage / "start.py"
    if "start.py" in names:
        try:
            bootstrap_info = bootstrap.lstat()
        except OSError as exc:
            raise OwnerStartError("bootstrap unavailable") from exc
        if (stat.S_ISLNK(bootstrap_info.st_mode) or not stat.S_ISREG(bootstrap_info.st_mode)
                or bootstrap_info.st_uid != owner_uid or stat.S_IMODE(bootstrap_info.st_mode) != 0o600):
            raise OwnerStartError("bootstrap ownership or permissions invalid")


def _extract(archive: Path, stage: Path, archive_sha: str, owner_uid: int) -> None:
    if not HEX_RE.fullmatch(archive_sha) or _digest(archive) != archive_sha:
        raise OwnerStartError("archive digest mismatch")
    seen: set[str] = set()
    directories: set[tuple[str, ...]] = set()
    with tarfile.open(archive, mode="r:") as tar:
        members = tar.getmembers()
        if not members:
            raise OwnerStartError("empty archive")
        for member in members:
            parts = _safe_member(member.name)
            normalized = "/".join(parts)
            if normalized in seen:
                raise OwnerStartError("duplicate archive entry")
            seen.add(normalized)
            if member.isdir():
                directories.add(parts)
                continue
            if not member.isreg():
                raise OwnerStartError("archive links and special files are forbidden")
            if member.mode & 0o022 or member.mode & ~0o777:
                raise OwnerStartError("unsafe archive file mode")
            if stat.S_IMODE(member.mode) not in (0o600, 0o700):
                raise OwnerStartError("archive files must be mode 0600 or 0700")
        for parts in directories:
            _mkdir_chain(stage, parts, owner_uid)
        for member in members:
            if member.isdir():
                continue
            parts = _safe_member(member.name)
            destination = stage.joinpath(*parts)
            _mkdir_chain(stage, parts[:-1], owner_uid)
            if destination.exists() or destination.is_symlink():
                raise OwnerStartError("archive extraction would overwrite a file")
            stream = tar.extractfile(member)
            if stream is None:
                raise OwnerStartError("archive member content unavailable")
            data = stream.read()
            if len(data) != member.size:
                raise OwnerStartError("archive member size mismatch")
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                         stat.S_IMODE(member.mode))
            try:
                os.fchown(fd, owner_uid, -1)
                os.fchmod(fd, stat.S_IMODE(member.mode))
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise


def _mkdir_chain(stage: Path, parts: tuple[str, ...], owner_uid: int) -> None:
    current = stage
    for part in parts:
        current = current / part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) & 0o022):
                raise OwnerStartError("unsafe extraction directory")
        else:
            current.mkdir(mode=0o700)
            os.chown(current, owner_uid, -1)
            os.chmod(current, 0o700)


def _load_source(path: Path, module_name: str) -> ModuleType:
    module = ModuleType(module_name)
    module.__file__ = str(path)
    code = compile(path.read_bytes(), str(path), "exec")
    exec(code, module.__dict__)
    return module


def _private_file(path: Path, data: bytes, owner_uid: int) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchown(fd, owner_uid, -1)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _prepare_runtime(stage: Path, env: dict[str, str], owner_uid: int) -> None:
    expected = {
        "HOME": stage / "home", "RUNNER_TEMP": stage / "tmp",
        "GITHUB_WORKSPACE": stage / "repo", "GITHUB_STEP_SUMMARY": stage / "github-summary",
        "GITHUB_OUTPUT": stage / "github-output",
    }
    for key, path in expected.items():
        if env.get(key) != str(path):
            raise OwnerStartError("runtime environment path mismatch")
    temp = stage / "tmp"
    temp.mkdir(mode=0o700)
    os.chown(temp, owner_uid, -1)
    os.chmod(temp, 0o700)
    actor, password = env.get("GITHUB_ACTOR"), env.get("KAMAL_REGISTRY_PASSWORD")
    if not actor or not password:
        raise OwnerStartError("registry credentials missing")
    auth = base64.b64encode(f"{actor}:{password}".encode()).decode("ascii")
    for name in ("github-summary", "github-output"):
        _private_file(stage / name, b"", owner_uid)
    # Hold the serialized Docker auth only in the attempt's private HOME.
    registry_config = json.dumps({"auths": {"ghcr.io": {"auth": auth}}}, separators=(",", ":")).encode()
    # Credentials.install owns creation of HOME/.ssh. The Docker directory is
    # created after that helper succeeds so its one-use key setup stays intact.
    _private_file(stage / ".registry-auth.pending", registry_config, owner_uid)


def _install_registry_config(stage: Path, owner_uid: int) -> None:
    pending = stage / ".registry-auth.pending"
    docker = stage / "home/.docker"
    docker.mkdir(mode=0o700)
    os.chown(docker, owner_uid, -1)
    os.chmod(docker, 0o700)
    os.replace(pending, docker / "config.json")
    os.chown(docker / "config.json", owner_uid, -1)
    os.chmod(docker / "config.json", 0o600)


def _cleanup_before_dispatch(stage: Path, credentials: ModuleType | None, owner_uid: int,
                             credential_cleanup: Callable[..., Any] | None,
                             installed: bool) -> None:
    if installed and credentials is not None:
        try:
            if credential_cleanup is not None:
                credential_cleanup(credentials, stage)
            else:
                _cleanup_module_credentials(credentials, stage, owner_uid)
        except Exception:
            pass
    sensitive_paths = (stage / "environment.json", stage / ".registry-auth.pending",
                       stage / "home/.docker/config.json", stage / "repo/.kamal/secrets")
    for path in sensitive_paths:
        try:
            if path.is_symlink():
                continue
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_uid == owner_uid:
                path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _cleanup_module_credentials(module: ModuleType, stage: Path, owner_uid: int) -> None:
    terminal = getattr(module, "terminal_cleanup", None)
    if terminal is not None:
        terminal(stage, AUTHORIZED_KEYS, owner_uid=owner_uid)
    else:
        module.remove(stage, AUTHORIZED_KEYS, owner_uid=owner_uid)


def _unit_status(runner: Callable[..., Any], systemctl_path: str, unit: str) -> str | None:
    result = runner([systemctl_path, "show", unit, "--property=ActiveState", "--property=SubState",
                     "--property=Result", "--value"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False, env=BOOTSTRAP_ENV)
    if result.returncode != 0:
        return None
    lines = result.stdout.splitlines()
    if not lines:
        return None
    return lines[0]


def _attempt_status(stage: Path, owner_uid: int) -> bool:
    for name in ("owner-status.json", "owner-result.json"):
        path = stage / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != 0o600):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (isinstance(record, dict) and record.get("schema") == SCHEMA
                and record.get("candidateSha") == stage.name.split("-")[0]
                and str(record.get("runId")) == stage.name.split("-")[1]
                and str(record.get("attempt")) == stage.name.split("-")[2]
                and record.get("state") in ("RUNNING", "RECOVERING", "COMPLETE", "RECOVERED", "BLOCKED")):
            return True
    return False


def start(stage: Path, archive: Path, archive_sha: str, manifest_sha: str,
          owner_sha: str, credentials_sha: str, *, runner: Callable[..., Any] = subprocess.run,
          systemd_run_path: str = "/usr/bin/systemd-run", systemctl_path: str = "/usr/bin/systemctl",
          owner_uid: int = 0, credential_install: Callable[..., Any] | None = None,
          credential_cleanup: Callable[..., Any] | None = None) -> str:
    stage, archive = Path(stage), Path(archive)
    if not all(HEX_RE.fullmatch(value) for value in (archive_sha, manifest_sha, owner_sha, credentials_sha)):
        raise OwnerStartError("invalid trusted digest")
    _check_stage(stage, archive, owner_uid)
    name = stage.name.split("-")
    if (len(name) != 3 or not SHA_RE.fullmatch(name[0]) or not RUN_RE.fullmatch(name[1])
            or not ATTEMPT_RE.fullmatch(name[2])):
        raise OwnerStartError("invalid attempt stage name")
    unit = f"gpoint-kamal-owner-{name[1]}-{name[2]}"
    try:
        _extract(archive, stage, archive_sha, owner_uid)
    except Exception as exc:
        _cleanup_before_dispatch(stage, None, owner_uid, credential_cleanup, False)
        try:
            archive.unlink()
        except OSError:
            pass
        if isinstance(exc, OwnerStartError):
            raise
        raise OwnerStartError("archive validation or extraction failed") from exc
    # The tar contains environment secrets; remove it once exact contents have
    # been verified and extracted.
    archive.unlink()
    owner_path = stage / OWNER_NAME
    credentials_path = stage / CREDENTIALS_NAME
    if _digest(owner_path) != owner_sha or _digest(credentials_path) != credentials_sha:
        _cleanup_before_dispatch(stage, None, owner_uid, credential_cleanup, False)
        raise OwnerStartError("trusted helper hash mismatch")
    credentials: ModuleType | None = None
    installed = False
    dispatched = False
    try:
        # Hash both trusted helper entrypoints before importing any bundle code.
        owner = _load_source(owner_path, "gpoint_kamal_cutover_owner")
        credentials = _load_source(credentials_path, "gpoint_kamal_cutover_credentials")
        _, _, env = owner._load_bundle(stage, manifest_sha, owner_uid)
        probe = runner([systemctl_path, "show", unit, "--property=LoadState", "--value"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       text=True, check=False, env=BOOTSTRAP_ENV)
        if probe.returncode != 0:
            raise OwnerStartError("cannot verify existing systemd unit")
        if probe.stdout.strip() not in ("not-found", ""):
            raise OwnerStartError("attempt systemd unit already exists")
        _prepare_runtime(stage, env, owner_uid)
        previous_environment = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(BOOTSTRAP_ENV)
            if credential_install is None:
                credentials.install(stage, AUTHORIZED_KEYS, HOST_KEY, owner_uid=owner_uid)
            else:
                credential_install(credentials, stage)
        finally:
            os.environ.clear()
            os.environ.update(previous_environment)
        installed = True
        _install_registry_config(stage, owner_uid)
        cleanup_command = (f"{PYTHON} -B {credentials_path} remove --stage {stage}")
        argv = [systemd_run_path, f"--unit={unit}", "--service-type=exec",
                "--property=Restart=no", "--property=KillMode=control-group", "--property=UMask=0077",
                f"--property=ExecStopPost={cleanup_command}", PYTHON, "-B", str(owner_path),
                "--stage", str(stage), "--manifest-sha", manifest_sha]
        dispatched = True
        try:
            result = runner(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=False, env=BOOTSTRAP_ENV)
        except Exception:
            return "UNKNOWN"
        if result.returncode != 0:
            return "UNKNOWN"
        active = _unit_status(runner, systemctl_path, unit)
        if active in ("active", "activating") or _attempt_status(stage, owner_uid):
            return "STARTED"
        return "UNKNOWN"
    except BaseException as exc:
        if not dispatched:
            _cleanup_before_dispatch(stage, credentials, owner_uid, credential_cleanup, installed)
            if isinstance(exc, OwnerStartError):
                raise
            if isinstance(exc, Exception):
                raise OwnerStartError("pre-dispatch bootstrap failed") from exc
            raise
        return "UNKNOWN"


def _production_stage(value: str) -> Path:
    stage = Path(value)
    if stage.parent != RUN_ROOT:
        raise argparse.ArgumentTypeError("stage must be a direct child of /run/gpoint-kamal-owner")
    return stage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, type=_production_stage)
    parser.add_argument("--archive-sha", required=True)
    parser.add_argument("--manifest-sha", required=True)
    parser.add_argument("--owner-sha", required=True)
    parser.add_argument("--credentials-sha", required=True)
    args = parser.parse_args(argv)
    try:
        state = start(args.stage, args.stage / "bundle.tar", args.archive_sha, args.manifest_sha,
                      args.owner_sha, args.credentials_sha)
        print(f"GPOINT_KAMAL_OWNER_START={state}", flush=True)
        return 0 if state == "STARTED" else 2
    except OwnerStartError:
        print("GPOINT_KAMAL_OWNER_START=BLOCKED", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
