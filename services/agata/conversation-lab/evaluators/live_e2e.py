"""One bounded V2 DEV run: Promptfoo generators, blind judges, AGATA aggregation."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Mapping

LAB = Path(__file__).resolve().parents[1]
WORKER = LAB.parents[0] / "worker"
for _path in (LAB / "evaluators", WORKER):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import approval_phases  # noqa: E402
import blind  # noqa: E402
import judge_coordinator  # noqa: E402
import lab_queue  # noqa: E402
import promptfoo_runner  # noqa: E402
import v2_provenance  # noqa: E402
import worker_client  # noqa: E402
from run_coordinator import CallLedger, run_v2_dev_scenario  # noqa: E402


def _private_sanity_findings(private_mapping: Mapping[str, object], scenario_id: str) -> list:
    records = private_mapping.get("sanity_findings")
    if (not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], Mapping)
            or records[0].get("scenario_id") != scenario_id
            or not isinstance(records[0].get("private_quality_findings"), list)):
        return [{"code": "INVALID_SANITY_RECEIPT"}]
    return list(records[0]["private_quality_findings"])


def run_v2_live_e2e(
    scenario_id: str, *, run_id: str, ledger: CallLedger, promptfoo_executable: str,
    receipt_directory: Path, private_directory: Path, env: Mapping[str, str],
) -> dict:
    """Run one immutable A+B authorization; never creates approvals or retries calls."""
    queue = worker_client.canonical_lab_queue(env)
    support = queue.parents[1]
    approval_path = Path(env.get("AGATA_LAB_APPROVAL_FILE") or
                         support / "config" / "lab-execution-approval.json")
    approval = lab_queue.load_owner_approval(
        approval_path, expected_bundle_sha256=lab_queue.worker_bundle_sha256(),
    )
    context = v2_provenance.live_run_context(scenario_id)
    if (approval.get("authorization_type") != "LIVE_RUN_AUTHORIZATION"
            or any(approval.get(key) != value for key, value in context.items())
            or approval.get("run_id") != run_id
            or approval.get("scenario_ids") != [scenario_id]
            or set(approval.get("experiment_ids", [])) != {
                "agata-v2-naturalness-dev", lab_queue.JUDGE_EXPERIMENT_ID}
            or set(approval.get("configuration_ids", [])) != {
                "baseline", "naturalness-v1", "judge-1", "judge-2", "judge-3"}
            or approval.get("model") != "gpt-6-luna"
            or approval.get("reasoning_effort") != "medium"
            or approval.get("max_generator_calls") != 8
            or approval.get("max_judge_calls") != 3
            or approval.get("retries") != 0 or approval.get("fallbacks") != 0
            or approval.get("concurrency") != 1):
        raise PermissionError("LIVE_RUN_AUTHORIZATION does not exactly match this run and current assets")
    if (not isinstance(ledger, CallLedger) or ledger.durable is not True
            or not ledger.verify_persisted()
            or ledger.snapshot().get("limits") != {
                "conversation_arms": 8, "generator_invocations": 32, "judge_invocations": 15}
            or any(ledger.snapshot()[stage]["attempted"] != 0
                   for stage in ("conversation_arms", "generator_invocations", "judge_invocations"))):
        raise PermissionError("LIVE E2E requires a fresh durable run-scoped call ledger")
    usage = lab_queue.owner_approval_usage(queue, approval)
    if usage != {"calls": 0, "generator_calls": 0, "judge_calls": 0}:
        raise PermissionError("LIVE_RUN_AUTHORIZATION has prior usage and cannot be reused")
    promptfoo_runner.validate_promptfoo_cli(promptfoo_executable)
    judge_coordinator._private_directory(Path(receipt_directory))
    judge_coordinator._private_directory(Path(private_directory))

    phase = approval_phases.PhaseApprovalState(queue / "state" / "v2-approval-phase.json")
    auth_sha256 = lab_queue.live_run_authorization_sha256(approval)
    phase.begin_generator(
        run_id=run_id, scenario_id=scenario_id, worker_bundle_sha256=approval["worker_bundle_sha256"],
        approval_id=approval["approval_id"], max_calls=8, authorization_sha256=auth_sha256,
    )
    stage = "GENERATOR"
    try:
        generator_results = run_v2_dev_scenario(
            scenario_id, run_id=run_id, ledger=ledger, promptfoo_executable=promptfoo_executable,
            worker_queue=queue, private_receipt_directory=receipt_directory, env=env,
            phase_state=phase,
        )
        stage = "PHASE_A_VERIFY"
        worker_receipts = [
            lab_queue.read_receipt(queue, job_id)
            for arm in generator_results for job_id in arm["worker_receipt_ids"]
        ]
        promptfoo_receipts = [
            json.loads((Path(receipt_directory) / (invocation_id + ".promptfoo-receipt.json")).read_text(encoding="utf-8"))
            for arm in generator_results for invocation_id in arm["promptfoo_invocation_ids"]
        ]
        phase.close_generator(
            worker_receipts, call_ledger_snapshot=ledger.snapshot(), promptfoo_receipts=promptfoo_receipts,
        )
        phase.begin_judges(
            run_id=run_id, scenario_id=scenario_id,
            worker_bundle_sha256=approval["worker_bundle_sha256"], approval_id=approval["approval_id"],
            max_calls=3, generator_receipts=worker_receipts,
        )

        stage = "BLIND_PACKET"
        _manifest, scenarios = v2_provenance.load_split("DEV")
        packet, private_mapping = blind.make_v2_blind_packets(
            [scenarios[scenario_id]], generator_results, batch_id=run_id,
        )
        judge_coordinator._private_directory(Path(private_directory))
        judge_coordinator._write_once(Path(private_directory) / (run_id + ".blind-packet.json"), packet)
        judge_coordinator._write_once(Path(private_directory) / (run_id + ".private-mapping.json"), private_mapping)
        rubric = json.loads((LAB / "configs" / "owner-preference-rubric-v1.json").read_text(encoding="utf-8"))

        stage = "JUDGES"
        outcome = judge_coordinator.run_judge_batch(
            packet, rubric, batch_id=run_id, dataset_split="DEV", ledger=ledger,
            private_mapping=private_mapping,
            sanity_findings=_private_sanity_findings(private_mapping, scenario_id),
            receipt_directory=Path(receipt_directory), private_directory=Path(private_directory),
            env=env, promptfoo_executable=promptfoo_executable,
        )
        if outcome.get("judge_invocations") != 3 or outcome.get("retries") != 0:
            raise RuntimeError("Phase B did not produce exactly three bounded judge invocations")
        phase.complete_run(judge_invocations=3, outcome_sha256=outcome["receipt_sha256"])
        result = {
            "schema_version": 1, "status": "COMPLETED", "run_id": run_id,
            "scenario_id": scenario_id, "approval_id": approval["approval_id"],
            "model": "gpt-6-luna", "reasoning_effort": "medium",
            "model_identity_status": "CONFIGURED_MODEL_ONLY / ACTUAL_MODEL_UNVERIFIED",
            "generator_invocations": ledger.snapshot()["generator_invocations"]["attempted"],
            "judge_invocations": outcome["judge_invocations"], "retries": 0,
            "decision_type": outcome["decision_type"], "scenarios": outcome["scenarios"],
            "call_ledger": ledger.snapshot(), "judge_outcome_receipt_sha256": outcome["receipt_sha256"],
            "private": True,
        }
        result["receipt_sha256"] = v2_provenance.stable_hash(result)
        judge_coordinator._write_once(Path(private_directory) / (run_id + ".live-e2e-receipt.json"), result)
        return result
    except BaseException as exc:
        try:
            phase.fail_run(stage=stage, reason_code="LIVE_E2E_FAILED")
        except (OSError, PermissionError, ValueError):
            pass
        # Deliberately suppress exception text: it can contain a prompt or local path.
        raise RuntimeError(f"LIVE E2E stopped during {stage}; no automatic retry is permitted") from exc
