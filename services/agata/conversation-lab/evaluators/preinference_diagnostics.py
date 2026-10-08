"""Private, payload-free receipts for failures before LAB inference."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re


STAGES = frozenset({
    "PROVIDER_CONFIG", "ENV_VALIDATION", "PYTHON_VALIDATION", "PROMPTFOO_CLI_VALIDATION",
    "LAB_QUEUE_VALIDATION", "APPROVAL_VALIDATION", "PHASE_STATE_VALIDATION",
    "IMMUTABLE_V2_JOB_CONTRACT", "GENERATOR_RESERVATION",
})
INVOCATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
REASON_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,95}$")
SOURCE_RE = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)?$")


def write_preinference_failure(
    directory: str | Path,
    *,
    invocation_id: str,
    stage: str,
    reason_code: str,
    exception: BaseException,
    source_location: str,
    generator_reservation_created: bool | None = False,
    worker_job_submitted: bool = False,
) -> Path:
    """Write one private receipt containing only stable, non-sensitive metadata."""
    root = Path(directory)
    if (not root.is_absolute() or not root.is_dir() or root.is_symlink()
            or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077):
        raise PermissionError("private pre-inference receipt directory is unsafe")
    if (not INVOCATION_ID_RE.fullmatch(invocation_id) or stage not in STAGES
            or not REASON_CODE_RE.fullmatch(reason_code) or not SOURCE_RE.fullmatch(source_location)):
        raise ValueError("pre-inference receipt metadata is invalid")
    payload = {
        "schema_version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "invocation_id": invocation_id,
        "stage": stage,
        "reason_code": reason_code,
        "exception_class": type(exception).__name__,
        "source_location": source_location,
        "generator_reservation_created": (None if generator_reservation_created is None
                                           else bool(generator_reservation_created)),
        "worker_job_submitted": bool(worker_job_submitted),
    }
    path = root / f"{invocation_id}.preinference-diagnostic.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
    return path
