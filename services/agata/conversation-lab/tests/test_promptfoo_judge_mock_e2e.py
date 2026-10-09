"""Real pinned Promptfoo CLI → LAB adapter → mocked slot-03 Worker E2E; no model calls."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

LAB = Path(__file__).resolve().parents[1]
WORKER_DIR = LAB.parents[0] / "worker"
for path in (LAB / "evaluators", WORKER_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import lab_queue
import approval_phases
import promptfoo_runner
import worker_client
from run_coordinator import CallLedger


class PromptfooJudgeMockE2ETests(unittest.TestCase):
    def test_real_promptfoo_cli_completes_three_receipt_judge_chain_without_model_call(self):
        promptfoo = LAB / "tooling/promptfoo/node_modules/.bin/promptfoo"
        self.assertTrue(promptfoo.is_file())
        version = shutil.which("node")
        self.assertIsNotNone(version)
        with tempfile.TemporaryDirectory(prefix="agata-judge-e2e-") as temp_name:
            root = Path(temp_name); root.chmod(0o700)
            support = root / "support"; support.mkdir(mode=0o700)
            config_dir = support / "config"; config_dir.mkdir(mode=0o700)
            approval_path = config_dir / "lab-approval.json"
            scenario = json.loads((LAB / "data/golden/synthetic/v2-dev-scenarios.jsonl").read_text().splitlines()[0])
            scenario_id = scenario["scenario_id"]
            run_id = "mock-e2e-batch"
            queue_root = worker_client.canonical_lab_queue({"GPOINT_AGATA_WORKER_SUPPORT": str(support)})
            lab_queue._layout(queue_root)
            phase_dir = queue_root / "state"; phase_dir.mkdir(mode=0o700)
            phase = approval_phases.PhaseApprovalState(phase_dir / "v2-approval-phase.json")
            # The LAB approval is bound to the immutable Slot 03 runtime, not
            # the supervisor source loaded by this test process.
            runtime_root = support / "runtime"
            runtime_root.mkdir(mode=0o700)
            lab_runtime = runtime_root / ("a" * 40)
            lab_runtime.mkdir(mode=0o700)
            for asset_name in lab_queue.WORKER_BUNDLE_ASSET_NAMES:
                shutil.copyfile(WORKER_DIR / asset_name, lab_runtime / asset_name)
                (lab_runtime / asset_name).chmod(0o600)
            slot_dir = runtime_root / "slots" / "LAB"
            slot_dir.mkdir(parents=True, mode=0o700)
            (slot_dir / "current").symlink_to(lab_runtime)
            bundle = lab_queue.worker_bundle_sha256(lab_runtime)
            phase.begin_generator(run_id=run_id, scenario_id=scenario_id, worker_bundle_sha256=bundle,
                                  approval_id="mock-phase-a", max_calls=8)
            generator_receipts = []
            generator_ledger_rows = []
            generator_promptfoo_receipts = []
            for arm in ("baseline", "naturalness-v1"):
                for turn in range(4):
                    job_id = f"seed-{arm}-{turn}"
                    invocation_id = "pfv2-" + __import__("hashlib").sha256(
                        "\0".join((run_id, arm, str(turn))).encode()).hexdigest()[:59]
                    lab_queue.finish(queue_root, job_id, status="completed", response="mock generator response",
                        execution={"requested_model": "gpt-6-luna", "requested_reasoning_effort": "medium",
                                   "actual_model": "gpt-6-luna", "inference_started": True},
                        identity={"experiment_id": "agata-v2-naturalness-dev", "configuration_id": arm,
                                  "scenario_id": scenario_id, "turn_index": turn,
                                  "job_type": "v2-dev-generator", "dataset_split": "DEV",
                                  "run_id": run_id, "arm_id": f"{run_id}-{arm}",
                                  "promptfoo_invocation_id": invocation_id}, slot_id="slot-03")
                    generator_receipts.append(lab_queue.read_receipt(queue_root, job_id))
                    generator_ledger_rows.append({"invocation_id": invocation_id, "outcome": "success",
                        "metadata": {"run_id": run_id, "scenario_id": scenario_id, "configuration": arm,
                                     "turn_index": turn, "worker_job_id": job_id}})
                    generator_promptfoo_receipts.append({"promptfoo_invocation_id": invocation_id,
                        "promptfoo_success": True, "result_count": 1, "run_id": run_id,
                        "worker_job_id": job_id})
            phase.close_generator(generator_receipts, call_ledger_snapshot={
                "generator_invocations": {"attempted": 8, "outcomes": {"success": 8},
                                          "invocations": generator_ledger_rows}
            }, promptfoo_receipts=generator_promptfoo_receipts)
            turns = [{"role": turn["role"], "text": turn["text"]} if turn["role"] == "user"
                     else {"role": "assistant", "text": "Хорошо, спасибо, что спросил."}
                     for turn in scenario["dialogue_turns"]]
            results = [{"scenario_id": scenario_id, "model_id": model, "status": "success", "turns": turns}
                       for model in ("baseline", "naturalness-v1")]
            blind_spec = importlib.util.spec_from_file_location("judge_e2e_blind", LAB / "evaluators" / "blind.py")
            blind = importlib.util.module_from_spec(blind_spec); blind_spec.loader.exec_module(blind)
            packet, private_mapping = blind.make_v2_blind_packets(
                [scenario], results, batch_id="judge-e2e-generation")
            approval = {
                "approval_id": "offline-mock-phase-b-approval", "phase": "JUDGES", "run_id": run_id,
                "worker_bundle_sha256": bundle,
                "experiment_ids": [lab_queue.JUDGE_EXPERIMENT_ID],
                "configuration_ids": ["judge-1", "judge-2", "judge-3"],
                "scenario_ids": [scenario_id], "max_calls": 3,
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            }
            approval_path.write_text(json.dumps(approval), encoding="utf-8")
            approval_path.chmod(0o600)
            phase.begin_judges(run_id=run_id, scenario_id=scenario_id, worker_bundle_sha256=bundle,
                               approval_id=approval["approval_id"], max_calls=3,
                               generator_receipts=generator_receipts)
            receipts = root / "receipts"; receipts.mkdir(mode=0o700)
            ledger = CallLedger(journal_path=root / "call-ledger.jsonl")
            python_paths = [Path(sys.executable).parent]
            env = {
                "PATH": os.environ.get("PATH", ""), "HOME": str(root),
                "TMPDIR": str(root), "PROMPTFOO_DISABLE_TELEMETRY": "1",
                "GPOINT_AGATA_WORKER_SUPPORT": str(support),
                "AGATA_LAB_APPROVAL_FILE": str(approval_path),
                "AGATA_WORKER_SLOT_ID": "slot-03",
            }
            old_env = dict(os.environ)
            completed = threading.Event()
            os.environ.update(env)
            try:
                spec = importlib.util.spec_from_file_location("agata_mock_e2e_worker", WORKER_DIR / "gpoint_mac_worker.py")
                worker = importlib.util.module_from_spec(spec)
                assert spec and spec.loader
                spec.loader.exec_module(worker)
                worker.SUPPORT = support
                worker.STATUS = support / "worker-status.json"

                def fake_inference(job, *, metadata_out=None, **_kwargs):
                    if metadata_out is not None:
                        metadata_out.update({"actual_model": "gpt-6-luna", "inference_started": True})
                    return json.dumps({
                        "overall": "first",
                        "core_dimensions": {key: "first" for key in lab_queue.JUDGE_CORE_DIMENSIONS},
                        "conditional_evidence": {}, "reasons": ["The first conversation responds directly."],
                        "defects": [], "uncertainty": False, "position_sensitivity": False,
                    }, ensure_ascii=False)

                worker.run_inference = fake_inference
                worker_errors = []

                def worker_loop():
                    try:
                        deadline = time.monotonic() + 40
                        while not completed.is_set() and time.monotonic() < deadline:
                            if worker.process_one("LAB"):
                                # The coordinator submits the next slot after
                                # each Promptfoo process; keep polling until all
                                # three terminal receipts exist.
                                pass
                            time.sleep(0.01)
                    except BaseException as exc:
                        worker_errors.append(exc)

                thread = threading.Thread(target=worker_loop, daemon=True)
                thread.start()
                rubric = json.loads((LAB / "configs/owner-preference-rubric-v1.json").read_text())
                import judge_coordinator
                invocation_errors = []
                cli_runs = []
                promptfoo_runner.EVAL_TIMEOUT_SECONDS = 18

                original_run = promptfoo_runner.run_promptfoo_judge_eval

                def run_real_cli(*args, **kwargs):
                    try:
                        result = original_run(*args, **kwargs)
                        cli_runs.append({"returncode": result.returncode, "stdout": result.stdout,
                                         "stderr": result.stderr})
                        if result.returncode != 0:
                            invocation_errors.append({"returncode": result.returncode,
                                                      "stdout": result.stdout, "stderr": result.stderr})
                        return result
                    except subprocess.TimeoutExpired as exc:
                        invocation_errors.append({"stdout": exc.stdout, "stderr": exc.stderr})
                        raise

                with patch.object(promptfoo_runner, "run_promptfoo_judge_eval", side_effect=run_real_cli):
                    result = judge_coordinator.run_judge_batch(
                        packet, rubric, batch_id="mock-e2e-batch", dataset_split="DEV",
                        ledger=ledger, private_mapping=private_mapping, sanity_findings=[],
                        receipt_directory=receipts, private_directory=root, env=env,
                        promptfoo_executable=str(promptfoo),
                    )
                completed.set(); thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertFalse(worker_errors, worker_errors)
                self.assertFalse(invocation_errors, {"cli": invocation_errors,
                    "files": {item.name: item.read_text(errors="replace")[:800]
                              for item in receipts.glob("*.json")}})
                self.assertEqual(result["judge_invocations"], 3)
                self.assertEqual(ledger.snapshot()["judge_invocations"]["attempted"], 3)
                self.assertEqual(ledger.snapshot()["judge_invocations"]["outcomes"], {"success": 3},
                                 {"result": result, "cli_runs": cli_runs, "invocations": ledger.snapshot()["judge_invocations"]["invocations"],
                                  "cli_errors": invocation_errors,
                                  "files": {item.name: item.read_text(errors="replace")[:500]
                                            for item in receipts.glob("*.json")}})
                self.assertEqual(len(cli_runs), 3)
                self.assertEqual(result["decision_type"], "HUMAN_ESCALATION")
                self.assertIn("ORDER_SENSITIVE", result["scenarios"][0]["reason_codes"])
                for row in ledger.snapshot()["judge_invocations"]["invocations"]:
                    job_id = row["metadata"]["worker_job_id"]
                    terminal = lab_queue.read_receipt(support / "queues/lab", job_id)
                    self.assertEqual(terminal["status"], "completed")
                    self.assertEqual(terminal["slot_id"], "slot-03")
                self.assertEqual(lab_queue.owner_approval_usage_calls(support, approval), 3)
            finally:
                completed.set()
                if "thread" in locals():
                    thread.join(timeout=5)
                os.environ.clear(); os.environ.update(old_env)


if __name__ == "__main__":
    unittest.main()
