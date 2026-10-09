"""One bounded V2 DEV run: Promptfoo generators, blind judges, AGATA aggregation."""
from __future__ import annotations

import json
import hashlib
import os
import sys
from pathlib import Path
from typing import Mapping

LAB = Path(__file__).resolve().parents[1]
WORKER = LAB.parents[0] / "worker"
REPO_ROOT = LAB.parents[2]


class OfflinePreflightError(PermissionError):
    """Sanitized, stable failure for a deterministic no-approval preflight."""

    def __init__(self, stage: str, reason_code: str, cause: BaseException):
        self.stage = stage
        self.reason_code = reason_code
        self.cause_type = type(cause).__name__
        super().__init__(f"{stage}:{reason_code}")


def worker_install_bundle_sha256() -> str:
    """Compute the canonical immutable install-manifest fingerprint without network access."""
    assets = {}
    for name in migrate_worker_runtime.ASSETS:
        path = WORKER / name
        if path.is_symlink() or not path.is_file():
            raise PermissionError("canonical Worker install asset is missing or unsafe")
        assets[name] = migrate_worker_runtime.sha256(path)
    return hashlib.sha256(json.dumps(assets, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
for _path in (LAB / "evaluators", WORKER):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import approval_phases  # noqa: E402
import blind  # noqa: E402
import judge_coordinator  # noqa: E402
import lab_queue  # noqa: E402
import migrate_worker_runtime  # noqa: E402
import promptfoo_runner  # noqa: E402
import v2_provenance  # noqa: E402
import worker_client  # noqa: E402
from run_coordinator import (  # noqa: E402
    CallLedger,
    build_v2_dev_request,
    run_v2_dev_scenario,
)


def _private_sanity_findings(private_mapping: Mapping[str, object], scenario_id: str) -> list:
    records = private_mapping.get("sanity_findings")
    if (not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], Mapping)
            or records[0].get("scenario_id") != scenario_id
            or not isinstance(records[0].get("private_quality_findings"), list)):
        return [{"code": "INVALID_SANITY_RECEIPT"}]
    return list(records[0]["private_quality_findings"])


