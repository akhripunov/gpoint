"""Offline contracts for the LAB-only Promptfoo judge provider."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

LAB = Path(__file__).resolve().parents[1]
PATH = LAB / "evaluators" / "promptfoo_judge_provider.py"
SPEC = importlib.util.spec_from_file_location("agata_promptfoo_judge_provider", PATH) if PATH.exists() else None
provider = importlib.util.module_from_spec(SPEC) if SPEC else None
if SPEC and SPEC.loader:
    SPEC.loader.exec_module(provider)

INVOCATION_ID = "judge-" + hashlib.sha256(json.dumps(
    ["batch-a", "dev-clean-01", 1], separators=(",", ":")
).encode()).hexdigest()[:58]


class FakeLedger:
    durable = True
    journal_path = Path("/private/fake/ledger")

    def __init__(self, invocation_id=INVOCATION_ID):
        self.invocation_id = invocation_id
        self.events = []

    def verify_persisted(self):
        return True

    def snapshot(self):
        return {"limits": {"conversation_arms": 8, "generator_invocations": 32, "judge_invocations": 15},
                "judge_invocations": {"invocations": [{"invocation_id": self.invocation_id, "outcome": None}]}}

    def complete(self, stage, invocation_id, outcome):
        self.events.append((stage, invocation_id, outcome))


class FakeTransport:
    def __init__(self, receipt=None):
        self.receipt = receipt
        self.jobs = []

    def submit(self, job):
        self.jobs.append(job)
        return job["job_id"]

    def wait_terminal(self, job_id, timeout_seconds):
        return self.receipt

    def receipt_path(self, job_id):
        return Path("/private/fake") / (job_id + ".json")

    def receipt_sha256(self, job_id):
        return "a" * 64


def config():
    return {"model": "gpt-6-luna", "reasoning_effort": "medium", "maxRetries": 0,
            "fallbacks": [], "run_id": "batch-a", "batch_id": "batch-a",
            "invocation_id": INVOCATION_ID, "scenario_id": "dev-clean-01",
            "dataset_split": "DEV", "judge_slot": 1, "presented_labels": ["X", "Y"]}


class PromptfooJudgeProviderTests(unittest.TestCase):
    def require_provider(self):
        self.assertIsNotNone(provider, "LAB Promptfoo judge provider is not implemented")

    def test_invocation_uses_only_reserved_approved_lab_job_and_structured_worker_receipt(self):
        self.require_provider()
        from judges import CORE_IDS
        verdict = {"overall": "first", "core_dimensions": {key: "first" for key in CORE_IDS},
                   "conditional_evidence": {}, "reasons": ["The first is responsive."], "defects": [],
                   "uncertainty": False, "position_sensitivity": False}
        job = provider.worker_client.build_judge_job(
            "blind prompt", batch_id="batch-a", scenario_id="dev-clean-01", dataset_split="DEV",
            judge_slot=1, presented_labels=["X", "Y"],
        )
        receipt = {"job_id": job["job_id"], "status": "completed", "slot_id": "slot-03",
                   "identity": {"experiment_id": job["experiment_id"], "configuration_id": job["configuration_id"],
                                "scenario_id": job["scenario_id"], "turn_index": 0, "job_type": "judge",
                                "dataset_split": "DEV", "judge_slot": 1,
                                "promptfoo_invocation_id": INVOCATION_ID, "judge_schema_version": 1,
                                "presented_labels": ["X", "Y"]},
                   "execution": {"requested_model": "gpt-6-luna", "requested_reasoning_effort": "medium",
                                 "actual_model": "gpt-6-luna", "inference_started": True},
                   "judge_verdict": verdict}
        ledger, transport = FakeLedger(), FakeTransport(receipt)
        adapter = provider.LabJudgeAdapter(ledger=ledger, transport=transport, run_id="batch-a")
        with patch.object(provider.worker_client, "validate_owner_approval"):
            result = adapter.invoke("blind prompt", config=config())
        self.assertEqual(json.loads(result["output"]), verdict)
        self.assertEqual(len(transport.jobs), 1)
        self.assertEqual(transport.jobs[0]["origin"], "conversation-lab-judge")
        self.assertEqual(transport.jobs[0]["model"], "gpt-6-luna")
        self.assertEqual(ledger.events, [("judge", INVOCATION_ID, "success")])

    def test_missing_provider_model_identity_is_success_but_explicitly_unverified(self):
        self.require_provider()
        from judges import CORE_IDS
        verdict = {"overall": "first", "core_dimensions": {key: "first" for key in CORE_IDS},
                   "conditional_evidence": {}, "reasons": ["The first is responsive."], "defects": [],
                   "uncertainty": False, "position_sensitivity": False}
        job = provider.worker_client.build_judge_job(
            "blind prompt", batch_id="batch-a", scenario_id="dev-clean-01", dataset_split="DEV",
            judge_slot=1, presented_labels=["X", "Y"],
        )
        receipt = {"job_id": job["job_id"], "status": "completed", "slot_id": "slot-03",
                   "identity": {"experiment_id": job["experiment_id"], "configuration_id": job["configuration_id"],
                                "scenario_id": job["scenario_id"], "turn_index": 0, "job_type": "judge",
                                "dataset_split": "DEV", "judge_slot": 1,
                                "promptfoo_invocation_id": INVOCATION_ID, "judge_schema_version": 1,
                                "presented_labels": ["X", "Y"]},
                   "execution": {"requested_model": "gpt-6-luna", "requested_reasoning_effort": "medium",
                                 "actual_model": "gpt-6-sol", "inference_started": True},
                   "judge_verdict": verdict}
        with tempfile.TemporaryDirectory() as temporary:
            receipt_dir = Path(temporary)
            os.chmod(receipt_dir, 0o700)
            ledger, transport = FakeLedger(), FakeTransport(receipt)
            adapter = provider.LabJudgeAdapter(ledger=ledger, transport=transport, run_id="batch-a",
                                               receipt_directory=receipt_dir)
            with patch.object(provider.worker_client, "validate_owner_approval"):
                result = adapter.invoke("blind prompt", config=config())
            evidence = json.loads((receipt_dir / f"{INVOCATION_ID}.judge-receipt.json").read_text())
        self.assertEqual(json.loads(result["output"]), verdict)
        self.assertEqual(evidence["outcome"], "success")
        self.assertIsNone(evidence["actual_model"])
        self.assertIsNone(evidence["actual_model_source"])
        self.assertEqual(evidence["model_identity_status"], "MODEL_IDENTITY_UNVERIFIED")

    def test_explicit_worker_model_mismatch_fails_closed_and_is_recorded(self):
        self.require_provider()
        from judges import CORE_IDS
        verdict = {"overall": "first", "core_dimensions": {key: "first" for key in CORE_IDS},
                   "conditional_evidence": {}, "reasons": ["The first is responsive."], "defects": [],
                   "uncertainty": False, "position_sensitivity": False}
        job = provider.worker_client.build_judge_job(
            "blind prompt", batch_id="batch-a", scenario_id="dev-clean-01", dataset_split="DEV",
            judge_slot=1, presented_labels=["X", "Y"],
        )
        receipt = {"job_id": job["job_id"], "status": "completed", "slot_id": "slot-03",
                   "identity": {"experiment_id": job["experiment_id"], "configuration_id": job["configuration_id"],
                                "scenario_id": job["scenario_id"], "turn_index": 0, "job_type": "judge",
                                "dataset_split": "DEV", "judge_slot": 1,
                                "promptfoo_invocation_id": INVOCATION_ID, "judge_schema_version": 1,
                                "presented_labels": ["X", "Y"]},
                   "execution": {"requested_model": "gpt-6-luna", "requested_reasoning_effort": "medium",
                                 "actual_model": "gpt-6.1-sol",
                                 "actual_model_source": "codex_provider_response_metadata",
                                 "inference_started": True},
                   "judge_verdict": verdict}
        with tempfile.TemporaryDirectory() as temporary:
            receipt_dir = Path(temporary)
            os.chmod(receipt_dir, 0o700)
            ledger, transport = FakeLedger(), FakeTransport(receipt)
            adapter = provider.LabJudgeAdapter(ledger=ledger, transport=transport, run_id="batch-a",
                                               receipt_directory=receipt_dir)
            with patch.object(provider.worker_client, "validate_owner_approval"), self.assertRaises(
                provider.JudgeModelIdentityMismatch
            ):
                adapter.invoke("blind prompt", config=config())
            evidence = json.loads((receipt_dir / f"{INVOCATION_ID}.judge-receipt.json").read_text())
        self.assertEqual(ledger.events, [("judge", INVOCATION_ID, "provider_error")])
        self.assertEqual(evidence["outcome"], "provider_error")
        self.assertEqual(evidence["actual_model"], "gpt-6.1-sol")
        self.assertEqual(evidence["model_identity_status"], "MODEL_IDENTITY_MISMATCH")
        self.assertIsNone(evidence["normalized_judge_receipt"])

    def test_api_key_model_override_and_unreserved_invocations_fail_before_submission(self):
        self.require_provider()
        for env, overrides in (({"OPENAI_API_KEY": "ignored"}, {}), ({}, {"model": "gpt-6.1-sol"}),
                               ({}, {"reasoning_effort": "high"}), ({}, {"maxRetries": 1}),
                               ({}, {"fallbacks": ["other"]})):
            ledger, transport = FakeLedger(), FakeTransport()
            adapter = provider.LabJudgeAdapter(ledger=ledger, transport=transport, run_id="batch-a", env=env)
            with self.subTest(env=bool(env), overrides=overrides), patch.object(
                provider.worker_client, "validate_owner_approval"
            ), self.assertRaises((PermissionError, ValueError)):
                adapter.invoke("blind prompt", config={**config(), **overrides})
            self.assertEqual(transport.jobs, [])

        ledger, transport = FakeLedger("some-other-invocation"), FakeTransport()
        adapter = provider.LabJudgeAdapter(ledger=ledger, transport=transport, run_id="batch-a")
        with patch.object(provider.worker_client, "validate_owner_approval"), self.assertRaises(PermissionError):
            adapter.invoke("blind prompt", config=config())
        self.assertEqual(transport.jobs, [])

    def test_worker_mismatch_is_ambiguous_and_never_retried(self):
        self.require_provider()
        job = provider.worker_client.build_judge_job("blind prompt", batch_id="batch-a", scenario_id="dev-clean-01",
            dataset_split="DEV", judge_slot=1, presented_labels=["X", "Y"])
        transport = FakeTransport({"job_id": job["job_id"], "status": "ambiguous"})
        ledger = FakeLedger()
        adapter = provider.LabJudgeAdapter(ledger=ledger, transport=transport, run_id="batch-a")
        with patch.object(provider.worker_client, "validate_owner_approval"), self.assertRaises(provider.AmbiguousJudgeResult):
            adapter.invoke("blind prompt", config=config())
        self.assertEqual(len(transport.jobs), 1)
        self.assertEqual(ledger.events, [("judge", INVOCATION_ID, "ambiguous")])


if __name__ == "__main__":
    unittest.main()
