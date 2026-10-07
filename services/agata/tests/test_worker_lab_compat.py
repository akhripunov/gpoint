"""Offline regressions for the isolated Promptfoo technical fixture on LAB."""
from __future__ import annotations

import importlib.util
import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
# The repo's supported system Python on older macOS releases lacks datetime.UTC.
if not hasattr(datetime, "UTC"):
    datetime.UTC = datetime.timezone.utc
WORKER_DIR = ROOT / "worker"
LAB_DIR = ROOT / "conversation-lab"
sys.path.insert(0, str(WORKER_DIR))
sys.path.insert(0, str(LAB_DIR / "evaluators"))
import lab_queue  # noqa: E402
import worker_client  # noqa: E402
import gpoint_mac_worker as worker  # noqa: E402


class WorkerLabCompatibilityTests(unittest.TestCase):
    def test_compatibility_fixture_is_separate_synthetic_one_turn_exact_model_job(self):
        job = worker_client.build_promptfoo_compat_job(invocation_id="invocation-1")
        self.assertEqual(job["experiment_id"], "promptfoo-worker-compat")
        self.assertEqual(job["configuration_id"], "compat")
        self.assertEqual(job["scenario_id"], "promptfoo-compat-001")
        self.assertEqual(job["turn_index"], 0)
        self.assertEqual(job["data_classification"], "synthetic")
        self.assertEqual(job["model"], "gpt-6-luna")
        self.assertEqual(job["reasoning_effort"], "medium")
        self.assertEqual(job["history_receipt_ids"], [])
        self.assertEqual(job["request"]["history"], [])
        self.assertEqual(lab_queue.pending_count(Path(tempfile.mkdtemp()) / "queues/lab"), 0)

    def test_worker_queue_accepts_compat_fixture_and_rejects_model_mutations(self):
        job = worker_client.build_promptfoo_compat_job(invocation_id="invocation-queue")
        with tempfile.TemporaryDirectory() as temp:
            queue = Path(temp) / "queues/lab"
            lab_queue.submit(job, root=queue)
            self.assertEqual(lab_queue.claim_next(queue)["job_id"], job["job_id"])
            for field, invalid in (("model", "gpt-6-sol"), ("reasoning_effort", "high")):
                mutated = dict(job, job_id=f"invalid-{field}", **{field: invalid})
                with self.subTest(field=field), self.assertRaises(lab_queue.LabQueueError):
                    lab_queue.submit(mutated, root=queue)

    def test_lab_model_override_is_scoped_and_does_not_change_production_lanes(self):
        resolver = getattr(worker, "resolve_codex_model_for_job", None)
        self.assertTrue(callable(resolver), "Worker has no per-job LAB model contract")
        self.assertEqual(resolver({"target": "LAB", "experiment_id": "promptfoo-worker-compat", "model": "gpt-6-luna", "reasoning_effort": "medium"}, {}),
                         ("gpt-6-luna", "medium"))
        for target in ("GPOINT", "GSALES"):
            self.assertEqual(resolver({"target": target, "model": "gpt-6-sol", "reasoning_effort": "high"},
                                      {"AGATA_CODEX_MODEL": "gpt-6-luna", "AGATA_CODEX_REASONING": "medium"}),
                             ("gpt-6-luna", "medium"))

    def test_lab_override_rejects_any_noncanonical_model_pair_before_inference(self):
        resolver = getattr(worker, "resolve_codex_model_for_job", None)
        self.assertTrue(callable(resolver), "Worker has no per-job LAB model contract")
        for model, reasoning in (("gpt-6-sol", "medium"), ("gpt-6-luna", "high"), (None, "medium")):
            with self.subTest(model=model, reasoning=reasoning), self.assertRaises(worker.FatalConfigurationError):
                resolver({"target": "LAB", "experiment_id": "promptfoo-worker-compat", "model": model, "reasoning_effort": reasoning}, {})

    def test_lab_rejects_api_key_auth_environment_before_inference(self):
        resolver = worker.resolve_codex_model_for_job
        job = {"target":"LAB", "experiment_id":"promptfoo-worker-compat",
               "model":"gpt-6-luna", "reasoning_effort":"medium"}
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OTHER_PROVIDER_API_KEY"):
            with self.subTest(key=key), self.assertRaisesRegex(worker.FatalConfigurationError, "lab_api_key_auth_forbidden"):
                resolver(job, {key:"redacted"})

    def test_worker_executor_passes_exact_lab_model_to_codex_and_receipts_it(self):
        job = worker_client.build_promptfoo_compat_job(invocation_id="executor-model")
        with tempfile.TemporaryDirectory() as temp:
            support = Path(temp)
            approval_file = support / "approval.json"
            approval_file.write_text(json.dumps({
                "approval_id":"offline-approval", "worker_bundle_sha256":worker._worker_bundle_sha256(),
                "experiment_ids":["promptfoo-worker-compat"], "configuration_ids":["compat"],
                "scenario_ids":["promptfoo-compat-001"], "max_calls":1,
                "expires_at":(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5)).isoformat(),
            }), encoding="utf-8")
            approval_file.chmod(0o600)
            observed = {}
            def fake_inference(inference_job, **kwargs):
                observed.update(inference_job)
                kwargs["metadata_out"].update({"inference_started":True, "actual_model":"gpt-6-luna"})
                return "synthetic response"
            with patch.object(worker, "SUPPORT", support), \
                 patch.object(worker, "write_status"), \
                 patch.object(worker, "run_inference", side_effect=fake_inference), \
                 patch.dict(worker.os.environ, {"AGATA_LAB_WORKER_ENABLED":"true", "AGATA_LAB_APPROVAL_FILE":str(approval_file)}, clear=False):
                self.assertTrue(worker.execute_lab_claimed_job(job))
            self.assertEqual(observed["target"], "LAB")
            self.assertEqual(observed["experiment_id"], "promptfoo-worker-compat")
            self.assertEqual((observed["model"], observed["reasoning_effort"]), ("gpt-6-luna", "medium"))
            receipt = lab_queue.read_receipt(support / "queues/lab", job["job_id"])
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(receipt["execution"]["requested_model"], "gpt-6-luna")
            self.assertEqual(receipt["execution"]["requested_reasoning_effort"], "medium")

    def test_missing_approval_fails_before_lab_job_is_claimed(self):
        with patch.dict(worker.os.environ, {"AGATA_LAB_WORKER_ENABLED": "true"}), \
             patch.object(worker, "_load_lab_approval", side_effect=worker.FatalConfigurationError("lab_owner_approval_missing_or_invalid")), \
             patch.object(worker.lab_queue, "claim_next") as claim:
            with self.assertRaisesRegex(worker.FatalConfigurationError, "lab_approval_required"):
                worker.claim_lab_job()
        claim.assert_not_called()


if __name__ == "__main__":
    unittest.main()
