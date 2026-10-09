"""Offline proof of the bounded V2 DEV → Promptfoo → Worker contract."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import sys
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch

LAB = Path(__file__).resolve().parents[1]
WORKER = LAB.parents[0] / "worker"
for path in (LAB / "evaluators", WORKER):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import lab_queue
import approval_phases
import live_e2e
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

    def test_live_phase_preflight_requires_fresh_idle_verified_production_queues(self):
        valid = {
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
        with patch.object(live_e2e.migrate_worker_runtime, "read_only_preflight", return_value=valid):
            evidence = live_e2e._require_phase_safety_preflight("PHASE_A")
            self.assertEqual(evidence["outcome"], "PASS")
            self.assertEqual(set(evidence["lanes"]), {"GPOINT", "GSALES"})

        invalid_cases = [
            ("candidate_verified", False), ("launchagent_loaded", False),
            ("active_inference_absent", False), ("release_and_transition_locks_free", False),
            ("pool_busy", 1), ("mutation_performed", True), ("outcome", "BLOCKED"),
        ]
        for key, value in invalid_cases:
            with self.subTest(key=key):
                result = {**valid, key: value}
                with patch.object(live_e2e.migrate_worker_runtime, "read_only_preflight", return_value=result):
                    with self.assertRaises(PermissionError):
                        live_e2e._require_phase_safety_preflight("PHASE_B")

        for lane in ("GPOINT", "GSALES"):
            for key, value in (
                ("health", "DEGRADED"), ("queue_snapshot_state", "UNAVAILABLE"),
                ("pending", 1), ("running", 1), ("snapshot_fresh", False),
                ("identity_and_readonly_provenance_verified", False),
            ):
                with self.subTest(lane=lane, key=key):
                    lanes = {name: dict(row) for name, row in valid["lanes"].items()}
                    lanes[lane][key] = value
                    result = {**valid, "lanes": lanes}
                    with patch.object(live_e2e.migrate_worker_runtime, "read_only_preflight", return_value=result):
                        with self.assertRaises(PermissionError):
                            live_e2e._require_phase_safety_preflight("PHASE_B")

    def test_phase_a_preflight_failure_stops_before_generator_reservation(self):
        self._assert_live_preflight_failure_stops_calls(fail_phase="PHASE_A")

    def test_phase_b_preflight_failure_stops_before_any_judge_invocation(self):
        self._assert_live_preflight_failure_stops_calls(fail_phase="PHASE_B")

    def _assert_live_preflight_failure_stops_calls(self, *, fail_phase):
        scenario_id = self.scenario["scenario_id"]
        run_id = "preflight-test-" + fail_phase.lower()
        bundle = "b" * 64
        context = {
            "source_sha": "a" * 40, "lab_bundle_sha256": "c" * 64,
            "naturalness_policy_sha256": "d" * 64, "rubric_sha256": "e" * 64,
            "dataset_id": "agata-conversation-lab-v2", "dataset_split": "DEV",
            "dataset_manifest_sha256": "f" * 64,
        }
        approval = {
            "authorization_type": "LIVE_RUN_AUTHORIZATION", "approval_id": "approval-preflight-test",
            "run_id": run_id, "worker_bundle_sha256": bundle, **context,
            "experiment_ids": ["agata-v2-naturalness-dev", lab_queue.JUDGE_EXPERIMENT_ID],
            "configuration_ids": ["baseline", "naturalness-v1", "judge-1", "judge-2", "judge-3"],
            "scenario_ids": [scenario_id], "model": "gpt-6-luna", "reasoning_effort": "medium",
            "max_generator_calls": 8, "max_judge_calls": 3, "retries": 0,
            "fallbacks": 0, "concurrency": 1, "expires_at": "2099-01-01T00:00:00Z",
        }
        valid = {
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
        blocked = {**valid, "outcome": "BLOCKED"}
        preflight_results = [blocked] if fail_phase == "PHASE_A" else [valid, blocked]

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name); root.chmod(0o700)
            support = root / "support"; support.mkdir(mode=0o700)
            config = support / "config"; config.mkdir(mode=0o700)
            queues = support / "queues"; queues.mkdir(mode=0o700)
            queue = queues / "lab"; queue.mkdir(mode=0o700)
            state_dir = queue / "state"; state_dir.mkdir(mode=0o700)
            receipts = root / "receipts"; receipts.mkdir(mode=0o700)
            private = root / "private"; private.mkdir(mode=0o700)
            ledger = CallLedger(journal_path=root / "call-ledger.jsonl")
            receipt_rows = {}
            generator_mock = unittest.mock.Mock()
            judge_mock = unittest.mock.Mock()

            def fake_generator(_scenario_id, **_kwargs):
                arms = []
                for configuration in ("baseline", "naturalness-v1"):
                    arm_id = f"{run_id}-{configuration}"
                    arm_ledger_id = "arm-" + configuration
                    ledger.reserve("arm", arm_ledger_id, configuration=configuration,
                                   metadata={"arm_id": arm_id, "scenario_id": scenario_id})
                    job_ids, invocation_ids = [], []
                    for turn in range(4):
                        invocation_id = "pfv2-" + hashlib.sha256(
                            f"{run_id}\0{configuration}\0{turn}".encode()).hexdigest()[:59]
                        job_id = f"job-{configuration}-{turn}"
                        ledger.reserve("generator", invocation_id, metadata={
                            "arm_id": arm_id, "turn_index": turn, "configuration": configuration,
                            "scenario_id": scenario_id, "run_id": run_id,
                            "experiment_id": "agata-v2-naturalness-dev",
                            "worker_job_id": f"job-{configuration}-{turn}",
                        })
                        ledger.complete("generator", invocation_id, "success")
                        job_ids.append(job_id); invocation_ids.append(invocation_id)
                        receipt_rows[job_id] = {
                            "job_id": job_id, "status": "completed", "slot_id": "slot-03",
                            "response": "mock response", "identity": {
                                "experiment_id": "agata-v2-naturalness-dev", "job_type": "v2-dev-generator",
                                "dataset_split": "DEV", "run_id": run_id, "scenario_id": scenario_id,
                                "configuration_id": configuration, "turn_index": turn,
                                "promptfoo_invocation_id": invocation_id,
                            },
                        }
                        (receipts / (invocation_id + ".promptfoo-receipt.json")).write_text(json.dumps({
                            "promptfoo_invocation_id": invocation_id, "promptfoo_success": True,
                            "result_count": 1, "run_id": run_id, "worker_job_id": job_id,
                        }), encoding="utf-8")
                    ledger.complete("arm", arm_ledger_id, "success")
                    arms.append({"worker_receipt_ids": job_ids, "promptfoo_invocation_ids": invocation_ids})
                return arms

            if fail_phase == "PHASE_B":
                generator_mock.side_effect = fake_generator

            env = {"GPOINT_AGATA_WORKER_SUPPORT": str(support)}
            with ExitStack() as stack:
                stack.enter_context(patch.object(live_e2e.worker_client, "canonical_lab_queue", return_value=queue))
                stack.enter_context(patch.object(lab_queue, "worker_bundle_sha256", return_value=bundle))
                stack.enter_context(patch.object(lab_queue, "load_owner_approval", return_value=approval))
                stack.enter_context(patch.object(lab_queue, "owner_approval_usage",
                                                 return_value={"calls": 0, "generator_calls": 0, "judge_calls": 0}))
                stack.enter_context(patch.object(v2_provenance, "live_run_context", return_value=context))
                stack.enter_context(patch.object(live_e2e.promptfoo_runner, "validate_promptfoo_cli",
                                                 return_value="0.123.1"))
                stack.enter_context(patch.object(live_e2e, "installed_lab_runtime_identity", return_value={
                    "runtime_source_sha": "1" * 40,
                    "worker_install_bundle_sha256": live_e2e.worker_install_bundle_sha256(),
                    "lab_approval_bundle_sha256": bundle,
                }))
                stack.enter_context(patch.object(live_e2e.migrate_worker_runtime, "read_only_preflight",
                                                 side_effect=preflight_results))
                stack.enter_context(patch.object(live_e2e, "run_v2_dev_scenario", generator_mock))
                stack.enter_context(patch.object(live_e2e.lab_queue, "read_receipt",
                                                 side_effect=lambda _queue, job_id: receipt_rows[job_id]))
                stack.enter_context(patch.object(live_e2e.judge_coordinator, "run_judge_batch", judge_mock))
                with self.assertRaisesRegex(RuntimeError, "LIVE E2E stopped during") as error:
                    live_e2e.run_v2_live_e2e(
                        scenario_id, run_id=run_id, ledger=ledger, promptfoo_executable="/mock/promptfoo",
                        receipt_directory=receipts, private_directory=private, env=env,
                        expected_source_sha=context["source_sha"],
                        expected_lab_bundle_sha256=context["lab_bundle_sha256"],
                        expected_lab_approval_bundle_sha256=bundle,
                        expected_worker_install_bundle_sha256=live_e2e.worker_install_bundle_sha256(),
                    )

            phase_state = approval_phases.PhaseApprovalState(
                lab_queue.live_run_phase_state_path(queue, run_id))._read()
            self.assertEqual(phase_state["terminal_status"], "FAILED")
            self.assertEqual(phase_state["failure"]["stage"], fail_phase + "_PREFLIGHT",
                             repr(error.exception.__cause__))
            self.assertEqual(ledger.snapshot()["generator_invocations"]["attempted"],
                             0 if fail_phase == "PHASE_A" else 8)
            self.assertEqual(ledger.snapshot()["judge_invocations"]["attempted"], 0)
            self.assertEqual(generator_mock.call_count, 0 if fail_phase == "PHASE_A" else 1)
            judge_mock.assert_not_called()

    def test_live_source_sha_rejects_dirty_lab_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            lab = repo / "services/agata/conversation-lab"
            asset = lab / "evaluators/naturalness.py"
            asset.parent.mkdir(parents=True)
            asset.write_text("committed code\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "lab-test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Lab Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "services/agata/conversation-lab"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
            asset.write_text("modified executable code\n", encoding="utf-8")
            with patch.object(v2_provenance, "LAB", lab):
                with self.assertRaisesRegex(PermissionError, "working tree is not clean"):
                    v2_provenance.current_source_sha()

    def test_live_bundle_hash_covers_naturalness_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = Path(directory)
            for relative in v2_provenance.LAB_RUN_ASSETS:
                path = lab / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")
            naturalness = lab / "evaluators/naturalness.py"
            with patch.object(v2_provenance, "LAB", lab):
                before = v2_provenance.lab_run_bundle_sha256()
                naturalness.write_text("changed executable\n", encoding="utf-8")
                after = v2_provenance.lab_run_bundle_sha256()
            self.assertNotEqual(before, after)

    def test_live_context_rejects_policy_bytes_that_disagree_with_worker_commitment(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            lab = repo / "services/agata/conversation-lab"
            worker = repo / "services/agata/worker"
            configs = lab / "configs"
            configs.mkdir(parents=True)
            worker.mkdir(parents=True)
            policy = configs / "naturalness-policy-v1.md"
            policy.write_text("## Candidate-visible policy\nactual treatment\n\n## Evidence map\nnotes\n", encoding="utf-8")
            (configs / "owner-preference-rubric-v1.json").write_text("{}", encoding="utf-8")
            manifest_path = configs / "splits-v2.json"
            manifest_path.write_text("{}", encoding="utf-8")
            worker_manifest_path = worker / "lab_v2_dev_manifest.json"
            worker_manifest_path.write_text(json.dumps({"naturalness_policy_sha256": "0" * 64}), encoding="utf-8")
            with patch.object(v2_provenance, "LAB", lab), \
                    patch.object(v2_provenance, "MANIFEST", manifest_path), \
                    patch.object(v2_provenance, "current_source_sha", return_value="a" * 40), \
                    patch.object(v2_provenance, "lab_run_bundle_sha256", return_value="b" * 64), \
                    patch.object(v2_provenance, "load_split", return_value=({"dataset_id": "lab-v2"}, {"dev": {}})):
                with self.assertRaisesRegex(PermissionError, "Naturalness Policy differs from Worker commitment"):
                    v2_provenance.live_run_context("dev")
                expected = hashlib.sha256(b"actual treatment").hexdigest()
                worker_manifest_path.write_text(json.dumps({"naturalness_policy_sha256": expected}), encoding="utf-8")
                context = v2_provenance.live_run_context("dev")
                self.assertEqual(context["naturalness_policy_sha256"], expected)

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
        import approval_phases
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            phase_dir = root / "state"; phase_dir.mkdir(mode=0o700)
            state = PhaseApprovalState(lab_queue.live_run_phase_state_path(root, "run-contract"))
            with patch.object(approval_phases.lab_queue, "_fsync_directory",
                              wraps=approval_phases.lab_queue._fsync_directory) as fsync_directory:
                state.begin_generator(run_id="run-contract", scenario_id=self.scenario["scenario_id"],
                                      worker_bundle_sha256=lab_queue.worker_bundle_sha256(),
                                      approval_id="approval-generator", max_calls=8)
            fsync_directory.assert_called_once_with(phase_dir)
            scoped_job = {"experiment_id": "agata-v2-naturalness-dev", "job_type": "v2-dev-generator",
                          "run_id": "run-contract", "scenario_id": self.scenario["scenario_id"]}
            approval = {"approval_id": "approval-generator", "worker_bundle_sha256": lab_queue.worker_bundle_sha256(),
                        "phase": "GENERATOR", "run_id": "run-contract", "max_calls": 8,
                        "experiment_ids": ["agata-v2-naturalness-dev"],
                        "configuration_ids": ["baseline", "naturalness-v1"],
                        "scenario_ids": [self.scenario["scenario_id"]]}
            with self.assertRaisesRegex(lab_queue.LabQueueError, "combined_authorization_required"):
                lab_queue.validate_approval_for_job(scoped_job, approval, root=root)
            with self.assertRaisesRegex(lab_queue.LabQueueError, "combined_authorization_required"):
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

    def test_phase_preflight_failure_is_durably_terminal_without_reopening_approval(self):
        from approval_phases import PhaseApprovalState
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            phase_dir = root / "state"; phase_dir.mkdir(mode=0o700)
            for stage in ("PHASE_A_PREFLIGHT", "PHASE_B_PREFLIGHT"):
                with self.subTest(stage=stage):
                    state = PhaseApprovalState(lab_queue.live_run_phase_state_path(root, "run-contract" + stage))
                    state.begin_generator(run_id="run-contract" + stage,
                                          scenario_id=self.scenario["scenario_id"],
                                          worker_bundle_sha256=lab_queue.worker_bundle_sha256(),
                                          approval_id="approval-generator", max_calls=8,
                                          authorization_sha256="a" * 64)
                    state.fail_run(stage=stage, reason_code="LIVE_E2E_FAILED")
                    self.assertEqual(state._read()["terminal_status"], "FAILED")
                    with self.assertRaises(PermissionError):
                        state.begin_generator(run_id="run-contract" + stage,
                                              scenario_id=self.scenario["scenario_id"],
                                              worker_bundle_sha256=lab_queue.worker_bundle_sha256(),
                                              approval_id="approval-generator", max_calls=8,
                                              authorization_sha256="a" * 64)

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

    def test_one_combined_live_authorization_covers_sequential_phases(self):
        from approval_phases import PhaseApprovalState
        authorization = {
            "schema_version": 1, "authorization_type": "LIVE_RUN_AUTHORIZATION",
            "approval_id": "live-run-one", "run_id": "run-contract",
            "source_sha": "a" * 40,
            "worker_bundle_sha256": lab_queue.worker_bundle_sha256(),
            "lab_bundle_sha256": "b" * 64,
            "naturalness_policy_sha256": "c" * 64,
            "rubric_sha256": "d" * 64,
            "dataset_id": "agata-conversation-lab-v2", "dataset_split": "DEV",
            "dataset_manifest_sha256": "e" * 64,
            "experiment_ids": ["agata-v2-naturalness-dev", lab_queue.JUDGE_EXPERIMENT_ID],
            "configuration_ids": ["baseline", "naturalness-v1", "judge-1", "judge-2", "judge-3"],
            "scenario_ids": [self.scenario["scenario_id"]],
            "model": "gpt-6-luna", "reasoning_effort": "medium",
            "max_generator_calls": 8, "max_judge_calls": 3,
            "retries": 0, "fallbacks": 0, "concurrency": 1,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            path = root / "authorization.json"
            path.write_text(json.dumps(authorization), encoding="utf-8"); path.chmod(0o600)
            loaded = lab_queue.load_owner_approval(
                path, expected_bundle_sha256=lab_queue.worker_bundle_sha256())
            self.assertEqual(loaded, authorization)
            phase_dir = root / "state"; phase_dir.mkdir(mode=0o700)
            state = PhaseApprovalState(lab_queue.live_run_phase_state_path(root, "run-contract"))
            auth_digest = hashlib.sha256(json.dumps(
                authorization, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            state.begin_generator(run_id="run-contract", scenario_id=self.scenario["scenario_id"],
                                  worker_bundle_sha256=loaded["worker_bundle_sha256"],
                                  approval_id=loaded["approval_id"], max_calls=8,
                                  authorization_sha256=auth_digest)
            # Ignore the pre-fix global phase file. It may contain a terminal
            # receipt from an older run and must neither block nor authorize this run.
            legacy_state = phase_dir / "v2-approval-phase.json"
            legacy_state.write_text(json.dumps({"terminal_status": "FAILED", "run_id": "old-run"}))
            legacy_state.chmod(0o600)
            state.assert_generator_active(run_id="run-contract", scenario_id=self.scenario["scenario_id"],
                                          worker_bundle_sha256=loaded["worker_bundle_sha256"],
                                          approval_id=loaded["approval_id"], authorization_sha256=auth_digest)
            generator_job = {
                "experiment_id": "agata-v2-naturalness-dev", "job_type": "v2-dev-generator",
                "run_id": "run-contract", "scenario_id": self.scenario["scenario_id"],
                "configuration_id": "baseline",
            }
            lab_queue.validate_approval_for_job(generator_job, loaded, root=root)
            with self.assertRaisesRegex(lab_queue.LabQueueError, "phase_state_mismatch"):
                lab_queue.validate_approval_for_job(
                    generator_job, {**loaded, "approval_id": "altered-authorization"}, root=root)
            with self.assertRaises(PermissionError):
                state.assert_generator_active(run_id="run-contract", scenario_id=self.scenario["scenario_id"],
                                              worker_bundle_sha256=loaded["worker_bundle_sha256"],
                                              approval_id=loaded["approval_id"], authorization_sha256="f" * 64)
            with self.assertRaises(lab_queue.LabQueueError):
                lab_queue.live_run_phase_state_path(root, "../other-run")

    def test_combined_live_authorization_rejects_phase_specific_or_expanded_scope(self):
        base = {
            "schema_version": 1, "authorization_type": "LIVE_RUN_AUTHORIZATION",
            "approval_id": "live-run-one", "run_id": "run-contract", "source_sha": "a" * 40,
            "worker_bundle_sha256": lab_queue.worker_bundle_sha256(), "lab_bundle_sha256": "b" * 64,
            "naturalness_policy_sha256": "c" * 64, "rubric_sha256": "d" * 64,
            "dataset_id": "agata-conversation-lab-v2", "dataset_split": "DEV",
            "dataset_manifest_sha256": "e" * 64,
            "experiment_ids": ["agata-v2-naturalness-dev", lab_queue.JUDGE_EXPERIMENT_ID],
            "configuration_ids": ["baseline", "naturalness-v1", "judge-1", "judge-2", "judge-3"],
            "scenario_ids": [self.scenario["scenario_id"]], "model": "gpt-6-luna",
            "reasoning_effort": "medium", "max_generator_calls": 8, "max_judge_calls": 3,
            "retries": 0, "fallbacks": 0, "concurrency": 1,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "authorization.json"
            path.write_text(json.dumps(base), encoding="utf-8"); path.chmod(0o600)
            path.parent.chmod(0o700)
            self.assertEqual(lab_queue.load_owner_approval(
                path, expected_bundle_sha256=lab_queue.worker_bundle_sha256())["authorization_type"],
                "LIVE_RUN_AUTHORIZATION")
            for changes in ({"phase": "GENERATOR"}, {"max_judge_calls": 4},
                            {"retries": 1}, {"fallbacks": 1}, {"model": "gpt-6.1-sol"},
                            {"dataset_split": "SEALED_HOLDOUT"}, {"unexpected": True}):
                path.write_text(json.dumps({**base, **changes}), encoding="utf-8")
                with self.subTest(changes=changes), self.assertRaises(lab_queue.LabQueueError):
                    lab_queue.load_owner_approval(
                        path, expected_bundle_sha256=lab_queue.worker_bundle_sha256())

    def test_failed_combined_run_is_terminal_and_cannot_reopen_phase_a(self):
        from approval_phases import PhaseApprovalState
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            phase_dir = root / "state"; phase_dir.mkdir(mode=0o700)
            state = PhaseApprovalState(phase_dir / "phase.json")
            state.begin_generator(run_id="run-failed", scenario_id=self.scenario["scenario_id"],
                                  worker_bundle_sha256=lab_queue.worker_bundle_sha256(),
                                  approval_id="approval-failed", max_calls=8,
                                  authorization_sha256="a" * 64)
            state.fail_run(stage="GENERATOR", reason_code="GENERATOR_FAILED")
            self.assertEqual(state._read()["terminal_status"], "FAILED")
            with self.assertRaisesRegex(PermissionError, "fresh, exact eight-call"):
                state.begin_generator(run_id="run-failed", scenario_id=self.scenario["scenario_id"],
                                      worker_bundle_sha256=lab_queue.worker_bundle_sha256(),
                                      approval_id="approval-failed", max_calls=8,
                                      authorization_sha256="a" * 64)
            # A terminal run remains non-reusable, while a new immutable run ID
            # receives a distinct phase receipt and can start independently.
            next_state = PhaseApprovalState(lab_queue.live_run_phase_state_path(root, "run-next"))
            next_state.begin_generator(run_id="run-next", scenario_id=self.scenario["scenario_id"],
                                       worker_bundle_sha256=lab_queue.worker_bundle_sha256(),
                                       approval_id="approval-next", max_calls=8,
                                       authorization_sha256="b" * 64)
            self.assertEqual(next_state._read()["run_id"], "run-next")
            self.assertEqual(state._read()["terminal_status"], "FAILED")

    def test_concurrent_same_run_start_has_one_durable_owner(self):
        from approval_phases import PhaseApprovalState
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            state_dir = root / "state"; state_dir.mkdir(mode=0o700)
            path = lab_queue.live_run_phase_state_path(root, "run-race")
            barrier = threading.Barrier(2)

            def start():
                state = PhaseApprovalState(path)
                barrier.wait()
                try:
                    state.begin_generator(run_id="run-race", scenario_id=self.scenario["scenario_id"],
                                          worker_bundle_sha256=lab_queue.worker_bundle_sha256(),
                                          approval_id="approval-race", max_calls=8,
                                          authorization_sha256="c" * 64)
                    return "OWNER"
                except (PermissionError, OSError, json.JSONDecodeError):
                    return "REJECTED"

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(start) for _ in range(2)]
                results = [future.result() for future in futures]
            self.assertCountEqual(results, ["OWNER", "REJECTED"])
            self.assertEqual(PhaseApprovalState(path)._read()["active_phase"], "GENERATOR")

    def test_live_e2e_passes_only_actual_persona_findings_to_aggregator(self):
        import live_e2e
        self.assertEqual(live_e2e._private_sanity_findings({
            "sanity_findings": [{"scenario_id": "dev", "private_quality_findings": []}],
        }, "dev"), [])
        finding = {"code": "AGATA_SELF_MASCULINE_GRAMMAR", "scenario_id": "dev", "turn": 1}
        self.assertEqual(live_e2e._private_sanity_findings({
            "sanity_findings": [{"scenario_id": "dev", "private_quality_findings": [finding]}],
        }, "dev"), [finding])
        self.assertEqual(live_e2e._private_sanity_findings({}, "dev"), [{"code": "INVALID_SANITY_RECEIPT"}])


if __name__ == "__main__":
    unittest.main()
