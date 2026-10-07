"""Offline shared-worker LAB lane checks; every executor invocation is mocked."""
from __future__ import annotations

import json
import fcntl
import os
import signal
import sys
import tempfile
import time
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import gpoint_mac_worker as worker  # noqa: E402
import lab_queue  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "conversation-lab" / "evaluators"))
import worker_client  # noqa: E402


def lab_job(job_id="lab-job-1", *, scenario="ru_naturalness-02", config="baseline"):
    job = worker_client.build_job(
        scenario_id=scenario, configuration_id=config, turn_index=0, job_id=job_id,
    )
    job["timeout_seconds"] = 30
    return job


class WorkerLabIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous = worker.SUPPORT, worker.STATUS, worker.INFERENCE_PROCESS
        worker.SUPPORT = self.root
        worker.STATUS = self.root / "status/worker-status.json"
        worker.INFERENCE_PROCESS = self.root / "state/inference-process.json"
        self.approval_path = self.root / "approval.json"
        self.lab_root = self.root / "queues/lab"

    def tearDown(self):
        worker.SUPPORT, worker.STATUS, worker.INFERENCE_PROCESS = self.previous
        self.temp.cleanup()

    def approval(self, max_calls=8, scenarios=None, *, expired=False):
        value = {
            "approval_id": "owner-approval-001",
            "worker_bundle_sha256": worker._worker_bundle_sha256(),
            "experiment_ids": ["agata-naturalness-v1"],
            "configuration_ids": ["baseline", "naturalness-v1"],
            "scenario_ids": scenarios or ["ru_naturalness-02"],
            "max_calls": max_calls,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 1)).isoformat(),
        }
        self.approval_path.write_text(json.dumps(value), encoding="utf-8")
        self.approval_path.chmod(0o600)

    def submit(self, job=None):
        lab_queue.submit(job or lab_job(), root=self.lab_root)

    def test_lab_uses_shared_executor_without_gateway_and_keeps_response_local(self):
        self.approval()
        self.submit()
        with patch.dict(os.environ, {
            "AGATA_LAB_APPROVAL_FILE": str(self.approval_path),
        }, clear=True), patch.object(worker, "run_inference", return_value="Синтетический ответ") as infer, patch.object(
            worker, "api_request", side_effect=AssertionError("LAB must not use a production gateway")
        ):
            self.assertTrue(worker.process_one("LAB"))
        infer.assert_called_once()
        args, kwargs = infer.call_args
        self.assertEqual(args[0]["target"], "LAB")
        self.assertEqual(args[0]["request"]["user_message"], lab_job()["request"]["user_message"])
        self.assertTrue(callable(kwargs["cancel_check"]))
        receipt = lab_queue.read_receipt(self.lab_root, "lab-job-1")
        self.assertEqual(receipt["response"], "Синтетический ответ")
        self.assertEqual(receipt["identity"]["configuration_id"], "baseline")
        self.assertGreaterEqual(receipt["execution"]["duration_ms"], 0)
        self.assertIsNone(receipt["execution"]["actual_model"])
        self.assertNotIn("cli_reported_usage", receipt["execution"])
        self.assertFalse(worker.STATUS.read_text().find("synthetic user turn") >= 0)

    def test_approval_missing_leaves_job_queued_and_never_calls_executor(self):
        self.submit()
        with patch.dict(os.environ, {"AGATA_LAB_APPROVAL_FILE": str(self.approval_path)}, clear=True), patch.object(
            worker, "run_inference"
        ) as infer:
            self.assertFalse(worker.process_one("LAB"))
        infer.assert_not_called()
        self.assertEqual(lab_queue.pending_count(self.lab_root), 1)

    def test_approval_is_pinned_to_both_worker_and_queue_helper_sources(self):
        self.approval()
        self.submit()
        with patch.dict(os.environ, {"AGATA_LAB_APPROVAL_FILE": str(self.approval_path)}, clear=True), patch.object(
            worker, "_worker_bundle_sha256", return_value="0" * 64
        ), patch.object(worker, "run_inference") as infer:
            self.assertFalse(worker.process_one("LAB"))
        infer.assert_not_called()
        self.assertEqual(lab_queue.pending_count(self.lab_root), 1)

    def test_source_hash_and_scenario_scope_gate_fail_before_generation(self):
        self.approval(scenarios=["daily-02"])
        self.submit()
        with patch.dict(os.environ, {"AGATA_LAB_APPROVAL_FILE": str(self.approval_path)}, clear=True), patch.object(
            worker, "run_inference"
        ) as infer:
            worker.process_one("LAB")
        infer.assert_not_called()
        self.assertEqual(lab_queue.read_receipt(self.lab_root, "lab-job-1")["status"], "failed")

    def test_approval_call_limit_is_consumed_before_inference(self):
        self.approval(max_calls=1)
        with patch.dict(os.environ, {"AGATA_LAB_APPROVAL_FILE": str(self.approval_path)}, clear=True), patch.object(
            worker, "run_inference", return_value="ok"
        ) as infer:
            self.submit(lab_job("job-one"))
            worker.process_one("LAB")
            self.submit(lab_job("job-two"))
            worker.process_one("LAB")
            self.assertEqual(worker._lab_approval_gate_status(), "CLOSED_CONSUMED")
        self.assertEqual(infer.call_count, 1)
        self.assertEqual(lab_queue.read_receipt(self.lab_root, "job-two")["status"], "failed")

    def test_expired_approval_keeps_lab_available_but_never_claims_or_infers(self):
        self.approval(expired=True)
        self.submit()
        with patch.dict(os.environ, {"AGATA_LAB_APPROVAL_FILE":str(self.approval_path)}, clear=True), \
             patch.object(worker, "run_inference") as infer:
            self.assertEqual(worker._lab_approval_gate_status(), "CLOSED_EXPIRED")
            self.assertFalse(worker.process_one("LAB"))
        infer.assert_not_called()
        self.assertEqual(lab_queue.pending_count(self.lab_root), 1)

    def test_lab_without_approval_never_claims_queued_work_without_legacy_flag(self):
        self.submit()
        with patch.dict(os.environ, {}, clear=True), patch.object(
            worker, "run_inference"
        ) as infer:
            self.assertFalse(worker.process_one("LAB"))
        infer.assert_not_called()
        self.assertEqual(lab_queue.pending_count(self.lab_root), 1)

    def test_executor_cancellation_kills_only_its_codex_process_group(self):
        codex = self.root / "codex"
        codex.write_text("test executable")
        calls = {"communicate": 0, "cancel": 0}

        class FakeProcess:
            pid = 987654
            returncode = None
            def poll(self):
                return self.returncode
            def communicate(self, _input=None, timeout=None):
                calls["communicate"] += 1
                if timeout == 3:
                    self.returncode = 0
                    return "", ""
                raise worker.subprocess.TimeoutExpired("codex", timeout)

        def cancel_check():
            calls["cancel"] += 1
            return calls["cancel"] > 1

        with patch.dict(os.environ, {"GPOINT_CODEX_CLI": str(codex), "AGATA_CODEX_JOB_TIMEOUT_SECONDS": "20"}), patch.object(
            worker.subprocess, "Popen", return_value=FakeProcess()
        ), patch.object(worker.os, "killpg") as killpg:
            with self.assertRaises(worker.InferenceCancelled):
                worker.run_inference({"target": "LAB", "request": {"user_message": "synthetic"}}, cancel_check=cancel_check, timeout_seconds=30)
        killpg.assert_called_once()
        self.assertEqual(killpg.call_args.args[0], 987654)

    def test_lab_timeout_caps_environment_timeout(self):
        codex = self.root / "codex"
        codex.write_text("test executable")
        timeouts = []

        class FakeProcess:
            pid = 987655
            returncode = None
            def poll(self):
                return self.returncode
            def communicate(self, _input=None, timeout=None):
                timeouts.append(timeout)
                if timeout == 3:
                    self.returncode = 0
                    return "", ""
                raise worker.subprocess.TimeoutExpired("codex", timeout)

        with patch.dict(os.environ, {
            "GPOINT_CODEX_CLI": str(codex), "AGATA_CODEX_JOB_TIMEOUT_SECONDS": "600",
        }), patch.object(worker.subprocess, "Popen", return_value=FakeProcess()), patch.object(worker.os, "killpg"):
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                worker.run_inference({"target": "LAB", "request": {"user_message": "fixture"}}, timeout_seconds=20)
        self.assertEqual(timeouts[0], 20)

    def test_worker_crash_keeps_global_lock_until_orphan_codex_exits(self):
        worker_dir = Path(worker.__file__).resolve().parent
        pid_file = self.root / "fake-codex.pid"
        fake_codex = self.root / "codex"
        fake_codex.write_text(
            "#!/usr/bin/env python3\nimport os,time\n"
            f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(300)\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o700)
        child_code = (
            "import sys, datetime; from pathlib import Path; "
            "datetime.UTC = getattr(datetime, 'UTC', datetime.timezone.utc); "
            f"sys.path.insert(0, {str(worker_dir)!r}); "
            "import gpoint_mac_worker as w; "
            f"w.SUPPORT=Path({str(self.root)!r}); "
            f"w.INFERENCE_PROCESS=Path({str(self.root / 'state/inference-process.json')!r}); "
            "lock=w.acquire_worker_lock(); w._WORKER_LOCK_FD=lock.fileno(); "
            "w.run_inference({'request': {'user_message': 'fixture'}})"
        )
        env = dict(os.environ)
        env.update({"GPOINT_CODEX_CLI": str(fake_codex), "PYTHONDONTWRITEBYTECODE": "1"})
        parent = subprocess.Popen(
            [sys.executable, "-c", child_code], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        codex_pid = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not pid_file.exists():
                time.sleep(0.02)
            self.assertTrue(pid_file.exists(), "fake Codex child did not start")
            codex_pid = int(pid_file.read_text())
            parent.kill()
            parent.wait(timeout=3)
            lock_path = self.root / "locks/pool.lock"
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.kill(codex_pid, signal.SIGTERM)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                        break
                    except BlockingIOError:
                        time.sleep(0.05)
                else:
                    self.fail("orphan Codex child retained the worker lock after exit")
            finally:
                os.close(descriptor)
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=3)
            if codex_pid is not None:
                try:
                    os.kill(codex_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
