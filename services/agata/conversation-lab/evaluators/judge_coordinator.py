"""Run three isolated Promptfoo judges and aggregate their private receipts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Mapping

LAB = Path(__file__).resolve().parents[1]
WORKER_DIR = LAB.parents[0] / "worker"
for _path in (LAB / "evaluators", WORKER_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import aggregator  # noqa: E402
import judges  # noqa: E402
import promptfoo_runner  # noqa: E402
import worker_client  # noqa: E402
import lab_queue  # noqa: E402
import v2_provenance  # noqa: E402


def _private_directory(path: Path) -> Path:
    path = Path(path)
    if (not path.is_absolute() or not path.is_dir() or path.is_symlink()
            or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
        raise PermissionError("judge artifacts require an existing owner-only directory")
    return path


def _write_once(path: Path, value: Mapping[str, object]) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)


def _approval_capacity(jobs: list[dict], env: Mapping[str, str]) -> None:
    queue = worker_client.canonical_lab_queue(env)
    support = queue.parents[1]
    approval_path = Path(env.get("AGATA_LAB_APPROVAL_FILE") or
                         support / "config/lab-execution-approval.json")
    approval = lab_queue.load_owner_approval(
        approval_path, expected_bundle_sha256=lab_queue.worker_bundle_sha256(),
    )
    if approval.get("experiment_ids") is None:
        raise PermissionError("LAB judge approval scope is incomplete")
    for job in jobs:
        worker_client.validate_owner_approval(job, env)
        if (job["experiment_id"] not in approval["experiment_ids"]
                or job["configuration_id"] not in approval["configuration_ids"]
                or job["scenario_id"] not in approval["scenario_ids"]):
            raise PermissionError("LAB approval does not cover every judge invocation")
    used = lab_queue.owner_approval_usage_calls(support, approval)
    if used + len(jobs) > approval["max_calls"]:
        raise PermissionError("LAB approval has insufficient remaining calls for the complete judge batch")


def _judge_receipt(path: Path, invocation_id: str, expected: dict) -> dict | None:
    try:
        value = json.loads((path / (invocation_id + ".judge-receipt.json")).read_text(encoding="utf-8"))
        cli_value = json.loads((path / (invocation_id + ".promptfoo-receipt.json")).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if (not isinstance(value, dict) or value.get("outcome") != "success"
            or value.get("worker_terminal_status") != "completed"
            or any(value.get(key) != item for key, item in expected.items())
            or not isinstance(value.get("worker_receipt_sha256"), str)
            or len(value["worker_receipt_sha256"]) != 64):
        return None
    if (not isinstance(cli_value, dict) or cli_value.get("promptfoo_success") is not True
            or cli_value.get("result_count") != 1
            or any(cli_value.get(key) != item for key, item in {
                "run_id": expected["run_id"], "batch_id": expected["batch_id"],
                "promptfoo_invocation_id": expected["promptfoo_invocation_id"],
                "worker_job_id": value["worker_job_id"],
                "worker_receipt_sha256": value["worker_receipt_sha256"],
            }.items())):
        return None
    normalized = value.get("normalized_judge_receipt")
    required = {"judge_slot", "scenario_id", "presented_labels", "preferred_position",
                "core_dimension_positions", "conditional_evidence", "reasons", "defects",
                "critical_defects", "uncertainty", "position_sensitivity"}
    if not isinstance(normalized, dict) or set(normalized) != required:
        return None
    return normalized


def run_judge_batch(packet: Mapping[str, object], rubric: Mapping[str, object], *, batch_id: str,
                    dataset_split: str, ledger, private_mapping: Mapping[str, object],
                    sanity_findings: list, receipt_directory: Path, private_directory: Path,
                    env: Mapping[str, str], promptfoo_executable: str,
                    _invoke_promptfoo=None) -> dict:
    """Execute at most 2 pairs / 6 judges through Promptfoo and the canonical LAB queue.

    The call ledger and OWNER approval are checked/reserved before the first CLI
    process. Missing or ambiguous receipts are never retried and always escalate.
    """
    if dataset_split not in {"DEV", "VALIDATION"}:
        raise PermissionError("judges cannot access SEALED HOLDOUT")
    if not isinstance(batch_id, str) or not lab_queue.ID_RE.fullmatch(batch_id):
        raise ValueError("immutable judge batch ID is required")
    if not isinstance(packet, Mapping) or not isinstance(packet.get("scenarios"), list):
        raise ValueError("blind packet is malformed")
    v2_provenance.validate_blind_packet(packet, private_mapping, dataset_split)
    scenarios = packet["scenarios"]
    if not 1 <= len(scenarios) <= 2:
        raise PermissionError("one Worker approval can cover at most two pairs / six judge calls")
    if (getattr(ledger, "durable", False) is not True or not ledger.verify_persisted()
            or ledger.snapshot().get("limits") != {
                "conversation_arms": 8, "generator_invocations": 32, "judge_invocations": 15
            }):
        raise PermissionError("judging requires the exact durable call ledger")
    receipt_directory = _private_directory(receipt_directory)
    private_directory = _private_directory(private_directory)
    prompts, private_plan = judges.judge_inputs(packet, rubric, batch_id=batch_id, judge_count=3)
    if len(prompts) != len(scenarios) * 3 or private_plan.get("private") is not True:
        raise ValueError("judge plan does not contain exactly three independent judges per pair")
    ledger_snapshot = ledger.snapshot()
    snapshot = ledger_snapshot["judge_invocations"]
    if snapshot["attempted"] + len(prompts) > ledger_snapshot["limits"]["judge_invocations"]:
        raise OverflowError("judge call ledger has insufficient remaining budget for complete batch")

    queue_path = worker_client.canonical_lab_queue(env)
    calls = []
    for prompt_record, plan in zip(prompts, private_plan["judge_plan"], strict=True):
        slot = plan["judge_slot"]
        scenario_id = plan["scenario_id"]
        labels = plan["presented_labels"]
        job = worker_client.build_judge_job(
            prompt_record["prompt"], batch_id=batch_id, scenario_id=scenario_id,
            dataset_split=dataset_split, judge_slot=slot, presented_labels=labels,
        )
        if job["job_type"] != "judge" or job["origin"] != "conversation-lab-judge":
            raise PermissionError("coordinator may create only source-verified LAB judge jobs")
        calls.append((prompt_record, plan, job))
    # The whole bounded batch must be approved before any call is reserved or
    # started; a partial approval cannot silently truncate the judge ensemble.
    _approval_capacity([job for _prompt, _plan, job in calls], env)
    existing_ids = {row["invocation_id"] for row in snapshot["invocations"]}
    existing_judge_ids = {row.get("metadata", {}).get("judge_id") for row in snapshot["invocations"]}
    if any(job["promptfoo_invocation_id"] in existing_ids for _prompt, _plan, job in calls):
        raise PermissionError("judge invocation already exists in the durable ledger; retries are forbidden")
    if any(f"{batch_id}:{plan['scenario_id']}:{plan['judge_slot']}" in existing_judge_ids
           for _prompt, plan, _job in calls):
        raise PermissionError("judge identity already exists in the durable ledger; retries are forbidden")

    plan_path = private_directory / (batch_id + ".judge-plan.json")
    # Persist seed, control selection and presentation order privately before
    # any reservation/provider invocation; write-once prevents accidental reuse.
    _write_once(plan_path, private_plan)

    for prompt_record, plan, job in calls:
        invocation_id = job["promptfoo_invocation_id"]
        ledger.reserve("judge", invocation_id, metadata={
            "judge_id": f"{batch_id}:{plan['scenario_id']}:{plan['judge_slot']}",
            "batch_id": batch_id, "scenario_id": plan["scenario_id"],
            "dataset_split": dataset_split, "judge_slot": plan["judge_slot"],
            "worker_job_id": job["job_id"], "model": "gpt-6-luna",
            "reasoning_effort": "medium",
        })

    invoke = _invoke_promptfoo
    collected: dict[str, list[dict]] = {scenario["scenario_id"]: [] for scenario in scenarios}
    failures: dict[str, list[str]] = {scenario["scenario_id"]: [] for scenario in scenarios}
    for prompt_record, plan, job in calls:
        invocation_id = job["promptfoo_invocation_id"]
        try:
            kwargs = {
                "run_id": batch_id, "batch_id": batch_id,
                "invocation_id": invocation_id, "scenario_id": plan["scenario_id"],
                "dataset_split": dataset_split, "judge_slot": plan["judge_slot"],
                "presented_labels": plan["presented_labels"],
                "promptfoo_executable": promptfoo_executable,
                "worker_queue": queue_path, "private_receipt_directory": receipt_directory,
                "call_ledger": ledger, "env": env,
            }
            if invoke:
                cli_result = invoke(prompt_record["prompt"], config=kwargs,
                                   receipt_directory=receipt_directory, call_ledger=ledger,
                                   worker_queue=queue_path, promptfoo_executable=promptfoo_executable,
                                   env=env, run_id=batch_id)
                success = cli_result == 0 or getattr(cli_result, "returncode", None) == 0
            else:
                cli_result = promptfoo_runner.run_promptfoo_judge_eval(
                    prompt_record["prompt"], **kwargs,
                )
                success = cli_result.returncode == 0
            normalized = _judge_receipt(receipt_directory, invocation_id, {
                "run_id": batch_id, "batch_id": batch_id,
                "promptfoo_invocation_id": invocation_id,
                "lab_ledger_attempt_id": invocation_id,
                "worker_job_id": job["job_id"], "judge_slot": plan["judge_slot"],
                "scenario_id": plan["scenario_id"], "dataset_split": dataset_split,
                "presentation_labels": plan["presented_labels"],
            }) if success else None
            if normalized is None:
                failures[plan["scenario_id"]].append("MISSING_OR_INVALID_JUDGE_OUTPUT")
                current = ledger.__class__(journal_path=ledger.journal_path)
                entry = next((row for row in current.snapshot()["judge_invocations"]["invocations"]
                              if row["invocation_id"] == invocation_id), None)
                if entry is not None and entry["outcome"] is None:
                    ledger.complete("judge", invocation_id, "ambiguous")
                continue
            collected[plan["scenario_id"]].append(normalized)
        except Exception:
            failures[plan["scenario_id"]].append("MISSING_OR_INVALID_JUDGE_OUTPUT")
            current = ledger.__class__(journal_path=ledger.journal_path)
            entry = next((row for row in current.snapshot()["judge_invocations"]["invocations"]
                          if row["invocation_id"] == invocation_id), None)
            if entry is not None and entry["outcome"] is None:
                ledger.complete("judge", invocation_id, "ambiguous")
        finally:
            # Each Promptfoo Python provider is a child process and appends to
            # the same journal; refresh before the next invocation validates it.
            ledger.refresh()

    scenario_results = []
    for scenario in scenarios:
        scenario_id = scenario["scenario_id"]
        outcome = aggregator.aggregate(collected[scenario_id], private_mapping, sanity_findings)
        if failures[scenario_id]:
            outcome = {
                "decision": "HUMAN_ESCALATION",
                "reason_codes": sorted(set(outcome.get("reason_codes", []) + failures[scenario_id])),
                "normalized_overall_votes": outcome.get("normalized_overall_votes", []),
                "core_dimension_majorities": outcome.get("core_dimension_majorities", {"X": 0, "Y": 0}),
                "aggregator_version": 1,
            }
        scenario_results.append({"scenario_id": scenario_id, **outcome,
                                 "judge_receipts": collected[scenario_id]})
    all_auto = all(row["decision"] in {"AUTO_WIN_X", "AUTO_WIN_Y"} for row in scenario_results)
    private_result = {
        "schema_version": 1, "batch_id": batch_id, "dataset_split": dataset_split,
        "decision": "AUTO_DECISION" if all_auto else "HUMAN_ESCALATION",
        "decision_type": "AUTO_DECISION" if all_auto else "HUMAN_ESCALATION",
        "scenarios": scenario_results, "judge_invocations": len(calls),
        "retries": 0, "aggregator_version": 1,
    }
    _write_once(private_directory / (batch_id + ".judge-outcome.json"), private_result)
    return private_result
