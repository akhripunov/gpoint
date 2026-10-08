"""Private local-only spool for synthetic Conversation Lab jobs."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from agata_worker_pool import LANE_SLOTS

MAX_PAYLOAD_BYTES = 64 * 1024
DEFAULT_MAX_PENDING = 16
MAX_TIMEOUT_SECONDS = 180
FIXTURE_MANIFEST_SHA256 = "2bf4936187d4ff7c1f67d7a97fdc43c7ab5025d00bd3cbbb654c3d62ccd865d1"
FIXTURE_MANIFEST = Path(__file__).with_name("lab_fixture_manifest.json")
PROMPTFOO_COMPAT_MANIFEST_SHA256 = "9e3a6e0536948787993057e3389b67378bd33012cbdad5807beddb3b9573e208"
PROMPTFOO_COMPAT_MANIFEST = Path(__file__).with_name("lab_promptfoo_compat_manifest.json")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REQUEST_KEYS = {
    "instructions", "persona", "communication_settings", "memories",
    "dialogue_summaries", "history", "user_message",
}
JOB_KEYS = {
    "job_id", "experiment_id", "configuration_id", "origin", "scenario_id",
    "turn_index", "data_classification", "timeout_seconds", "history_receipt_ids", "request",
}
JUDGE_EXPERIMENT_ID = "agata-lab-judge-v1"
JUDGE_INSTRUCTIONS = "AGATA Lab isolated judge protocol v1."
JUDGE_SLOT_ID = "slot-03"
JUDGE_CORE_DIMENSIONS = (
    "naturalness", "human_likeness", "emotional_appropriateness", "persona_consistency",
    "initiative", "less_ai_template_like", "conversation_coherence", "direct_request_fulfillment_responsiveness",
)
JUDGE_CONDITIONAL_DIMENSIONS = ("memory", "disagreement_boundaries", "humor_flirt", "engagement")
JUDGE_PRIVATE_MARKERS = ("baseline", "naturalness-v1", "candidate_sha", "model_id", "gpt-6-luna", "policy text", "judge 1", "judge 2", "judge 3")


class LabQueueError(RuntimeError):
    """A local LAB submission or queue operation was rejected."""


def worker_bundle_sha256() -> str:
    """Hash the exact Worker asset set used to bind a private LAB approval."""
    worker_dir = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    assets = (
        ("gpoint_mac_worker.py", worker_dir / "gpoint_mac_worker.py"),
        ("agata_worker_pool.py", worker_dir / "agata_worker_pool.py"),
        ("lab_queue.py", Path(__file__)),
        ("lab_fixture_manifest.json", FIXTURE_MANIFEST),
        ("lab_promptfoo_compat_manifest.json", PROMPTFOO_COMPAT_MANIFEST),
        ("run_codex_worker.sh", worker_dir / "run_codex_worker.sh"),
        ("requirements.txt", worker_dir / "requirements.txt"),
    )
    for name, path in assets:
        if not path.is_file() or path.is_symlink():
            raise LabQueueError("worker_asset_missing")
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def load_owner_approval(path: Path, *, expected_bundle_sha256: str, now=None) -> dict:
    """Validate the Worker-owned, file-backed LAB approval without inference."""
    path = Path(path)
    try:
        stat = path.stat()
        if path.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise ValueError
        approval = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise LabQueueError("lab_owner_approval_missing_or_invalid") from exc
    required = {"approval_id", "worker_bundle_sha256", "experiment_ids", "configuration_ids", "scenario_ids", "max_calls", "expires_at"}
    if not isinstance(approval, dict) or set(approval) != required:
        raise LabQueueError("lab_owner_approval_schema_invalid")
    if (not isinstance(approval["approval_id"], str) or not ID_RE.fullmatch(approval["approval_id"])
            or approval["worker_bundle_sha256"] != expected_bundle_sha256
            or not all(isinstance(approval[key], list) and approval[key] and all(
                isinstance(item, str) and ID_RE.fullmatch(item) for item in approval[key]
            ) for key in ("experiment_ids", "configuration_ids", "scenario_ids"))
            or isinstance(approval["max_calls"], bool) or not isinstance(approval["max_calls"], int)
            or not 1 <= approval["max_calls"] <= 8):
        raise LabQueueError("lab_owner_approval_scope_invalid")
    try:
        expires = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise LabQueueError("lab_owner_approval_expiry_invalid") from exc
    current = datetime.now(timezone.utc) if now is None else now
    if expires.tzinfo is None or expires <= current:
        raise LabQueueError("lab_owner_approval_expired")
    return approval


def owner_approval_usage_calls(root: Path, approval: dict) -> int:
    """Read the Worker-owned usage receipt; malformed state fails closed."""
    root = Path(root)
    state_dir = root / "state"
    usage_dir = state_dir / "lab-approval-usage"
    usage = usage_dir / (approval["approval_id"] + ".json")
    for directory in (root, state_dir, usage_dir):
        if directory.is_symlink():
            raise LabQueueError("lab_approval_usage_invalid")
        try:
            stat = directory.stat()
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise LabQueueError("lab_approval_usage_invalid") from exc
        if (not directory.is_dir() or stat.st_uid != os.getuid() or stat.st_mode & 0o077):
            raise LabQueueError("lab_approval_usage_invalid")
    if usage.is_symlink():
        raise LabQueueError("lab_approval_usage_invalid")
    if not usage.exists():
        return 0
    try:
        stat = usage.stat()
        if (not usage.is_file() or stat.st_uid != os.getuid() or stat.st_mode & 0o077):
            raise ValueError
        saved = json.loads(usage.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise LabQueueError("lab_approval_usage_invalid") from exc
    calls = saved.get("calls") if isinstance(saved, dict) else None
    if (not isinstance(saved, dict) or set(saved) != {"worker_bundle_sha256", "calls"}
            or saved["worker_bundle_sha256"] != approval["worker_bundle_sha256"]
            or isinstance(calls, bool) or not isinstance(calls, int) or calls < 0):
        raise LabQueueError("lab_approval_usage_invalid")
    return calls


def ensure_owner_approval_usage_dir(root: Path) -> Path:
    """Create the private approval ledger path without following symlinks."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise LabQueueError("lab_approval_usage_invalid")
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise LabQueueError("lab_approval_usage_invalid")
    state_dir = root / "state"
    usage_dir = state_dir / "lab-approval-usage"
    for directory in (state_dir, usage_dir):
        if directory.is_symlink():
            raise LabQueueError("lab_approval_usage_invalid")
        directory.mkdir(mode=0o700, exist_ok=True)
        stat = directory.stat()
        if (not directory.is_dir() or stat.st_uid != os.getuid() or stat.st_mode & 0o077):
            raise LabQueueError("lab_approval_usage_invalid")
    return usage_dir


