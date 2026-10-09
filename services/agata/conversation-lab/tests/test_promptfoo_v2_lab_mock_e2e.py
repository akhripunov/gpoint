"""Real Promptfoo CLI → V2 Worker LAB generator and judge chain, all inference mocked."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

LAB = Path(__file__).resolve().parents[1]
WORKER_DIR = LAB.parents[0] / "worker"
for path in (LAB / "evaluators", WORKER_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import approval_phases
import blind
import judge_coordinator
import live_e2e
import lab_queue
import promptfoo_runner
import worker_client
import v2_provenance
from run_coordinator import CallLedger, run_v2_dev_scenario

MIGRATION_PATH = WORKER_DIR / "migrate_worker_runtime.py"
MIGRATION_SPEC = importlib.util.spec_from_file_location("lab_runtime_migration", MIGRATION_PATH)
migration = importlib.util.module_from_spec(MIGRATION_SPEC)
assert MIGRATION_SPEC and MIGRATION_SPEC.loader
MIGRATION_SPEC.loader.exec_module(migration)


class PromptfooV2MockE2ETests(unittest.TestCase):
    def test_real_cli_runs_eight_generators_then_three_judges_in_distinct_phases(self):
        tooling = LAB / "tooling/promptfoo"
        promptfoo = Path(os.environ.get("AGATA_PROMPTFOO_CLI", tooling / "node_modules/.bin/promptfoo"))
        self.assertTrue(promptfoo.is_file())
        self.assertEqual(promptfoo_runner.validate_promptfoo_cli(str(promptfoo)), "0.123.1")
        _manifest, scenarios = v2_provenance.load_split("DEV")
        scenario_id = "v2-dev-clean-20261006-everyday"
        scenario = scenarios[scenario_id]
        with tempfile.TemporaryDirectory(prefix="agata-v2-cli-e2e-") as temp_name:
            root = Path(temp_name); root.chmod(0o700)
            support = root / "support"; support.mkdir(mode=0o700)
            config_dir = support / "config"; config_dir.mkdir(mode=0o700)
            approval_path = config_dir / "lab-execution-approval.json"
            queue_root = worker_client.canonical_lab_queue({"GPOINT_AGATA_WORKER_SUPPORT": str(support)})
            queue_root.mkdir(parents=True, mode=0o700)
            phase_dir = queue_root / "state"; phase_dir.mkdir(mode=0o700)
            receipts = root / "receipts"; receipts.mkdir(mode=0o700)
            ledger = CallLedger(journal_path=root / "call-ledger.jsonl")
            run_id = "v2-run-e2e-001"
            # Model the canonical installed Slot 03 pointer. LAB approvals bind
            # to the selected lane runtime, which may differ from the supervisor.
            # Build it through the real Worker installer code; never repair fixture
            # permissions by hand, since that would hide installer regressions.
            runtime_root = support / "runtime"
            runtime_root.mkdir(mode=0o700)
            runtime_root.chmod(0o700)
            source_sha = "a" * 40
            worker_bundle_expected = lab_queue.worker_bundle_sha256(WORKER_DIR)
            manifest = {
                "source_sha": source_sha,
                "bundle_sha256": worker_bundle_expected,
                "assets": {name: migration.sha256(WORKER_DIR / name) for name in migration.ASSETS},
            }

            def fake_installer_subprocess_run(command, **_kwargs):
                if command[1:3] == ["-m", "venv"]:
                    venv_python = Path(command[3]) / "bin/python3.12"
                    venv_python.parent.mkdir(parents=True)
                    venv_python.write_text("staged Python 3.12")
                    venv_python.chmod(0o700)
                return __import__("subprocess").CompletedProcess(command, 0)

            with patch.object(migration, "ROOT", support), \
                    patch.object(migration, "subprocess", SimpleNamespace(run=fake_installer_subprocess_run)):
                staged_runtime = migration._stage_runtime(manifest)
                lab_runtime = migration._install_staged_runtime(staged_runtime, manifest)
            slot_dir = runtime_root / "slots" / "LAB"
            slot_dir.mkdir(parents=True, mode=0o700)
            (slot_dir / "current").symlink_to(lab_runtime)
            bundle = lab_queue.worker_bundle_sha256(lab_runtime)
            self.assertEqual(bundle, worker_bundle_expected)
            env = {
                "PATH": os.environ.get("PATH", ""), "HOME": str(root), "TMPDIR": str(root),
                "LANG": "en_US.UTF-8", "PROMPTFOO_DISABLE_TELEMETRY": "1",
                "GPOINT_AGATA_WORKER_SUPPORT": str(support),
                "AGATA_LAB_APPROVAL_FILE": str(approval_path), "AGATA_WORKER_SLOT_ID": "slot-03",
            }

            approval = {
                    "schema_version": 1, "authorization_type": "LIVE_RUN_AUTHORIZATION",
                    "approval_id": "mock-combined-live-run", "run_id": run_id, "source_sha": source_sha,
                    "worker_bundle_sha256": bundle, "lab_bundle_sha256": "b" * 64,
                    "naturalness_policy_sha256": "c" * 64, "rubric_sha256": "d" * 64,
                    "dataset_id": "agata-conversation-lab-v2", "dataset_split": "DEV",
                    "dataset_manifest_sha256": "e" * 64,
                    "experiment_ids": ["agata-v2-naturalness-dev", lab_queue.JUDGE_EXPERIMENT_ID],
                    "configuration_ids": ["baseline", "naturalness-v1", "judge-1", "judge-2", "judge-3"],
                    "scenario_ids": [scenario_id], "model": "gpt-6-luna", "reasoning_effort": "medium",
                    "max_generator_calls": 8, "max_judge_calls": 3, "retries": 0, "fallbacks": 0,
                    "concurrency": 1,
                    "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                }
            # The test simulates an immutable committed candidate while this
            # source tree may be under development; live paths never patch this.
            with patch.object(v2_provenance, "current_source_sha", return_value=source_sha):
                approval.update(v2_provenance.live_run_context(scenario_id))
            def write_approval():
                approval_path.write_text(json.dumps(approval), encoding="utf-8")
                approval_path.chmod(0o600)
                return approval

            generator_approval = write_approval()
            old_env = dict(os.environ)
            os.environ.update(env)
            stopped = threading.Event()
            worker_errors = []
            cli_runs = []
            try:
                spec = importlib.util.spec_from_file_location("agata_v2_e2e_worker", WORKER_DIR / "gpoint_mac_worker.py")
                worker = importlib.util.module_from_spec(spec)
                assert spec and spec.loader
                spec.loader.exec_module(worker)
                worker.SUPPORT = support
                worker.STATUS = support / "worker-status.json"
                self.assertEqual(worker._lab_slot_bundle_sha256(support), worker_bundle_expected)

                def mock_inference(job, *, metadata_out=None, **_kwargs):
                    if metadata_out is not None:
                        metadata_out.update({"actual_model": None, "inference_started": True})
                    if job.get("job_type") == "judge":
                        prompt = job["request"]["user_message"]
                        payload = json.loads(prompt[prompt.index("\n") + 1:])
                        candidate_marker = "новая дорога приятно проветривает мысли"
                        preferred_position = next(
                            position for position in ("first", "second")
                            if any(turn["role"] == "assistant" and candidate_marker in turn["text"]
                                   for candidate in payload["pair"]
                                   if candidate["display_position"] == position
                                   for turn in candidate["turns"])
                        )
                        return json.dumps({
                            "overall": preferred_position,
                            "core_dimensions": {key: preferred_position for key in lab_queue.JUDGE_CORE_DIMENSIONS},
                            "conditional_evidence": {}, "reasons": ["Both transcripts are evaluated consistently."],
                            "defects": [], "uncertainty": False, "position_sensitivity": False,
                        })
                    if "Naturalness V1 policy:" in job["request"]["instructions"]:
                        return "Я сама люблю такие прогулки: иногда новая дорога приятно проветривает мысли."
                    return "Понятно. Что ещё ты обычно делаешь?"

                worker.run_inference = mock_inference

                def worker_loop():
                    deadline = time.monotonic() + 240
                    try:
                        while not stopped.is_set() and time.monotonic() < deadline:
                            worker.process_one("LAB")
                            time.sleep(0.01)
                    except BaseException as exc:
                        worker_errors.append(exc)

                thread = threading.Thread(target=worker_loop, daemon=True)
                thread.start()
                with patch.object(v2_provenance, "current_source_sha", return_value=source_sha):
                    outcome = live_e2e.run_v2_live_e2e(
                        scenario_id, run_id=run_id, ledger=ledger,
                        promptfoo_executable=str(promptfoo), receipt_directory=receipts,
                        private_directory=root, env=env,
                    )
                self.assertEqual(outcome["status"], "COMPLETED")
                self.assertEqual(outcome["decision_type"], "AUTO_DECISION", outcome["scenarios"])
                phase_state_after = approval_phases.PhaseApprovalState(
                    queue_root / "state" / "v2-approval-phase.json")._read()
                self.assertEqual(phase_state_after["terminal_status"], "COMPLETED")
                self.assertEqual(phase_state_after["generator_success_count"], 8)
                self.assertEqual(phase_state_after["judge_invocations"], 3)
                with self.assertRaises(PermissionError):
                    approval_phases.PhaseApprovalState(
                        queue_root / "state" / "v2-approval-phase.json").begin_generator(
                            run_id=run_id, scenario_id=scenario_id, worker_bundle_sha256=bundle,
                            approval_id=generator_approval["approval_id"], max_calls=8,
                            authorization_sha256=lab_queue.live_run_authorization_sha256(generator_approval),
                        )
                self.assertEqual(ledger.snapshot()["generator_invocations"]["attempted"], 8)
                self.assertEqual(ledger.snapshot()["generator_invocations"]["outcomes"], {"success": 8})
                self.assertEqual(lab_queue.owner_approval_usage_calls(
                    support, generator_approval, phase="GENERATOR"), 8)
                worker_receipts = [row for path in (queue_root / "receipts").glob("*.json")
                                   if (row := json.loads(path.read_text())).get("identity", {}).get("job_type")
                                   == "v2-dev-generator"]
                self.assertTrue(all(row and row["status"] == "completed" for row in worker_receipts))
                promptfoo_receipts = [json.loads(path.read_text()) for path in receipts.glob("*.promptfoo-receipt.json")
                                      if json.loads(path.read_text()).get("promptfoo_success")]
                adapter_receipts = [json.loads((receipts / (invocation_id + ".worker-receipt.json")).read_text())
                                    for invocation_id in [row["promptfoo_invocation_id"] for row in promptfoo_receipts
                                                          if row["promptfoo_invocation_id"].startswith("pfv2-")]]
                self.assertTrue(all(row["model_identity_status"] == "MODEL_IDENTITY_UNVERIFIED"
                                    and row["actual_model"] is None for row in adapter_receipts))
                judge_approval = generator_approval
                self.assertEqual(outcome["judge_invocations"], 3)
                self.assertEqual(ledger.snapshot()["judge_invocations"]["outcomes"], {"success": 3})
                self.assertEqual(lab_queue.owner_approval_usage_calls(
                    support, judge_approval, phase="JUDGES"), 3)
                self.assertEqual(lab_queue.owner_approval_usage_calls(support, generator_approval), 11)
                judge_adapter_receipts = [json.loads(path.read_text()) for path in receipts.glob("*.judge-receipt.json")]
                self.assertEqual(len(judge_adapter_receipts), 3)
                self.assertTrue(all(row["model_identity_status"] == "MODEL_IDENTITY_UNVERIFIED"
                                    and row["actual_model"] is None for row in judge_adapter_receipts))
                self.assertEqual(len(list(receipts.glob("*.promptfoo-receipt.json"))), 11)
                self.assertEqual(len(list(queue_root.glob("receipts/*.json"))), 11)
                self.assertEqual(outcome["retries"], 0)
                self.assertIn(outcome["decision_type"], {"AUTO_DECISION", "HUMAN_ESCALATION"})
                self.assertFalse(worker_errors, worker_errors)
            finally:
                stopped.set()
                if "thread" in locals():
                    thread.join(timeout=5)
                os.environ.clear(); os.environ.update(old_env)


if __name__ == "__main__":
    unittest.main()