def validate_offline_runner_prerequisites(
    scenario_id: str, promptfoo_executable: str, env: Mapping[str, str], *, cwd: Path | None = None,
    expected_source_sha: str | None = None, expected_lab_bundle_sha256: str | None = None,
    expected_lab_approval_bundle_sha256: str | None = None,
    expected_worker_install_bundle_sha256: str | None = None,
) -> dict:
    """Validate deterministic source/runtime/CLI prerequisites without approval or run state."""
    def checked(stage: str, reason_code: str, function):
        try:
            return function()
        except OfflinePreflightError:
            raise
        except Exception as exc:
            raise OfflinePreflightError(stage, reason_code, exc) from exc

    def verify_root():
        if (Path.cwd() if cwd is None else Path(cwd)).resolve() != REPO_ROOT.resolve():
            raise PermissionError("repository root mismatch")

    checked("WORKING_DIRECTORY", "REPOSITORY_ROOT_MISMATCH", verify_root)
    if not all((expected_source_sha, expected_lab_bundle_sha256,
                expected_lab_approval_bundle_sha256, expected_worker_install_bundle_sha256)):
        raise OfflinePreflightError("EXACT_CANDIDATE", "EXPECTED_FINGERPRINT_REQUIRED",
                                    ValueError("exact source and bundle fingerprints are required"))
    context = checked("SOURCE_AND_FIXTURES", "V2_CONTEXT_INVALID",
                      lambda: v2_provenance.live_run_context(scenario_id))
    if context.get("dataset_split") != "DEV":
        raise OfflinePreflightError("SOURCE_AND_FIXTURES", "V2_SCENARIO_NOT_DEV",
                                    PermissionError("scenario is outside DEV"))
    if expected_source_sha is not None and context.get("source_sha") != expected_source_sha:
        raise OfflinePreflightError("SOURCE_AND_FIXTURES", "EXACT_SOURCE_MISMATCH",
                                    PermissionError("exact source mismatch"))
    if (expected_lab_bundle_sha256 is not None
            and context.get("lab_bundle_sha256") != expected_lab_bundle_sha256):
        raise OfflinePreflightError("SOURCE_AND_FIXTURES", "LAB_BUNDLE_MISMATCH",
                                    PermissionError("Lab bundle mismatch"))
    lab_approval_bundle = checked("LAB_APPROVAL_BUNDLE", "LAB_APPROVAL_FINGERPRINT_INVALID",
                                  lab_queue.worker_bundle_sha256)
    if (expected_lab_approval_bundle_sha256 is not None
            and lab_approval_bundle != expected_lab_approval_bundle_sha256):
        raise OfflinePreflightError("LAB_APPROVAL_BUNDLE", "LAB_APPROVAL_BUNDLE_MISMATCH",
                                    PermissionError("LAB approval bundle mismatch"))

    worker_install_bundle = checked(
        "WORKER_INSTALL_BUNDLE", "WORKER_INSTALL_FINGERPRINT_INVALID", worker_install_bundle_sha256,
    )
    if (expected_worker_install_bundle_sha256 is not None
            and worker_install_bundle != expected_worker_install_bundle_sha256):
        raise OfflinePreflightError("WORKER_INSTALL_BUNDLE", "WORKER_INSTALL_BUNDLE_MISMATCH",
                                    PermissionError("Worker install bundle mismatch"))
    promptfoo_version = checked(
        "PROMPTFOO_CLI", "PINNED_CLI_INVALID",
        lambda: promptfoo_runner.validate_promptfoo_cli(promptfoo_executable),
    )
    checked("NODE_RUNTIME", "NODE_VERSION_OR_PATH_INVALID",
            lambda: promptfoo_runner.resolve_promptfoo_node(promptfoo_runner.build_cli_environment(env)))
    checked("PYTHON_RUNTIME", "WORKER_PYTHON_INVALID", lambda: promptfoo_runner.validate_promptfoo_python(
        env.get("PROMPTFOO_PYTHON") or sys.executable, env,
    ))
    def validate_provider_contracts():
        manifest, scenarios = v2_provenance.load_split("DEV")
        scenario = scenarios.get(scenario_id)
        if scenario is None:
            raise PermissionError("scenario not in committed DEV split")
        run_id = "agata-offline-preflight"
        configs = []
        for configuration_id in ("baseline", "naturalness-v1"):
            request = build_v2_dev_request(scenario, configuration_id, 0, [])
            invocation_id = "pfv2-" + hashlib.sha256(
                f"{run_id}\0{configuration_id}\0{0}".encode()).hexdigest()[:59]
            config = promptfoo_runner.build_worker_v2_provider_request(
                request, run_id=run_id, invocation_id=invocation_id,
                arm_id=f"{run_id}-{configuration_id}", configuration_id=configuration_id,
                scenario_id=scenario_id, turn_index=0, history_receipt_ids=[],
                python_executable=env.get("PROMPTFOO_PYTHON") or sys.executable,
            )
            provider = config["providers"][0]
            provider_config = provider.get("config", {})
            if (provider_config.get("model") != "gpt-6-luna"
                    or provider_config.get("reasoning_effort") != "medium"
                    or provider_config.get("maxRetries") != 0
                    or provider_config.get("fallbacks") != []):
                raise PermissionError("provider model/budget contract is not exact")
            promptfoo_runner.validate_live_environment(env, config)
            configs.append(config)
        if len(configs) != 2 or manifest.get("dataset_id") != "agata-conversation-lab-v2":
            raise PermissionError("V2 provider arm contracts are incomplete")

    checked("PROVIDER_AND_ENVIRONMENT", "V2_WORKER_PROVIDER_CONTRACT_INVALID", validate_provider_contracts)
    return {
        "status": "OFFLINE_PREFLIGHT_PASS", "source_sha": context["source_sha"],
        "lab_bundle_sha256": context["lab_bundle_sha256"], "scenario_id": scenario_id,
        "dataset_split": "DEV", "promptfoo_version": promptfoo_version,
        "node_requirement": "22.22.0+ verified", "python_requirement": "3.11+ verified",
        "model_contract": "gpt-6-luna / medium", "provider": "canonical Worker LAB adapter",
        "lab_approval_bundle_sha256": lab_approval_bundle,
        "worker_install_bundle_sha256": worker_install_bundle,
        "approval_read": False, "run_state_created": False,
    }