def _layout(root: Path) -> tuple[Path, Path, Path, Path]:
    root = Path(root)
    if not root.is_absolute():
        raise LabQueueError("queue_path_must_be_absolute")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or root.stat().st_uid != os.getuid():
        raise LabQueueError("queue_owner_invalid")
    root.chmod(0o700)
    result = tuple(root / name for name in ("pending", "running", "receipts", "control"))
    for path in result:
        path.mkdir(mode=0o700, exist_ok=True)
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            raise LabQueueError("queue_owner_invalid")
        path.chmod(0o700)
    return result  # type: ignore[return-value]


def _read_fixture_manifest() -> dict:
    try:
        payload = FIXTURE_MANIFEST.read_bytes()
        if hashlib.sha256(payload).hexdigest() != FIXTURE_MANIFEST_SHA256:
            raise ValueError
        value = json.loads(payload)
    except (OSError, ValueError) as exc:
        raise LabQueueError("fixture_manifest_invalid") from exc
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("experiment_id") != "agata-naturalness-v1"
            or not isinstance(value.get("scenarios"), dict)):
        raise LabQueueError("fixture_manifest_invalid")
    return value


def judge_output_schema() -> dict:
    """Single source of truth for the versioned Worker/Lab judge verdict contract."""
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "overall": {"type": "string", "enum": ["first", "second", "tie", "uncertain"]},
            "core_dimensions": {"type": "object", "additionalProperties": False,
                "properties": {key: {"type": "string", "enum": ["first", "second", "tie"]} for key in JUDGE_CORE_DIMENSIONS},
                "required": list(JUDGE_CORE_DIMENSIONS)},
            "conditional_evidence": {"type": "object", "additionalProperties": False,
                "properties": {key: {"type": "object", "additionalProperties": False,
                    "properties": {"applicable": {"type": "boolean"}, "winner": {"type": "string", "enum": ["first", "second", "tie", "not_applicable"]}, "reason": {"type": "string", "maxLength": 500}},
                    "required": ["applicable", "winner", "reason"]} for key in JUDGE_CONDITIONAL_DIMENSIONS}},
            "reasons": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "string", "minLength": 1, "maxLength": 500}},
            "defects": {"type": "array", "maxItems": 16, "items": {"type": "object", "additionalProperties": False,
                "properties": {"code": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]{1,63}$"}, "severity": {"type": "string", "enum": ["critical", "major", "minor"]}, "position": {"type": "string", "enum": ["first", "second", "both"]}, "reason": {"type": "string", "minLength": 1, "maxLength": 500}},
                "required": ["code", "severity", "position", "reason"]}},
            "uncertainty": {"type": "boolean"},
            "position_sensitivity": {"type": "boolean"},
        },
        "required": ["overall", "core_dimensions", "conditional_evidence", "reasons", "defects", "uncertainty", "position_sensitivity"],
    }


