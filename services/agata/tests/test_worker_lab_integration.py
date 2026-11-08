"""Conversation Lab keeps its local queue; the Mac Product Worker has no LAB lane."""
from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

WORKER_DIR = Path(__file__).resolve().parents[1] / "worker"
LAB_DIR = Path(__file__).resolve().parents[1] / "conversation-lab"
sys.path.insert(0, str(WORKER_DIR))
import gpoint_mac_worker as worker  # noqa: E402
import lab_queue  # noqa: E402
sys.path.insert(0, str(LAB_DIR / "evaluators"))
import worker_client  # noqa: E402


class ErosWorkerAndConversationLabBoundaryTests(unittest.TestCase):
    def test_mac_worker_has_no_lab_import_claim_poll_or_slot_entry_points(self):
        tree = ast.parse(Path(worker.__file__).read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.assertNotIn("lab_queue", imported)
        self.assertNotIn("agata_worker_pool", imported)
        self.assertFalse({"claim_lab_job", "process_lab_one", "execute_slot", "build_pool_supervisor"} & functions)
        self.assertEqual(worker.TARGET_NAMES, ("GPOINT",))
        self.assertFalse(hasattr(worker, "claim_lab_job"))
        self.assertFalse(hasattr(worker, "execute_slot"))

    def test_eros_worker_rejects_lab_route_without_network_or_executor(self):
        with patch.dict("os.environ", {
            "GPOINT_EROS_WORKER_URL": "https://gpoint.one/api/gpoint-worker",
            "GPOINT_EROS_WORKER_TOKEN": "synthetic-test-token",
            "GPOINT_EROS_TENANT_ID": "a1a46146-4b6e-4b03-8332-0661ba4d46f4",
            "AGATA_LAB_APPROVAL_FILE": "/private/unused/approval.json",
        }, clear=True), patch.object(worker, "api_request") as request, patch.object(
            worker, "run_inference"
        ) as inference:
            with self.assertRaisesRegex(worker.GatewayResponseError, "worker_target_invalid"):
                worker.process_one("LAB")
            with self.assertRaisesRegex(worker.FatalConfigurationError, "worker_target_unconfigured"):
                worker.target_request("LAB", "claim")
        request.assert_not_called()
        inference.assert_not_called()

    def test_conversation_lab_local_queue_still_round_trips_independently(self):
        with tempfile.TemporaryDirectory(prefix="conversation-lab-local-queue-") as temp:
            root = Path(temp) / "lab-queue"
            job = worker_client.build_job(
                scenario_id="ru_naturalness-02", configuration_id="baseline",
                turn_index=0, job_id="lab-independent-001",
            )
            lab_queue.submit(job, root=root)
            claimed = lab_queue.claim_next(root)
            self.assertEqual(claimed["job_id"], job["job_id"])
            lab_queue.finish(root, job["job_id"], status="completed", response="Synthetic local result")
            receipt = lab_queue.read_receipt(root, job["job_id"])
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(receipt["response"], "Synthetic local result")
            self.assertEqual(lab_queue.pending_count(root), 0)


if __name__ == "__main__":
    unittest.main()
