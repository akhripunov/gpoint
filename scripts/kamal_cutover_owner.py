#!/usr/bin/env python3
"""Attempt-bound serial owner for the first GPoint Kamal cutover."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "gpoint-kamal-owner/v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_RE = re.compile(r"^[0-9]{1,20}$")
ATTEMPT_RE = re.compile(r"^[1-9][0-9]{0,5}$")
PHASES = ("pause", "retire", "requests", "routes", "consumers", "verify")
PRE_RECOVERY = "recover-pre"
POST_RECOVERY = "recover-post"
DEFAULT_LOCK = Path("/run/gpoint-kamal-owner.lock")
CONTROLLED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class OwnerError(Exception):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_path(root: Path, relative: str, owner_uid: int, *, private: bool = False) -> Path:
    rel = PurePosixPath(relative)
    if rel.is_absolute() or not rel.parts or any(part in ("", ".", "..") for part in rel.parts):
        raise OwnerError("unsafe manifest path")
    current = root
    for index, part in enumerate(rel.parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise OwnerError("manifest file missing") from exc
        if stat.S_ISLNK(info.st_mode) or info.st_uid != owner_uid:
            raise OwnerError("bundle ownership or symlink invalid")
        mode = stat.S_IMODE(info.st_mode)
        is_final = index == len(rel.parts) - 1
        if not is_final:
            if not stat.S_ISDIR(info.st_mode) or mode & 0o022:
                raise OwnerError("unsafe bundle directory")
        elif not stat.S_ISREG(info.st_mode) or mode & 0o022:
            raise OwnerError("unsafe bundle file")
        elif private and mode != 0o600:
            raise OwnerError("private bundle file must be mode 0600")
    resolved_root = root.resolve(strict=True)
    resolved = current.resolve(strict=True)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise OwnerError("manifest path escaped stage")
    return current


def _verify_git_workspace(stage: Path, repo: Path, candidate_sha: str, owner_uid: int) -> None:
    git_dir = repo / ".git"
    try:
        git_info = git_dir.lstat()
    except OSError as exc:
        raise OwnerError("Git workspace .git directory missing") from exc
    if (stat.S_ISLNK(git_info.st_mode) or not stat.S_ISDIR(git_info.st_mode)
            or git_info.st_uid != owner_uid or stat.S_IMODE(git_info.st_mode) & 0o077):
        raise OwnerError("Git workspace .git directory unsafe")
    head_path = _safe_path(stage, "repo/.git/HEAD", owner_uid, private=True)
    if head_path.read_text(encoding="utf-8") != candidate_sha + "\n":
        raise OwnerError("Git workspace HEAD does not match candidate")
    env = {"PATH": CONTROLLED_PATH, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=repo, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise OwnerError("Git workspace candidate commit is not verifiable") from exc
    if result.stdout.strip() != candidate_sha:
        raise OwnerError("Git workspace resolved HEAD does not match candidate")


def _validate_parent_chain(stage: Path, owner_uid: int) -> None:
    # Production permits the shared /run mount (0755), but checks every
    # attempt-owner ancestor. Temporary tests use their private fixture root as
    # the trust boundary so the shared system /tmp ancestors are not mistaken
    # for an owned stage directory.
    ancestors = list(stage.parents)
    if Path("/run") in ancestors:
        boundary = Path("/run")
    else:
        boundary = stage.parent.parent
    current = stage.parent
    while True:
        try:
            info = current.lstat()
        except OSError as exc:
            raise OwnerError("stage parent unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OwnerError("stage parent is not a real directory")
        if info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) & 0o022:
            raise OwnerError("stage parent ownership or permissions invalid")
        if current == boundary:
            break
        if current.parent == current:
            raise OwnerError("stage parent boundary missing")
        current = current.parent


def _refuse_unfinished_sibling(stage: Path, owner_uid: int) -> None:
    for sibling in stage.parent.iterdir():
        if sibling == stage:
            continue
        parts = sibling.name.split("-")
        if (len(parts) != 3 or not SHA_RE.fullmatch(parts[0])
                or not RUN_RE.fullmatch(parts[1]) or not ATTEMPT_RE.fullmatch(parts[2])):
            continue
        try:
            info = sibling.lstat()
        except OSError as exc:
            raise OwnerError("sibling attempt changed") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid:
            raise OwnerError("unsafe sibling attempt")
        status_path = sibling / "owner-status.json"
        try:
            status_info = status_path.lstat()
        except (FileNotFoundError, OSError) as exc:
            raise OwnerError("sibling attempt has unknown status") from exc
        if (stat.S_ISLNK(status_info.st_mode) or not stat.S_ISREG(status_info.st_mode)
                or status_info.st_uid != owner_uid or stat.S_IMODE(status_info.st_mode) != 0o600):
            raise OwnerError("unsafe sibling status")
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OwnerError("unknown sibling status") from exc
        if (not isinstance(status, dict) or status.get("schema") != SCHEMA
                or status.get("candidateSha") != parts[0]
                or str(status.get("runId")) != parts[1]
                or str(status.get("attempt")) != parts[2]
                or status.get("state") not in ("COMPLETE", "RECOVERED")):
            raise OwnerError("unfinished or unknown sibling attempt")


def _load_bundle(stage: Path, manifest_sha: str, owner_uid: int) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    if not HEX_RE.fullmatch(manifest_sha):
        raise OwnerError("invalid manifest digest")
    if os.geteuid() != owner_uid:
        raise OwnerError("effective identity mismatch")
    try:
        stage_info = stage.lstat()
        parent_info = stage.parent.lstat()
    except OSError as exc:
        raise OwnerError("stage unavailable") from exc
    if stat.S_ISLNK(stage_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
        raise OwnerError("stage path contains symlink")
    if not stat.S_ISDIR(stage_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise OwnerError("stage is not a directory")
    if stage_info.st_uid != owner_uid or stat.S_IMODE(stage_info.st_mode) & 0o077:
        raise OwnerError("stage ownership or permissions invalid")
    _validate_parent_chain(stage, owner_uid)
    parts = stage.name.split("-")
    if (len(parts) != 3 or not SHA_RE.fullmatch(parts[0]) or not RUN_RE.fullmatch(parts[1])
            or not ATTEMPT_RE.fullmatch(parts[2])):
        raise OwnerError("invalid attempt directory name")
    manifest_path = _safe_path(stage, "manifest.json", owner_uid, private=True)
    if _sha256(manifest_path) != manifest_sha:
        raise OwnerError("manifest digest mismatch")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OwnerError("invalid manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise OwnerError("unsupported manifest schema")
    sha, run_id, attempt = parts
    if (manifest.get("candidateSha"), str(manifest.get("runId")), str(manifest.get("attempt"))) != (sha, run_id, attempt):
        raise OwnerError("attempt binding mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise OwnerError("manifest files missing")
    checked: dict[str, str] = {}
    for rel, expected in files.items():
        if not isinstance(rel, str) or not isinstance(expected, str) or not HEX_RE.fullmatch(expected):
            raise OwnerError("invalid file record")
        path = _safe_path(stage, rel, owner_uid)
        if _sha256(path) != expected:
            raise OwnerError("bundle file digest mismatch")
        checked[rel] = expected
    required = {f"phases/{phase}.sh" for phase in PHASES}
    required.update({f"phases/{PRE_RECOVERY}.sh", f"phases/{POST_RECOVERY}.sh"})
    if not required.issubset(checked):
        raise OwnerError("required phase missing")
    env_rel = manifest.get("environmentFile", "environment.json")
    if not isinstance(env_rel, str) or env_rel not in checked:
        raise OwnerError("environment file missing from manifest")
    try:
        values = json.loads(_safe_path(stage, env_rel, owner_uid, private=True).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OwnerError("invalid environment file") from exc
    if not isinstance(values, dict):
        raise OwnerError("environment must be an object")
    env = {"PATH": CONTROLLED_PATH}
    for key, value in values.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key)
                or key in {"PATH", "BASH_ENV", "ENV", "SHELLOPTS", "CDPATH"}
                or key.startswith(("LD_", "DYLD_")) or not isinstance(value, str)):
            raise OwnerError("unsafe environment entry")
        env[key] = value
    repo = stage / "repo"
    try:
        repo_info = repo.lstat()
    except OSError as exc:
        raise OwnerError("repository directory missing") from exc
    if (stat.S_ISLNK(repo_info.st_mode) or not stat.S_ISDIR(repo_info.st_mode)
            or repo_info.st_uid != owner_uid or stat.S_IMODE(repo_info.st_mode) & 0o022):
        raise OwnerError("repository directory identity invalid")
    _verify_git_workspace(stage, repo, sha, owner_uid)
    return manifest, checked, env


def _write_json(stage: Path, filename: str, record: dict[str, Any], owner_uid: int) -> None:
    destination = stage / filename
    fd, temp_name = tempfile.mkstemp(prefix=f".{filename}.", dir=stage)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, destination)
        os.chown(destination, owner_uid, -1)
        dir_fd = os.open(stage, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _record(manifest: dict[str, Any], state: str, phase: str | None,
            exit_code: int | None, recovery: str) -> dict[str, Any]:
    return {"schema": SCHEMA, "candidateSha": manifest["candidateSha"],
            "runId": str(manifest["runId"]), "attempt": str(manifest["attempt"]),
            "state": state, "phase": phase, "exitCode": exit_code, "recovery": recovery}


def run_owner(stage: Path, manifest_sha: str, owner_uid: int = 0,
              lock_path: Path = DEFAULT_LOCK) -> int:
    """Run a complete attempt serially; returns 0, 1 (recovered), or 2 (blocked)."""
    stage = Path(stage)
    lock_path = Path(lock_path)
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        print("GPOINT_KAMAL_OWNER=BLOCKED", flush=True)
        return 2
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("GPOINT_KAMAL_OWNER=BLOCKED", flush=True)
            return 2
        lock_info = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != owner_uid
                or stat.S_IMODE(lock_info.st_mode) & 0o077):
            print("GPOINT_KAMAL_OWNER=BLOCKED", flush=True)
            return 2
        try:
            manifest, files, env = _load_bundle(stage, manifest_sha, owner_uid)
            _refuse_unfinished_sibling(stage, owner_uid)
            if any((stage / name).exists() or (stage / name).is_symlink()
                   for name in ("owner-result.json", "owner-status.json")):
                raise OwnerError("attempt already used")
            log_path = stage / "owner.log"
            log_fd = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.fchown(log_fd, owner_uid, -1)
            with os.fdopen(log_fd, "ab", buffering=0) as log:
                _write_json(stage, "owner-status.json", _record(manifest, "RUNNING", None, None, "none"), owner_uid)
                last_phase = None
                failed_phase = None
                for phase in PHASES:
                    # Revalidate the manifest and every executable/config file at each boundary.
                    _, current_files, current_env = _load_bundle(stage, manifest_sha, owner_uid)
                    if current_files != files or current_env != env:
                        raise OwnerError("bundle changed during execution")
                    last_phase = phase
                    _write_json(stage, "owner-status.json", _record(manifest, "RUNNING", phase, None, "none"), owner_uid)
                    script = _safe_path(stage, f"phases/{phase}.sh", owner_uid)
                    result = subprocess.run(["bash", str(script)], cwd=stage / "repo", env=env,
                                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                            check=False)
                    log.flush()
                    os.fsync(log.fileno())
                    if result.returncode != 0:
                        failed_phase = phase
                        break
                if failed_phase is None:
                    complete = _record(manifest, "COMPLETE", last_phase, 0, "none")
                    _write_json(stage, "owner-status.json", complete, owner_uid)
                    _write_json(stage, "owner-result.json", complete, owner_uid)
                    print("GPOINT_KAMAL_OWNER=COMPLETE", flush=True)
                    return 0
                recovery_name = PRE_RECOVERY if failed_phase == "pause" else POST_RECOVERY
                recovery_kind = "pre" if failed_phase == "pause" else "post"
                try:
                    _load_bundle(stage, manifest_sha, owner_uid)
                    recovery_script = _safe_path(stage, f"phases/{recovery_name}.sh", owner_uid)
                    _write_json(stage, "owner-status.json",
                                _record(manifest, "RECOVERING", failed_phase, result.returncode, recovery_kind), owner_uid)
                    recovery_result = subprocess.run(["bash", str(recovery_script)], cwd=stage / "repo", env=env,
                                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                                     check=False)
                    log.flush()
                    os.fsync(log.fileno())
                    if recovery_result.returncode != 0:
                        raise OwnerError("recovery command failed")
                    recovered = _record(manifest, "RECOVERED", failed_phase, result.returncode, recovery_kind)
                    _write_json(stage, "owner-status.json", recovered, owner_uid)
                    _write_json(stage, "owner-result.json", recovered, owner_uid)
                    print("GPOINT_KAMAL_OWNER=RECOVERED", flush=True)
                    return 1
                except OwnerError:
                    blocked = _record(manifest, "BLOCKED", failed_phase, result.returncode, recovery_kind)
                    _write_json(stage, "owner-status.json", blocked, owner_uid)
                    _write_json(stage, "owner-result.json", blocked, owner_uid)
                    print("GPOINT_KAMAL_OWNER=BLOCKED", flush=True)
                    return 2
        except (OwnerError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            print("GPOINT_KAMAL_OWNER=BLOCKED", flush=True)
            return 2
    finally:
        os.close(lock_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, type=Path)
    parser.add_argument("--manifest-sha", required=True)
    args = parser.parse_args(argv)
    if not str(args.stage).startswith("/run/gpoint-kamal-owner/") or args.stage.parent != Path("/run/gpoint-kamal-owner"):
        parser.error("--stage must be a direct child of /run/gpoint-kamal-owner")
    if not HEX_RE.fullmatch(args.manifest_sha):
        parser.error("--manifest-sha must be 64 lowercase hexadecimal characters")
    return run_owner(args.stage, args.manifest_sha)


if __name__ == "__main__":
    sys.exit(main())
