"""Local-only contract tests for the subscription-backed worker."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory.worker import claimed_job_types

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import gpoint_mac_worker as worker  # noqa: E402

TEST_GPOINT_TENANT_ID = "7508f36f-774a-48aa-8c6b-42f6f160415d"


class MacWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gpoint-worker-test-")
        self.root = Path(self.temp.name)
        self.old_paths = worker.CONFIG, worker.SUPPORT, worker.STATUS, worker.INFERENCE_PROCESS
        self.old_heartbeat = worker._last_heartbeat_attempt
        worker._last_heartbeat_attempt = float("-inf")
        worker.CONFIG = self.root / "worker.env"
        worker.SUPPORT = self.root / "support"
        worker.STATUS = worker.SUPPORT / "worker-status.json"
        worker.INFERENCE_PROCESS = worker.SUPPORT / "inference-process.json"

    def tearDown(self):
        worker.CONFIG, worker.SUPPORT, worker.STATUS, worker.INFERENCE_PROCESS = self.old_paths
        worker._last_heartbeat_attempt = self.old_heartbeat
        self.temp.cleanup()

    def test_missing_embedding_credential_does_not_block_mac_response_jobs(self):
        without_embeddings = claimed_job_types(embedding_available=False)
        with_embeddings = claimed_job_types(embedding_available=True)
        self.assertIn("response_retry", without_embeddings)
        self.assertNotIn("memory_embed", without_embeddings)
        self.assertIn("memory_embed", with_embeddings)

    def test_eros_cancelled_lease_clears_local_running_state_without_reporting_failure(self):
        self._assert_eros_terminal_lease_clears_state("CANCELLED")

    def test_eros_expired_lease_clears_local_running_state_without_reporting_failure(self):
        self._assert_eros_terminal_lease_clears_state("EXPIRED")

    def _assert_eros_terminal_lease_clears_state(self, terminal_status):
        claim = {
            "id": "e0fbe77f-a09d-4d85-b52d-c277e6af1187",
            "leaseToken": "a2d9dab3-f2d1-488a-8b1a-d31c4f4eec44",
            "target": "EROS",
            "tenant_id": TEST_GPOINT_TENANT_ID,
            "creator_id": "728bf984-79be-475c-bd80-2b8dfb504a3e",
            "job_type": "CHAT_COMPLETION",
            "request": {"model": "gpoint-codex", "messages": [{"role": "user", "content": "synthetic"}]},
        }
        calls = []

        def request(target, path, body=None):
            calls.append((target, path, body))
            if path == "cancel-check":
                return {"active": False, "terminal_status": terminal_status}
            if path == "fail":
                raise worker.GatewayResponseError("worker_gateway_http_409")
            raise AssertionError(f"unexpected request path: {path}")

        with patch.dict(os.environ, {
            "EROS_WORKER_LOCAL_TEST": "true",
            "EROS_WORKER_URL": "http://127.0.0.1:18080/eros-worker",
            "EROS_WORKER_TOKEN": "local-eros-token",
            "EROS_WORKER_TENANT_ID": TEST_GPOINT_TENANT_ID,
        }, clear=True), patch.object(worker, "target_request", side_effect=request), patch.object(
            worker, "run_inference", side_effect=worker.InferenceCancelled("worker_job_cancelled")
        ):
            result = worker.execute_claimed_job("EROS", claim)

        self.assertFalse(result)
        status = json.loads(worker.STATUS.read_text(encoding="utf-8"))
        target_status = status["targets"]["EROS"]
        self.assertEqual(target_status["state"], "idle")
        self.assertFalse(target_status["current_job_present"])
        self.assertIsNone(target_status["last_error_code"])
        self.assertEqual([call[1] for call in calls], ["cancel-check"])

    def test_codex_tool_events_are_rejected(self):
        event = json.dumps({"type": "item.started", "item": {"type": "command_execution"}})
        self.assertTrue(worker._codex_used_tools(event))
        self.assertFalse(worker._codex_used_tools(json.dumps({"type": "item.completed", "item": {"type": "agent_message"}})))
        self.assertTrue(worker._codex_used_tools("unexpected non-JSON output"))

    def test_inference_uses_read_only_isolated_codex_and_parses_structured_result(self):
        executable = self.root / "codex"
        executable.touch()
        executable.chmod(0o700)
        observed = {}

        def execute(args, **kwargs):
            starting_record = json.loads(worker.INFERENCE_PROCESS.read_text(encoding="utf-8"))
            self.assertEqual(starting_record["state"], "starting")
            self.assertIsNone(starting_record["pid"])
            observed["args"] = args
            observed["cwd"] = args[args.index("--cd") + 1]
            observed["env"] = kwargs["env"]
            observed["stderr"] = kwargs["stderr"]
            Path(args[args.index("--output-last-message") + 1]).write_text(
                json.dumps({"text": "Привет!"}), encoding="utf-8"
            )
            class MockProcess:
                pid = 100
                returncode = 0

                def poll(self):
                    return self.returncode

                def communicate(self, input=None, timeout=None):
                    observed["prompt"] = input
                    return None, None

            return MockProcess()

        with patch.dict(os.environ, {"GPOINT_CODEX_CLI": str(executable), "AGATA_CODEX_MODEL": "smoke", "AGATA_CODEX_REASONING": "low"}), patch.object(worker.subprocess, "Popen", side_effect=execute):
            result = worker.run_inference({"request": {"user_message": "hello"}})

        self.assertEqual(result, "Привет!")
        args = observed["args"]
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertIn("--ephemeral", observed["args"])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
        self.assertEqual(args[args.index("--model") + 1], "smoke")
        self.assertIn('model_reasoning_effort="low"', args)
        self.assertEqual(observed["stderr"], subprocess.DEVNULL)
        self.assertEqual(Path(observed["cwd"]).parent, Path(tempfile.gettempdir()))
        self.assertIn('"user_message":"hello"', observed["prompt"])
        self.assertNotIn("GPOINT_AGATA_WORKER_TOKEN", observed["env"])

    def test_memory_task_uses_a_separate_server_authored_prompt(self):
        executable = self.root / "codex"
        executable.touch()
        observed = {}

        def execute(args, **kwargs):
            Path(args[args.index("--output-last-message") + 1]).write_text(
                json.dumps({"text": '{"abstain_reason":"uncertain","candidates":[]}'}),
                encoding="utf-8",
            )

            class MockProcess:
                pid = 101
                returncode = 0

                def poll(self):
                    return self.returncode

                def communicate(self, input=None, timeout=None):
                    observed["prompt"] = input
                    return None, None

            return MockProcess()

        job = {
            "purpose": "memory_extract",
            "request": {"user_message": "Extract from this synthetic message."},
        }
        with patch.dict(os.environ, {"GPOINT_CODEX_CLI": str(executable)}), patch.object(
            worker.subprocess, "Popen", side_effect=execute
        ):
            result = worker.run_inference(job)

        self.assertEqual(result, '{"abstain_reason":"uncertain","candidates":[]}')
        self.assertIn("structured user-memory extraction task", observed["prompt"])
        self.assertIn('"user_message":"Extract from this synthetic message."', observed["prompt"])
        self.assertIn("Return only the JSON value as text", observed["prompt"])
        self.assertNotIn("Return only an object with one key, text", observed["prompt"])

    def test_inference_timeout_terminates_the_process_group(self):
        executable = self.root / "codex"
        executable.touch()
        observed = []

        class HangingProcess:
            pid = 123
            returncode = None

            def poll(self):
                return self.returncode

            def communicate(self, input=None, timeout=None):
                if not observed:
                    observed.append(timeout)
                    raise subprocess.TimeoutExpired("codex", timeout)
                self.returncode = -15

        with patch.dict(os.environ, {"GPOINT_CODEX_CLI": str(executable), "AGATA_CODEX_JOB_TIMEOUT_SECONDS": "15"}), \
             patch.object(worker.subprocess, "Popen", return_value=HangingProcess()), \
             patch("os.killpg") as kill_group:
            with self.assertRaisesRegex(RuntimeError, "codex_execution_timeout"):
                worker.run_inference({"request": {"user_message": "hello"}})

        self.assertEqual(observed, [15])
        kill_group.assert_called_once()

    def test_claim_inference_completion_and_status_are_secret_free(self):
        token = "test-worker-secret"
        responses = iter([
            {"status": "ready"},
            {"job": {"id": "job-1", "leaseToken": "lease-1", "request": {"user_message": "hi"}}},
            {"completed": True},
            {"status": "ready"},
            {
                "source_id": "agata-worker-queue-status-v1:gpoint",
                "lane": "GPOINT",
                "pending_jobs": 2,
                "running_jobs": 1,
                "snapshot_at": "2026-10-07T10:00:00+00:00",
            },
        ])
        calls = []
        output = io.StringIO()

        def api(path, body=None):
            calls.append((path, body))
            return next(responses)

        with patch.dict(os.environ, {"GPOINT_AGATA_WORKER_TOKEN": token, "GPOINT_AGATA_TENANT_ID": TEST_GPOINT_TENANT_ID, "AGATA_WORKER_MODEL": "test-model"}), patch.object(worker, "api_request", side_effect=api), patch.object(worker, "run_inference", return_value="answer"), patch("sys.stdout", output):
            self.assertTrue(worker.process_one())
            self.assertEqual(worker.status(), 0)

        complete = next(body for path, body in calls if path == "complete")
        self.assertEqual(complete["text"], "answer")
        self.assertEqual([path for path, _ in calls], ["health", "claim", "complete", "health", "queue-status"])
        self.assertIsNone(calls[-1][1])
        status_output = output.getvalue()
        self.assertIn('"queue_snapshot_state": "VERIFIED"', status_output)
        self.assertIn('"pending_inference_jobs": 2', status_output)
        self.assertIn('"running_inference_jobs": 1', status_output)
        self.assertNotIn(token, status_output)
        self.assertNotIn("lease-1", status_output)
        self.assertNotIn("user_message", status_output)
        saved = worker.STATUS.read_text(encoding="utf-8")
        self.assertNotIn(token, saved)
        self.assertEqual(json.loads(saved)["last_success_job_id"], "job-1")

    def test_blank_default_model_completion_satisfies_server_contract(self):
        from app.api.internal import WorkerCompleteRequest

        for model, expected in [("", "codex-default"), ("  ", "codex-default"), (" explicit ", "explicit")]:
            worker._last_heartbeat_attempt = float("-inf")
            def api(path, body=None, expected_model=expected):
                if path == "claim":
                    return {"job": {"id": "7508f36f-774a-48aa-8c6b-42f6f160415d", "leaseToken": "x" * 32, "request": {}}}
                if path == "complete":
                    validated = WorkerCompleteRequest(**body)
                    self.assertEqual(validated.model, expected_model)
                return {"accepted": True}

            with self.subTest(model=model), patch.dict(os.environ, {"AGATA_CODEX_MODEL": model, "GPOINT_AGATA_WORKER_TOKEN": "test-only", "GPOINT_AGATA_TENANT_ID": TEST_GPOINT_TENANT_ID}), patch.object(worker, "api_request", side_effect=api), patch.object(worker, "run_inference", return_value="answer"):
                self.assertTrue(worker.process_one())

    def test_inference_failure_reports_retryable_safe_code(self):
        calls = []

        def api(path, body=None):
            calls.append((path, body))
            if path == "claim":
                return {"job": {"id": "job-2", "leaseToken": "lease-2", "request": {}}}
            return {"accepted": True, "state": "pending"}

        with patch.dict(os.environ, {"GPOINT_AGATA_WORKER_TOKEN": "test-only", "GPOINT_AGATA_TENANT_ID": TEST_GPOINT_TENANT_ID}), patch.object(worker, "api_request", side_effect=api), patch.object(
            worker, "run_inference", side_effect=RuntimeError("codex_execution_failed_1")
        ):
            self.assertFalse(worker.process_one())

        self.assertEqual(calls[-1][0], "fail")
        self.assertEqual(calls[-1][1]["error_code"], "codex_execution_failed_1")
        self.assertNotIn("lease-2", worker.STATUS.read_text(encoding="utf-8"))

    def test_gateway_requires_https_and_token(self):
        with patch.dict(os.environ, {"GPOINT_AGATA_WORKER_URL": "http://example.test", "GPOINT_AGATA_WORKER_TOKEN": "secret"}):
            with self.assertRaisesRegex(RuntimeError, "worker_gateway_or_auth_unconfigured"):
                worker.api_request("claim")

if __name__ == "__main__":
    unittest.main()
