#!/usr/bin/env python3
"""Install and remove the one-attempt localhost SSH credential for cutover.

This helper adds exactly one expiring authorized_keys record.  Its client key,
host key pin, and SSH configuration live in the private attempt directory and
are removed by :func:`remove` at terminal cleanup.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


RUN_ROOT = Path("/run/gpoint-kamal-owner")
AUTHORIZED_KEYS = Path("/root/.ssh/authorized_keys")
HOST_PUBLIC_KEY = Path("/etc/ssh/ssh_host_ed25519_key.pub")
STAGE_RE = re.compile(r"^(?P<sha>[0-9a-f]{40})-(?P<run>[0-9]{1,20})-(?P<attempt>[1-9][0-9]{0,5})$")
LINE_NAME = "localhost-key.line"
OFFSET_NAME = "localhost-key.offset"
LOCK_NAME = ".authorized_keys.gpoint-cutover.lock"
PRIVATE_HOME = "home"
PRIVATE_SSH = "home/.ssh"


class CredentialError(RuntimeError):
    """The credential operation could not be completed safely."""


def _lstat(path: Path, description: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise CredentialError(f"{description} is unavailable") from exc


def _check_dir(path: Path, owner_uid: int, *, private: bool = False) -> None:
    info = _lstat(path, "directory")
    mode = stat.S_IMODE(info.st_mode)
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != owner_uid or mode & 0o022
            or (private and mode & 0o077)):
        raise CredentialError("directory ownership or permissions are unsafe")


def _check_stage(stage: Path, owner_uid: int) -> None:
    stage = Path(stage)
    if not STAGE_RE.fullmatch(stage.name):
        raise CredentialError("stage name is not bound to a cutover attempt")
    _check_dir(stage, owner_uid, private=True)
    # Do not follow any symlink in the path. The stage parent is a private
    # attempt container (or /run itself in production).
    parent = stage.parent
    _check_dir(parent, owner_uid)
    if stage.parent.parent != RUN_ROOT and stage.parent.parent != Path("/"):
        # Test callers may inject a stage under a private fixture directory.
        _check_dir(stage.parent.parent, owner_uid)


def _check_file(path: Path, owner_uid: int, description: str, *, private: bool) -> os.stat_result:
    info = _lstat(path, description)
    mode = stat.S_IMODE(info.st_mode)
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner_uid or mode & 0o022
            or (private and mode & 0o077)):
        raise CredentialError(f"{description} ownership or permissions are unsafe")
    return info


def _secure_read(path: Path, owner_uid: int, description: str, *, private: bool = False) -> bytes:
    before = _check_file(path, owner_uid, description, private=private)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CredentialError(f"{description} could not be opened safely") from exc
    try:
        after = os.fstat(fd)
        if (not stat.S_ISREG(after.st_mode) or after.st_ino != before.st_ino
                or after.st_dev != before.st_dev or after.st_uid != owner_uid):
            raise CredentialError(f"{description} changed while opening")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)


def _open_authorized_keys(path: Path, owner_uid: int) -> int:
    parent = path.parent
    _check_dir(parent, owner_uid)
    info = _check_file(path, owner_uid, "authorized_keys", private=False)
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CredentialError("authorized_keys could not be opened safely") from exc
    opened = os.fstat(fd)
    if (not stat.S_ISREG(opened.st_mode) or opened.st_dev != info.st_dev
            or opened.st_ino != info.st_ino or opened.st_uid != owner_uid
            or stat.S_IMODE(opened.st_mode) & 0o022):
        os.close(fd)
        raise CredentialError("authorized_keys changed while opening")
    return fd


def _open_adjacent_lock(path: Path, owner_uid: int) -> int:
    lock_path = path.parent / LOCK_NAME
    _check_dir(lock_path.parent, owner_uid)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise CredentialError("authorized_keys lock could not be opened safely") from exc
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid
            or stat.S_IMODE(info.st_mode) != 0o600):
        os.close(fd)
        raise CredentialError("authorized_keys lock ownership or permissions are unsafe")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        os.close(fd)
        raise CredentialError("authorized_keys lock could not be acquired") from exc
    return fd


def _write_private(path: Path, data: bytes, owner_uid: int, *, exclusive: bool = True) -> None:
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CredentialError(f"private file already exists or cannot be created: {path.name}") from exc
    try:
        os.fchmod(fd, 0o600)
        if os.fstat(fd).st_uid != owner_uid:
            if os.geteuid() != 0:
                raise CredentialError("private file has the wrong owner")
            os.fchown(fd, owner_uid, -1)
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise CredentialError("short private file write")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_host_key(path: Path, owner_uid: int) -> tuple[str, str, str]:
    raw = _secure_read(path, owner_uid, "SSH host public key")
    try:
        parts = raw.decode("ascii").strip().split()
    except UnicodeDecodeError as exc:
        raise CredentialError("SSH host public key is not ASCII") from exc
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise CredentialError("SSH host key must be an Ed25519 public key")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise CredentialError("SSH host public key encoding is invalid") from exc
    # OpenSSH validates the full wire blob when it reads known_hosts. Keeping
    # this helper's parser to algorithm plus base64 allows fixture keys while
    # still failing closed at the SSH client if a supplied pin is malformed.
    if not blob:
        raise CredentialError("SSH host public key blob is empty")
    comment = " ".join(parts[2:]) if len(parts) > 2 else "gpoint-host"
    return parts[0], parts[1], comment


def _create_client(stage: Path, owner_uid: int, host_key: tuple[str, str, str]) -> None:
    home = stage / PRIVATE_HOME
    ssh = stage / PRIVATE_SSH
    try:
        home.mkdir(mode=0o700)
        ssh.mkdir(mode=0o700)
        os.chmod(home, 0o700)
        os.chmod(ssh, 0o700)
        if os.geteuid() == 0:
            os.chown(home, owner_uid, -1)
            os.chown(ssh, owner_uid, -1)
    except OSError as exc:
        raise CredentialError("private SSH directories could not be created") from exc
    executable = shutil.which("ssh-keygen")
    if not executable:
        raise CredentialError("ssh-keygen is unavailable")
    private_key = ssh / "id_ed25519"
    result = subprocess.run([executable, "-q", "-t", "ed25519", "-N", "", "-f", str(private_key)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise CredentialError("ssh-keygen failed")
    if os.geteuid() == 0:
        os.chown(private_key, owner_uid, -1)
        os.chown(Path(str(private_key) + ".pub"), owner_uid, -1)
    os.chmod(private_key, 0o600)
    os.chmod(Path(str(private_key) + ".pub"), 0o600)
    config = (
        "Host 127.0.0.1 localhost ::1\n"
        "    HostName 127.0.0.1\n"
        "    User root\n"
        f"    IdentityFile {private_key}\n"
        "    IdentitiesOnly yes\n"
        "    StrictHostKeyChecking yes\n"
        f"    UserKnownHostsFile {ssh / 'known_hosts'}\n"
        "    GlobalKnownHostsFile /dev/null\n"
    ).encode()
    known_hosts = f"127.0.0.1,localhost,::1 {host_key[0]} {host_key[1]} {host_key[2]}\n".encode()
    _write_private(ssh / "config", config, owner_uid)
    _write_private(ssh / "known_hosts", known_hosts, owner_uid)


def _expiry() -> str:
    # Ubuntu 22.04's OpenSSH 8.9 rejects the trailing Z on authorized_keys
    # expiry-time values. Generate a local-time value without a suffix; sshd
    # interprets it in the same host timezone.
    return (datetime.now().astimezone() + timedelta(minutes=90)).strftime("%Y%m%d%H%M%S")


def _make_authorized_line(public_key: bytes, stage: Path) -> bytes:
    try:
        parts = public_key.decode("ascii").strip().split()
    except UnicodeDecodeError as exc:
        raise CredentialError("generated public key is invalid") from exc
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise CredentialError("generated key is not Ed25519")
    options = f'restrict,expiry-time="{_expiry()}",from="127.0.0.1,::1"'
    comment = f"gpoint-cutover-{stage.name}"
    return f"{options} {parts[0]} {parts[1]} {comment}\n".encode("ascii")


def _attempt_private_paths(stage: Path, owner_uid: int) -> None:
    _check_stage(stage, owner_uid)
    ssh = stage / PRIVATE_SSH
    _check_dir(stage / PRIVATE_HOME, owner_uid, private=True)
    _check_dir(ssh, owner_uid, private=True)
    for name, private in (("id_ed25519", True), ("id_ed25519.pub", True), ("config", True), ("known_hosts", True)):
        _check_file(ssh / name, owner_uid, "attempt SSH file", private=private)


def install(stage: Path, authorized_keys: Path, host_public_key: Path, owner_uid: int = 0) -> None:
    """Install a temporary loopback-only SSH key for this cutover attempt."""
    stage, authorized_keys, host_public_key = Path(stage), Path(authorized_keys), Path(host_public_key)
    _check_stage(stage, owner_uid)
    line_path = stage / LINE_NAME
    offset_path = stage / OFFSET_NAME
    if any(path.exists() or path.is_symlink() for path in
           (line_path, offset_path, stage / PRIVATE_HOME)):
        raise CredentialError("credential attempt already exists")
    host_key = _read_host_key(host_public_key, owner_uid)
    auth_fd = _open_authorized_keys(authorized_keys, owner_uid)
    lock_fd = -1
    try:
        lock_fd = _open_adjacent_lock(authorized_keys, owner_uid)
        before = os.fstat(auth_fd)
        original = os.pread(auth_fd, before.st_size, 0)
        if len(original) != before.st_size:
            raise CredentialError("authorized_keys changed while reading")
        _create_client(stage, owner_uid, host_key)
        public_bytes = _secure_read(stage / PRIVATE_SSH / "id_ed25519.pub", owner_uid,
                                    "generated SSH public key", private=True)
        line = _make_authorized_line(public_bytes, stage)
        # The cleanup record is durable before authorized_keys is changed.
        _write_private(line_path, line, owner_uid)
        needs_separator = bool(original and not original.endswith(b"\n"))
        metadata = f"{len(original)} {int(needs_separator)}\n".encode("ascii")
        _write_private(offset_path, metadata, owner_uid)
        suffix = (b"\n" if original and not original.endswith(b"\n") else b"") + line
        try:
            os.lseek(auth_fd, 0, os.SEEK_END)
            view = memoryview(suffix)
            while view:
                count = os.write(auth_fd, view)
                if count <= 0:
                    raise CredentialError("authorized_keys append was incomplete")
                view = view[count:]
            os.fsync(auth_fd)
        except Exception:
            # If append failed partway through the single record, roll back
            # only a tail that still exactly matches our original prefix plus
            # a prefix of the bytes we attempted to append.
            current_stat = os.fstat(auth_fd)
            current = os.pread(auth_fd, current_stat.st_size, 0)
            tail = current[len(original):] if current.startswith(original) else None
            if tail is not None and suffix.startswith(tail):
                os.ftruncate(auth_fd, len(original))
                os.fsync(auth_fd)
                _remove_private(stage, owner_uid)
            else:
                _remove_locked(stage, authorized_keys, owner_uid, auth_fd)
            raise
    except Exception:
        # Remove generated secrets when setup fails before or during append.
        _remove_private(stage, owner_uid)
        raise
    finally:
        os.close(auth_fd)
        if lock_fd >= 0:
            os.close(lock_fd)


def _read_record(path: Path, owner_uid: int, description: str) -> bytes | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    return _secure_read(path, owner_uid, description, private=True)


def _remove_line(auth_fd: int, line: bytes, offset_record: bytes | None) -> None:
    if not line.endswith(b"\n") or line.count(b"\n") != 1:
        raise CredentialError("saved authorized key record is malformed")
    st = os.fstat(auth_fd)
    raw = os.pread(auth_fd, st.st_size, 0)
    if len(raw) != st.st_size:
        raise CredentialError("authorized_keys changed while reading")
    # Only whole exact lines are owned by this helper. Preserve every other
    # byte, including comments, ordering, and any concurrent unrelated appends.
    pieces = raw.splitlines(keepends=True)
    output: list[bytes] = []
    cursor = 0
    found_positions: list[int] = []
    for piece in pieces:
        if piece == line:
            found_positions.append(cursor)
        else:
            output.append(piece)
        cursor += len(piece)
    if found_positions:
        if offset_record is None:
            raise CredentialError("saved authorized_keys boundary is missing")
        cleaned = b"".join(output)
        try:
            offset_text = offset_record.decode("ascii").strip().split()
            if len(offset_text) != 2:
                raise ValueError("wrong field count")
            original_size, separator = int(offset_text[0]), int(offset_text[1])
        except (UnicodeDecodeError, ValueError, IndexError) as exc:
            raise CredentialError("saved authorized_keys boundary is invalid") from exc
        if separator not in (0, 1) or original_size < 0:
            raise CredentialError("saved authorized_keys boundary is invalid")
        if separator:
            # A separator is ours only at the recorded original boundary.
            # Later appends move nothing before the attempt line.
            position = found_positions[0]
            if position == original_size + 1 and original_size < len(raw) and raw[original_size:original_size + 1] == b"\n":
                cleaned = cleaned[:original_size] + cleaned[original_size + 1:]
        os.ftruncate(auth_fd, 0)
        view = memoryview(cleaned)
        while view:
            count = os.write(auth_fd, view)
            if count <= 0:
                raise CredentialError("authorized_keys cleanup write failed")
            view = view[count:]
        os.fsync(auth_fd)


def _remove_private(stage: Path, owner_uid: int, *, preserve_home: bool = False) -> None:
    # Remove only our known credential files and empty directories. Do not
    # follow links and refuse to erase an unexpected replacement.
    for relative in (LINE_NAME, OFFSET_NAME, "home/.ssh/id_ed25519", "home/.ssh/id_ed25519.pub",
                     "home/.ssh/config", "home/.ssh/known_hosts"):
        path = stage / relative
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        _check_file(path, owner_uid, "attempt credential file", private=True)
        path.unlink()
    for relative in (PRIVATE_SSH,):
        path = stage / relative
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        _check_dir(path, owner_uid, private=True)
        try:
            path.rmdir()
        except OSError as exc:
            raise CredentialError("unexpected content remains in credential directory") from exc
    if not preserve_home:
        home = stage / PRIVATE_HOME
        try:
            home.lstat()
        except FileNotFoundError:
            return
        _check_dir(home, owner_uid, private=True)
        try:
            home.rmdir()
        except OSError as exc:
            raise CredentialError("unexpected content remains in credential home") from exc


def _remove_locked(stage: Path, authorized_keys: Path, owner_uid: int, auth_fd: int,
                   *, preserve_home: bool = False) -> None:
    line = _read_record(stage / LINE_NAME, owner_uid, "saved authorized key record")
    offset_record = _read_record(stage / OFFSET_NAME, owner_uid, "saved authorized key boundary")
    if line is not None:
        _remove_line(auth_fd, line, offset_record)
    _remove_private(stage, owner_uid, preserve_home=preserve_home)


def remove(stage: Path, authorized_keys: Path, owner_uid: int = 0) -> None:
    """Remove this attempt's exact authorized_keys entry and client material."""
    stage, authorized_keys = Path(stage), Path(authorized_keys)
    _check_stage(stage, owner_uid)
    # If install never reached the authorized_keys mutation, private cleanup is
    # still useful and idempotent. When the file exists, lock it across removal.
    if not authorized_keys.exists() or authorized_keys.is_symlink():
        if authorized_keys.is_symlink():
            raise CredentialError("authorized_keys is a symlink")
        _remove_private(stage, owner_uid)
        return
    auth_fd = _open_authorized_keys(authorized_keys, owner_uid)
    lock_fd = -1
    try:
        lock_fd = _open_adjacent_lock(authorized_keys, owner_uid)
        _remove_locked(stage, authorized_keys, owner_uid, auth_fd)
    finally:
        os.close(auth_fd)
        if lock_fd >= 0:
            os.close(lock_fd)