def _require_phase_safety_preflight(phase: str) -> dict:
    """Require canonical, fresh production safety evidence before each live phase."""
    result = migrate_worker_runtime.read_only_preflight()
    if not isinstance(result, Mapping):
        raise PermissionError(f"{phase} canonical safety preflight returned no result")
    valid = (
        result.get("outcome") == "PASS"
        and result.get("candidate_verified") is True
        and result.get("launchagent_loaded") is True
        and result.get("active_inference_absent") is True
        and result.get("release_and_transition_locks_free") is True
        and type(result.get("pool_busy")) is int and result["pool_busy"] == 0
        and result.get("mutation_performed") is False
    )
    lanes = result.get("lanes")
    if not isinstance(lanes, Mapping) or set(lanes) != {"GPOINT", "GSALES"}:
        valid = False
    else:
        for lane in ("GPOINT", "GSALES"):
            evidence = lanes.get(lane)
            if not isinstance(evidence, Mapping) or not (
                evidence.get("health") == "HEALTHY"
                and evidence.get("queue_snapshot_state") == "VERIFIED"
                and type(evidence.get("pending")) is int and evidence["pending"] == 0
                and type(evidence.get("running")) is int and evidence["running"] == 0
                and evidence.get("snapshot_fresh") is True
                and evidence.get("identity_and_readonly_provenance_verified") is True
            ):
                valid = False
    if not valid:
        raise PermissionError(f"{phase} canonical production safety preflight failed closed")

    # Keep only the canonical controller's fixed, non-sensitive summary fields.
    return {
        "outcome": "PASS", "candidate_verified": True, "launchagent_loaded": True,
        "active_inference_absent": True, "release_and_transition_locks_free": True,
        "pool_busy": 0, "mutation_performed": False,
        "lanes": {
            lane: {
                "health": "HEALTHY", "queue_snapshot_state": "VERIFIED",
                "pending": 0, "running": 0, "snapshot_fresh": True,
                "identity_and_readonly_provenance_verified": True,
            }
            for lane in ("GPOINT", "GSALES")
        },
    }


def run_v2_live_e2e(
    scenario_id: str, *, run_id: str, ledger: CallLedger, promptfoo_executable: str,
    receipt_directory: Path, private_directory: Path, env: Mapping[str, str],
    expected_source_sha: str, expected_lab_bundle_sha256: str,
    expected_lab_approval_bundle_sha256: str, expected_worker_install_bundle_sha256: str,
) -> dict:
    """Run one immutable A+B authorization; never creates approvals or retries calls."""
    # Keep every deterministic local prerequisite ahead of approval lookup and
    # phase-state creation. The CLI also runs this before creating the durable run directory.
    validate_offline_runner_prerequisites(
        scenario_id, promptfoo_executable, env,
        expected_source_sha=expected_source_sha,
        expected_lab_bundle_sha256=expected_lab_bundle_sha256,
        expected_lab_approval_bundle_sha256=expected_lab_approval_bundle_sha256,
        expected_worker_install_bundle_sha256=expected_worker_install_bundle_sha256,
    )
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

    phase = approval_phases.PhaseApprovalState(lab_queue.live_run_phase_state_path(queue, run_id))
    auth_sha256 = lab_queue.live_run_authorization_sha256(approval)
    phase_preflight_evidence = {}
    stage = "PHASE_A_PREFLIGHT"
    try:
        phase.begin_generator(
            run_id=run_id, scenario_id=scenario_id, worker_bundle_sha256=approval["worker_bundle_sha256"],
            approval_id=approval["approval_id"], max_calls=8, authorization_sha256=auth_sha256,
        )
        phase_preflight_evidence["phase_a"] = _require_phase_safety_preflight("PHASE_A")
        stage = "GENERATOR"
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
        stage = "PHASE_B_PREFLIGHT"
        phase_preflight_evidence["phase_b"] = _require_phase_safety_preflight("PHASE_B")
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
            "production_preflight": phase_preflight_evidence,
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
