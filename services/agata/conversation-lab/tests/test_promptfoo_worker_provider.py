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
        if isinstance(self.receipt, dict) and self.receipt.get("job_id") == "job-1":
            self.receipt["job_id"] = job["job_id"]
        return job["job_id"]

    def wait_terminal(self, job_id, timeout_seconds):
        self.events.append(("wait", job_id, timeout_seconds))
        return self.receipt

    def receipt_sha256(self, job_id):
        return "a" * 64


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

    def test_v2_approval_failure_writes_stage_reason_without_reservation_or_job(self):
        self.require_provider()
        with tempfile.TemporaryDirectory() as temp:
            receipts = Path(temp); receipts.chmod(0o700)
            ledger, transport = FakeLedger(), FakeTransport()
            adapter = provider.WorkerLabAdapter(
                ledger=ledger, transport=transport, run_id="run-v2",
                receipt_directory=receipts, env={"AGATA_LAB_PRIVATE_RECEIPTS": str(receipts)},
            )
            job = {"job_id": "job-v2", "experiment_id": "agata-v2-naturalness-dev"}
            with patch.object(provider.worker_client, "build_v2_dev_job", return_value=job), \
                 patch.object(provider.worker_client, "validate_owner_approval",
                              side_effect=PermissionError("approval missing; private token must not be copied")), \
                 self.assertRaisesRegex(PermissionError, "approval missing"):
                adapter.invoke_v2_dev(
                    {"user_message": "synthetic"}, invocation_id="inv-v2-approval", run_id="run-v2",
                    arm_id="run-v2-baseline", configuration_id="baseline", scenario_id="dev-sample",
                    turn_index=0, history_receipt_ids=[],
                )
            receipt = receipts / "inv-v2-approval.preinference-diagnostic.json"
            payload = json.loads(receipt.read_text())
            self.assertEqual(payload["stage"], "APPROVAL_VALIDATION")
            self.assertEqual(payload["reason_code"], "OWNER_APPROVAL_REJECTED")
            self.assertEqual(payload["exception_class"], "PermissionError")
            self.assertFalse(payload["generator_reservation_created"])
            self.assertFalse(payload["worker_job_submitted"])
            self.assertNotIn("private token", receipt.read_text())
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertEqual(ledger.events, [])
            self.assertEqual(transport.events, [])

    def test_v2_immutable_job_contract_failure_has_private_reason_receipt(self):
        self.require_provider()
        with tempfile.TemporaryDirectory() as temp:
            receipts = Path(temp); receipts.chmod(0o700)
            ledger, transport = FakeLedger(), FakeTransport()
            adapter = provider.WorkerLabAdapter(
                ledger=ledger, transport=transport, run_id="run-v2",
                receipt_directory=receipts, env={"AGATA_LAB_PRIVATE_RECEIPTS": str(receipts)},
            )
            with patch.object(provider.worker_client, "build_v2_dev_job",
                              side_effect=PermissionError("immutable request rejected")), \
                 self.assertRaisesRegex(PermissionError, "immutable request"):
                adapter.invoke_v2_dev(
                    {}, invocation_id="inv-v2-job", run_id="run-v2", arm_id="run-v2-baseline",
                    configuration_id="baseline", scenario_id="dev-sample", turn_index=0,
                    history_receipt_ids=[],
                )
            payload = json.loads((receipts / "inv-v2-job.preinference-diagnostic.json").read_text())
            self.assertEqual((payload["stage"], payload["reason_code"]),
                             ("IMMUTABLE_V2_JOB_CONTRACT", "V2_JOB_CONTRACT_INVALID"))
            self.assertFalse(payload["generator_reservation_created"])
            self.assertFalse(payload["worker_job_submitted"])
            self.assertEqual(ledger.events, [])
            self.assertEqual(transport.events, [])

    def test_v2_generator_reservation_failure_is_receipted_before_worker_submission(self):
        self.require_provider()
        class RefusingLedger(FakeLedger):
            def reserve(self, stage, invocation_id, *, metadata):
                raise PermissionError("private reservation failure")
        with tempfile.TemporaryDirectory() as temp:
            receipts = Path(temp); receipts.chmod(0o700)
            ledger, transport = RefusingLedger(), FakeTransport()
            adapter = provider.WorkerLabAdapter(
                ledger=ledger, transport=transport, run_id="run-v2",
                receipt_directory=receipts, env={"AGATA_LAB_PRIVATE_RECEIPTS": str(receipts)},
            )
            job = {
                "job_id": "job-v2", "experiment_id": "agata-v2-naturalness-dev",
                "configuration_id": "baseline", "scenario_id": "dev-sample", "turn_index": 0,
                "run_id": "run-v2", "arm_id": "run-v2-baseline",
            }
            with patch.object(provider.worker_client, "build_v2_dev_job", return_value=job), \
                 self.assertRaisesRegex(PermissionError, "private reservation"):
                adapter.invoke_v2_dev(
                    {"user_message": "synthetic"}, invocation_id="inv-v2-reserve", run_id="run-v2",
                    arm_id="run-v2-baseline", configuration_id="baseline", scenario_id="dev-sample",
                    turn_index=0, history_receipt_ids=[],
                )
            payload = json.loads((receipts / "inv-v2-reserve.preinference-diagnostic.json").read_text())
            self.assertEqual((payload["stage"], payload["reason_code"]),
                             ("GENERATOR_RESERVATION", "GENERATOR_RESERVATION_FAILED"))
            self.assertIsNone(payload["generator_reservation_created"])
            self.assertFalse(payload["worker_job_submitted"])
            self.assertEqual(transport.events, [])

    def test_v2_missing_actual_model_succeeds_but_marks_identity_unverified(self):
        self.require_provider()
        with tempfile.TemporaryDirectory() as temp:
            receipts = Path(temp); receipts.chmod(0o700)
            ledger = FakeLedger()
            job = {
                "job_id": "job-v2", "experiment_id": "agata-v2-naturalness-dev",
                "configuration_id": "baseline", "scenario_id": "dev-sample", "turn_index": 0,
                "run_id": "run-v2", "arm_id": "run-v2-baseline",
                "promptfoo_invocation_id": "inv-v2-model-missing", "dataset_split": "DEV",
                "source_scenario_sha256": "a" * 64,
            }
            identity = {key: job[key] for key in (
                "experiment_id", "configuration_id", "scenario_id", "turn_index", "run_id",
                "arm_id", "promptfoo_invocation_id", "dataset_split", "source_scenario_sha256")}
            transport = FakeTransport({
                "job_id": "job-v2", "status": "completed", "response": "synthetic response",
                "slot_id": "slot-03", "identity": identity,
                "execution": {"requested_model": "gpt-6-luna", "requested_reasoning_effort": "medium",
                              "actual_model": None, "inference_started": True},
            }, ledger=ledger)
            adapter = provider.WorkerLabAdapter(
                ledger=ledger, transport=transport, run_id="run-v2",
                receipt_directory=receipts, env={"AGATA_LAB_PRIVATE_RECEIPTS": str(receipts)},
            )
            with patch.object(provider.worker_client, "build_v2_dev_job", return_value=job):
                result = adapter.invoke_v2_dev(
                    {"user_message": "synthetic"}, invocation_id="inv-v2-model-missing", run_id="run-v2",
                    arm_id="run-v2-baseline", configuration_id="baseline", scenario_id="dev-sample",
                    turn_index=0, history_receipt_ids=[],
                )
            payload = json.loads((receipts / "inv-v2-model-missing.worker-receipt.json").read_text())
            self.assertEqual(payload["worker_terminal_status"], "completed")
            self.assertEqual(payload["promptfoo_outcome"], "success")
            self.assertEqual(payload["model_identity_status"], "MODEL_IDENTITY_UNVERIFIED")
            self.assertIsNone(payload["actual_model"])
            self.assertIsNotNone(payload["output_sha256"])
            self.assertEqual(result, {"output": "synthetic response"})
            self.assertEqual(ledger.events[-1],
                             ("complete", "generator", "inv-v2-model-missing", "success"))
            self.assertEqual(sum(event[0] == "submit" for event in transport.events), 1)

    def test_v2_explicit_model_mismatch_stops_and_records_provider_error(self):
        self.require_provider()
        with tempfile.TemporaryDirectory() as temp:
            receipts = Path(temp); receipts.chmod(0o700)
            ledger = FakeLedger()
            job = {
                "job_id": "job-v2", "experiment_id": "agata-v2-naturalness-dev",
                "configuration_id": "baseline", "scenario_id": "dev-sample", "turn_index": 0,
                "run_id": "run-v2", "arm_id": "run-v2-baseline",
                "promptfoo_invocation_id": "inv-v2-model-mismatch", "dataset_split": "DEV",
                "source_scenario_sha256": "a" * 64,
            }
            identity = {key: job[key] for key in (
                "experiment_id", "configuration_id", "scenario_id", "turn_index", "run_id",
                "arm_id", "promptfoo_invocation_id", "dataset_split", "source_scenario_sha256")}
            transport = FakeTransport({
                "job_id": "job-v2", "status": "completed", "response": "must not escape",
                "slot_id": "slot-03", "identity": identity,
                "execution": {"requested_model": "gpt-6-luna", "requested_reasoning_effort": "medium",
                              "actual_model": "gpt-6-sol", "actual_model_source": "codex_provider_response_metadata",
                              "inference_started": True},
            }, ledger=ledger)
            adapter = provider.WorkerLabAdapter(
                ledger=ledger, transport=transport, run_id="run-v2",
                receipt_directory=receipts, env={"AGATA_LAB_PRIVATE_RECEIPTS": str(receipts)},
            )
            with patch.object(provider.worker_client, "build_v2_dev_job", return_value=job), \
                 self.assertRaises(provider.ModelIdentityMismatch):
                adapter.invoke_v2_dev(
                    {"user_message": "synthetic"}, invocation_id="inv-v2-model-mismatch", run_id="run-v2",
                    arm_id="run-v2-baseline", configuration_id="baseline", scenario_id="dev-sample",
                    turn_index=0, history_receipt_ids=[],
                )
            payload = json.loads((receipts / "inv-v2-model-mismatch.worker-receipt.json").read_text())
            self.assertEqual(payload["worker_terminal_status"], "completed")
            self.assertEqual(payload["promptfoo_outcome"], "provider_error")
            self.assertEqual(payload["model_identity_status"], "MODEL_IDENTITY_MISMATCH")
            self.assertEqual(payload["actual_model"], "gpt-6-sol")
            self.assertIsNone(payload["output_sha256"])
            self.assertEqual(ledger.events[-1],
                             ("complete", "generator", "inv-v2-model-mismatch", "provider_error"))
            self.assertEqual(sum(event[0] == "submit" for event in transport.events), 1)

    def test_unattributed_worker_model_field_is_unverified_not_provider_identity(self):
        self.require_provider()
        with tempfile.TemporaryDirectory() as temp:
            receipts = Path(temp); receipts.chmod(0o700)
            ledger = FakeLedger()
            job = {
                "job_id": "job-v2", "experiment_id": "agata-v2-naturalness-dev",
                "configuration_id": "baseline", "scenario_id": "dev-sample", "turn_index": 0,
                "run_id": "run-v2", "arm_id": "run-v2-baseline",
                "promptfoo_invocation_id": "inv-v2-unattributed-model", "dataset_split": "DEV",
                "source_scenario_sha256": "a" * 64,
            }
            identity = {key: job[key] for key in (
                "experiment_id", "configuration_id", "scenario_id", "turn_index", "run_id",
                "arm_id", "promptfoo_invocation_id", "dataset_split", "source_scenario_sha256")}
            transport = FakeTransport({
                "job_id": "job-v2", "status": "completed", "response": "synthetic response",
                "slot_id": "slot-03", "identity": identity,
                "execution": {"requested_model": "gpt-6-luna", "requested_reasoning_effort": "medium",
                              "actual_model": "gpt-6-sol", "inference_started": True},
            }, ledger=ledger)
            adapter = provider.WorkerLabAdapter(
                ledger=ledger, transport=transport, run_id="run-v2",
                receipt_directory=receipts, env={"AGATA_LAB_PRIVATE_RECEIPTS": str(receipts)},
            )
            with patch.object(provider.worker_client, "build_v2_dev_job", return_value=job):
                result = adapter.invoke_v2_dev(
                    {"user_message": "synthetic"}, invocation_id="inv-v2-unattributed-model", run_id="run-v2",
                    arm_id="run-v2-baseline", configuration_id="baseline", scenario_id="dev-sample",
                    turn_index=0, history_receipt_ids=[],
                )
            payload = json.loads((receipts / "inv-v2-unattributed-model.worker-receipt.json").read_text())
            self.assertEqual(payload["model_identity_status"], "MODEL_IDENTITY_UNVERIFIED")
            self.assertIsNone(payload["actual_model"])
            self.assertEqual(result, {"output": "synthetic response"})

    def test_v2_missing_receipt_wrong_identity_and_failed_or_ambiguous_inference_fail_closed(self):
        self.require_provider()
        receipt_variants = ("missing", "wrong_identity", "failed_after_inference", "ambiguous")
        for variant in receipt_variants:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temp:
                receipts = Path(temp); receipts.chmod(0o700)
                ledger = FakeLedger()
                invocation_id = f"inv-v2-{variant}"
                job = {
                    "job_id": "job-v2", "experiment_id": "agata-v2-naturalness-dev",
                    "configuration_id": "baseline", "scenario_id": "dev-sample", "turn_index": 0,
                    "run_id": "run-v2", "arm_id": "run-v2-baseline",
                    "promptfoo_invocation_id": invocation_id, "dataset_split": "DEV",
                    "source_scenario_sha256": "a" * 64,
                }
                identity = {key: job[key] for key in (
                    "experiment_id", "configuration_id", "scenario_id", "turn_index", "run_id",
                    "arm_id", "promptfoo_invocation_id", "dataset_split", "source_scenario_sha256")}
                if variant == "wrong_identity":
                    identity["arm_id"] = "some-other-arm"
                if variant == "missing":
                    receipt = None
                elif variant == "ambiguous":
                    receipt = {"job_id": "job-v2", "status": "ambiguous", "slot_id": "slot-03",
                               "identity": identity, "execution": {"inference_started": True}}
                elif variant == "failed_after_inference":
                    receipt = {"job_id": "job-v2", "status": "failed", "slot_id": "slot-03",
                               "identity": identity, "execution": {"inference_started": True}}
                else:
                    receipt = {"job_id": "job-v2", "status": "completed", "response": "text",
                               "slot_id": "slot-03", "identity": identity,
                               "execution": {"requested_model": "gpt-6-luna",
                                             "requested_reasoning_effort": "medium",
                                             "actual_model": None, "inference_started": True}}
                transport = FakeTransport(receipt, ledger=ledger)
                transport.receipt = receipt
                adapter = provider.WorkerLabAdapter(
                    ledger=ledger, transport=transport, run_id="run-v2",
                    receipt_directory=receipts, env={"AGATA_LAB_PRIVATE_RECEIPTS": str(receipts)},
                )
                with patch.object(provider.worker_client, "build_v2_dev_job", return_value=job), \
                     self.assertRaises(provider.AmbiguousWorkerResult):
                    adapter.invoke_v2_dev(
                        {"user_message": "synthetic"}, invocation_id=invocation_id, run_id="run-v2",
                        arm_id="run-v2-baseline", configuration_id="baseline", scenario_id="dev-sample",
                        turn_index=0, history_receipt_ids=[],
                    )
                self.assertEqual(sum(event[0] == "submit" for event in transport.events), 1)
                self.assertEqual(ledger.events[-1], ("complete", "generator", invocation_id, "ambiguous"))
                payload = json.loads((receipts / f"{invocation_id}.worker-receipt.json").read_text())
                self.assertEqual(payload["promptfoo_outcome"], "ambiguous")

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
