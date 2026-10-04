"""Offline shared-worker LAB lane checks; every executor invocation is mocked."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import gpoint_mac_worker as worker  # noqa: E402
import lab_queue  # noqa: E402


def lab_job(job_id="lab-job-1", *, scenario="ru_naturalness-02", config="baseline"):
    return {
        "job_id": job_id,
        "experiment_id": "agata-naturalness-v1",
        "configuration_id": config,
        "origin": "conversation-lab",
        "scenario_id": scenario,
        "turn_index": 0,
        "data_classification": "synthetic",
        "timeout_seconds": 30,
        "request": {
            "instructions": "synthetic instruction",
            "persona": {"name": "synthetic persona"},
            "communication_settings": {"tone": "warm"},
            "memories": [], "dialogue_summaries": [], "history": [],
            "user_message": "synthetic user turn",
        },
    }


class WorkerLabIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous = worker.SUPPORT, worker.STATUS
        worker.SUPPORT = self.root
        worker.STATUS = self.root / "worker-status.json"
        self.approval_path = self.root / "approval.json"

    def tearDown(self):
        worker.SUPPORT, worker.STATUS = self.previous
        self.temp.cleanup()

    def approval(self, max_calls=8, scenarios=None):
        value = {
            "approval_id": "owner-approval-001",
            "worker_bundle_sha256": worker._worker_bundle_sha256(),
            "experiment_ids": ["agata-naturalness-v1"],
            "configuration_ids": ["baseline", "naturalness-v1"],
            "scenario_ids": scenarios or ["ru_naturalness-02"],
            "max_calls": max_calls,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        self.approval_path.write_text(json.dumps(value), encoding="utf-8")
        self.approval_path.chmod(0o600)

    def submit(self, job=None):
        lab_queue.submit(job or lab_job(), root=self.root / "lab-queue")

    def test_lab_uses_shared_executor_without_gateway_and_keeps_response_local(self):
        self.approval()
        self.submit()
        with patch.dict(os.environ, {
            "AGATA_LAB_WORKER_ENABLED": "true",
            "AGATA_LAB_APPROVAL_FILE": str(self.approval_path),
        }), patch.object(worker, "run_inference", return_value="Синтетический ответ") as infer, patch.object(
            worker, "api_request", side_effect=AssertionError("LAB must not use a production gateway")
        ):
            self.assertTrue(worker.process_one("LAB"))
        infer.assert_called_once()
        args, kwargs = infer.call_args
        self.assertEqual(args[0]["target"], "LAB")
        self.assertEqual(args[0]["request"]["user_message"], "synthetic user turn")
        self.assertTrue(callable(kwargs["cancel_check"]))
        receipt = lab_queue.read_receipt(self.root / "lab-queue", "lab-job-1")
        self.assertEqual(receipt["response"], "Синтетический ответ")
        self.assertEqual(receipt["identity"]["configuration_id"], "baseline")
        self.assertGreaterEqual(receipt["execution"]["duration_ms"], 0)
        self.assertIsNone(receipt["execution"]["actual_model"])
        self.assertNotIn("cli_reported_usage", receipt["execution"])
        self.assertFalse((self.root / "worker-status.json").read_text().find("synthetic user turn") >= 0)

    def test_approval_missing_leaves_job_queued_and_never_calls_executor(self):
        self.submit()
        with patch.dict(os.environ, {"AGATA_LAB_WORKER_ENABLED": "true", "AGATA_LAB_APPROVAL_FILE": str(self.approval_path)}), patch.object(
            worker, "run_inference"
        ) as infer:
            with self.assertRaisesRegex(worker.FatalConfigurationError, "lab_owner_approval"):
                worker.process_one("LAB")
        infer.assert_not_called()
        self.assertEqual(lab_queue.pending_count(self.root / "lab-queue"), 1)

    def test_approval_is_pinned_to_both_worker_and_queue_helper_sources(self):
        self.approval()
        self.submit()
        with patch.dict(os.environ, {"AGATA_LAB_WORKER_ENABLED": "true", "AGATA_LAB_APPROVAL_FILE": str(self.approval_path)}), patch.object(
            worker, "_worker_bundle_sha256", return_value="0" * 64
        ), patch.object(worker, "run_inference") as infer:
            with self.assertRaisesRegex(worker.FatalConfigurationError, "lab_owner_approval_scope_invalid"):
                worker.process_one("LAB")
        infer.assert_not_called()
        self.assertEqual(lab_queue.pending_count(self.root / "lab-queue"), 1)

    def test_source_hash_and_scenario_scope_gate_fail_before_generation(self):
        self.approval(scenarios=["daily-02"])
        self.submit()
        with patch.dict(os.environ, {"AGATA_LAB_WORKER_ENABLED": "true", "AGATA_LAB_APPROVAL_FILE": str(self.approval_path)}), patch.object(
            worker, "run_inference"
        ) as infer:
            worker.process_one("LAB")
        infer.assert_not_called()
        self.assertEqual(lab_queue.read_receipt(self.root / "lab-queue", "lab-job-1")["status"], "failed")

    def test_approval_call_limit_is_consumed_before_inference(self):
        self.approval(max_calls=1)
        with patch.dict(os.environ, {"AGATA_LAB_WORKER_ENABLED": "true", "AGATA_LAB_APPROVAL_FILE": str(self.approval_path)}), patch.object(
            worker, "run_inference", return_value="ok"
        ) as infer:
            self.submit(lab_job("job-one"))
            worker.process_one("LAB")
            self.submit(lab_job("job-two"))
            worker.process_one("LAB")
        self.assertEqual(infer.call_count, 1)
        self.assertEqual(lab_queue.read_receipt(self.root / "lab-queue", "job-two")["status"], "failed")

    def test_scheduler_weights_user_lanes_and_reserves_a_lab_slot(self):
        env = {
            "GPOINT_AGATA_WORKER_URL": "https://point.example/worker",
            "GPOINT_AGATA_WORKER_TOKEN": "point-test",
            "GPOINT_AGATA_TENANT_ID": "81a4ba28-6de5-4b03-8332-0661ba4d46f4",
            "GSALES_AGATA_WORKER_URL": "https://sales.example/worker",
            "GSALES_AGATA_WORKER_TOKEN": "sales-test",
            "GSALES_AGATA_TENANT_ID": "c4d67889-877e-4d1b-a8d8-597c479d638f",
            "AGATA_LAB_WORKER_ENABLED": "true",
        }
        calls = []
        with patch.dict(os.environ, env), patch.object(worker, "process_one", side_effect=lambda lane: calls.append(lane) or False):
            scheduler = worker.TargetScheduler()
            self.assertEqual(scheduler.schedule.count("GPOINT"), 4)
            self.assertEqual(scheduler.schedule.count("GSALES"), 2)
            self.assertEqual(scheduler.schedule.count("LAB"), 1)
            for _ in range(7):
                scheduler.poll(now=0)
        self.assertEqual(calls.count("LAB"), 1)
        self.assertEqual(calls.count("GPOINT"), 4)
        self.assertEqual(calls.count("GSALES"), 2)

    def test_disabled_lab_never_claims_queued_work(self):
        self.submit()
        with patch.dict(os.environ, {"AGATA_LAB_WORKER_ENABLED": "false"}), patch.object(
            worker, "run_inference"
        ) as infer:
            with self.assertRaisesRegex(worker.FatalConfigurationError, "lab_worker_disabled"):
                worker.process_one("LAB")
        infer.assert_not_called()
        self.assertEqual(lab_queue.pending_count(self.root / "lab-queue"), 1)

    def test_lab_lane_failure_does_not_stop_later_user_lane_polls(self):
        env = {
            "GPOINT_AGATA_WORKER_URL": "https://point.example/worker",
            "GPOINT_AGATA_WORKER_TOKEN": "point-test",
            "GPOINT_AGATA_TENANT_ID": "81a4ba28-6de5-4b03-8332-0661ba4d46f4",
            "GSALES_AGATA_WORKER_URL": "https://sales.example/worker",
            "GSALES_AGATA_WORKER_TOKEN": "sales-test",
            "GSALES_AGATA_TENANT_ID": "c4d67889-877e-4d1b-a8d8-597c479d638f",
            "AGATA_LAB_WORKER_ENABLED": "true",
        }
        calls = []
        def poll_lane(lane):
            calls.append(lane)
            if lane == "LAB":
                raise lab_queue.LabQueueError("fixture_only_failure")
            return False
        with patch.dict(os.environ, env), patch.object(worker, "process_one", side_effect=poll_lane):
            scheduler = worker.TargetScheduler()
            for _ in range(7):
                scheduler.poll(now=0)
            scheduler.poll(now=0)
        self.assertEqual(calls[6], "LAB")
        self.assertEqual(calls[7], "GPOINT")

    def test_executor_cancellation_kills_only_its_codex_process_group(self):
        codex = self.root / "codex"
        codex.write_text("test executable")
        calls = {"communicate": 0, "cancel": 0}

        class FakeProcess:
            pid = 987654
            returncode = None
            def communicate(self, _input=None, timeout=None):
                calls["communicate"] += 1
                if timeout == 3:
                    return "", ""
                raise worker.subprocess.TimeoutExpired("codex", timeout)

        def cancel_check():
            calls["cancel"] += 1
            return calls["cancel"] > 1

        with patch.dict(os.environ, {"GPOINT_CODEX_CLI": str(codex), "AGATA_CODEX_JOB_TIMEOUT_SECONDS": "20"}), patch.object(
            worker.subprocess, "Popen", return_value=FakeProcess()
        ), patch.object(worker.os, "killpg") as killpg:
            with self.assertRaises(worker.InferenceCancelled):
                worker.run_inference({"target": "LAB", "request": {"user_message": "synthetic"}}, cancel_check=cancel_check)
        killpg.assert_called_once()
        self.assertEqual(killpg.call_args.args[0], 987654)


if __name__ == "__main__":
    unittest.main()
