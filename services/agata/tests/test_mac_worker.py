"""Local-only contract tests for the subscription-backed worker."""

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
        self.old_paths = worker.CONFIG, worker.SUPPORT, worker.STATUS
        self.old_heartbeat = worker._last_heartbeat_attempt
        worker._last_heartbeat_attempt = float("-inf")
        worker.CONFIG = self.root / "worker.env"
        worker.SUPPORT = self.root / "support"
        worker.STATUS = worker.SUPPORT / "worker-status.json"

    def tearDown(self):
        worker.CONFIG, worker.SUPPORT, worker.STATUS = self.old_paths
        worker._last_heartbeat_attempt = self.old_heartbeat
        self.temp.cleanup()

    def test_missing_embedding_credential_does_not_block_mac_response_jobs(self):
        without_embeddings = claimed_job_types(embedding_available=False)
        with_embeddings = claimed_job_types(embedding_available=True)
        self.assertIn("response_retry", without_embeddings)
        self.assertNotIn("memory_embed", without_embeddings)
        self.assertIn("memory_embed", with_embeddings)

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

    def test_inference_timeout_terminates_the_process_group(self):
        executable = self.root / "codex"
        executable.touch()
        observed = []

        class HangingProcess:
            pid = 123
            returncode = None

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
        ])
        calls = []

        def api(path, body=None):
            calls.append((path, body))
            return next(responses)

        with patch.dict(os.environ, {"GPOINT_AGATA_WORKER_TOKEN": token, "GPOINT_AGATA_TENANT_ID": TEST_GPOINT_TENANT_ID, "AGATA_WORKER_MODEL": "test-model"}), patch.object(worker, "api_request", side_effect=api), patch.object(worker, "run_inference", return_value="answer"):
            self.assertTrue(worker.process_one())
            self.assertEqual(worker.status(), 0)

        complete = next(body for path, body in calls if path == "complete")
        self.assertEqual(complete["text"], "answer")
        self.assertEqual([path for path, _ in calls], ["health", "claim", "complete", "health"])
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
