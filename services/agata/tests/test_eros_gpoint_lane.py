"""Eros provider jobs use the existing GPOINT Product Worker lane."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import gpoint_mac_worker as worker


ENV = {
    "GPOINT_EROS_WORKER_URL": "https://gpoint.example/api/gpoint-worker",
    "GPOINT_EROS_WORKER_TOKEN": "gpoint-private",
    "GPOINT_EROS_TENANT_ID": "81a4ba28-6de5-4b03-8332-0661ba4d46f4",
    "GSALES_AGATA_WORKER_URL": "https://sales.example/worker",
    "GSALES_AGATA_WORKER_TOKEN": "gsales-private",
    "GSALES_AGATA_TENANT_ID": "c4d67889-877e-4d1b-a8d8-597c479d638f",
}


class ErosGpointLaneTests(unittest.TestCase):
    def test_empty_poll_records_idle_state_for_migration_verification(self):
        with tempfile.TemporaryDirectory() as root:
            status = Path(root) / 'worker-status.json'
            with patch.dict(os.environ, ENV, clear=True), \
                 patch.object(worker, 'SUPPORT', Path(root)), \
                 patch.object(worker, 'STATUS', status), \
                 patch.object(worker, 'target_request', return_value={'job': None}):
                self.assertIsNone(worker.claim_target_job('GPOINT'))
            saved = json.loads(status.read_text())
            self.assertEqual(saved['state'], 'idle')
            self.assertIs(saved.get('current_job_present'), False)
            self.assertIsNone(saved.get('last_error_code'))
            self.assertIs(saved['targets']['GPOINT'].get('current_job_present'), False)

    def test_eros_chat_claim_uses_the_only_gpoint_worker_route(self):
        with patch.dict(os.environ, ENV, clear=True):
            self.assertEqual(worker.TARGET_NAMES, ("GPOINT",))
            claim = {
                "id": "eros-job-1", "leaseToken": "lease-eros", "target": "GPOINT",
                "tenant_id": ENV["GPOINT_EROS_TENANT_ID"], "purpose": "eros_chat_completion",
                "request": {"messages": [{"role": "user", "content": "Привет"}], "max_tokens": 300},
            }
            worker.validate_target_claim("GPOINT", claim)
            with self.assertRaisesRegex(worker.GatewayResponseError, "worker_target_invalid"):
                worker.process_one("GSALES")

    def test_eros_chat_claim_uses_restricted_codex_command_in_gpoint_lane(self):
        command = worker.build_codex_exec_command(
            Path("/usr/bin/codex"), Path("/tmp/eros"), Path("/tmp/schema.json"),
            Path("/tmp/result.json"), {"target": "GPOINT", "purpose": "eros_chat_completion"},
        )
        self.assertIn("--ignore-user-config", command)
        self.assertNotIn("--ignore-rules", command)
        disabled_features = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--disable"]
        self.assertEqual(disabled_features, ["shell_tool", "unified_exec", "browser_use", "apps", "standalone_web_search"])
        self.assertIn("--ignore-user-config", command)

    def test_eros_chat_purpose_runs_through_existing_gpoint_claim(self):
        with tempfile.TemporaryDirectory() as root:
            temp = Path(root)
            executable = temp / "codex"
            executable.touch()
            observed = {}

            def execute(args, **kwargs):
                observed["args"] = args
                observed["env"] = kwargs["env"]
                observed["stderr"] = kwargs["stderr"]
                Path(args[args.index("--output-last-message") + 1]).write_text(
                    json.dumps({"text": "Привет, рада тебя видеть!"}), encoding="utf-8"
                )

                class MockProcess:
                    pid = 133
                    returncode = 0

                    def poll(self):
                        return self.returncode

                    def communicate(self, input=None, timeout=None):
                        observed["prompt"] = input
                        return None, None

                return MockProcess()

            with patch.dict(os.environ, {**ENV, "GPOINT_CODEX_CLI": str(executable)}, clear=True), \
                 patch.object(worker, "SUPPORT", temp), \
                 patch.object(worker, "INFERENCE_PROCESS", temp / "inference-process.json"), \
                 patch.object(worker.subprocess, "Popen", side_effect=execute):
                result = worker.run_inference({
                    "target": "GPOINT", "purpose": "eros_chat_completion",
                    "request": {"model": "gpoint-codex", "messages": [
                        {"role": "system", "content": "Character context"},
                        {"role": "user", "content": "Hi"},
                    ], "max_tokens": 300},
                })

            self.assertEqual(result, "Привет, рада тебя видеть!")
            self.assertIn("Character context", observed["prompt"])
            self.assertIn("Hi", observed["prompt"])
            self.assertIn("--ignore-user-config", observed["args"])
            self.assertNotIn("GPOINT_EROS_WORKER_TOKEN", observed["env"])
            self.assertEqual(observed["stderr"], subprocess.DEVNULL)

    def test_eros_max_tokens_is_forwarded_as_tokens_not_character_limit(self):
        with tempfile.TemporaryDirectory() as root:
            temp = Path(root)
            executable = temp / "codex"
            executable.touch()
            observed = {}

            def execute(args, **kwargs):
                observed["args"] = args
                schema_path = Path(args[args.index("--output-schema") + 1])
                observed["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
                Path(args[args.index("--output-last-message") + 1]).write_text(
                    json.dumps({"text": "A response longer than thirty-two characters."}),
                    encoding="utf-8",
                )

                class MockProcess:
                    pid = 134
                    returncode = 0

                    def poll(self):
                        return self.returncode

                    def communicate(self, input=None, timeout=None):
                        return None, None

                return MockProcess()

            with patch.dict(os.environ, {**ENV, "GPOINT_CODEX_CLI": str(executable)}, clear=True), \
                 patch.object(worker, "SUPPORT", temp), \
                 patch.object(worker, "INFERENCE_PROCESS", temp / "inference-process.json"), \
                 patch.object(worker.subprocess, "Popen", side_effect=execute):
                result = worker.run_inference({
                    "target": "GPOINT", "purpose": "eros_chat_completion",
                    "request": {"model": "gpoint-codex", "messages": [
                        {"role": "user", "content": "Please answer naturally."},
                    ], "max_tokens": 32},
                })

            self.assertEqual(result, "A response longer than thirty-two characters.")
            self.assertIn("model_max_output_tokens=32", observed["args"])
            self.assertEqual(observed["schema"]["properties"]["text"]["maxLength"], 4096)

    def test_eros_chat_prompt_accepts_full_queue_request_including_model(self):
        prompt = worker.build_eros_completion_prompt({
            "model": "gpoint-codex",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 32,
            "temperature": 1,
        })
        self.assertIn('"content":"hello"', prompt)

    def test_eros_chat_rejects_unsupported_temperature(self):
        with self.assertRaisesRegex(RuntimeError, "eros_chat_request_invalid"):
            worker.build_eros_completion_prompt({
                "messages": [{"role": "user", "content": "hello"}], "temperature": 0.4,
            })

    def test_eros_chat_request_rejects_invalid_role_and_oversized_payload(self):
        with self.assertRaisesRegex(RuntimeError, "eros_chat_request_invalid"):
            worker.build_eros_completion_prompt({"messages": [{"role": "tool", "content": "bad"}]})
        with self.assertRaisesRegex(RuntimeError, "eros_chat_request_invalid"):
            worker.build_eros_completion_prompt({"messages": [{"role": "user", "content": "x" * 16_001}]})


if __name__ == "__main__":
    unittest.main()
