"""Submit approved synthetic Naturalness development turns to the local LAB spool.

This module is deliberately a local file adapter. It implements no network client and
does not import or launch Codex; inference remains exclusively inside the Mac worker.
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
WORKER_DIR = LAB.parents[0] / "worker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))
import lab_queue  # noqa: E402

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
