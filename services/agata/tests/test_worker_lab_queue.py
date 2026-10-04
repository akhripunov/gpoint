"""Local LAB ingress tests; no network, Codex process, or production fixture."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parents[1] / "worker"
SPEC = importlib.util.spec_from_file_location("agata_lab_queue", WORKER_DIR / "lab_queue.py")
lab_queue = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(lab_queue)


def request(text: str = "Синтетическое приветствие") -> dict:
    return {
        "instructions": "Ответь коротко и естественно.",
        "persona": {"name": "Тестовая персона"},
        "communication_settings": {"tone": "warm"},
        "memories": [],
        "dialogue_summaries": [],
        "history": [],
        "user_message": text,
    }


def payload(job_id: str = "job-001", text: str = "Синтетическое приветствие") -> dict:
    return {
        "job_id": job_id,
        "experiment_id": "naturalness-offline-2026-10",
        "configuration_id": "baseline",
        "origin": "conversation-lab",
        "scenario_id": "ru_naturalness-02",
        "turn_index": 0,
        "data_classification": "synthetic",
        "timeout_seconds": 20,
        "request": request(text),
    }


class LabQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_submit_claim_complete_round_trip_is_local_and_receipt_is_private(self):
        job = payload()
        lab_queue.submit(job, root=self.root)
        self.assertEqual(lab_queue.pending_count(self.root), 1)
        claimed = lab_queue.claim_next(self.root)
        self.assertEqual(claimed["job_id"], job["job_id"])
        self.assertEqual(list((self.root / "pending").glob("*.json")), [])
        lab_queue.finish(self.root, job["job_id"], status="completed", response="Привет!")
        receipt = lab_queue.read_receipt(self.root, job["job_id"])
        self.assertEqual(receipt["response"], "Привет!")
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual((self.root / "receipts/job-001.json").stat().st_mode & 0o077, 0)

    def test_duplicate_id_and_queue_limit_fail_closed(self):
        lab_queue.submit(payload(), root=self.root, max_pending=1)
        with self.assertRaisesRegex(lab_queue.LabQueueError, "duplicate"):
            lab_queue.submit(payload(), root=self.root, max_pending=1)
        with self.assertRaisesRegex(lab_queue.LabQueueError, "queue_full"):
            lab_queue.submit(payload("job-002"), root=self.root, max_pending=1)

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
            with self.subTest(mutation=mutation):
                with self.assertRaises(lab_queue.LabQueueError):
                    lab_queue.submit(job, root=self.root)
        self.assertEqual(lab_queue.pending_count(self.root), 0)

    def test_oversized_payload_is_rejected(self):
        with self.assertRaisesRegex(lab_queue.LabQueueError, "payload_too_large"):
            lab_queue.submit(payload(text="x" * 70_000), root=self.root)

    def test_running_job_after_restart_is_marked_ambiguous_and_never_requeued(self):
        lab_queue.submit(payload(), root=self.root)
        lab_queue.claim_next(self.root)
        self.assertEqual(lab_queue.reconcile_running(self.root), 1)
        self.assertEqual(lab_queue.pending_count(self.root), 0)
        self.assertEqual(lab_queue.read_receipt(self.root, "job-001")["status"], "ambiguous")

    def test_cancel_pending_job_creates_terminal_receipt_without_executor(self):
        lab_queue.submit(payload(), root=self.root)
        self.assertEqual(lab_queue.cancel(self.root, "job-001"), "cancelled")
        self.assertEqual(lab_queue.pending_count(self.root), 0)
        self.assertEqual(lab_queue.read_receipt(self.root, "job-001")["status"], "cancelled")

    def test_terminal_receipt_is_immutable_and_job_id_cannot_be_reused(self):
        lab_queue.submit(payload(), root=self.root)
        lab_queue.claim_next(self.root)
        lab_queue.finish(self.root, "job-001", status="completed", response="one")
        lab_queue.finish(self.root, "job-001", status="ambiguous", error_code="late-reconcile")
        self.assertEqual(lab_queue.read_receipt(self.root, "job-001")["status"], "completed")
        with self.assertRaisesRegex(lab_queue.LabQueueError, "duplicate"):
            lab_queue.submit(payload(), root=self.root)


if __name__ == "__main__":
    unittest.main()
