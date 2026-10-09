"""Promptfoo Python provider for one isolated AGATA LAB judge invocation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Mapping

LAB = Path(__file__).resolve().parents[1]
WORKER_DIR = LAB.parents[0] / "worker"
if str(LAB / "evaluators") not in sys.path:
    sys.path.insert(0, str(LAB / "evaluators"))
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

import judges  # noqa: E402
import worker_client  # noqa: E402
from promptfoo_worker_provider import (  # noqa: E402
    LocalLabTransport, _validate_no_api_auth,
)
from run_coordinator import CallLedger  # noqa: E402

MODEL = "gpt-6-luna"
REASONING = "medium"
PROVIDER_MODEL_IDENTITY_SOURCE = "codex_provider_response_metadata"
MAX_WAIT_SECONDS = 240


class AmbiguousJudgeResult(RuntimeError):
    """The Worker may have started a judge invocation; retries are forbidden."""


class JudgeModelIdentityMismatch(RuntimeError):
    """A provider-reported judge model differs from the approved model."""


def _config(options: object) -> Mapping[str, object]:
    if not isinstance(options, Mapping):
        raise ValueError("Promptfoo judge options are required")
    result = options.get("config", options)
    if not isinstance(result, Mapping):
        raise ValueError("Promptfoo judge configuration is malformed")
    return result


def _reserved(ledger, invocation_id: str) -> bool:
    rows = ledger.snapshot().get("judge_invocations", {}).get("invocations", [])
    return any(isinstance(row, Mapping) and row.get("invocation_id") == invocation_id
               and row.get("outcome") is None for row in rows)


def _private_receipt(directory: Path | None, invocation_id: str, evidence: dict) -> None:
    if directory is None:
        return
    directory = Path(directory)
    if (not directory.is_absolute() or not directory.is_dir() or directory.is_symlink()
            or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077):
        raise PermissionError("private judge receipt directory is unsafe")
    path = directory / (invocation_id + ".judge-receipt.json")
    payload = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


class LabJudgeAdapter:
    """Use only the canonical local LAB spool; the coordinator owns reservation."""
    def __init__(self, *, ledger, transport, run_id: str, env=None, receipt_directory=None):
        self.ledger = ledger
        self.transport = transport
        self.run_id = run_id
        self.env = os.environ if env is None else env
        self.receipt_directory = Path(receipt_directory) if receipt_directory else None

    def invoke(self, prompt: str, *, config: Mapping[str, object]) -> dict:
        _validate_no_api_auth(self.env, config)
        if (config.get("model") != MODEL or config.get("reasoning_effort") != REASONING
                or config.get("maxRetries") != 0 or config.get("fallbacks", []) != []):
            raise PermissionError("LAB judge model, reasoning, retries, and fallback are fixed")
        batch_id = config.get("batch_id")
        invocation_id = config.get("invocation_id")
        scenario_id = config.get("scenario_id")
        split = config.get("dataset_split")
        slot = config.get("judge_slot")
        order = config.get("presented_labels")
        job = worker_client.build_judge_job(
            prompt, batch_id=batch_id, scenario_id=scenario_id, dataset_split=split,
            judge_slot=slot, presented_labels=order,
        )
        if job["promptfoo_invocation_id"] != invocation_id:
            raise PermissionError("judge invocation ID does not match its immutable batch identity")
        if (getattr(self.ledger, "durable", False) is not True
                or not callable(getattr(self.ledger, "verify_persisted", None))
                or not self.ledger.verify_persisted()
                or self.ledger.snapshot().get("limits") != {
                    "conversation_arms": 8, "generator_invocations": 32, "judge_invocations": 15
                }
                or not _reserved(self.ledger, invocation_id)):
            raise PermissionError("judge invocation must have a durable pre-reserved ledger attempt")
        worker_client.validate_owner_approval(job, self.env)
        submitted = False
        completed = False
        receipt = None
        outcome = "ambiguous"
        actual_model = None
        actual_model_source = None
        model_identity_status = "MODEL_IDENTITY_UNVERIFIED"
        try:
            returned_id = self.transport.submit(job)
            submitted = True
            if returned_id != job["job_id"]:
                raise AmbiguousJudgeResult("LAB queue returned a mismatched correlation ID")
            receipt = self.transport.wait_terminal(job["job_id"], MAX_WAIT_SECONDS)
            if (not isinstance(receipt, dict) or receipt.get("job_id") != job["job_id"]
                    or receipt.get("status") not in {"completed", "failed", "cancelled", "ambiguous"}):
                raise AmbiguousJudgeResult("Worker terminal judge receipt is missing or malformed")
            identity = receipt.get("identity") if isinstance(receipt.get("identity"), dict) else {}
            execution = receipt.get("execution") if isinstance(receipt.get("execution"), dict) else {}
            expected = {
                "experiment_id": job["experiment_id"], "configuration_id": job["configuration_id"],
                "scenario_id": scenario_id, "turn_index": 0, "job_type": "judge",
                "dataset_split": split, "judge_slot": slot,
                "promptfoo_invocation_id": invocation_id, "judge_schema_version": 1,
                "presented_labels": order,
            }
            if (receipt.get("slot_id") != "slot-03"
                    or any(identity.get(key) != value for key, value in expected.items())):
                raise AmbiguousJudgeResult("Worker receipt does not match the approved LAB judge invocation")
            if receipt["status"] == "completed":
                verdict = receipt.get("judge_verdict")
                try:
                    from worker_client import lab_queue
                    lab_queue.validate_judge_verdict(verdict)
                    normalized = judges.parse_judge_output(
                        json.dumps(verdict, ensure_ascii=False), judge_slot=slot,
                        scenario_id=scenario_id, presented_labels=order,
                    )
                except (ValueError, TypeError) as exc:
                    raise AmbiguousJudgeResult("Worker receipt has no valid structured judge verdict") from exc
                actual_model_source = execution.get("actual_model_source")
                actual_model = (execution.get("actual_model")
                                if actual_model_source == PROVIDER_MODEL_IDENTITY_SOURCE else None)
                if actual_model is None:
                    actual_model_source = None
                if (execution.get("requested_model") != MODEL
                        or execution.get("requested_reasoning_effort") != REASONING):
                    raise AmbiguousJudgeResult("Worker receipt does not prove the exact judge model contract")
                if actual_model is None:
                    model_identity_status = "MODEL_IDENTITY_UNVERIFIED"
                    outcome = "success"
                elif not isinstance(actual_model, str) or not actual_model.strip():
                    raise AmbiguousJudgeResult("Worker receipt has malformed provider model identity")
                elif actual_model != MODEL:
                    model_identity_status = "MODEL_IDENTITY_MISMATCH"
                    normalized, outcome = None, "provider_error"
                else:
                    model_identity_status = "MODEL_IDENTITY_VERIFIED"
                    outcome = "success"
            elif receipt["status"] == "failed" and execution.get("inference_started") is False:
                normalized, outcome = None, "provider_error"
            else:
                normalized, outcome = None, "ambiguous"

            evidence = {
                "schema_version": 1, "run_id": self.run_id, "batch_id": batch_id,
                "promptfoo_invocation_id": invocation_id, "lab_ledger_attempt_id": invocation_id,
                "worker_job_id": job["job_id"],
                "worker_receipt_sha256": self.transport.receipt_sha256(job["job_id"]),
                "worker_terminal_status": receipt["status"], "model": MODEL,
                "actual_model": actual_model, "actual_model_source": actual_model_source,
                "model_identity_status": model_identity_status,
                "reasoning_effort": REASONING, "judge_slot": slot,
                "scenario_id": scenario_id, "dataset_split": split,
                "presentation_labels": order, "outcome": outcome,
                "normalized_judge_receipt": normalized,
            }
            self.ledger.complete("judge", invocation_id, outcome)
            completed = True
            _private_receipt(self.receipt_directory, invocation_id, evidence)
            if outcome == "success":
                return {"output": json.dumps(verdict, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}
            if outcome == "ambiguous":
                raise AmbiguousJudgeResult("judge result is ambiguous; retries are forbidden")
            if model_identity_status == "MODEL_IDENTITY_MISMATCH":
                raise JudgeModelIdentityMismatch("provider-reported judge model mismatched the approved model")
            raise RuntimeError("Worker rejected judge job before inference")
        except BaseException:
            if not completed and _reserved(self.ledger, invocation_id):
                outcome = "ambiguous" if submitted else "provider_error"
                try:
                    self.ledger.complete("judge", invocation_id, outcome)
                    completed = True
                    _private_receipt(self.receipt_directory, invocation_id, {
                        "schema_version": 1, "run_id": self.run_id, "batch_id": batch_id,
                        "promptfoo_invocation_id": invocation_id, "lab_ledger_attempt_id": invocation_id,
                        "worker_job_id": job["job_id"],
                        "worker_receipt_sha256": self.transport.receipt_sha256(job["job_id"])
                        if submitted else None,
                        "worker_terminal_status": receipt.get("status") if isinstance(receipt, dict) else "unavailable",
                        "model": MODEL, "actual_model": actual_model,
                        "actual_model_source": actual_model_source,
                        "model_identity_status": model_identity_status,
                        "reasoning_effort": REASONING, "judge_slot": slot,
                        "scenario_id": scenario_id, "dataset_split": split, "presentation_labels": order,
                        "outcome": outcome, "normalized_judge_receipt": None,
                    })
                except (ValueError, OSError):
                    pass
            raise


def call_api(prompt: str, options: dict, context: dict | None = None) -> dict:
    config = _config(options)
    if (config.get("model") != MODEL or config.get("reasoning_effort") != REASONING
            or config.get("maxRetries") != 0 or config.get("fallbacks", []) != []):
        raise PermissionError("Promptfoo judge contract is invalid")
    ledger_path, queue_path = os.environ.get("AGATA_LAB_CALL_LEDGER"), os.environ.get("AGATA_LAB_QUEUE")
    private_path = os.environ.get("AGATA_LAB_PRIVATE_RECEIPTS")
    if not ledger_path or not queue_path or not private_path:
        raise PermissionError("Promptfoo judge private LAB paths are not configured")
    ledger = CallLedger(journal_path=Path(ledger_path))
    adapter = LabJudgeAdapter(
        ledger=ledger, transport=LocalLabTransport(Path(queue_path)),
        run_id=str(config.get("run_id", "")), env=os.environ,
        receipt_directory=Path(private_path),
    )
    return adapter.invoke(prompt, config=config)
