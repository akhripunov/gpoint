#!/usr/bin/env python3
"""Controlled migration of the local AGATA Worker LaunchAgent runtime.

The plan is read-only. Apply is fail-closed on non-main/dirty source, active
inference, a concurrent transition, invalid credentials/configuration, or any
failed health/restart check. VPS, database, and MAXIBOT are never contacted.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

LABEL = "one.gpoint.agata-codex-worker"
HOME = Path.home()
ROOT = HOME / "Downloads/Codex/Workers/AGATA Workers"
LEGACY = HOME / "Library/Application Support/GPoint/agata-codex-worker"
CONFIG = HOME / ".config/gpoint/agata-worker.env"
PLIST = HOME / "Library/LaunchAgents" / f"{LABEL}.plist"
TRANSITION_LOCK = HOME / ".config/gpoint/agata-worker-pool-transition.lock"
RELEASE_LOCK = HOME / ".config/gpoint/release-production.lock"
CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
WORKER_DIR = Path(__file__).resolve().parent
ASSETS = ("gpoint_mac_worker.py", "agata_worker_pool.py", "lab_queue.py",
          "lab_fixture_manifest.json", "requirements.txt", "run_codex_worker.sh")
UTC = timezone.utc


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=WORKER_DIR.parents[2], check=True,
                          capture_output=True, text=True, timeout=15).stdout.strip()


def launchctl_loaded() -> bool:
    return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=8, check=False).returncode == 0


def _release_lock_busy() -> bool:
    if not RELEASE_LOCK.exists():
        return False
    descriptor = os.open(RELEASE_LOCK, os.O_RDONLY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def safe_worker_status(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    fields = ("state", "current_job_present", "last_poll_at", "last_heartbeat_at", "last_error_code")
    result = {key: value.get(key) for key in fields if key in value}
    targets = value.get("targets")
    if isinstance(targets, dict):
        result["targets"] = {name: {key: lane.get(key) for key in fields if key in lane}
                              for name, lane in targets.items()
                              if name in {"GPOINT", "GSALES", "LAB"} and isinstance(lane, dict)}
    return result


def _running_inference_pids() -> list[int]:
    result = subprocess.run(["ps", "-axo", "pid=,command="], check=True,
                            capture_output=True, text=True, timeout=8)
    found = []
    for line in result.stdout.splitlines():
        lower = line.lower()
        if "gpoint_mac_worker.py execute-slot" in lower:
            try:
                found.append(int(line.strip().split(None, 1)[0]))
            except (ValueError, IndexError):
                continue
    for record_path in (LEGACY / "inference-process.json", ROOT / "state/inference-process.json"):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = record.get("pid")
        if pid is None and record.get("state") == "starting":
            found.append(-1)
        elif isinstance(pid, int) and pid > 1:
            try:
                os.kill(pid, 0)
                found.append(pid)
            except ProcessLookupError:
                pass
            except PermissionError:
                found.append(pid)
    return found


def _launchagent_pid() -> tuple[int, str]:
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], check=True,
                            capture_output=True, text=True, timeout=8)
    match = re.search(r"^\s*pid = ([0-9]+)\s*$", result.stdout, re.MULTILINE)
    if not match:
        raise RuntimeError("launchagent_pid_ambiguous")
    pid = int(match.group(1))
    command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], check=True,
                             capture_output=True, text=True, timeout=8).stdout.strip()
    if "gpoint_mac_worker.py watch" not in command or not any(
            str(root) in command for root in (LEGACY, ROOT / "runtime")):
        raise RuntimeError("launchagent_process_identity_invalid")
    return pid, command


def _pause_idle_launchagent() -> int | None:
    if not launchctl_loaded():
        return None
    pid, _command = _launchagent_pid()
    os.kill(pid, signal.SIGSTOP)
    time.sleep(0.2)
    try:
        if inventory()["active_inference"]:
            raise RuntimeError("active_inference_blocks_transition")
    except Exception:
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        raise
    return pid


def inventory() -> dict:
    if LEGACY.is_symlink() or (ROOT.exists() and ROOT.is_symlink()):
        raise RuntimeError("worker_runtime_root_symlink_invalid")
    if not CONFIG.is_file() or CONFIG.is_symlink() or CONFIG.stat().st_uid != os.getuid() or CONFIG.stat().st_mode & 0o077:
        raise RuntimeError("worker_config_missing_or_permissions_invalid")
    if not CODEX.is_file():
        raise RuntimeError("codex_cli_missing")
    legacy_status = safe_worker_status(LEGACY / "worker-status.json")
    new_status = safe_worker_status(ROOT / "status/worker-status.json")
    pids = _running_inference_pids()
    lab_running = sum(len(list((queue / "running").glob("*.json")))
                      for queue in (LEGACY / "lab-queue", ROOT / "queues/lab")
                      if (queue / "running").is_dir())
    if PLIST.is_symlink() or (PLIST.exists() and PLIST.stat().st_uid != os.getuid()):
        raise RuntimeError("launchagent_plist_owner_invalid")
    old_plist = PLIST.read_bytes() if PLIST.is_file() else b""
    old_args = []
    if old_plist:
        try:
            old_args = plistlib.loads(old_plist).get("ProgramArguments", [])
        except Exception as exc:
            raise RuntimeError("launchagent_plist_invalid") from exc
    def active_status(status: dict) -> bool:
        if status.get("current_job_present") or status.get("state") == "running":
            return True
        targets = status.get("targets", {})
        return any(isinstance(value, dict) and (value.get("current_job_present") or value.get("state") == "running")
                   for value in targets.values()) if isinstance(targets, dict) else False
    active = bool(pids or lab_running or active_status(legacy_status) or active_status(new_status))
    return {
        "label": LABEL,
        "launchagent_loaded": launchctl_loaded(),
        "production_release_lock_busy": _release_lock_busy(),
        "launchagent_program_arguments": old_args,
        "legacy_runtime_present": LEGACY.exists(),
        "legacy_worker_status": legacy_status,
        "new_worker_status": new_status,
        "active_inference_pids": pids,
        "lab_running_jobs": lab_running,
        "active_inference": active,
        "external_config_present_mode_0600": True,
        "codex_cli_present": True,
        "configured_concurrency": 1,
        "provider_parallel_capability": "NOT_VERIFIED",
    }


def candidate() -> dict:
    head = git("rev-parse", "HEAD")
    main = git("rev-parse", "origin/main")
    if head != main:
        raise RuntimeError("candidate_must_be_origin_main")
    if git("status", "--porcelain"):
        raise RuntimeError("candidate_worktree_dirty")
    assets = {name: sha256(WORKER_DIR / name) for name in ASSETS}
    bundle_hash = hashlib.sha256(json.dumps(assets, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"source_sha": head, "assets": assets, "bundle_sha256": bundle_hash}


def read_only_plan() -> dict:
    return {"mode": "READ_ONLY_PLAN", "candidate": candidate(), "inventory": inventory(),
            "runtime_root": str(ROOT), "legacy_root": str(LEGACY), "mutation_performed": False}


def _atomic_json(path: Path, value: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _stage_runtime(manifest: dict) -> Path:
    release = ROOT / "runtime" / manifest["source_sha"]
    if release.exists():
        for name in ASSETS:
            if sha256(release / name) != manifest["assets"][name]:
                raise RuntimeError("existing_runtime_bundle_mismatch")
        return release
    stage = release.with_name(f".{release.name}.staging-{os.getpid()}")
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, mode=0o700)
    for name in ASSETS:
        shutil.copy2(WORKER_DIR / name, stage / name)
    python = shutil.which("python3.12")
    if not python:
        raise RuntimeError("python_312_missing")
    subprocess.run([python, "-m", "venv", str(stage / ".venv")], check=True,
                   capture_output=True, timeout=60)
    subprocess.run([str(stage / ".venv/bin/python3.12"), "-m", "pip", "install", "--disable-pip-version-check",
                    "-r", str(stage / "requirements.txt")], check=True, timeout=180)
    for name in ASSETS:
        if sha256(stage / name) != manifest["assets"][name]:
            raise RuntimeError("staged_runtime_asset_mismatch")
    (stage / "bundle-manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    (stage / "bundle-manifest.json").chmod(0o600)
    release.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.replace(stage, release)
    return release


def _migrate_tenant_binding(manifest: dict) -> None:
    source = LEGACY / "tenant-bindings.json"
    destination = ROOT / "config/tenant-bindings.json"
    if not source.is_file() or source.is_symlink() or destination.exists():
        return
    try:
        stat = source.stat()
        value = json.loads(source.read_text(encoding="utf-8"))
        captured = datetime.fromisoformat(str(value.get("snapshot_at", "")).replace("Z", "+00:00"))
        tenant = UUID(value.get("tenant_id", ""))
        source_id = value.get("source_id", "")
        if (stat.st_uid != os.getuid() or stat.st_mode & 0o077
                or set(value) != {"version", "candidate_sha", "worker_sha256", "tenant_id", "source_id", "snapshot_at"}
                or value.get("version") != 1
                or value.get("worker_sha256") != sha256(LEGACY / "gpoint_mac_worker.py")
                or not isinstance(value.get("candidate_sha"), str) or len(value["candidate_sha"]) != 40
                or tenant.int == 0 or str(tenant) != value.get("tenant_id")
                or captured.tzinfo is None
                or not re.fullmatch(r"gpoint-vps:[0-9a-f]{40}:api=sha256:[0-9a-f]{64}:agata=sha256:[0-9a-f]{64}", source_id)):
            raise ValueError
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("legacy_tenant_binding_invalid") from exc
    value["candidate_sha"] = manifest["source_sha"]
    value["worker_sha256"] = manifest["assets"]["gpoint_mac_worker.py"]
    _atomic_json(destination, value)


def _render_plist(runtime: Path) -> bytes:
    wrapper = str(runtime / "run_codex_worker.sh")
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [wrapper, "watch"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(runtime),
        "StandardOutPath": str(ROOT / "logs/worker.stdout.log"),
        "StandardErrorPath": str(ROOT / "logs/worker.stderr.log"),
        "EnvironmentVariables": {
            "HOME": str(HOME),
            "GPOINT_AGATA_WORKER_CONFIG": str(CONFIG),
            "GPOINT_AGATA_WORKER_SUPPORT": str(ROOT),
            "AGATA_WORKER_POOL_CONFIG": str(ROOT / "config/pool.env"),
        },
    }, fmt=plistlib.FMT_XML, sort_keys=True)


def _safe_remove_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _same_tree(left: Path, right: Path) -> bool:
    if not left.is_dir() or left.is_symlink() or not right.is_dir() or right.is_symlink():
        return False

    def inventory_tree(root: Path) -> dict[str, tuple[str, str | None]]:
        result = {}
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in (*dirs, *files):
                item = Path(directory) / name
                rel = str(item.relative_to(root))
                info = item.lstat()
                if item.is_symlink():
                    result[rel] = ("symlink", os.readlink(item))
                elif item.is_dir():
                    result[rel] = ("dir", None)
                else:
                    # Compare all bytes in memory, but never store or print
                    # hashes of potentially secret-bearing files.
                    result[rel] = (sha256(item), None)
        return result

    return inventory_tree(left) == inventory_tree(right)


def _health_check(deadline: float = 60.0) -> dict:
    python = ROOT / "runtime/current/.venv/bin/python3.12"
    worker = ROOT / "runtime/current/gpoint_mac_worker.py"
    last = {}
    until = time.monotonic() + deadline
    while time.monotonic() < until:
        if not launchctl_loaded():
            time.sleep(2)
            continue
        result = subprocess.run([str(python), str(worker), "status"], capture_output=True,
                                text=True, timeout=15, check=False)
        try:
            last = json.loads(result.stdout)
        except ValueError:
            last = {}
        lanes = last.get("lanes", {}) if isinstance(last.get("lanes"), dict) else {}
        production_healthy = all(lanes.get(lane, {}).get("health") == "HEALTHY" for lane in ("GPOINT", "GSALES"))
        if result.returncode == 0 and last.get("pool") and lanes and production_healthy:
            return last
        time.sleep(2)
    raise RuntimeError("new_worker_health_check_failed")


def _network_state_sha256() -> str:
    observations = []
    for command in (("/usr/sbin/scutil", "--nwi"), ("/sbin/route", "-n", "get", "default")):
        result = subprocess.run(command, capture_output=True, text=True, timeout=8, check=True)
        observations.append(result.stdout)
    return hashlib.sha256("\n".join(observations).encode()).hexdigest()


def _ensure_pool_config() -> None:
    pool_config = ROOT / "config/pool.env"
    if not pool_config.exists():
        pool_config.write_text("AGATA_WORKER_CONCURRENCY=1\nAGATA_CODEX_PARALLEL_CAPABILITY=NOT_VERIFIED\nAGATA_WORKER_POLL_SECONDS=3\n")
        pool_config.chmod(0o600)
        return
    if pool_config.is_symlink() or pool_config.stat().st_uid != os.getuid() or pool_config.stat().st_mode & 0o077:
        raise RuntimeError("worker_pool_config_permissions_invalid")
    values = {}
    for line in pool_config.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if item and not item.startswith("#") and "=" in item:
            key, value = item.split("=", 1)
            if key not in {"AGATA_WORKER_CONCURRENCY", "AGATA_CODEX_PARALLEL_CAPABILITY", "AGATA_WORKER_POLL_SECONDS"}:
                raise RuntimeError("worker_pool_config_key_invalid")
            values[key] = value.strip().strip('"').strip("'")
    if values.get("AGATA_WORKER_CONCURRENCY", "1") != "1" or values.get("AGATA_CODEX_PARALLEL_CAPABILITY", "NOT_VERIFIED") != "NOT_VERIFIED":
        raise RuntimeError("worker_pool_must_start_at_n1_unverified")


def apply() -> dict:
    TRANSITION_LOCK.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(TRANSITION_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise RuntimeError("worker_transition_already_running") from exc
    try:
        # Repeat all safety checks at the point of mutation.
        manifest = candidate()
        before = inventory()
        if before["production_release_lock_busy"]:
            raise RuntimeError("production_release_lock_busy")
        if before["active_inference"]:
            raise RuntimeError("active_inference_blocks_transition")
        existing_backup = ROOT / "state/rollback/legacy-runtime"
        if existing_backup.exists() and not LEGACY.exists():
            raise RuntimeError("worker_runtime_already_migrated")
        network_before = _network_state_sha256()
        ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        if ROOT.is_symlink() or ROOT.stat().st_uid != os.getuid():
            raise RuntimeError("worker_runtime_root_owner_invalid")
        ROOT.chmod(0o700)
        for name in ("runtime", "config", "state", "locks", "receipts", "status", "logs", "queues", "slots"):
            directory = ROOT / name
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink() or directory.stat().st_uid != os.getuid():
                raise RuntimeError("worker_runtime_directory_owner_invalid")
            directory.chmod(0o700)
        release = _stage_runtime(manifest)
        _ensure_pool_config()
        backup = ROOT / "state/rollback/legacy-runtime"
        if PLIST.is_file() and not (ROOT / "state/rollback/original-launchagent.plist").exists():
            shutil.copy2(PLIST, ROOT / "state/rollback/original-launchagent.plist")
            (ROOT / "state/rollback/original-launchagent.plist").chmod(0o600)
        original_plist = PLIST.read_bytes() if PLIST.is_file() else None
        target_link = ROOT / "runtime/current"
        prior_link_target = os.readlink(target_link) if target_link.is_symlink() else None
        maintenance_marker = ROOT / "state/maintenance-stop"
        next_plist = _render_plist(target_link)
        switch_started = False
        try:
            # Freeze the verified LaunchAgent and repeat the idle check while
            # it cannot claim another job. This closes the preflight/bootout race.
            marker_fd = os.open(maintenance_marker, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(marker_fd)
            maintenance_marker.chmod(0o600)
            paused_pid = _pause_idle_launchagent()
            if paused_pid is not None:
                switch_started = True
                try:
                    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=True, timeout=20)
                finally:
                    try:
                        os.kill(paused_pid, signal.SIGCONT)
                    except ProcessLookupError:
                        pass
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and _running_inference_pids():
                time.sleep(1)
            if _running_inference_pids():
                raise RuntimeError("worker_process_did_not_stop")
            # Snapshot only while the old process is stopped, so queue/status
            # state cannot change between rollback capture and the switch.
            if backup.exists():
                if not LEGACY.exists() or not _same_tree(LEGACY, backup):
                    raise RuntimeError("rollback_snapshot_does_not_match_legacy")
            elif LEGACY.exists():
                backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copytree(LEGACY, backup, symlinks=True, copy_function=shutil.copy2)
                for directory, dirs, files in os.walk(backup):
                    Path(directory).chmod(0o700)
                    for filename in files:
                        item = Path(directory) / filename
                        if not item.is_symlink():
                            item.chmod(0o600 if filename.endswith((".env", ".json")) else 0o700 if os.access(item, os.X_OK) else 0o600)
            else:
                raise RuntimeError("legacy_runtime_missing")
            # Migrate only queue/status state. Secret credentials stay in their
            # existing external mode-600 config; the LAB approval is rechecked
            # against the new bundle and remains fail-closed if stale.
            migrations = (("lab-queue", ROOT / "queues/lab"),
                          ("worker-status.json", ROOT / "status/worker-status.json"),
                          ("lab-approval-usage", ROOT / "state/lab-approval-usage"))
            for source_name, destination in migrations:
                source = LEGACY / source_name
                if source.exists() and not destination.exists():
                    shutil.copytree(source, destination, dirs_exist_ok=True) if source.is_dir() else shutil.copy2(source, destination)
            _migrate_tenant_binding(manifest)
            switch_started = True
            next_link = ROOT / "runtime/.current-next"
            _safe_remove_tree(next_link)
            next_link.symlink_to(release.name)
            os.replace(next_link, target_link)
            temp = PLIST.with_suffix(".plist.new")
            temp.write_bytes(next_plist)
            temp.chmod(0o600)
            os.replace(temp, PLIST)
            maintenance_marker.unlink(missing_ok=True)
            subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], check=True, timeout=20)
            subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{LABEL}"], check=True, timeout=15)
            status = _health_check()
            # Explicit restart/recovery verification for the same LaunchAgent.
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"], check=True, timeout=20)
            restarted = _health_check()
            if not launchctl_loaded() or restarted.get("configured_concurrency") != 1:
                raise RuntimeError("restart_verification_failed")
            if any(not isinstance(restarted.get("lanes", {}).get(lane), dict) for lane in ("GPOINT", "GSALES", "LAB")):
                raise RuntimeError("lane_status_missing")
            network_after = _network_state_sha256()
            if network_before != network_after:
                raise RuntimeError("network_state_changed_during_transition")
            # Legacy runtime is removed only after health and restart pass.
            _safe_remove_tree(LEGACY)
            evidence = {
                "state": "COMPLETE", "at": datetime.now(UTC).isoformat(), "label": LABEL,
                "source_sha": manifest["source_sha"], "bundle_sha256": manifest["bundle_sha256"],
                "configured_concurrency": 1, "provider_parallel_capability": "NOT_VERIFIED",
                "health_check": status, "restart_check": restarted,
                "legacy_runtime_removed": not LEGACY.exists(),
                "network_state_sha256_before": network_before,
                "network_state_sha256_after": network_after,
                "network_configuration_writes_performed": False,
                "rollback_snapshot": str(backup),
                "original_plist_sha256": hashlib.sha256(original_plist or b"").hexdigest(),
            }
            _atomic_json(ROOT / "receipts/runtime-migration.json", evidence)
            return evidence
        except Exception:
            if not switch_started:
                maintenance_marker.unlink(missing_ok=True)
                raise
            # Restore old service and its files from the private snapshot.
            try:
                subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], timeout=15, check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if not LEGACY.exists() and backup.exists():
                    shutil.copytree(backup, LEGACY, symlinks=True)
                maintenance_marker.unlink(missing_ok=True)
                if original_plist is not None:
                    PLIST.write_bytes(original_plist)
                    PLIST.chmod(0o600)
                    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], timeout=20, check=False)
                    subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{LABEL}"], timeout=15, check=False)
                target_link = ROOT / "runtime/current"
                if prior_link_target is None:
                    _safe_remove_tree(target_link)
                else:
                    temporary_link = ROOT / "runtime/.current-rollback"
                    _safe_remove_tree(temporary_link)
                    temporary_link.symlink_to(prior_link_target)
                    os.replace(temporary_link, target_link)
                _atomic_json(ROOT / "receipts/runtime-migration-rollback.json", {
                    "state": "ROLLED_BACK", "at": datetime.now(UTC).isoformat(),
                    "source_sha": manifest["source_sha"], "legacy_restored": LEGACY.exists(),
                    "launchagent_restored": original_plist is not None,
                })
            finally:
                raise
    finally:
        os.close(fd)


def rollback() -> dict:
    backup = ROOT / "state/rollback/legacy-runtime"
    original_plist_file = ROOT / "state/rollback/original-launchagent.plist"
    if not backup.is_dir() or not original_plist_file.is_file():
        raise RuntimeError("rollback_snapshot_missing")
    TRANSITION_LOCK.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(TRANSITION_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise RuntimeError("worker_transition_already_running") from exc
    try:
        before = inventory()
        if before["production_release_lock_busy"]:
            raise RuntimeError("production_release_lock_busy")
        if before["active_inference"]:
            raise RuntimeError("active_inference_blocks_rollback")
        paused_pid = _pause_idle_launchagent()
        if paused_pid is not None:
            try:
                subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=True, timeout=20)
            finally:
                try:
                    os.kill(paused_pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and _running_inference_pids():
            time.sleep(1)
        if _running_inference_pids():
            raise RuntimeError("worker_process_did_not_stop")
        _safe_remove_tree(LEGACY)
        shutil.copytree(backup, LEGACY, symlinks=True)
        LEGACY.chmod(0o700)
        original_plist = original_plist_file.read_bytes()
        PLIST.write_bytes(original_plist)
        PLIST.chmod(0o600)
        (ROOT / "state/maintenance-stop").unlink(missing_ok=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], check=True, timeout=20)
        subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{LABEL}"], check=True, timeout=15)
        if not launchctl_loaded():
            raise RuntimeError("rollback_launchagent_not_loaded")
        evidence = {
            "state": "ROLLED_BACK", "at": datetime.now(UTC).isoformat(), "label": LABEL,
            "legacy_runtime_restored": LEGACY.exists(), "launchagent_loaded": True,
            "restored_plist_sha256": hashlib.sha256(original_plist).hexdigest(),
            "rollback_snapshot": str(backup),
        }
        _atomic_json(ROOT / "receipts/runtime-migration-rollback.json", evidence)
        return evidence
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "apply", "rollback"))
    args = parser.parse_args()
    result = read_only_plan() if args.action == "plan" else rollback() if args.action == "rollback" else apply()
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