def validate_judge_verdict(value: object) -> bool:
    """Validate parsed Codex judge JSON against the strict LAB-only schema."""
    import re
    if not isinstance(value, dict) or set(value) != set(judge_output_schema()["required"]):
        raise LabQueueError("judge_verdict_schema_invalid")
    if value["overall"] not in {"first", "second", "tie", "uncertain"}:
        raise LabQueueError("judge_verdict_schema_invalid")
    dimensions = value["core_dimensions"]
    if not isinstance(dimensions, dict) or set(dimensions) != set(JUDGE_CORE_DIMENSIONS) or any(
            winner not in {"first", "second", "tie"} for winner in dimensions.values()):
        raise LabQueueError("judge_verdict_dimensions_invalid")
    conditional = value["conditional_evidence"]
    if not isinstance(conditional, dict) or set(conditional) - set(JUDGE_CONDITIONAL_DIMENSIONS):
        raise LabQueueError("judge_verdict_conditional_invalid")
    for item in conditional.values():
        if (not isinstance(item, dict) or set(item) != {"applicable", "winner", "reason"}
                or type(item["applicable"]) is not bool
                or item["winner"] not in {"first", "second", "tie", "not_applicable"}
                or not isinstance(item["reason"], str) or len(item["reason"]) > 500):
            raise LabQueueError("judge_verdict_conditional_invalid")
    reasons = value["reasons"]
    if (not isinstance(reasons, list) or not 1 <= len(reasons) <= 8
            or any(not isinstance(reason, str) or not reason.strip() or len(reason) > 500 for reason in reasons)):
        raise LabQueueError("judge_verdict_reasons_invalid")
    defects = value["defects"]
    if not isinstance(defects, list) or len(defects) > 16:
        raise LabQueueError("judge_verdict_defects_invalid")
    for defect in defects:
        if (not isinstance(defect, dict) or set(defect) != {"code", "severity", "position", "reason"}
                or not isinstance(defect["code"], str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", defect["code"])
                or defect["severity"] not in {"critical", "major", "minor"}
                or defect["position"] not in {"first", "second", "both"}
                or not isinstance(defect["reason"], str) or not defect["reason"].strip() or len(defect["reason"]) > 500):
            raise LabQueueError("judge_verdict_defects_invalid")
    if type(value["uncertainty"]) is not bool or type(value["position_sensitivity"]) is not bool:
        raise LabQueueError("judge_verdict_flags_invalid")
    for text in reasons + [defect["reason"] for defect in defects]:
        lowered = text.casefold()
        if any(marker in lowered for marker in JUDGE_PRIVATE_MARKERS):
            raise LabQueueError("judge_verdict_private_metadata")
    return True


def read_promptfoo_compat_fixture() -> dict:
    """Read the separately pinned one-turn technical fixture; never load dataset splits."""
    try:
        payload = PROMPTFOO_COMPAT_MANIFEST.read_bytes()
        if hashlib.sha256(payload).hexdigest() != PROMPTFOO_COMPAT_MANIFEST_SHA256:
            raise ValueError
        value = json.loads(payload)
    except (OSError, ValueError) as exc:
        raise LabQueueError("promptfoo_compat_manifest_invalid") from exc
    required = {"schema_version", "experiment_id", "scenario_id", "model", "reasoning_effort", "user_message", "request_sha256"}
    if (not isinstance(value, dict) or set(value) != required or value.get("schema_version") != 1
            or value.get("experiment_id") != "promptfoo-worker-compat"
            or value.get("scenario_id") != "promptfoo-compat-001"
            or value.get("model") != "gpt-6-luna" or value.get("reasoning_effort") != "medium"
            or not isinstance(value.get("user_message"), str) or not value["user_message"].strip()
            or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("request_sha256")))):
        raise LabQueueError("promptfoo_compat_manifest_invalid")
    return value


