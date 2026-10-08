"""Offline proof of the bounded V2 DEV → Promptfoo → Worker contract."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

LAB = Path(__file__).resolve().parents[1]
WORKER = LAB.parents[0] / "worker"
for path in (LAB / "evaluators", WORKER):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import lab_queue
import worker_client
import v2_provenance
from run_coordinator import CallLedger


class V2GeneratorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _manifest, scenarios = v2_provenance.load_split("DEV")
        cls.scenario = scenarios["v2-dev-clean-20261006-everyday"]

    def request(self, configuration="baseline", turn=0, history=None, scenario=None):
        from run_coordinator import build_v2_dev_request
        scenario = scenario or self.scenario
        if history is None:
            history = [
                {"role": "user", "text": f"prior user {index // 2}"} if index % 2 == 0
                else {"role": "assistant", "text": f"prior assistant {index // 2}"}
                for index in range(turn * 2)
            ]
        return build_v2_dev_request(scenario, configuration, turn, history)

    def test_worker_accepts_only_committed_v2_dev_request_and_exact_contract(self):
        request = self.request()
        invocation_id = "pfv2-" + hashlib.sha256("\0".join(("run-contract", "baseline", "0")).encode()).hexdigest()[:59]
        job = worker_client.build_v2_dev_job(
            request=request, run_id="run-contract", arm_id="run-contract-baseline",
            configuration_id="baseline", scenario_id=self.scenario["scenario_id"],
            turn_index=0, history_receipt_ids=[], invocation_id=invocation_id,
        )
        _validated, _payload = lab_queue._validate(job, root=Path("/tmp/agata-v2-queue"))
        self.assertEqual(job["job_type"], "v2-dev-generator")
        self.assertEqual((job["model"], job["reasoning_effort"]), ("gpt-6-luna", "medium"))
        changed = json.loads(json.dumps(job))
        changed["request"]["user_message"] += " произвольный текст"
        with self.assertRaises(lab_queue.LabQueueError):
            lab_queue._validate(changed, root=Path("/tmp/agata-v2-queue"))
        wrong_model = json.loads(json.dumps(job)); wrong_model["model"] = "gpt-6.1-sol"
        with self.assertRaises(lab_queue.LabQueueError):
            lab_queue._validate(wrong_model, root=Path("/tmp/agata-v2-queue"))

    def test_worker_manifest_matches_only_hash_committed_v2_dev_fixtures(self):
        _split_manifest, source = v2_provenance.load_split("DEV")
        bundled = lab_queue.read_v2_dev_manifest()
        self.assertEqual(set(bundled["scenarios"]), set(source))
        for scenario_id, scenario in source.items():
            row = bundled["scenarios"][scenario_id]
            self.assertEqual(row["source_sha256"], v2_provenance.stable_hash(scenario))
            self.assertEqual(row["user_turns"], [turn["text"] for turn in scenario["dialogue_turns"]
                                                  if turn["role"] == "user"])
            for arm in ("baseline", "naturalness-v1"):
                for turn in range(4):
                    from run_coordinator import build_v2_dev_request
                    request = self.request(arm, turn, scenario=scenario)
                    digest = v2_provenance.stable_hash({**request, "history": []})
                    self.assertEqual(row["request_hashes"][arm][turn], digest)
        self.assertEqual(bundled["dataset_split"], "DEV")
        self.assertEqual(bundled["scenarios"].keys(), source.keys())

    def test_promptfoo_provider_request_is_bound_to_one_arm_turn_and_never_sealed(self):
        from promptfoo_runner import build_worker_v2_provider_request
        request = self.request("naturalness-v1", 0)
        invocation_id = "pfv2-" + hashlib.sha256("\0".join(("run-contract", "naturalness-v1", "0")).encode()).hexdigest()[:59]
        config = build_worker_v2_provider_request(
            request, run_id="run-contract", invocation_id=invocation_id,
            arm_id="run-contract-naturalness-v1", configuration_id="naturalness-v1",
            scenario_id=self.scenario["scenario_id"], turn_index=0,
            history_receipt_ids=[], python_executable=sys.executable,
        )
        self.assertEqual(config["evaluateOptions"], {"maxConcurrency": 1, "cache": False})
        self.assertEqual(config["providers"][0]["config"]["maxRetries"], 0)
        self.assertEqual(config["providers"][0]["config"]["fallbacks"], [])
        with self.assertRaises(PermissionError):
            build_worker_v2_provider_request(
                request, run_id="run-contract", invocation_id="pfv2-contract-2",
                arm_id="run-contract-naturalness-v1", configuration_id="naturalness-v1",
                scenario_id="sealed-holdout", turn_index=0,
                history_receipt_ids=[], python_executable=sys.executable,
            )

    def test_missing_nonlocal_promptfoo_cli_fails_before_any_provider_invocation(self):
        from promptfoo_runner import validate_promptfoo_cli
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PermissionError):
                validate_promptfoo_cli(str(Path(directory) / "promptfoo"))

    def test_two_phase_approval_contract_is_sequential_and_bounded(self):
        from approval_phases import PhaseApprovalState
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            phase_dir = root / "state"; phase_dir.mkdir(mode=0o700)
            state = PhaseApprovalState(phase_dir / "v2-approval-phase.json")
            state.begin_generator(run_id="run-contract", scenario_id=self.scenario["scenario_id"],
                                  worker_bundle_sha256=lab_queue.worker_bundle_sha256(),
                                  approval_id="approval-generator", max_calls=8)
            scoped_job = {"experiment_id": "agata-v2-naturalness-dev", "job_type": "v2-dev-generator",
                          "run_id": "run-contract", "scenario_id": self.scenario["scenario_id"]}
            approval = {"approval_id": "approval-generator", "worker_bundle_sha256": lab_queue.worker_bundle_sha256(),
                        "phase": "GENERATOR", "run_id": "run-contract", "max_calls": 8,
                        "experiment_ids": ["agata-v2-naturalness-dev"],
                        "configuration_ids": ["baseline", "naturalness-v1"],
                        "scenario_ids": [self.scenario["scenario_id"]]}
            lab_queue.validate_approval_for_job(scoped_job, approval, root=root)
            with self.assertRaisesRegex(lab_queue.LabQueueError, "phase_scope_mismatch"):
                lab_queue.validate_approval_for_job(
                    scoped_job, {**approval, "scenario_ids": [self.scenario["scenario_id"], "other-scenario"]}, root=root)
            with self.assertRaises(PermissionError):
                state.begin_judges(run_id="run-contract", scenario_id=self.scenario["scenario_id"],
                                   worker_bundle_sha256=lab_queue.worker_bundle_sha256(),
                                   approval_id="approval-judges", max_calls=3,
                                   generator_receipts=[])
            receipts = []
            ledger_rows = []
            promptfoo_receipts = []
            for arm in ("baseline", "naturalness-v1"):
                for turn in range(4):
                    invocation_id = "pfv2-" + hashlib.sha256(
                        "\0".join(("run-contract", arm, str(turn))).encode()).hexdigest()[:59]
                    job_id = f"gen-{arm}-{turn}"
                    receipts.append({"job_id": f"gen-{arm}-{turn}", "status": "completed",
                        "slot_id": "slot-03", "response": "mock reply", "identity": {
                            "experiment_id": "agata-v2-naturalness-dev", "run_id": "run-contract",
                            "scenario_id": self.scenario["scenario_id"], "configuration_id": arm,
                            "job_type": "v2-dev-generator", "dataset_split": "DEV",
                            "turn_index": turn, "promptfoo_invocation_id": invocation_id}})
                    ledger_rows.append({"invocation_id": invocation_id, "outcome": "success",
                        "metadata": {"run_id": "run-contract", "scenario_id": self.scenario["scenario_id"],
                                     "configuration": arm, "turn_index": turn, "worker_job_id": job_id}})
                    promptfoo_receipts.append({"promptfoo_invocation_id": invocation_id,
                        "promptfoo_success": True, "result_count": 1, "run_id": "run-contract",
                        "worker_job_id": job_id})
            state.close_generator(receipts, call_ledger_snapshot={
                "generator_invocations": {"attempted": 8, "outcomes": {"success": 8}, "invocations": ledger_rows}
            }, promptfoo_receipts=promptfoo_receipts)
            state.begin_judges(run_id="run-contract", scenario_id=self.scenario["scenario_id"],
                               worker_bundle_sha256=lab_queue.worker_bundle_sha256(),
                               approval_id="approval-judges", max_calls=3,
                               generator_receipts=receipts)
            with self.assertRaises(PermissionError):
                state.begin_judges(run_id="run-contract", scenario_id=self.scenario["scenario_id"],
                                   worker_bundle_sha256=lab_queue.worker_bundle_sha256(),
                                   approval_id="approval-generator", max_calls=3,
                                   generator_receipts=receipts)

    def test_worker_approval_schema_enforces_expiry_bundle_and_phase_budgets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "approval.json"
            path.parent.chmod(0o700)
            approval = {
                "approval_id": "phase-a", "worker_bundle_sha256": lab_queue.worker_bundle_sha256(),
                "experiment_ids": ["agata-v2-naturalness-dev"],
                "configuration_ids": ["baseline", "naturalness-v1"],
                "scenario_ids": [self.scenario["scenario_id"]], "max_calls": 8,
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "phase": "GENERATOR", "run_id": "run-contract",
            }
            path.write_text(json.dumps(approval), encoding="utf-8")
            path.chmod(0o600)
            self.assertEqual(lab_queue.load_owner_approval(
                path, expected_bundle_sha256=lab_queue.worker_bundle_sha256())["phase"], "GENERATOR")
            for changed in (
                {"max_calls": 7}, {"max_calls": 9}, {"phase": "JUDGES"},
                {"worker_bundle_sha256": "0" * 64},
                {"expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()},
            ):
                bad = {**approval, **changed}
                path.write_text(json.dumps(bad), encoding="utf-8")
                with self.subTest(changed=changed), self.assertRaises(lab_queue.LabQueueError):
                    lab_queue.load_owner_approval(path, expected_bundle_sha256=lab_queue.worker_bundle_sha256())
            approval.update({"approval_id": "phase-b", "phase": "JUDGES", "max_calls": 3,
                             "experiment_ids": [lab_queue.JUDGE_EXPERIMENT_ID],
                             "configuration_ids": ["judge-1", "judge-2", "judge-3"]})
            path.write_text(json.dumps(approval), encoding="utf-8")
            self.assertEqual(lab_queue.load_owner_approval(
                path, expected_bundle_sha256=lab_queue.worker_bundle_sha256())["max_calls"], 3)


if __name__ == "__main__":
    unittest.main()
