"""Offline shared-worker LAB lane checks; every executor invocation is mocked."""
from __future__ import annotations

import json
import hashlib
import fcntl
import os
import importlib.util
import shutil
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


def judge_job(job_id="judge-job-1"):
    invocation_id = "batch-a-judge-1"
    job_id = "judge-" + hashlib.sha256(invocation_id.encode()).hexdigest()[:58]
    return {
        "job_id": job_id, "job_type": "judge", "experiment_id": "agata-lab-judge-v1",
        "run_id": "batch-a",
        "configuration_id": "judge-1", "origin": "conversation-lab-judge",
        "scenario_id": "v2-dev-clean-20261006-everyday",
        "turn_index": 0, "data_classification": "synthetic", "dataset_split": "DEV",
        "timeout_seconds": 30, "history_receipt_ids": [], "judge_slot": 1,
        "promptfoo_invocation_id": invocation_id, "judge_schema_version": 1,
        "presented_labels": ["X", "Y"], "model": "gpt-6-luna", "reasoning_effort": "medium",
        "request": {"instructions": lab_queue.JUDGE_INSTRUCTIONS, "persona": {},
                    "communication_settings": {}, "memories": [], "dialogue_summaries": [],
                    "history": [], "user_message": "Compare anonymous X and Y."},
    }


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
        self.lab_runtime = self.root / "runtime" / ("a" * 40)
        self.lab_runtime.mkdir(parents=True, mode=0o700)
        (self.root / "runtime").chmod(0o700)
        self.lab_runtime.chmod(0o700)
        worker_dir = Path(worker.__file__).resolve().parent
        for name in (
            "gpoint_mac_worker.py", "agata_worker_pool.py", "lab_queue.py",
            "lab_fixture_manifest.json", "lab_promptfoo_compat_manifest.json",
            "lab_v2_dev_manifest.json", "run_codex_worker.sh", "requirements.txt",
        ):
            copied = self.lab_runtime / name
            shutil.copy2(worker_dir / name, copied)
            copied.chmod(0o600)
        self.lab_pointer = self.root / "runtime" / "slots" / "LAB" / "current"
        self.lab_pointer.parent.mkdir(parents=True, mode=0o700)
        self.lab_pointer.parent.chmod(0o700)
        self.lab_pointer.symlink_to(self.lab_runtime, target_is_directory=True)
        self.lab_bundle = lab_queue.worker_bundle_sha256()

    def tearDown(self):
        worker.SUPPORT, worker.STATUS, worker.INFERENCE_PROCESS = self.previous
        self.temp.cleanup()

    def approval(self, max_calls=8, scenarios=None, *, expired=False,
                 experiments=None, configurations=None, bundle_sha256=None,
                 phase=None, run_id=None, approval_id="owner-approval-001"):
        value = {
            "approval_id": approval_id,
            "worker_bundle_sha256": bundle_sha256 or self.lab_bundle,
            "experiment_ids": experiments or ["agata-naturalness-v1"],
            "configuration_ids": configurations or ["baseline", "naturalness-v1"],
            "scenario_ids": scenarios or ["ru_naturalness-02"],
            "max_calls": max_calls,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 1)).isoformat(),
        }
        if phase is not None:
            value.update({"phase": phase, "run_id": run_id})
        self.approval_path.write_text(json.dumps(value), encoding="utf-8")
        self.approval_path.chmod(0o600)

    def prepare_judge_phase(self, *, approval_experiments=None):
        """Create valid offline Phase-A receipts and an active Phase-B state."""
        lab_queue._layout(self.lab_root)
        rows = []
        for arm in ("baseline", "naturalness-v1"):
            for turn in range(4):
                job_id = f"seed-{arm}-{turn}"
                invocation_id = f"batch-a-{arm}-{turn}"
                lab_queue.finish(
                    self.lab_root, job_id, status="completed", response="mock response",
                    execution={"requested_model": "gpt-6-luna", "requested_reasoning_effort": "medium"},
                    identity={"experiment_id": "agata-v2-naturalness-dev", "configuration_id": arm,
                              "scenario_id": "v2-dev-clean-20261006-everyday", "turn_index": turn,
                              "job_type": "v2-dev-generator", "dataset_split": "DEV",
                              "run_id": "batch-a", "promptfoo_invocation_id": invocation_id},
                    slot_id="slot-03",
                )
                receipt = lab_queue.read_receipt(self.lab_root, job_id)
                rows.append({
                    "job_id": job_id,
                    "receipt_sha256": hashlib.sha256(json.dumps(
                        receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ).encode()).hexdigest(),
                    "configuration_id": arm, "turn_index": turn,
                    "promptfoo_invocation_id": invocation_id,
                    "promptfoo_receipt_sha256": "a" * 64,
                })
        state_dir = self.lab_root / "state"
        state_dir.mkdir(mode=0o700, exist_ok=True)
        state = {
            "schema_version": 2, "run_id": "batch-a", "scenario_id": "v2-dev-clean-20261006-everyday",
            "worker_bundle_sha256": self.lab_bundle, "active_phase": "JUDGES",
            "generator_approval_id": "phase-combined-approval", "generator_max_calls": 8,
            "generator_closed": True, "generator_receipts": rows,
            "generator_success_count": 8, "authorization_id": "phase-combined-approval",
            "judge_approval_id": "phase-combined-approval", "judge_max_calls": 3,
        }
        authorization = {
            "schema_version": 1, "authorization_type": "LIVE_RUN_AUTHORIZATION",
            "approval_id": "phase-combined-approval", "run_id": "batch-a", "source_sha": "a" * 40,
            "worker_bundle_sha256": self.lab_bundle, "lab_bundle_sha256": "b" * 64,
            "naturalness_policy_sha256": "c" * 64, "rubric_sha256": "d" * 64,
            "dataset_id": "agata-conversation-lab-v2", "dataset_split": "DEV",
            "dataset_manifest_sha256": "e" * 64,
            "experiment_ids": ["agata-v2-naturalness-dev", lab_queue.JUDGE_EXPERIMENT_ID],
            "configuration_ids": ["baseline", "naturalness-v1", "judge-1", "judge-2", "judge-3"],
            "scenario_ids": ["v2-dev-clean-20261006-everyday"],
            "model": "gpt-6-luna", "reasoning_effort": "medium", "max_generator_calls": 8,
            "max_judge_calls": 3, "retries": 0, "fallbacks": 0, "concurrency": 1,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        state["authorization_sha256"] = lab_queue.live_run_authorization_sha256(authorization)
        state_path = state_dir / "v2-approval-phase.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        state_path.chmod(0o600)
        if approval_experiments is not None:
            authorization["experiment_ids"] = approval_experiments
        authorization["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        state["authorization_sha256"] = lab_queue.live_run_authorization_sha256(authorization)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        state_path.chmod(0o600)
        self.approval_path.write_text(json.dumps(authorization), encoding="utf-8")
        self.approval_path.chmod(0o600)

    def test_supervisor_verifies_approval_against_lab_slot_runtime_bundle(self):
        worker_dir = Path(worker.__file__).resolve().parent
        runtime_root = self.root / "runtime"
        runtime_root.chmod(0o700)
        lab_runtime = runtime_root / ("1" * 40)
        lab_runtime.mkdir(parents=True)
        lab_runtime.chmod(0o700)
        asset_names = (
            "gpoint_mac_worker.py", "agata_worker_pool.py", "lab_queue.py",
            "lab_fixture_manifest.json", "lab_promptfoo_compat_manifest.json",
            "lab_v2_dev_manifest.json", "run_codex_worker.sh", "requirements.txt",
        )
        for name in asset_names:
            copied = lab_runtime / name
            shutil.copy2(worker_dir / name, copied)
            copied.chmod(0o600)
        # Model the installed LAB slot as a separately versioned immutable bundle.
        worker_asset = lab_runtime / "gpoint_mac_worker.py"
        worker_asset.write_text(worker_asset.read_text(encoding="utf-8") + "\n# LAB slot revision\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("test_lab_slot_queue", lab_runtime / "lab_queue.py")
        self.assertIsNotNone(spec)
        slot_queue = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(slot_queue)
        lab_bundle = slot_queue.worker_bundle_sha256()
        self.assertNotEqual(lab_bundle, worker._worker_bundle_sha256())

        self.lab_pointer.unlink()
        self.lab_pointer.symlink_to(lab_runtime, target_is_directory=True)
        self.assertEqual(worker._lab_slot_bundle_sha256(self.root), lab_bundle)
        self.approval(bundle_sha256=lab_bundle)
        with patch.dict(os.environ, {
            "AGATA_LAB_APPROVAL_FILE": str(self.approval_path),
            "GPOINT_AGATA_WORKER_SUPPORT": str(self.root),
        }, clear=True), patch.object(worker, "configured_targets", return_value={}):
            self.assertEqual(worker._lab_approval_gate_status(expected_bundle_sha256=lab_bundle), "OPEN_APPROVED")
            supervisor = worker.build_pool_supervisor()
            supervisor._write_status()
            status = json.loads((self.root / "status/pool-status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["lanes"]["LAB"]["inference_gate"], "OPEN_APPROVED")
        self.submit()
        with patch.dict(os.environ, {
            "AGATA_LAB_APPROVAL_FILE": str(self.approval_path),
            "GPOINT_AGATA_WORKER_SUPPORT": str(self.root),
        }, clear=True):
            claimed = worker.claim_lab_job()

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["job_id"], "lab-job-1")
        self.assertEqual(lab_queue.pending_count(self.lab_root), 0)
        self.assertEqual(len(list((self.lab_root / "running").glob("*.json"))), 1)

    def test_supervisor_rejects_lab_runtime_pointer_outside_managed_runtime_root(self):
        self.approval()
        self.submit()
        outside = self.root / ("b" * 40)
        outside.mkdir(mode=0o700)
        outside.chmod(0o700)
        self.lab_pointer.unlink()
        self.lab_pointer.symlink_to(outside, target_is_directory=True)
        with patch.dict(os.environ, {"AGATA_LAB_APPROVAL_FILE": str(self.approval_path)}, clear=True), \
             patch.object(worker, "run_inference") as infer:
            self.assertIsNone(worker.claim_lab_job())
        infer.assert_not_called()
        self.assertEqual(lab_queue.pending_count(self.lab_root), 1)

    def test_supervisor_rejects_symlinked_runtime_root(self):
        runtime_root = self.root / "runtime"
        saved_runtime = self.root / "runtime-saved"
        runtime_root.rename(saved_runtime)
        runtime_root.symlink_to(saved_runtime, target_is_directory=True)
        with self.assertRaises(worker.FatalConfigurationError):
            worker._lab_slot_bundle_sha256(self.root)

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
            self.assertEqual(worker._lab_slot_bundle_sha256(self.root), self.lab_bundle)
            worker._load_lab_approval(expected_bundle_sha256=self.lab_bundle)
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

    def test_approval_is_pinned_to_lab_slot_bundle(self):
        self.approval()
        self.submit()
        with patch.dict(os.environ, {"AGATA_LAB_APPROVAL_FILE": str(self.approval_path)}, clear=True), patch.object(
            worker, "_lab_slot_bundle_sha256", return_value="0" * 64
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

    def test_judge_approval_and_worker_receipt_are_scoped_and_structured(self):
        invocation_id = "batch-a-judge-1"
        job = judge_job("judge-" + hashlib.sha256(invocation_id.encode()).hexdigest()[:58])
        from judges import CORE_IDS
        verdict = {"overall": "first", "core_dimensions": {key: "first" for key in CORE_IDS},
                   "conditional_evidence": {}, "reasons": ["The first reply is direct."], "defects": [],
                   "uncertainty": False, "position_sensitivity": False}
        self.prepare_judge_phase()
        self.submit(job)
        with patch.dict(os.environ, {"AGATA_LAB_APPROVAL_FILE": str(self.approval_path), "AGATA_WORKER_SLOT_ID": "slot-03"}, clear=True), \
             patch.object(worker, "run_inference", return_value=json.dumps(verdict)) as infer:
            self.assertTrue(worker.process_one("LAB"))
        call_job = infer.call_args.args[0]
        self.assertEqual(call_job["target"], "LAB")
        self.assertEqual(call_job["job_type"], "judge")
        self.assertEqual(worker.resolve_codex_model_for_job(call_job, {}), ("gpt-6-luna", "medium"))
        receipt = lab_queue.read_receipt(self.lab_root, job["job_id"])
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["slot_id"], "slot-03")
        self.assertEqual(receipt["identity"]["promptfoo_invocation_id"], "batch-a-judge-1")
        self.assertEqual(receipt["judge_verdict"]["overall"], "first")
        approval = json.loads(self.approval_path.read_text(encoding="utf-8"))
        self.assertEqual(lab_queue.owner_approval_usage_calls(self.root, approval, phase="JUDGES"), 1)
        self.assertEqual(lab_queue.owner_approval_usage_calls(self.root, approval), 1)

    def test_judge_approval_mismatch_fails_before_executor(self):
        self.prepare_judge_phase(approval_experiments=["agata-naturalness-v1"])
        invocation_id = "batch-a-judge-1"
        job = judge_job("judge-" + hashlib.sha256(invocation_id.encode()).hexdigest()[:58])
        self.submit(job)
        with patch.dict(os.environ, {"AGATA_LAB_APPROVAL_FILE": str(self.approval_path), "AGATA_WORKER_SLOT_ID": "slot-03"}, clear=True), \
             patch.object(worker, "run_inference") as infer:
            self.assertFalse(worker.process_one("LAB"))
        infer.assert_not_called()
        self.assertEqual(lab_queue.pending_count(self.lab_root), 1)

    def test_judge_execution_is_rejected_outside_slot_03(self):
        invocation_id = "batch-a-judge-1"
        job = judge_job("judge-" + hashlib.sha256(invocation_id.encode()).hexdigest()[:58])
        self.prepare_judge_phase()
        self.submit(job)
        with patch.dict(os.environ, {"AGATA_LAB_APPROVAL_FILE": str(self.approval_path), "AGATA_WORKER_SLOT_ID": "slot-01"}, clear=True), \
             patch.object(worker, "run_inference") as infer:
            self.assertTrue(worker.process_one("LAB"))
        infer.assert_not_called()
        receipt = lab_queue.read_receipt(self.lab_root, job["job_id"])
        self.assertEqual(receipt["error_code"], "worker_slot_lane_mismatch")
        self.assertNotIn("response", receipt)

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
            "paths=w.PoolPaths(w.SUPPORT); paths.ensure(); "
            "provider=w.acquire_pool_lock(paths.provider_lock); "
            "w._WORKER_INHERITED_LOCK_FDS=(provider.fileno(),); "
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
            for lock_path in (self.root / "locks/pool.lock", self.root / "locks/provider.lock"):
                descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(descriptor)
            os.kill(codex_pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    released = []
                    for lock_path in (self.root / "locks/pool.lock", self.root / "locks/provider.lock"):
                        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        released.append(descriptor)
                    for descriptor in released:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                        os.close(descriptor)
                    break
                except BlockingIOError:
                    for descriptor in locals().get("released", []):
                        os.close(descriptor)
                    time.sleep(0.05)
            else:
                self.fail("orphan Codex child retained a worker/provider lock after exit")
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=3)
            if codex_pid is not None:
                try:
                    os.kill(codex_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass



    def test_execute_slot_rejects_lane_mismatch_before_lock_or_queue_access(self):
        with self.assertRaisesRegex(worker.FatalConfigurationError, "worker_slot_lane_mismatch"):
            worker.execute_slot("LAB", "slot-01")

if __name__ == "__main__":
    unittest.main()

    def test_execute_slot_rejects_lane_mismatch_before_lock_or_queue_access(self):
        with self.assertRaisesRegex(worker.FatalConfigurationError, "worker_slot_lane_mismatch"):
            worker.execute_slot("LAB", "slot-01")