def _validate(job: object, *, root: Path) -> tuple[dict, bytes]:
    if not isinstance(job, dict) or not JOB_KEYS <= set(job):
        raise LabQueueError("job_schema_invalid")
    for field in ("job_id", "experiment_id", "configuration_id"):
        value = job.get(field)
        if not isinstance(value, str) or not ID_RE.fullmatch(value):
            raise LabQueueError("job_identity_invalid")
    if job["data_classification"] != "synthetic":
        raise LabQueueError("lab_origin_or_data_classification_invalid")
    if (not isinstance(job["scenario_id"], str) or not ID_RE.fullmatch(job["scenario_id"])
            or isinstance(job["turn_index"], bool) or not isinstance(job["turn_index"], int)
            or job["turn_index"] < 0):
        raise LabQueueError("lab_scenario_or_turn_invalid")
    timeout = job["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 15 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise LabQueueError("job_timeout_invalid")
    request = job["request"]
    if not isinstance(request, dict) or set(request) != REQUEST_KEYS:
        raise LabQueueError("lab_request_schema_invalid")
    if (not isinstance(request["instructions"], str) or not request["instructions"].strip()
            or not isinstance(request["user_message"], str) or not request["user_message"].strip()
            or not all(isinstance(request[key], dict) for key in ("persona", "communication_settings"))
            or not all(isinstance(request[key], list) for key in ("memories", "dialogue_summaries", "history"))):
        raise LabQueueError("lab_request_context_invalid")
    if job.get("experiment_id") == JUDGE_EXPERIMENT_ID:
        judge_keys = JOB_KEYS | {"job_type", "dataset_split", "judge_slot", "promptfoo_invocation_id",
                                 "judge_schema_version", "presented_labels", "model", "reasoning_effort"}
        if (set(job) != judge_keys or job.get("job_type") != "judge"
                or job.get("origin") != "conversation-lab-judge"
                or job.get("dataset_split") not in {"DEV", "VALIDATION"}
                or type(job.get("judge_schema_version")) is not int or job["judge_schema_version"] != 1
                or type(job.get("judge_slot")) is not int or job["judge_slot"] not in {1, 2, 3}
                or job.get("configuration_id") != f"judge-{job['judge_slot']}"
                or not isinstance(job.get("promptfoo_invocation_id"), str)
                or not ID_RE.fullmatch(job["promptfoo_invocation_id"])
                or job.get("presented_labels") not in (["X", "Y"], ["Y", "X"])
                or job.get("model") != "gpt-6-luna" or job.get("reasoning_effort") != "medium"
                or job.get("turn_index") != 0 or job.get("history_receipt_ids") != []
                or request["instructions"] != JUDGE_INSTRUCTIONS or request["persona"] != {}
                or request["communication_settings"] != {} or request["memories"] != []
                or request["dialogue_summaries"] != [] or request["history"] != []):
            raise LabQueueError("judge_job_contract_invalid")
        expected_job_id = "judge-" + hashlib.sha256(job["promptfoo_invocation_id"].encode()).hexdigest()[:58]
        if job["job_id"] != expected_job_id:
            raise LabQueueError("judge_job_correlation_invalid")
        prompt = request["user_message"]
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 48 * 1024:
            raise LabQueueError("judge_prompt_invalid")
        lowered = prompt.casefold()
        if any(marker in lowered for marker in JUDGE_PRIVATE_MARKERS):
            raise LabQueueError("judge_prompt_private_metadata")
    elif job["experiment_id"] == "promptfoo-worker-compat":
        if set(job) != JOB_KEYS | {"model", "reasoning_effort"} or job["origin"] != "conversation-lab":
            raise LabQueueError("job_schema_invalid")
        fixture = read_promptfoo_compat_fixture()
        if (job["configuration_id"] != "compat"
                or job["scenario_id"] != fixture["scenario_id"] or job["turn_index"] != 0
                or job["model"] != fixture["model"]
                or job["reasoning_effort"] != fixture["reasoning_effort"]
                or job["history_receipt_ids"] != []
                or request["user_message"] != fixture["user_message"]):
            raise LabQueueError("lab_fixture_identity_mismatch")
        static_request = dict(request)
        static_request["history"] = []
        canonical = json.dumps(static_request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != fixture["request_sha256"]:
            raise LabQueueError("lab_fixture_request_hash_mismatch")
        if request["history"] != []:
            raise LabQueueError("lab_history_not_receipt_backed")
    elif job["experiment_id"] == "agata-naturalness-v1":
        if set(job) != JOB_KEYS or job["origin"] != "conversation-lab":
            raise LabQueueError("job_schema_invalid")
        fixtures = _read_fixture_manifest()
        scenario = fixtures["scenarios"].get(job["scenario_id"])
        turn = job["turn_index"]
        arm = job["configuration_id"]
        receipt_ids = job["history_receipt_ids"]
        if (job["experiment_id"] != fixtures["experiment_id"] or arm not in {"baseline", "naturalness-v1"}
                or not isinstance(scenario, dict) or turn >= len(scenario.get("user_turns", []))
                or not isinstance(receipt_ids, list) or len(receipt_ids) != turn
                or request["user_message"] != scenario["user_turns"][turn]):
            raise LabQueueError("lab_fixture_identity_mismatch")
        static_request = dict(request)
        static_request["history"] = []
        canonical = json.dumps(static_request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected_hashes = scenario.get("request_hashes", {}).get(arm, [])
        if turn >= len(expected_hashes) or hashlib.sha256(canonical).hexdigest() != expected_hashes[turn]:
            raise LabQueueError("lab_fixture_request_hash_mismatch")
        expected_history = []
        receipts_dir = Path(root) / "receipts"
        for index, receipt_id in enumerate(receipt_ids):
            if not isinstance(receipt_id, str) or not ID_RE.fullmatch(receipt_id):
                raise LabQueueError("lab_history_receipt_invalid")
            try:
                receipt = json.loads((receipts_dir / (receipt_id + ".json")).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise LabQueueError("lab_history_receipt_missing") from exc
            identity = receipt.get("identity", {}) if isinstance(receipt, dict) else {}
            response = receipt.get("response") if isinstance(receipt, dict) else None
            if (receipt.get("job_id") != receipt_id or receipt.get("status") != "completed"
                    or identity.get("experiment_id") != job["experiment_id"]
                    or identity.get("configuration_id") != arm or identity.get("scenario_id") != job["scenario_id"]
                    or identity.get("turn_index") != index or not isinstance(response, str) or not response.strip()):
                raise LabQueueError("lab_history_receipt_invalid")
            expected_history.extend([
                {"role": "user", "content": scenario["user_turns"][index]},
                {"role": "assistant", "content": response},
            ])
        if request["history"] != expected_history:
            raise LabQueueError("lab_history_not_receipt_backed")
    else:
        raise LabQueueError("lab_experiment_not_allowed")
    try:
        payload = json.dumps(job, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LabQueueError("job_payload_invalid") from exc
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise LabQueueError("payload_too_large")
    return job, payload


def _queue_lock(root: Path):
    path = root / ".queue.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _atomic_write(path: Path, payload: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".incoming-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _atomic_create(path: Path, payload: bytes) -> bool:
    fd, temp_name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp_name, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def submit(job: object, *, root: Path, max_pending: int = DEFAULT_MAX_PENDING) -> None:
    job, payload = _validate(job, root=Path(root))
    if isinstance(max_pending, bool) or not isinstance(max_pending, int) or not 1 <= max_pending <= DEFAULT_MAX_PENDING:
        raise LabQueueError("queue_limit_invalid")
    pending, running, receipts, control = _layout(root)
    fd = _queue_lock(Path(root))
    try:
        job_id = job["job_id"]
        if any(folder.joinpath(job_id + ".json").exists() for folder in (pending, running, receipts)):
            raise LabQueueError("duplicate_job_id")
        if len(list(pending.glob("*.json"))) + len(list(running.glob("*.json"))) >= max_pending:
            raise LabQueueError("queue_full")
        _atomic_write(pending / (job_id + ".json"), payload)
    finally:
        os.close(fd)


def pending_count(root: Path) -> int:
    pending, _, _, _ = _layout(root)
    return len(list(pending.glob("*.json")))


def claim_next(root: Path) -> dict | None:
    pending, running, _, _ = _layout(root)
    for path in sorted(pending.glob("*.json"), key=lambda item: (item.stat().st_mtime_ns, item.name)):
        claimed = running / path.name
        try:
            os.replace(path, claimed)
        except FileNotFoundError:
            continue
        try:
            value = json.loads(claimed.read_text(encoding="utf-8"))
            _validate(value, root=Path(root))
        except Exception as exc:
            _finish_path(root, claimed.stem, {"job_id": claimed.stem, "status": "failed", "error_code": "queue_job_invalid"})
            raise LabQueueError("queue_job_invalid") from exc
        return value
    return None


def _finish_path(root: Path, job_id: str, receipt: dict) -> None:
    _, running, receipts, _ = _layout(root)
    _atomic_create(receipts / (job_id + ".json"), json.dumps(receipt, ensure_ascii=False, separators=(",", ":")).encode())
    (running / (job_id + ".json")).unlink(missing_ok=True)
    (root / "control" / (job_id + ".cancel")).unlink(missing_ok=True)


def finish(
    root: Path, job_id: str, *, status: str, response: str = "",
    error_code: str | None = None, execution: dict | None = None,
    identity: dict | None = None, slot_id: str | None = None,
) -> None:
    if not ID_RE.fullmatch(job_id) or status not in {"completed", "failed", "cancelled", "ambiguous"}:
        raise LabQueueError("receipt_identity_or_status_invalid")
    if slot_id is not None and slot_id != LANE_SLOTS["LAB"]:
        raise LabQueueError("receipt_slot_lane_mismatch")
    receipt = {"job_id": job_id, "status": status, "finished_at": time.time()}
    if slot_id is not None:
        receipt["slot_id"] = slot_id
    if response:
        receipt["response"] = response
    if error_code:
        receipt["error_code"] = re.sub(r"[^a-zA-Z0-9_-]", "_", error_code)[:64]
    if identity:
        receipt["identity"] = {
            key: identity[key] for key in ("experiment_id", "configuration_id", "scenario_id", "turn_index",
                                           "job_type", "dataset_split", "judge_slot", "promptfoo_invocation_id",
                                           "judge_schema_version", "presented_labels")
            if key in identity
        }
        if identity.get("job_type") == "judge" and status == "completed":
            try:
                verdict = json.loads(response)
                validate_judge_verdict(verdict)
            except (TypeError, ValueError, json.JSONDecodeError, LabQueueError) as exc:
                raise LabQueueError("judge_receipt_verdict_invalid") from exc
            receipt["judge_verdict"] = verdict
    if execution:
        safe = {}
        for key in ("duration_ms", "timeout_seconds", "requested_model", "requested_reasoning_effort", "actual_model", "inference_started"):
            value = execution.get(key)
            if isinstance(value, (str, int, float)) or value is None:
                safe[key] = value
        usage = execution.get("cli_reported_usage")
        if isinstance(usage, dict) and all(
            isinstance(key, str) and (isinstance(value, (str, int, float, bool)) or value is None)
            for key, value in usage.items()
        ):
            safe["cli_reported_usage"] = usage
        receipt["execution"] = safe
    _finish_path(root, job_id, receipt)


def read_receipt(root: Path, job_id: str) -> dict | None:
    _, _, receipts, _ = _layout(root)
    if not ID_RE.fullmatch(job_id):
        raise LabQueueError("job_identity_invalid")
    path = receipts / (job_id + ".json")
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def reconcile_running(root: Path, *, active_job_ids: set[str] | None = None) -> int:
    _, running, _, _ = _layout(root)
    count = 0
    active_job_ids = active_job_ids or set()
    for path in list(running.glob("*.json")):
        if path.stem in active_job_ids:
            continue
        finish(root, path.stem, status="ambiguous", error_code="worker_restart_during_generation")
        count += 1
    return count


def cancel(root: Path, job_id: str) -> str:
    pending, running, receipts, control = _layout(root)
    if not ID_RE.fullmatch(job_id):
        raise LabQueueError("job_identity_invalid")
    receipt = receipts / (job_id + ".json")
    if receipt.exists():
        return json.loads(receipt.read_text(encoding="utf-8"))["status"]
    waiting = pending / (job_id + ".json")
    if waiting.exists():
        try:
            os.replace(waiting, running / waiting.name)
        except FileNotFoundError:
            pass
        else:
            finish(root, job_id, status="cancelled", error_code="cancelled_before_execution")
            return "cancelled"
    active = running / (job_id + ".json")
    if active.exists():
        marker = control / (job_id + ".cancel")
        fd = os.open(marker, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        return "cancel_requested"
    return "unknown"


def cancel_requested(root: Path, job_id: str) -> bool:
    _, _, _, control = _layout(root)
    return (control / (job_id + ".cancel")).exists()
