"""Retained LAB queue safety tests; no retired evaluator or Promptfoo dependency."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

WORKER_DIR = Path(__file__).resolve().parents[1] / "worker"
sys.path.insert(0, str(WORKER_DIR))
SPEC = importlib.util.spec_from_file_location("agata_lab_queue", WORKER_DIR / "lab_queue.py")
lab_queue = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(lab_queue)


def payload(job_id: str = "job-001") -> dict:
    return {
        "job_id": job_id, "experiment_id": "agata-naturalness-v1",
        "configuration_id": "baseline", "origin": "conversation-lab",
        "scenario_id": "ru_naturalness-02", "turn_index": 0,
        "data_classification": "synthetic", "timeout_seconds": 20,
        "history_receipt_ids": [],
        "request": {"instructions": "synthetic fixture", "persona": {},
                    "communication_settings": {}, "memories": [],
                    "dialogue_summaries": [], "history": [],
                    "user_message": "synthetic fixture"},
    }


def submit_validated(job: dict, root: Path, max_pending: int = 16) -> None:
    encoded = json.dumps(job, ensure_ascii=False, separators=(",", ":")).encode()
    with patch.object(lab_queue, "_validate", return_value=(job, encoded)):
        lab_queue.submit(job, root=root, max_pending=max_pending)


def claim_validated(root: Path):
    with patch.object(lab_queue, "_validate", side_effect=lambda job, root: (job, b"")):
        return lab_queue.claim_next(root)


class LabQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.support = Path(self.temp.name)
        self.root = self.support / "lab-queue"

    def tearDown(self):
        self.temp.cleanup()

    def test_submit_claim_complete_round_trip_is_local_and_receipt_is_private(self):
        job = payload()
        submit_validated(job, self.root)
        self.assertEqual(lab_queue.pending_count(self.root), 1)
        claimed = claim_validated(self.root)
        self.assertEqual(claimed["job_id"], job["job_id"])
        self.assertEqual(list((self.root / "pending").glob("*.json")), [])
        lab_queue.finish(self.root, job["job_id"], status="completed", response="synthetic reply")
        receipt = lab_queue.read_receipt(self.root, job["job_id"])
        self.assertEqual(receipt["response"], "synthetic reply")
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual((self.root / "receipts/job-001.json").stat().st_mode & 0o077, 0)

    def test_receipt_preserves_only_source_attributed_provider_model_identity(self):
        job = payload()
        submit_validated(job, self.root)
        claim_validated(self.root)
        lab_queue.finish(
            self.root, job["job_id"], status="completed", response="reply",
            execution={"requested_model": "gpt-6-luna", "requested_reasoning_effort": "medium",
                       "actual_model": "gpt-6-sol", "actual_model_source": "codex_provider_response_metadata"},
        )
        execution = lab_queue.read_receipt(self.root, job["job_id"])["execution"]
        self.assertEqual(execution["actual_model"], "gpt-6-sol")
        self.assertEqual(execution["actual_model_source"], "codex_provider_response_metadata")

        second = payload("job-002")
        submit_validated(second, self.root)
        claim_validated(self.root)
        lab_queue.finish(
            self.root, second["job_id"], status="completed", response="reply",
            execution={"requested_model": "gpt-6-luna", "requested_reasoning_effort": "medium",
                       "actual_model": "gpt-6-sol", "actual_model_source": "untrusted-banner"},
        )
        execution = lab_queue.read_receipt(self.root, second["job_id"])["execution"]
        self.assertIsNone(execution["actual_model"])
        self.assertIsNone(execution["actual_model_source"])

    def test_duplicate_id_and_queue_limit_fail_closed(self):
        submit_validated(payload(), self.root, max_pending=1)
        with self.assertRaisesRegex(lab_queue.LabQueueError, "duplicate"):
            submit_validated(payload(), self.root, max_pending=1)
        with self.assertRaisesRegex(lab_queue.LabQueueError, "queue_full"):
            submit_validated(payload("job-002"), self.root, max_pending=1)

    def test_invalid_origin_classification_timeout_or_context_is_rejected(self):
        mutations = (
            {"origin": "https://public.example"},
            {"data_classification": "production"},
            {"timeout_seconds": 601},
            {"request": {"user_message": "x"}},
        )
        for mutation in mutations:
            job = payload()
            job.update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(lab_queue.LabQueueError):
                lab_queue.submit(job, root=self.root)
        self.assertEqual(lab_queue.pending_count(self.root), 0)

    def test_oversized_payload_is_rejected(self):
        job = payload()
        job["request"]["history"] = [{"role": "assistant", "content": "x" * 70_000}]
        with self.assertRaises(lab_queue.LabQueueError):
            lab_queue.submit(job, root=self.root)

    def test_owner_approval_usage_fails_closed_on_malformed_receipt_or_symlink(self):
        approval = {"approval_id": "offline-approval", "worker_bundle_sha256": "a" * 64}
        usage_dir = self.support / "state/lab-approval-usage"
        usage_dir.mkdir(parents=True, mode=0o700)
        receipt = usage_dir / "offline-approval.json"
        receipt.write_text("null", encoding="utf-8")
        receipt.chmod(0o600)
        with self.assertRaisesRegex(lab_queue.LabQueueError, "usage_invalid"):
            lab_queue.owner_approval_usage_calls(self.support, approval)
        receipt.unlink()
        receipt.symlink_to(self.support / "missing-target")
        with self.assertRaisesRegex(lab_queue.LabQueueError, "usage_invalid"):
            lab_queue.owner_approval_usage_calls(self.support, approval)

    def test_running_job_after_restart_is_marked_ambiguous_and_never_requeued(self):
        submit_validated(payload(), self.root)
        claim_validated(self.root)
        self.assertEqual(lab_queue.reconcile_running(self.root), 1)
        self.assertEqual(lab_queue.pending_count(self.root), 0)
        self.assertEqual(lab_queue.read_receipt(self.root, "job-001")["status"], "ambiguous")

    def test_cancel_pending_job_creates_terminal_receipt_without_executor(self):
        submit_validated(payload(), self.root)
        self.assertEqual(lab_queue.cancel(self.root, "job-001"), "cancelled")
        self.assertEqual(lab_queue.pending_count(self.root), 0)
        self.assertEqual(lab_queue.read_receipt(self.root, "job-001")["status"], "cancelled")

    def test_terminal_receipt_is_immutable_and_job_id_cannot_be_reused(self):
        submit_validated(payload(), self.root)
        claim_validated(self.root)
        lab_queue.finish(self.root, "job-001", status="completed", response="one")
        lab_queue.finish(self.root, "job-001", status="ambiguous", error_code="late-reconcile")
        self.assertEqual(lab_queue.read_receipt(self.root, "job-001")["status"], "completed")
        with self.assertRaisesRegex(lab_queue.LabQueueError, "duplicate"):
            submit_validated(payload(), self.root)

    def test_terminal_receipt_records_fixed_lab_slot_identity(self):
        submit_validated(payload("slot-receipt"), self.root)
        claim_validated(self.root)
        lab_queue.finish(self.root, "slot-receipt", status="completed", slot_id="slot-03")
        receipt = lab_queue.read_receipt(self.root, "slot-receipt")
        self.assertEqual(receipt["slot_id"], "slot-03")


if __name__ == "__main__":
    unittest.main()
