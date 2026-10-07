"""Submit approved synthetic Naturalness development turns to the local LAB spool.

This module is deliberately a local file adapter. It implements no network client and
does not import or launch Codex; inference remains exclusively inside the Mac worker.
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
WORKER_DIR = LAB.parents[0] / "worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))
import lab_queue  # noqa: E402


def canonical_lab_queue(env=None) -> Path:
    """Return the sole LAB queue path selected by the installed Worker contract."""
    source_env = dict(os.environ if env is None else env)
    root = source_env.get("GPOINT_AGATA_WORKER_SUPPORT") or source_env.get("AGATA_WORKER_ROOT")
    support = Path(root).expanduser() if root else Path.home() / "Library" / "Application Support" / "GPoint" / "agata-codex-worker"
    return support / "queues" / "lab"


def validate_owner_approval(job: dict, env=None) -> None:
    """Ask the canonical Worker verifier before reserving a Promptfoo attempt.

    The Worker revalidates and consumes the same approval again immediately
    before inference; this first check prevents an unapproved job from charging
    the Lab's durable generator ledger.
    """
    source_env = dict(os.environ if env is None else env)
    support = canonical_lab_queue(source_env).parents[1]
    try:
        approval_path = Path(source_env.get("AGATA_LAB_APPROVAL_FILE") or
                             (support / "config/lab-execution-approval.json"))
        approval = lab_queue.load_owner_approval(
            approval_path, expected_bundle_sha256=lab_queue.worker_bundle_sha256(),
        )
        if (job.get("experiment_id") not in approval["experiment_ids"]
                or job.get("configuration_id") not in approval["configuration_ids"]
                or job.get("scenario_id") not in approval["scenario_ids"]):
            raise PermissionError("LAB OWNER approval does not cover this experiment invocation")
        calls = lab_queue.owner_approval_usage_calls(support, approval)
        if calls >= approval["max_calls"]:
            raise PermissionError("LAB OWNER approval is consumed or unavailable")
    except PermissionError:
        raise
    except (lab_queue.LabQueueError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise PermissionError("LAB OWNER approval is missing, invalid, expired, or unavailable") from exc

NATURALNESS = LAB / "evaluators" / "naturalness.py"
SPEC = importlib.util.spec_from_file_location("agata_lab_naturalness_for_worker", NATURALNESS)
naturalness = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(naturalness)


def build_job(
    *, scenario_id: str, configuration_id: str, turn_index: int,
    history_receipt_ids: list[str] | None = None, support: Path | None = None,
    job_id: str | None = None,
) -> dict:
    """Build a frozen `-02` turn, taking prior replies only from local receipts."""
    if configuration_id not in {"baseline", "naturalness-v1"}:
        raise ValueError("configuration is outside frozen Naturalness V1")
    config = naturalness.load_naturalness_config(LAB)
    if scenario_id not in config["development"]["scenario_ids"] or not scenario_id.endswith("-02"):
        raise ValueError("scenario is outside Naturalness V1 development; holdout is unavailable")
    if isinstance(turn_index, bool) or not isinstance(turn_index, int) or turn_index < 0:
        raise ValueError("turn index must be a non-negative integer")
    receipt_ids = [] if history_receipt_ids is None else history_receipt_ids
    if (not isinstance(receipt_ids, list) or len(receipt_ids) != turn_index
            or any(not isinstance(item, str) for item in receipt_ids)):
        raise ValueError("history must identify one completed receipt per reached turn")
    benchmark = naturalness._module(LAB, "benchmark")
    scenarios = benchmark.load_scenarios(LAB / "data/golden/synthetic/agata-benchmark-v1.jsonl")
    scenario = next(item for item in scenarios if item["scenario_id"] == scenario_id)
    user_turns = [item["text"] for item in scenario["dialogue_turns"] if item["role"] == "user"]
    if turn_index >= len(user_turns):
        raise ValueError("turn index is outside the frozen scenario")
    history = []
    if receipt_ids:
        if support is None:
            raise ValueError("completed local receipts are required for prior turns")
        for index, receipt_id in enumerate(receipt_ids):
            receipt = lab_queue.read_receipt(Path(support) / "lab-queue", receipt_id)
            identity = receipt.get("identity", {}) if isinstance(receipt, dict) else {}
            response = receipt.get("response") if isinstance(receipt, dict) else None
            if (not receipt or receipt.get("status") != "completed"
                    or identity.get("experiment_id") != config["experiment_id"]
                    or identity.get("configuration_id") != configuration_id
                    or identity.get("scenario_id") != scenario_id or identity.get("turn_index") != index
                    or not isinstance(response, str) or not response.strip()):
                raise ValueError("prior history must come from completed same-arm local receipts")
            history.extend([
                {"role": "user", "text": user_turns[index]},
                {"role": "assistant", "text": response},
            ])
    # The offline builder requires both arms to have a complete history. Only
    # the selected arm's invocation is returned, and both derive history from
    # that arm's own completed worker receipts.
    histories = {"baseline": history, "naturalness-v1": history}
    baseline, candidate = naturalness.build_ab_invocations(
        LAB, config, scenario_id, turn_index, histories,
    )
    invocation = baseline if configuration_id == "baseline" else candidate
    request = invocation["core_request"]
    if invocation.get("fixture_only") is not True:
        raise ValueError("Naturalness builder did not return its offline canonical fixture")
    if turn_index >= config["development"]["scenario_rounds"][scenario_id]:
        raise ValueError("turn index is outside the frozen Naturalness development set")
    return {
        "job_id": job_id or str(uuid.uuid4()),
        "experiment_id": config["experiment_id"],
        "configuration_id": configuration_id,
        "origin": "conversation-lab",
        "scenario_id": scenario_id,
        "turn_index": turn_index,
        "data_classification": "synthetic",
        "timeout_seconds": 180,
        "history_receipt_ids": receipt_ids,
        "request": request,
    }


def submit_job(job: dict, *, support: Path) -> None:
    lab_queue.submit(job, root=Path(support) / "lab-queue")


def get_receipt(job_id: str, *, support: Path) -> dict | None:
    return lab_queue.read_receipt(Path(support) / "lab-queue", job_id)


def cancel_job(job_id: str, *, support: Path) -> str:
    return lab_queue.cancel(Path(support) / "lab-queue", job_id)


def build_promptfoo_compat_job(*, invocation_id: str, prompt: str | None = None) -> dict:
    """Build only the separately pinned one-turn technical smoke job."""
    if not isinstance(invocation_id, str) or not invocation_id.strip():
        raise ValueError("Promptfoo invocation id is required")
    fixture = lab_queue.read_promptfoo_compat_fixture()
    user_message = fixture["user_message"]
    if prompt is not None and prompt.strip() != user_message:
        raise ValueError("Promptfoo prompt is outside the frozen technical fixture")
    request = {
        "instructions": (
            "You are AGATA, a Russian-speaking conversational character. Reply to the user's "
            "single synthetic message in one concise sentence. Do not mention tools, code, "
            "internal labels, or the experiment."
        ),
        "persona": {"name": "AGATA", "gender": "female", "personality": {"tone": "warm and natural"}},
        "communication_settings": {"language": "ru", "tone": "warm", "response_length": "short", "use_name": False},
        "memories": [],
        "dialogue_summaries": [],
        "history": [],
        "user_message": user_message,
    }
    canonical = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != fixture["request_sha256"]:
        raise ValueError("Promptfoo technical request does not match its frozen fixture")
    job_id = "pf-" + hashlib.sha256(invocation_id.encode("utf-8")).hexdigest()[:61]
    return {
        "job_id": job_id,
        "experiment_id": fixture["experiment_id"],
        "configuration_id": "compat",
        "origin": "conversation-lab",
        "scenario_id": fixture["scenario_id"],
        "turn_index": 0,
        "data_classification": "synthetic",
        "timeout_seconds": 120,
        "history_receipt_ids": [],
        "request": request,
        "model": fixture["model"],
        "reasoning_effort": fixture["reasoning_effort"],
    }