def _preflight_terminal_secrets(stage: Path, owner_uid: int) -> None:
    """Reject unsafe sensitive paths before changing any credential state."""
    environment = stage / "environment.json"
    try:
        environment.lstat()
    except FileNotFoundError:
        pass
    else:
        _check_file(environment, owner_uid, "attempt environment", private=True)

    home = stage / PRIVATE_HOME
    try:
        home.lstat()
    except FileNotFoundError:
        return
    _check_dir(home, owner_uid, private=True)
    docker = home / ".docker"
    try:
        docker.lstat()
    except FileNotFoundError:
        return
    _check_dir(docker, owner_uid, private=True)
    config = docker / "config.json"
    try:
        config.lstat()
    except FileNotFoundError:
        return
    _check_file(config, owner_uid, "Docker credentials", private=True)


def terminal_cleanup(stage: Path, authorized_keys: Path, owner_uid: int = 0) -> None:
    """Remove attempt SSH access and only the two terminal secret inputs.

    Owner status/result/log, manifest, repository, and other stage contents are
    retained as evidence. Unexpected paths are rejected instead of traversed.
    """
    stage, authorized_keys = Path(stage), Path(authorized_keys)
    _check_stage(stage, owner_uid)
    _preflight_terminal_secrets(stage, owner_uid)
    if not authorized_keys.exists() or authorized_keys.is_symlink():
        if authorized_keys.is_symlink():
            raise CredentialError("authorized_keys is a symlink")
        _remove_private(stage, owner_uid, preserve_home=True)
    else:
        auth_fd = _open_authorized_keys(authorized_keys, owner_uid)
        lock_fd = -1
        try:
            lock_fd = _open_adjacent_lock(authorized_keys, owner_uid)
            _remove_locked(stage, authorized_keys, owner_uid, auth_fd, preserve_home=True)
        finally:
            os.close(auth_fd)
            if lock_fd >= 0:
                os.close(lock_fd)

    environment = stage / "environment.json"
    if environment.exists():
        _check_file(environment, owner_uid, "attempt environment", private=True)
        environment.unlink()
    docker = stage / PRIVATE_HOME / ".docker"
    config = docker / "config.json"
    if config.exists():
        _check_file(config, owner_uid, "Docker credentials", private=True)
        config.unlink()
    if docker.exists():
        _check_dir(docker, owner_uid, private=True)
        try:
            docker.rmdir()
        except OSError:
            # Preserve unrelated Docker data, but never follow it.
            pass
    home = stage / PRIVATE_HOME
    if home.exists():
        _check_dir(home, owner_uid, private=True)
        try:
            home.rmdir()
        except OSError:
            # Preserve unrelated attempt data in the private home.
            pass


def _production_stage(value: str) -> Path:
    stage = Path(value)
    if not STAGE_RE.fullmatch(stage.name) or stage.parent != RUN_ROOT:
        raise CredentialError("stage must be an attempt directory directly under /run/gpoint-kamal-owner")
    return stage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "remove"))
    parser.add_argument("--stage", required=True)
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise CredentialError("production credential command requires root")
        stage = _production_stage(args.stage)
        if args.action == "install":
            install(stage, AUTHORIZED_KEYS, HOST_PUBLIC_KEY, owner_uid=0)
        else:
            terminal_cleanup(stage, AUTHORIZED_KEYS, owner_uid=0)
        return 0
    except CredentialError as exc:
        print(f"kamal cutover credentials: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
