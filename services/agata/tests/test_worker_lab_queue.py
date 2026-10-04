"""Local LAB ingress tests; no network, Codex process, or production fixture."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parents[1] / "worker"
LAB_DIR = Path(__file__).resolve().parents[1] / "conversation-lab"
import sys
sys.path.insert(0, str(WORKER_DIR))
sys.path.insert(0, str(LAB_DIR / "evaluators"))
import worker_client
SPEC = importlib.util.spec_from_file_location("agata_lab_queue", WORKER_DIR / "lab_queue.py")
lab_queue = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(lab_queue)


def payload(job_id: str = "job-001") -> dict:
    job = worker_client.build_job(
        scenario_id="ru_naturalness-02", configuration_id="baseline",
        turn_index=0, job_id=job_id,
    )
    job["timeout_seconds"] = 20
    return job


class LabQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.support = Path(self.temp.name)
        self.root = self.support / "lab-queue"

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
        job = payload()
        job["request"]["history"] = [{"role": "assistant", "content": "x" * 70_000}]
        with self.assertRaises(lab_queue.LabQueueError):
            lab_queue.submit(job, root=self.root)

    def test_running_job_after_restart_is_marked_ambiguous_and_never_requeued(self):
        lab_queue.submit(payload(), root=self.root)
        lab_queue.claim_next(self.root)
        self.assertEqual(lab_queue.reconcile_running(self.root), 1)
        self.assertEqual(lab_queue.pending_count(self.root), 0)
        self.assertEqual(lab_queue.read_receipt(self.root, "job-001")["status"], "ambiguous")

    def test_second_turn_requires_a_same_arm_completed_worker_receipt(self):
        first = payload()
        lab_queue.submit(first, root=self.root)
        lab_queue.claim_next(self.root)
        lab_queue.finish(self.root, first["job_id"], status="completed", response="reply one", identity=first)
        second = worker_client.build_job(
            scenario_id="ru_naturalness-02", configuration_id="baseline", turn_index=1,
            history_receipt_ids=[first["job_id"]], support=self.support, job_id="job-002",
        )
        lab_queue.submit(second, root=self.root)
        claimed = lab_queue.claim_next(self.root)
        self.assertEqual(claimed["request"]["history"][1]["content"], "reply one")
        second["request"]["history"][1]["content"] = "arbitrary unverified text"
        with self.assertRaisesRegex(lab_queue.LabQueueError, "hash|receipt_backed"):
            lab_queue.submit(second | {"job_id": "job-003"}, root=self.root)

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
