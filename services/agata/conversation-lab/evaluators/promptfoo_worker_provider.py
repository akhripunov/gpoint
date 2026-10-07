"""Promptfoo Python provider that delegates every generation to AGATA Worker LAB."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Mapping


MODEL = "gpt-6-luna"
REASONING = "medium"
EXPERIMENT = "promptfoo-worker-compat"
SCENARIO = "promptfoo-compat-001"
POLL_SECONDS = 0.1
MAX_WAIT_SECONDS = 240
API_KEY_ENV = frozenset({"OPENAI_API_KEY", "CODEX_API_KEY"})

LAB = Path(__file__).resolve().parents[1]
WORKER_DIR = LAB.parents[0] / "worker"
if str(LAB / "evaluators") not in sys.path:
    sys.path.insert(0, str(LAB / "evaluators"))
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

import lab_queue  # noqa: E402
import worker_client  # noqa: E402
from run_coordinator import CallLedger  # noqa: E402


class AmbiguousWorkerResult(RuntimeError):
    """The Worker may have started inference; this invocation cannot be retried."""


def _validate_no_api_auth(env: Mapping[str, str], config: Mapping[str, object]) -> None:
    present = sorted(key for key in env if key in API_KEY_ENV or key.upper().endswith("_API_KEY"))
    if present:
        raise PermissionError("API-key auth is forbidden for Promptfoo → Worker LAB")

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalized = str(key).replace("-", "_").casefold()
                if normalized in {"apikey", "api_key", "authorization", "access_token"} or normalized.endswith("_api_key"):
                    raise PermissionError("API-key auth configuration is forbidden for Promptfoo → Worker LAB")
                if normalized == "base_url":
                    raise PermissionError("custom provider endpoints are forbidden for Promptfoo → Worker LAB")
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
    visit(config)


def _atomic_receipt(directory: Path, invocation_id: str, payload: dict) -> Path:
    if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
        raise PermissionError("private Conversation Lab receipt directory must already exist")
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise PermissionError("private Conversation Lab receipt directory permissions are unsafe")
    if not invocation_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Promptfoo invocation id is invalid")
    path = directory / (invocation_id + ".worker-receipt.json")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    return path


def _validate_receipt_directory(directory: Path, invocation_id: str) -> None:
    if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
        raise PermissionError("private Conversation Lab receipt directory must already exist")
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise PermissionError("private Conversation Lab receipt directory permissions are unsafe")
    if (directory / (invocation_id + ".worker-receipt.json")).exists():
        raise FileExistsError("Promptfoo invocation receipt already exists; retries are forbidden")


class LocalLabTransport:
    """A LAB-only capability over the existing local queue and receipt layout."""
    def __init__(self, queue_root: Path, env: Mapping[str, str] | None = None):
        self.root = Path(queue_root).expanduser()
        expected = worker_client.canonical_lab_queue(env)
        if (not self.root.is_absolute() or self.root.is_symlink()
                or self.root.absolute() != expected.absolute()
                or self.root.resolve(strict=False) != expected.resolve(strict=False)):
            raise PermissionError("Promptfoo may submit only to the canonical LAB queue")

    def submit(self, job: dict) -> str:
        lab_queue.submit(job, root=self.root)
        return job["job_id"]

    def receipt_path(self, job_id: str) -> Path:
        if not lab_queue.ID_RE.fullmatch(job_id):
            raise ValueError("Worker job id is invalid")
        return self.root / "receipts" / (job_id + ".json")

    def receipt_sha256(self, job_id: str) -> str:
        path = self.receipt_path(job_id)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def wait_terminal(self, job_id: str, timeout_seconds: int):
        deadline = time.monotonic() + min(timeout_seconds, MAX_WAIT_SECONDS)
        receipts = self.root / "receipts"
        path = receipts / (job_id + ".json")
        while time.monotonic() < deadline:
            if path.exists():
                if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
                    raise PermissionError("Worker receipt ownership or mode is unsafe")
                return json.loads(path.read_text(encoding="utf-8"))
            time.sleep(POLL_SECONDS)
        raise AmbiguousWorkerResult("Worker terminal receipt did not arrive before bounded timeout")


class WorkerLabAdapter:
    def __init__(self, *, ledger, transport, run_id: str, receipt_directory: Path | None = None,
                 env: Mapping[str, str] | None = None):
        self.ledger = ledger
        self.transport = transport
        self.run_id = run_id
        self.receipt_directory = receipt_directory
        self.env = os.environ if env is None else env

    def invoke(self, prompt: str, *, invocation_id: str, scenario_id: str,
               model: str = MODEL, reasoning: str = REASONING) -> dict:
        config = {"model": model, "reasoning_effort": reasoning, "scenario_id": scenario_id}
        _validate_no_api_auth(self.env, config)
        if (model, reasoning) != (MODEL, REASONING) or scenario_id != SCENARIO:
            raise PermissionError("only the frozen gpt-6-luna / medium LAB compatibility fixture is allowed")
        if self.receipt_directory is not None:
            _validate_receipt_directory(Path(self.receipt_directory), invocation_id)
        limits = {"conversation_arms": 8, "generator_invocations": 32, "judge_invocations": 15}
        if (getattr(self.ledger, "durable", False) is not True
                or not callable(getattr(self.ledger, "verify_persisted", None))
                or not self.ledger.verify_persisted()
                or self.ledger.snapshot().get("limits") != limits):
            raise PermissionError("Promptfoo Worker LAB invocation requires the verified durable call ledger")

        job = worker_client.build_promptfoo_compat_job(invocation_id=invocation_id, prompt=prompt)
        attempt = {
            "run_id": self.run_id,
            "arm_id": EXPERIMENT,
            "experiment_id": EXPERIMENT,
            "scenario_id": SCENARIO,
            "turn_index": 0,
            "model": MODEL,
            "reasoning_effort": REASONING,
            "worker_job_id": job["job_id"],
        }
        self.ledger.reserve("generator", invocation_id, metadata=attempt)

        submitted = False
        ledger_completed = False
        receipt = None
        try:
            returned_job_id = self.transport.submit(job)
            submitted = True
            if returned_job_id != job["job_id"]:
                raise AmbiguousWorkerResult("Worker submission returned a mismatched correlation id")
            receipt = self.transport.wait_terminal(job["job_id"], MAX_WAIT_SECONDS)
            if (not isinstance(receipt, dict) or receipt.get("job_id") != job["job_id"]
                    or receipt.get("status") not in {"completed", "failed", "cancelled", "ambiguous"}):
                raise AmbiguousWorkerResult("Worker terminal receipt is missing or malformed")

            identity = receipt.get("identity") if isinstance(receipt.get("identity"), dict) else {}
            execution = receipt.get("execution") if isinstance(receipt.get("execution"), dict) else {}
            expected_identity = {"experiment_id": EXPERIMENT, "configuration_id": "compat",
                                 "scenario_id": SCENARIO, "turn_index": 0}
            if any(identity.get(key) != value for key, value in expected_identity.items()):
                raise AmbiguousWorkerResult("Worker receipt does not match the frozen fixture")

            if receipt["status"] == "completed":
                output = receipt.get("response")
                if (not isinstance(output, str) or not output.strip()
                        or execution.get("requested_model") != MODEL
                        or execution.get("requested_reasoning_effort") != REASONING
                        or execution.get("actual_model") not in {None, MODEL}):
                    raise AmbiguousWorkerResult("completed Worker receipt does not prove the exact model contract")
                outcome = "success"
            elif receipt["status"] == "failed" and (
                    execution.get("inference_started") is False
                    or receipt.get("error_code") in {"lab_approval_required", "owner_approval_scope_mismatch"}):
                output, outcome = None, "provider_error"
            else:
                output, outcome = None, "ambiguous"

            evidence = {
                "schema_version": 1,
                **attempt,
                "promptfoo_invocation_id": invocation_id,
                "lab_ledger_attempt_id": invocation_id,
                "worker_receipt_sha256": self.transport.receipt_sha256(job["job_id"])
                if callable(getattr(self.transport, "receipt_sha256", None)) else hashlib.sha256(
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "worker_receipt_path": str(self.transport.receipt_path(job["job_id"]))
                if callable(getattr(self.transport, "receipt_path", None)) else None,
                "worker_terminal_status": receipt["status"],
                "promptfoo_outcome": outcome,
                "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest() if output is not None else None,
            }
            self.ledger.complete("generator", invocation_id, outcome)
            ledger_completed = True
            if self.receipt_directory is not None:
                _atomic_receipt(Path(self.receipt_directory), invocation_id, evidence)
            if outcome == "success":
                return {"output": output}
            if outcome == "ambiguous":
                raise AmbiguousWorkerResult("Worker outcome is ambiguous; retries are forbidden")
            raise RuntimeError("Worker rejected the LAB job before inference")
        except BaseException as exc:
            if not submitted:
                # Submission failure could occur after the queue's atomic rename/write.
                outcome = "ambiguous"
            elif isinstance(exc, AmbiguousWorkerResult):
                outcome = "ambiguous"
            else:
                outcome = "ambiguous"
            if not ledger_completed:
                try:
                    self.ledger.complete("generator", invocation_id, outcome)
                    ledger_completed = True
                except (ValueError, KeyError):
                    pass
            if self.receipt_directory is not None and not (Path(self.receipt_directory) / (invocation_id + ".worker-receipt.json")).exists():
                evidence = {
                    "schema_version": 1,
                    **attempt,
                    "promptfoo_invocation_id": invocation_id,
                    "lab_ledger_attempt_id": invocation_id,
                    "worker_receipt_sha256": None,
                    "worker_receipt_path": str(self.transport.receipt_path(job["job_id"]))
                    if callable(getattr(self.transport, "receipt_path", None)) else None,
                    "worker_terminal_status": receipt.get("status") if isinstance(receipt, dict) else "unavailable",
                    "promptfoo_outcome": outcome,
                    "output_sha256": None,
                }
                try:
                    _atomic_receipt(Path(self.receipt_directory), invocation_id, evidence)
                except FileExistsError:
                    pass
            raise


def _provider_config(options: object) -> Mapping[str, object]:
    if not isinstance(options, Mapping):
        raise ValueError("Promptfoo provider options are required")
    config = options.get("config", options)
    if not isinstance(config, Mapping):
        raise ValueError("Promptfoo provider configuration is malformed")
    return config


def call_api(prompt: str, options: dict, context: dict | None = None) -> dict:
    """Promptfoo Python provider entry point; the only execution route is Worker LAB."""
    config = _provider_config(options)
    _validate_no_api_auth(os.environ, config)
    if (config.get("model") != MODEL or config.get("reasoning_effort") != REASONING
            or config.get("maxRetries") != 0 or config.get("fallbacks", []) != []):
        raise PermissionError("Promptfoo provider retry, fallback, and model contract must remain exact")
    run_id = config.get("run_id")
    invocation_id = config.get("invocation_id")
    if not isinstance(run_id, str) or not run_id or not isinstance(invocation_id, str) or not invocation_id:
        raise PermissionError("Promptfoo invocation must include immutable run and invocation IDs")
    ledger_path = os.environ.get("AGATA_LAB_CALL_LEDGER")
    queue_root = os.environ.get("AGATA_LAB_QUEUE")
    private_receipts = os.environ.get("AGATA_LAB_PRIVATE_RECEIPTS")
    if not ledger_path or not queue_root or not private_receipts:
        raise PermissionError("Promptfoo Worker LAB private paths are not configured")
    ledger = CallLedger(journal_path=Path(ledger_path))
    adapter = WorkerLabAdapter(
        ledger=ledger,
        transport=LocalLabTransport(Path(queue_root)),
        run_id=run_id,
        receipt_directory=Path(private_receipts),
        env=os.environ,
    )
    return adapter.invoke(
        prompt,
        invocation_id=invocation_id,
        scenario_id=str(config.get("scenario_id", "")),
        model=str(config.get("model", "")),
        reasoning=str(config.get("reasoning_effort", "")),
    )
