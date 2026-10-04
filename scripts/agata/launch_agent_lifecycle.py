"""Serialized, observable LaunchAgent transitions for the AGATA worker."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence


_PID = re.compile(r"\bpid\s*=\s*(\d+)")
_SECRET_LINE = re.compile(
    r"(?im)^(\s*[A-Z0-9_.-]*(?:TOKEN|SECRET|PASSWORD|API_KEY|DATABASE_URL|CREDENTIAL|PRIVATE_KEY)"
    r"[A-Z0-9_.-]*\s*[:=]\s*).*$"
)
_SECRET_TOKEN = re.compile(r"(?i)\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{16,})\b")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_ENVIRONMENT_BLOCK = re.compile(r"(?ims)((?:EnvironmentVariables|environment)\s*=\s*\{)(.*?)(^\s*\})")
_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@")
DEFAULT_OPERATION_LOCK = Path.home() / ".config/gpoint/unified-worker-transition.lock"
DEFAULT_RECEIPT_PATH = (
    Path.home()
    / "Library/Application Support/GPoint/agata-codex-worker/launch-agent-lifecycle.jsonl"
)


def _redact_output(value: str) -> str:
    value = _SECRET_LINE.sub(r"\1[REDACTED]", value)
    value = _ENVIRONMENT_BLOCK.sub(
        lambda match: match.group(1)
        + re.sub(
            r"(?m)^(\s*[A-Za-z_][A-Za-z0-9_]*\s*(?:=|=>)\s*).*$",
            r"\1[REDACTED]",
            match.group(2),
        )
        + match.group(3),
        value,
    )
    value = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", value)
    value = _BEARER.sub("Bearer [REDACTED]", value)
    return _SECRET_TOKEN.sub("[REDACTED_TOKEN]", value)


class LaunchAgentCommandError(RuntimeError):
    def __init__(self, stage: str, result: subprocess.CompletedProcess[str]):
        detail = _redact_output((result.stderr or result.stdout or "").strip())
        super().__init__(f"{stage} failed with error {result.returncode}: {detail}")
        self.stage = stage
        self.returncode = result.returncode


class LaunchAgentController:
    """Coordinates LaunchAgent transitions and writes a private JSONL receipt."""

    def __init__(
        self,
        *,
        lock_path: Path | None = None,
        receipt_path: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        pid_is_alive: Callable[[int], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[float], None] = time.sleep,
        timeout_seconds: float = 15.0,
        lock_timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        # The default lock is shared with scripts/transition-agata-worker-v3.py.
        self.lock_path = lock_path or DEFAULT_OPERATION_LOCK
        self.receipt_path = receipt_path or DEFAULT_RECEIPT_PATH
        self.runner = runner
        self.pid_is_alive = pid_is_alive or self._pid_is_alive
        self.monotonic = monotonic
        self.wait = wait
        self.timeout_seconds = timeout_seconds
        self.lock_timeout_seconds = lock_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @contextmanager
    def _operation_lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        deadline = self.monotonic() + self.lock_timeout_seconds
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - self.monotonic()
                    if remaining <= 0:
                        self._record(
                            stage="operation_lock",
                            description="Acquire exclusive LaunchAgent lifecycle lock",
                            result=subprocess.CompletedProcess(
                                ["launchctl"], 124, "", "launch_agent_operation_lock_timeout"
                            ),
                        )
                        raise TimeoutError("launch_agent_operation_lock_timeout")
                    self.wait(min(self.poll_interval_seconds, remaining))
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @staticmethod
    def _redact(value: str) -> str:
        return _redact_output(value)

    def _record(
        self,
        *,
        stage: str,
        description: str,
        result: subprocess.CompletedProcess[str],
    ) -> None:
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "command_description": description,
            "exit_code": result.returncode,
            "stdout": self._redact(result.stdout or ""),
            "stderr": self._redact(result.stderr or ""),
        }
        descriptor = os.open(
            self.receipt_path,
            os.O_CREAT | os.O_APPEND | os.O_WRONLY,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, (json.dumps(entry, ensure_ascii=False) + "\n").encode())
        finally:
            os.close(descriptor)

    def _run(
        self,
        command: Sequence[str],
        *,
        stage: str,
        description: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self.runner(
                list(command),
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            result = subprocess.CompletedProcess(
                list(command),
                None,
                self._as_text(exc.stdout),
                self._as_text(exc.stderr) or "command timed out",
            )
            self._record(stage=stage, description=description, result=result)
            raise TimeoutError(f"{stage} timed out") from exc
        self._record(stage=stage, description=description, result=result)
        if check and result.returncode != 0:
            raise LaunchAgentCommandError(stage, result)
        return result

    @staticmethod
    def _as_text(value: bytes | str | None) -> str:
        if isinstance(value, bytes):
            return value.decode(errors="replace")
        return value or ""

    def _service_state(self, domain: str, label: str, *, stage: str) -> tuple[bool, int | None]:
        result = self._run(
            ["launchctl", "print", f"{domain}/{label}"],
            stage=stage,
            description=f"Inspect LaunchAgent {label} in {domain}",
            check=False,
        )
        if result.returncode != 0:
            if result.returncode in (3, 113) or "Could not find service" in (result.stderr or ""):
                return False, None
            raise LaunchAgentCommandError(stage, result)
        match = _PID.search(result.stdout or "")
        return True, int(match.group(1)) if match else None

    def _wait_until_unloaded(self, domain: str, label: str, old_pid: int | None, *, stage: str) -> None:
        deadline = self.monotonic() + self.timeout_seconds
        while True:
            service_present, _ = self._service_state(domain, label, stage=f"{stage}_state_check")
            process_alive = old_pid is not None and self.pid_is_alive(old_pid)
            if not service_present and not process_alive:
                return
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                message = f"{stage} timed out waiting for LaunchAgent removal and old worker exit"
                self._record(
                    stage=f"{stage}_timeout",
                    description=f"Wait for LaunchAgent {label} removal and old worker process exit",
                    result=subprocess.CompletedProcess(["launchctl"], 124, "", message),
                )
                raise TimeoutError(message)
            self.wait(min(self.poll_interval_seconds, remaining))

    def _unload_and_wait(self, domain: str, label: str, *, stage: str = "bootout") -> None:
        service_present, old_pid = self._service_state(domain, label, stage=f"{stage}_initial_state")
        if service_present:
            self._run(
                ["launchctl", "bootout", f"{domain}/{label}"],
                stage=stage,
                description=f"Unload LaunchAgent {label} from {domain}",
            )
        self._wait_until_unloaded(domain, label, old_pid, stage=f"{stage}_wait_unloaded")

    @staticmethod
    def _atomic_write(path: Path, contents: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _bootstrap(self, domain: str, label: str, plist_path: Path, *, stage: str) -> None:
        self._run(
            ["launchctl", "bootstrap", domain, str(plist_path)],
            stage=stage,
            description=f"Load LaunchAgent {label} from its plist into {domain}",
        )

    def replace_plist(self, *, domain: str, label: str, plist_path: Path, candidate: bytes) -> None:
        """Unload/wait, atomically replace the plist, then bootstrap; restore old bytes on failure."""
        with self._operation_lock():
            previous = plist_path.read_bytes() if plist_path.exists() else None
            self._unload_and_wait(domain, label)
            try:
                self._atomic_write(plist_path, candidate)
                self._bootstrap(domain, label, plist_path, stage="candidate_bootstrap")
            except Exception as candidate_error:
                try:
                    self._unload_and_wait(domain, label, stage="candidate_rollback_bootout")
                    if previous is None:
                        plist_path.unlink(missing_ok=True)
                    else:
                        self._atomic_write(plist_path, previous)
                        self._bootstrap(domain, label, plist_path, stage="rollback_bootstrap")
                except Exception as rollback_error:
                    raise RuntimeError(
                        f"candidate activation failed ({candidate_error}); "
                        f"rollback failed ({rollback_error})"
                    ) from rollback_error
                raise RuntimeError(f"candidate activation failed; original plist restored: {candidate_error}") from candidate_error

    def remove_agent(
        self,
        *,
        domain: str,
        label: str,
        plist_path: Path,
        files_to_remove: Sequence[Path] = (),
    ) -> None:
        """Wait for launchd and the old PID before deleting agent files."""
        with self._operation_lock():
            self._unload_and_wait(domain, label)
            for path in (plist_path, *files_to_remove):
                path.unlink(missing_ok=True)

    def activate(self, domain: str, label: str, plist_path: Path) -> None:
        """Bootstrap only after proving the service is absent, under the shared lock."""
        with self._operation_lock():
            self._wait_until_unloaded(domain, label, None, stage="pre_bootstrap_wait_unloaded")
            self._bootstrap(domain, label, plist_path, stage="bootstrap")
