"""Offline contract tests for Promptfoo -> AGATA Worker LAB adapter."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


LAB = Path(__file__).resolve().parents[1]
PROVIDER_PATH = LAB / "evaluators" / "promptfoo_worker_provider.py"
SPEC = importlib.util.spec_from_file_location("promptfoo_worker_provider", PROVIDER_PATH) if PROVIDER_PATH.exists() else None
provider = importlib.util.module_from_spec(SPEC) if SPEC else None
if SPEC and SPEC.loader:
    SPEC.loader.exec_module(provider)


class FakeLedger:
    durable = True

    def __init__(self):
        self.events = []

    def verify_persisted(self):
        return True

    def snapshot(self):
        return {"limits": {"conversation_arms": 8, "generator_invocations": 32, "judge_invocations": 15}}

    def reserve(self, stage, invocation_id, *, metadata):
        self.events.append(("reserve", stage, invocation_id, metadata))

    def complete(self, stage, invocation_id, outcome):
        self.events.append(("complete", stage, invocation_id, outcome))


class FakeTransport:
    def __init__(self, receipt=None, ledger=None):
        self.events = []
        self.ledger = ledger
        self.receipt = receipt or {
            "job_id": "job-1", "status": "completed", "response": "Привет.",
            "identity": {"experiment_id": "promptfoo-worker-compat", "configuration_id": "compat",
                         "scenario_id": "promptfoo-compat-001", "turn_index": 0},
            "execution": {"requested_model": "gpt-6-luna", "requested_reasoning_effort": "medium"},
        }

    def submit(self, job):
        if self.ledger is not None:
            self.ledger.snapshot()
            assert any(event[0] == "reserve" for event in self.ledger.events)
        self.events.append(("submit", job))
        if self.receipt.get("job_id") == "job-1":
            self.receipt["job_id"] = job["job_id"]
        return job["job_id"]

    def wait_terminal(self, job_id, timeout_seconds):
        self.events.append(("wait", job_id, timeout_seconds))
        return self.receipt


class PromptfooWorkerProviderTests(unittest.TestCase):
    def setUp(self):
        approval = patch.object(provider.worker_client, "validate_owner_approval", return_value=None)
        approval.start()
        self.addCleanup(approval.stop)

    def require_provider(self):
        self.assertIsNotNone(provider, "Worker LAB Promptfoo adapter is not implemented")

    def test_promptfoo_invocation_reserves_then_submits_only_lab_job_and_returns_receipt(self):
        self.require_provider()
        ledger, transport = FakeLedger(), FakeTransport(ledger=None)
        transport.ledger = ledger
        adapter = provider.WorkerLabAdapter(ledger=ledger, transport=transport, run_id="run-1")
        result = adapter.invoke("Привет! Что тебе обычно нравится делать в спокойный выходной?", invocation_id="inv-1", scenario_id="promptfoo-compat-001")
        self.assertEqual(result, {"output": "Привет."})
        self.assertEqual([event[0] for event in ledger.events + transport.events].count("reserve"), 1)
        job = transport.events[0][1]
        self.assertEqual(job["origin"], "conversation-lab")
        self.assertNotIn("target", job)
        self.assertEqual(job["model"], "gpt-6-luna")
        self.assertEqual(job["reasoning_effort"], "medium")
        self.assertEqual(result["output"], transport.receipt["response"])
        self.assertEqual(ledger.events[-1], ("complete", "generator", "inv-1", "success"))

    def test_model_or_reasoning_override_fails_closed_before_ledger_or_submission(self):
        self.require_provider()
        for kwargs in ({"model": "gpt-6-sol"}, {"reasoning": "high"}):
            ledger, transport = FakeLedger(), FakeTransport()
            adapter = provider.WorkerLabAdapter(ledger=ledger, transport=transport, run_id="run-1")
            with self.subTest(kwargs=kwargs), self.assertRaises(PermissionError):
                adapter.invoke("Привет! Что тебе обычно нравится делать в спокойный выходной?", invocation_id="inv-1", scenario_id="promptfoo-compat-001", **kwargs)
            self.assertEqual(ledger.events, [])
            self.assertEqual(transport.events, [])

    def test_ambiguous_worker_result_is_terminal_and_never_resubmitted(self):
        self.require_provider()
        ledger = FakeLedger()
        transport = FakeTransport({"job_id": "job-1", "status": "ambiguous", "identity": {
            "experiment_id": "promptfoo-worker-compat", "configuration_id": "compat",
            "scenario_id": "promptfoo-compat-001", "turn_index": 0}})
        adapter = provider.WorkerLabAdapter(ledger=ledger, transport=transport, run_id="run-1")
        with self.assertRaises(provider.AmbiguousWorkerResult):
            adapter.invoke("Привет! Что тебе обычно нравится делать в спокойный выходной?", invocation_id="inv-1", scenario_id="promptfoo-compat-001")
        self.assertEqual(sum(event[0] == "submit" for event in transport.events), 1)
        self.assertEqual(ledger.events[-1], ("complete", "generator", "inv-1", "ambiguous"))

    def test_duplicate_invocation_is_reserved_once_and_not_resubmitted(self):
        self.require_provider()
        class DuplicateLedger(FakeLedger):
            def reserve(self, stage, invocation_id, *, metadata):
                raise OverflowError("call ledger duplicate")
        ledger, transport = DuplicateLedger(), FakeTransport()
        adapter = provider.WorkerLabAdapter(ledger=ledger, transport=transport, run_id="run-1")
        with self.assertRaises(OverflowError):
            adapter.invoke("Привет! Что тебе обычно нравится делать в спокойный выходной?", invocation_id="inv-1", scenario_id="promptfoo-compat-001")
        self.assertEqual(transport.events, [])

    def test_receipt_links_worker_job_and_promptfoo_invocation(self):
        self.require_provider()
        ledger, transport = FakeLedger(), FakeTransport()
        adapter = provider.WorkerLabAdapter(ledger=ledger, transport=transport, run_id="run-link")
        adapter.invoke("Привет! Что тебе обычно нравится делать в спокойный выходной?", invocation_id="promptfoo-invocation", scenario_id="promptfoo-compat-001")
        attempt = ledger.events[0][3]
        self.assertEqual(ledger.events[-1][3], "success")
        self.assertEqual(attempt["worker_job_id"], transport.events[0][1]["job_id"])
        self.assertEqual(attempt["run_id"], "run-link")

    def test_local_transport_has_only_the_lab_queue_capability(self):
        self.require_provider()
        with tempfile.TemporaryDirectory() as temp:
            support = Path(temp) / "support"
            queue_root = support / "queues" / "lab"
            with patch.dict(provider.os.environ, {"GPOINT_AGATA_WORKER_SUPPORT":str(support)}, clear=True):
                transport = provider.LocalLabTransport(queue_root)
                job = provider.worker_client.build_promptfoo_compat_job(invocation_id="local-transport")
                self.assertEqual(transport.submit(job), job["job_id"])
                pending = list((queue_root / "pending").glob("*.json"))
                self.assertEqual(len(pending), 1)
                stored = json.loads(pending[0].read_text(encoding="utf-8"))
                self.assertEqual(stored["experiment_id"], "promptfoo-worker-compat")
                self.assertNotIn("target", stored)

    def test_local_transport_rejects_noncanonical_lane_queue(self):
        self.require_provider()
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            provider.os.environ,
            {"GPOINT_AGATA_WORKER_SUPPORT":str(Path(temp)/"support")},
            clear=True,
        ):
            with self.assertRaisesRegex(PermissionError, "canonical LAB queue"):
                provider.LocalLabTransport(Path(temp)/"support/queues/gpoint")

    def test_api_key_environment_fails_closed_before_reservation(self):
        self.require_provider()
        ledger, transport = FakeLedger(), FakeTransport()
        adapter = provider.WorkerLabAdapter(ledger=ledger, transport=transport, run_id="run-env", env={"OPENAI_API_KEY": "redacted"})
        with self.assertRaises(PermissionError):
            adapter.invoke("Привет! Что тебе обычно нравится делать в спокойный выходной?", invocation_id="inv-env", scenario_id="promptfoo-compat-001")
        self.assertEqual(ledger.events, [])
        self.assertEqual(transport.events, [])

    def test_missing_owner_approval_fails_before_durable_generator_reservation(self):
        self.require_provider()
        ledger, transport = FakeLedger(), FakeTransport()
        adapter = provider.WorkerLabAdapter(ledger=ledger, transport=transport, run_id="run-no-approval")
        with patch.object(provider.worker_client, "validate_owner_approval",
                          side_effect=PermissionError("approval missing")):
            with self.assertRaisesRegex(PermissionError, "approval missing"):
                adapter.invoke("Привет! Что тебе обычно нравится делать в спокойный выходной?",
                               invocation_id="inv-no-approval", scenario_id="promptfoo-compat-001")
        self.assertEqual(ledger.events, [])
        self.assertEqual(transport.events, [])

    def test_failed_receipt_proven_before_inference_is_provider_error_once(self):
        self.require_provider()
        ledger = FakeLedger()
        transport = FakeTransport({
            "job_id": "job-1", "status": "failed", "error_code": "lab_approval_required",
            "identity": {"experiment_id":"promptfoo-worker-compat", "configuration_id":"compat",
                         "scenario_id":"promptfoo-compat-001", "turn_index":0},
        }, ledger=ledger)
        adapter = provider.WorkerLabAdapter(ledger=ledger, transport=transport, run_id="run-error")
        with self.assertRaisesRegex(RuntimeError, "rejected the LAB job before inference"):
            adapter.invoke("Привет! Что тебе обычно нравится делать в спокойный выходной?",
                           invocation_id="inv-error", scenario_id="promptfoo-compat-001")
        completions = [event for event in ledger.events if event[0] == "complete"]
        self.assertEqual(completions, [("complete", "generator", "inv-error", "provider_error")])

    def test_private_receipt_links_terminal_worker_proof_and_promptfoo_invocation(self):
        self.require_provider()
        with tempfile.TemporaryDirectory() as temp:
            private = Path(temp)
            private.chmod(0o700)
            ledger = FakeLedger()
            transport = FakeTransport(ledger=ledger)
            adapter = provider.WorkerLabAdapter(ledger=ledger, transport=transport, run_id="run-link",
                                                receipt_directory=private)
            adapter.invoke("Привет! Что тебе обычно нравится делать в спокойный выходной?",
                           invocation_id="inv-private", scenario_id="promptfoo-compat-001")
            evidence = json.loads((private / "inv-private.worker-receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["promptfoo_invocation_id"], "inv-private")
            self.assertEqual(evidence["lab_ledger_attempt_id"], "inv-private")
            self.assertEqual(evidence["worker_job_id"], transport.events[0][1]["job_id"])
            self.assertRegex(evidence["worker_receipt_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(evidence["promptfoo_outcome"], "success")
            self.assertEqual((private / "inv-private.worker-receipt.json").stat().st_mode & 0o777, 0o600)

    def test_promptfoo_entrypoint_builds_only_the_worker_adapter(self):
        self.require_provider()
        class FakeAdapter:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            def invoke(self, prompt, **kwargs):
                return {"output": "Привет.", "adapter": type(self.kwargs["transport"]).__name__}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ledger").mkdir(mode=0o700)
            config = {"model":"gpt-6-luna", "reasoning_effort":"medium", "maxRetries":0, "scenario_id":"promptfoo-compat-001",
                      "run_id":"run-entry", "invocation_id":"inv-entry"}
            env = {"AGATA_LAB_CALL_LEDGER":str(root / "ledger/attempts.jsonl"),
                   "AGATA_LAB_QUEUE":str(root / "support/queues/lab"), "AGATA_LAB_PRIVATE_RECEIPTS":str(root / "ledger"),
                   "GPOINT_AGATA_WORKER_SUPPORT":str(root / "support"),
                   "PATH":"/usr/bin", "HOME":temp}
            with patch.dict(provider.os.environ, env, clear=True), \
                 patch.object(provider, "LocalLabTransport", return_value=object()), \
                 patch.object(provider, "WorkerLabAdapter", FakeAdapter):
                result = provider.call_api("fixture", {"config":config}, {"vars":{}})
        self.assertEqual(result["output"], "Привет.")
        self.assertEqual(result["adapter"], "object")

    def test_promptfoo_entrypoint_rejects_retry_or_fallback_configuration(self):
        self.require_provider()
        for changed in ({"maxRetries":1}, {"fallbacks":["openai:codex-sdk"]}):
            config = {"model":"gpt-6-luna", "reasoning_effort":"medium", "scenario_id":"promptfoo-compat-001",
                      "run_id":"run-guard", "invocation_id":"inv-guard", **changed}
            with self.subTest(changed=changed), self.assertRaises(PermissionError):
                provider.call_api("fixture", {"config":config}, {"vars":{}})

    def test_nonterminal_or_mismatched_receipt_fails_closed(self):
        self.require_provider()
        for receipt in (
            {"job_id": "job-1", "status": "failed", "identity": {}},
            {"job_id": "other", "status": "completed", "response": "x", "identity": {}},
        ):
            ledger, transport = FakeLedger(), FakeTransport(receipt)
            adapter = provider.WorkerLabAdapter(ledger=ledger, transport=transport, run_id="run-1")
            with self.assertRaises(Exception):
                adapter.invoke("hello", invocation_id="inv-1", scenario_id="promptfoo-compat-001")


if __name__ == "__main__":
    unittest.main()
