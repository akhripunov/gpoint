import importlib.util
import json
import hashlib
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

LAB = Path(__file__).resolve().parents[1]
RUNNER_PATH = LAB / "evaluators" / "promptfoo_runner.py"
CONFIG_PATH = LAB / "tooling" / "promptfoo" / "promptfooconfig.yaml"
PROVIDER_PATH = LAB / "evaluators" / "promptfoo_worker_provider.py"
SPEC = importlib.util.spec_from_file_location("agata_promptfoo_runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
PROVIDER_SPEC = importlib.util.spec_from_file_location("agata_promptfoo_provider_for_runner_test", PROVIDER_PATH)
provider = importlib.util.module_from_spec(PROVIDER_SPEC)
PROVIDER_SPEC.loader.exec_module(provider)


def new_ledger(temp):
    path = LAB / "evaluators" / "run_coordinator.py"
    spec = importlib.util.spec_from_file_location("promptfoo_runner_test_coordinator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = Path(temp) / "private"
    root.mkdir(mode=0o700)
    ledger = module.CallLedger(journal_path=root / "call-ledger.jsonl")
    return ledger


class WorkerPromptfooRunnerTests(unittest.TestCase):
    prompt = "Привет! Что тебе обычно нравится делать в спокойный выходной?"

    def test_offline_preview_config_uses_valid_promptfoo_test_case(self):
        with tempfile.TemporaryDirectory() as temp:
            working_dir = Path(temp) / "empty"
            working_dir.mkdir()
            config = runner.build_provider_request(self.prompt, working_dir=str(working_dir))
        self.assertEqual(config["tests"], [{"vars": {}}])

    def test_config_uses_exact_python_lab_provider_and_frozen_compat_fixture(self):
        config = runner.build_worker_provider_request(
            self.prompt, run_id="run-1", invocation_id="inv-1", scenario_id="promptfoo-compat-001")
        provider = config["providers"][0]
        self.assertEqual(provider["id"], "file://" + str((LAB / "evaluators/promptfoo_worker_provider.py").resolve()))
        self.assertEqual(provider["config"]["model"], "gpt-6-luna")
        self.assertEqual(provider["config"]["reasoning_effort"], "medium")
        self.assertEqual(provider["config"]["maxRetries"], 0)
        self.assertEqual(config["evaluateOptions"], {"maxConcurrency": 1, "cache": False})
        self.assertEqual(config["tests"], [{"vars": {}}])
        self.assertNotIn("fallbacks", provider["config"])

    def test_config_rejects_other_prompt_scenario_or_provider_configuration(self):
        for prompt, scenario in (("other", "promptfoo-compat-001"), (self.prompt, "ru_naturalness-01")):
            with self.subTest(prompt=prompt, scenario=scenario), self.assertRaises(PermissionError):
                runner.build_worker_provider_request(prompt, run_id="r", invocation_id="i", scenario_id=scenario)

    def test_environment_rejects_api_keys_before_promptfoo_process(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = new_ledger(temp)
            invoked = []
            with self.assertRaises(PermissionError):
                runner.run_promptfoo_worker_eval(
                    self.prompt, run_id="run-1", invocation_id="inv-1", scenario_id="promptfoo-compat-001",
                    promptfoo_executable="/fake/promptfoo", worker_queue=Path(temp)/"queue",
                    private_receipt_directory=Path(temp), call_ledger=ledger,
                    env={"PATH":"/usr/bin", "OPENAI_API_KEY":"never-print-or-forward"},
                    eval_runner=lambda *args, **kwargs: invoked.append((args, kwargs)),
                )
            self.assertEqual(invoked, [])

    def test_promptfoo_python_preflight_rejects_older_runtime(self):
        with patch.object(runner.subprocess, "run", return_value=SimpleNamespace(stdout="3.9\n")):
            with self.assertRaisesRegex(PermissionError, r"Python 3.11\+"):
                runner.validate_promptfoo_python(sys.executable, {"PATH": "/usr/bin"})

    def test_restricted_promptfoo_path_adds_verified_node_without_changing_parent_environment(self):
        node = "/Users/akhripuniov/.local/bin/node"
        parent_path = os.environ.get("PATH")
        child_path = runner.ensure_promptfoo_node_path({"PATH": "/usr/bin:/bin"}, node_executable=node)
        self.assertEqual(child_path.split(":")[0], str(Path(node).resolve().parent))
        self.assertIn("/usr/bin:/bin", child_path)
        self.assertEqual(os.environ.get("PATH"), parent_path)

    def test_promptfoo_01231_cli_runs_with_the_same_restricted_child_path(self):
        cli = LAB / "tooling/promptfoo/node_modules/.bin/promptfoo"
        restricted_path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        if shutil.which("node", path=restricted_path):
            self.skipTest("the canonical restricted PATH already contains Node on this host")
        original = subprocess.run([str(cli), "--version"],
                                  env={"PATH": restricted_path, "HOME": "/tmp"},
                                  capture_output=True, text=True, timeout=15)
        self.assertEqual(original.returncode, 127)
        self.assertIn("env: node: No such file or directory", original.stderr)
        env = runner.build_cli_environment({"PATH": restricted_path, "HOME": "/tmp"})
        result = subprocess.run([str(cli), "--version"], env=env, capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0.123.1")
        self.assertTrue(Path(env["PATH"].split(":")[0], "node").is_file())

    def test_preinference_receipt_is_private_and_contains_no_error_or_secret_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); root.chmod(0o700)
            path = runner.write_preinference_failure(
                root, invocation_id="inv-safe-1", stage="ENV_VALIDATION",
                reason_code="API_KEY_AUTH_FORBIDDEN", exception=PermissionError("sk-secret prompt text"),
                source_location="promptfoo_runner.run_promptfoo_v2_dev_eval",
            )
            payload = json.loads(path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(payload["stage"], "ENV_VALIDATION")
            self.assertEqual(payload["reason_code"], "API_KEY_AUTH_FORBIDDEN")
            self.assertEqual(payload["exception_class"], "PermissionError")
            self.assertFalse(payload["generator_reservation_created"])
            self.assertFalse(payload["worker_job_submitted"])
            self.assertNotIn("sk-secret", path.read_text())
            self.assertNotIn("prompt text", path.read_text())

    def test_v2_runner_preflight_failures_record_stage_specific_safe_receipts(self):
        cases = {
            "provider": ("PROVIDER_CONFIG", "PROVIDER_AUTH_CONFIG_FORBIDDEN"),
            "environment": ("ENV_VALIDATION", "API_KEY_ENV_FORBIDDEN"),
            "python": ("PYTHON_VALIDATION", "PYTHON_RUNTIME_INVALID"),
            "cli": ("PROMPTFOO_CLI_VALIDATION", "NODE_RUNTIME_INVALID"),
            "queue": ("LAB_QUEUE_VALIDATION", "CANONICAL_LAB_QUEUE_INVALID"),
            "phase": ("PHASE_STATE_VALIDATION", "PHASE_STATE_INACTIVE"),
            "job": ("IMMUTABLE_V2_JOB_CONTRACT", "V2_JOB_CONTRACT_INVALID"),
            "ledger": ("GENERATOR_RESERVATION", "DURABLE_LEDGER_INVALID"),
        }
        for name, expected in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); root.chmod(0o700)
                receipts = root / "receipts"; receipts.mkdir(mode=0o700)
                support = root / "support"
                ledger = new_ledger(temp)
                config = {"providers": [{
                    "id": "file://" + str(PROVIDER_PATH.resolve()),
                    "config": {"pythonExecutable": sys.executable},
                }]}
                env = {"PATH": "/usr/bin:/bin", "HOME": str(root),
                       "PROMPTFOO_PYTHON": sys.executable,
                       "GPOINT_AGATA_WORKER_SUPPORT": str(support)}
                if name == "environment":
                    env["OPENAI_API_KEY"] = "sentinel-not-for-receipt"
                if name == "provider":
                    config["providers"][0]["config"]["api_key"] = "sentinel-not-for-receipt"
                if name == "ledger":
                    ledger.durable = False
                patches = []
                if name != "job":
                    patches.append(patch.object(runner, "build_worker_v2_provider_request", return_value=config))
                patches.append(patch.object(runner, "validate_promptfoo_cli", return_value="0.123.1"))
                if name == "python":
                    patches.append(patch.object(runner, "validate_promptfoo_python",
                                                side_effect=PermissionError("python rejected")))
                if name == "cli":
                    patches.append(patch.object(runner, "resolve_promptfoo_node",
                                                side_effect=PermissionError("node rejected")))
                if name == "queue":
                    patches.append(patch.object(runner.worker_client, "canonical_lab_queue",
                                                side_effect=PermissionError("queue rejected")))
                if name == "job":
                    patches.append(patch.object(runner, "build_worker_v2_provider_request",
                                                side_effect=PermissionError("contract rejected")))
                for mocked in patches:
                    mocked.start()
                try:
                    with self.assertRaises(PermissionError):
                        runner.run_promptfoo_v2_dev_eval(
                            {"user_message": "synthetic"}, run_id="run-safe", invocation_id="inv-safe",
                            arm_id="run-safe-baseline", configuration_id="baseline", scenario_id="dev-safe",
                            turn_index=0, history_receipt_ids=[], promptfoo_executable="/fake/promptfoo",
                            worker_queue=support / "queues/lab", private_receipt_directory=receipts,
                            call_ledger=ledger, env=env,
                            eval_runner=lambda *args, **kwargs: self.fail("preflight reached Promptfoo"),
                        )
                finally:
                    for mocked in reversed(patches):
                        mocked.stop()
                receipt = receipts / "inv-safe.preinference-diagnostic.json"
                payload = json.loads(receipt.read_text())
                self.assertEqual((payload["stage"], payload["reason_code"]), expected)
                self.assertFalse(payload["generator_reservation_created"])
                self.assertFalse(payload["worker_job_submitted"])
                self.assertNotIn("sentinel-not-for-receipt", receipt.read_text())

    def test_eval_uses_promptfoo_and_only_explicit_nonsecret_lab_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = new_ledger(temp)
            observed = {}
            support = Path(temp) / "support"
            queue = support / "queues" / "lab"

            def fake_eval(args, *, env, timeout_seconds, on_started, on_ambiguous):
                observed["args"] = args
                observed["env"] = env
                observed["timeout"] = timeout_seconds
                config = json.loads(Path(args[3]).read_text(encoding="utf-8"))
                observed["provider"] = config["providers"][0]["id"]
                child_ledger = ledger.__class__(journal_path=ledger.journal_path)
                child_ledger.reserve("generator", "inv-1", metadata={
                    "run_id":"run-1", "arm_id":"promptfoo-worker-compat",
                    "experiment_id":"promptfoo-worker-compat", "scenario_id":"promptfoo-compat-001",
                    "turn_index":0, "model":"gpt-6-luna", "reasoning_effort":"medium"})
                child_ledger.complete("generator", "inv-1", "success")
                on_started()
                return SimpleNamespace(returncode=0, stdout="offline", stderr="")

            result = runner.run_promptfoo_worker_eval(
                self.prompt, run_id="run-1", invocation_id="inv-1", scenario_id="promptfoo-compat-001",
                promptfoo_executable="/fake/promptfoo", worker_queue=queue,
                private_receipt_directory=Path(temp), call_ledger=ledger,
                env={"PATH":"/usr/bin", "HOME":"/tmp/home", "OTHER_SECRET":"drop-me",
                     "GPOINT_AGATA_WORKER_SUPPORT":str(support)}, eval_runner=fake_eval)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(observed["args"][1:3], ["eval", "--config"])
            self.assertTrue(observed["provider"].startswith("file://"))
            self.assertEqual(observed["timeout"], runner.EVAL_TIMEOUT_SECONDS)
            self.assertEqual(observed["env"]["AGATA_LAB_QUEUE"], str(queue))
            self.assertNotIn("OTHER_SECRET", observed["env"])
            # An ordinary Lab experiment is queue work, not a lifecycle operation.
            # No Worker status snapshot, lock, plist, or activation file is needed.
            self.assertFalse(support.exists())

    def test_ordinary_eval_leaves_launchagent_pid_and_runtime_sentinels_byte_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ledger = new_ledger(temp)
            support = root / "support"
            plist = root / "one.gpoint.agata-codex-worker.plist"
            pid = root / "worker.pid"
            runtime = root / "runtime/current/worker.py"
            for path, content in ((plist, b"plist-before"), (pid, b"74500"), (runtime, b"worker-before")):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            targets = (plist, pid, runtime)
            before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in targets}

            def fake_eval(_args, *, env, timeout_seconds, on_started, on_ambiguous):
                child = ledger.__class__(journal_path=ledger.journal_path)
                child.reserve("generator", "inv-safe", metadata={
                    "run_id": "run-safe", "arm_id": "promptfoo-worker-compat",
                    "experiment_id": "promptfoo-worker-compat", "scenario_id": "promptfoo-compat-001",
                    "turn_index": 0, "model": "gpt-6-luna", "reasoning_effort": "medium",
                })
                child.complete("generator", "inv-safe", "success")
                on_started()
                return SimpleNamespace(returncode=0, stdout="offline", stderr="")

            result = runner.run_promptfoo_worker_eval(
                self.prompt, run_id="run-safe", invocation_id="inv-safe", scenario_id="promptfoo-compat-001",
                promptfoo_executable="/fake/promptfoo", worker_queue=support / "queues/lab",
                private_receipt_directory=root / "receipts", call_ledger=ledger,
                env={"PATH": "/usr/bin", "GPOINT_AGATA_WORKER_SUPPORT": str(support)}, eval_runner=fake_eval,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in targets})
            self.assertFalse(support.exists())

    def test_runner_rejects_noncanonical_worker_queue_before_promptfoo(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = new_ledger(temp)
            support = Path(temp) / "support"
            with self.assertRaisesRegex(PermissionError, "canonical LAB queue"):
                runner.run_promptfoo_worker_eval(
                    self.prompt, run_id="run-1", invocation_id="inv-1", scenario_id="promptfoo-compat-001",
                    promptfoo_executable="/fake/promptfoo", worker_queue=Path(temp)/"other-lane",
                    private_receipt_directory=Path(temp), call_ledger=ledger,
                    env={"PATH":"/usr/bin", "GPOINT_AGATA_WORKER_SUPPORT":str(support)},
                    eval_runner=lambda *args, **kwargs: self.fail("noncanonical queue reached Promptfoo"),
                )

    def test_outer_timeout_exceeds_the_bounded_worker_wait_by_startup_margin(self):
        self.assertGreater(runner.EVAL_TIMEOUT_SECONDS, provider.MAX_WAIT_SECONDS + 30)

    def test_timeout_records_ambiguous_before_runner_terminates_and_never_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = new_ledger(temp)
            support = Path(temp) / "support"

            def timeout(args, *, env, timeout_seconds, on_started, on_ambiguous):
                ledger.reserve("generator", "inv-timeout", metadata={
                    "run_id":"run-timeout", "arm_id":"promptfoo-worker-compat",
                    "experiment_id":"promptfoo-worker-compat", "scenario_id":"promptfoo-compat-001",
                    "turn_index":0, "model":"gpt-6-luna", "reasoning_effort":"medium"})
                on_started()
                on_ambiguous()
                raise subprocess.TimeoutExpired(args, timeout_seconds)

            with self.assertRaises(subprocess.TimeoutExpired):
                runner.run_promptfoo_worker_eval(
                    self.prompt, run_id="run-timeout", invocation_id="inv-timeout", scenario_id="promptfoo-compat-001",
                    promptfoo_executable="/fake/promptfoo", worker_queue=support/"queues/lab",
                    private_receipt_directory=Path(temp), call_ledger=ledger,
                    env={"PATH":"/usr/bin", "GPOINT_AGATA_WORKER_SUPPORT":str(support)}, eval_runner=timeout)
            snapshot = ledger.snapshot()["generator_invocations"]
            self.assertEqual(snapshot["attempted"], 1)
            self.assertEqual(snapshot["outcomes"], {"ambiguous":1})

    def test_native_codex_sdk_route_is_disabled(self):
        with self.assertRaisesRegex(RuntimeError, "NATIVE_PROVIDER_UNAVAILABLE_CURRENT_ENV"):
            runner.run_promptfoo_eval("prompt", working_dir="/tmp", promptfoo_executable="/fake/promptfoo",
                call_ledger=object(), stage="generator", invocation_id="native", invocation_metadata={})

    def test_bounded_eval_runner_terminates_promptfoo_process_group_on_timeout(self):
        class HungProcess:
            pid = 4242
            calls = 0
            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("promptfoo", timeout)
                return "", ""
        process = HungProcess()
        order = []
        with patch.object(runner.subprocess, "Popen", return_value=process) as popen, \
             patch.object(runner, "_terminate_process_group", side_effect=lambda child: order.append("terminated")):
            with self.assertRaises(subprocess.TimeoutExpired):
                runner._run_eval_bounded(["promptfoo", "eval"], env={"PATH":"/bin"}, timeout_seconds=120,
                    on_ambiguous=lambda: order.append("ambiguous"))
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(order, ["ambiguous", "terminated"])

    def test_static_config_is_a_nonexecutable_python_provider_template(self):
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        provider = config["providers"][0]
        self.assertIn("promptfoo_worker_provider.py", provider["id"])
        self.assertEqual(provider["config"]["model"], "gpt-6-luna")
        self.assertEqual(provider["config"]["reasoning_effort"], "medium")
        self.assertEqual(provider["config"]["maxRetries"], 0)
        self.assertEqual(provider["config"]["fallbacks"], [])
        self.assertNotIn("prompts", config)
        self.assertNotIn("tests", config)


if __name__ == "__main__":
    unittest.main()
