import unittest
from datetime import datetime, timezone
import json
import tempfile
from pathlib import Path

from evaluators.live_smoke_health import (
    WorkerHealthStop,
    read_json_status,
    read_lab_queue_state,
    wait_for_post_completion_ready,
    write_private_diagnostic_receipt,
)


def healthy_status(now):
    stamp = now.isoformat()
    return {
        "_controller_readback": {
            "source": "worker-status.json",
            "source_mtime_at": stamp,
        },
        "state": "healthy",
        "current_job_present": False,
        "last_heartbeat_at": stamp,
        "last_error_code": None,
        "pool": {"state": "HEALTHY", "busy": 0, "idle": 1},
        "targets": {
            lane: {
                "state": "idle",
                "current_job_present": False,
                "last_poll_at": stamp,
                "last_error_code": None,
            }
            for lane in ("GPOINT", "GSALES")
        } | {
            "LAB": {
                "state": "idle",
                "current_job_present": False,
                "last_error_code": None,
            }
        },
    }


def healthy_pool_status(now):
    return {
        "_controller_readback": {
            "source": "pool-status.json",
            "source_mtime_at": now.isoformat(),
        },
        "pool": {"state": "HEALTHY", "busy": 0, "idle": 1},
    }


class LiveSmokeHealthTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 5, 14, 56, 53, tzinfo=timezone.utc)
        self.receipt_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.receipt_dir.cleanup)
        self.receipt_path = Path(self.receipt_dir.name) / "guard-stop.json"
        self.agent = {
            "loaded": True,
            "label": "one.gpoint.agata-codex-worker",
            "observed_at": self.now.isoformat(),
        }
        self.queue = {
            "observed_at": self.now.isoformat(),
            "pending_job_ids": [],
            "running_job_ids": [],
            "completed_job_ids": ["completed-job"],
            "current_job_id": None,
            "readback_valid": True,
        }
        self.monotonic_value = 0.0

    def test_success_receipt_reconciles_worker_busy_transition_before_next_job(self):
        transitional = healthy_status(self.now)
        transitional.update(state="running", current_job_present=True)
        transitional["pool"] = {"state": "HEALTHY", "busy": 1, "idle": 0}
        transitional["targets"]["LAB"].update(
            state="running", current_job_present=True
        )
        snapshots = iter((transitional, healthy_status(self.now)))
        reads = []

        report = wait_for_post_completion_ready(
            completed_job_id="completed-job",
            diagnostic_receipt_path=self.receipt_path,
            status_reader=lambda: next(snapshots),
            pool_status_reader=lambda: healthy_pool_status(self.now),
            launch_agent_reader=lambda: self.agent,
            queue_reader=lambda: self.queue,
            utcnow=lambda: self.now,
            monotonic=lambda: self.monotonic_value,
            sleep=lambda delay: setattr(self, "monotonic_value", self.monotonic_value + delay),
            poll_interval_seconds=0.01,
            timeout_seconds=0.1,
            on_observation=reads.append,
        )

        self.assertTrue(report["healthy"])
        self.assertEqual(len(reads), 2)
        self.assertEqual(report["completed_job_id"], "completed-job")
        transitional_checks = {item["predicate"]: item for item in reads[0]["failed_predicates"]}
        self.assertEqual(
            transitional_checks["worker.state_healthy"]["observed"], "running"
        )
        self.assertEqual(
            transitional_checks["worker.current_job_cleared"]["observed"], True
        )
        self.assertEqual(
            transitional_checks["worker.current_job_cleared"]["source"],
            "worker-status.json.current_job_present",
        )
        self.assertEqual(
            transitional_checks["worker.current_job_cleared"]["controller_timestamp"],
            self.now.isoformat(),
        )

    def test_persistent_unhealthy_state_fails_closed_with_exact_predicates(self):
        unhealthy = healthy_status(self.now)
        unhealthy["last_error_code"] = "worker_gateway_network_error"
        self.receipt_path.unlink(missing_ok=True)

        with self.assertRaises(WorkerHealthStop) as raised:
            wait_for_post_completion_ready(
                completed_job_id="completed-job",
                diagnostic_receipt_path=self.receipt_path,
                status_reader=lambda: unhealthy,
                pool_status_reader=lambda: healthy_pool_status(self.now),
                launch_agent_reader=lambda: self.agent,
                queue_reader=lambda: self.queue,
                utcnow=lambda: self.now,
                monotonic=lambda: self.monotonic_value,
                sleep=lambda delay: None,
            )

        receipt = raised.exception.receipt
        self.assertEqual(receipt["guard"], "worker_unhealthy_or_stale")
        self.assertIn(
            "worker.last_error_code_is_clear",
            {item["predicate"] for item in receipt["failed_predicates"]},
        )
        self.assertEqual(receipt["observed"]["worker"]["last_error_code"],
                         "worker_gateway_network_error")
        self.assertEqual(receipt["observed"]["queue"]["running_job_ids"], [])
        self.assertEqual(receipt["observed"]["launch_agent"]["loaded"], True)
        self.assertTrue(self.receipt_path.is_file())
        self.assertEqual(self.receipt_path.stat().st_mode & 0o777, 0o600)

    def test_stale_heartbeat_remains_a_fail_closed_gate(self):
        stale = healthy_status(self.now)
        stale["last_heartbeat_at"] = "2026-10-05T14:55:00+00:00"

        with self.assertRaises(WorkerHealthStop) as raised:
            wait_for_post_completion_ready(
                completed_job_id="completed-job",
                diagnostic_receipt_path=self.receipt_path,
                status_reader=lambda: stale,
                pool_status_reader=lambda: healthy_pool_status(self.now),
                launch_agent_reader=lambda: self.agent,
                queue_reader=lambda: self.queue,
                utcnow=lambda: self.now,
                monotonic=lambda: self.monotonic_value,
                sleep=lambda delay: None,
                timeout_seconds=0,
            )

        heartbeat = next(
            item for item in raised.exception.receipt["failed_predicates"]
            if item["predicate"] == "worker.heartbeat_fresh"
        )
        self.assertEqual(heartbeat["observed_age_seconds"], 113.0)
        self.assertEqual(heartbeat["threshold_seconds"], 45)

    def test_stale_cached_status_snapshot_remains_a_fail_closed_gate(self):
        stale = healthy_status(self.now)
        stale["_controller_readback"]["source_mtime_at"] = "2026-10-05T14:55:00+00:00"

        with self.assertRaises(WorkerHealthStop) as raised:
            wait_for_post_completion_ready(
                completed_job_id="completed-job",
                diagnostic_receipt_path=self.receipt_path,
                status_reader=lambda: stale,
                pool_status_reader=lambda: healthy_pool_status(self.now),
                launch_agent_reader=lambda: self.agent,
                queue_reader=lambda: self.queue,
                utcnow=lambda: self.now,
                monotonic=lambda: self.monotonic_value,
                sleep=lambda delay: None,
            )

        snapshot = next(
            item for item in raised.exception.receipt["failed_predicates"]
            if item["predicate"] == "worker.status_snapshot_fresh"
        )
        self.assertEqual(snapshot["observed_age_seconds"], 113.0)
        self.assertEqual(snapshot["threshold_seconds"], 45)

    def test_gpoint_or_gsales_failure_cannot_be_hidden_by_lab_completion(self):
        status = healthy_status(self.now)
        status["targets"]["GSALES"]["last_error_code"] = "sales_poll_failed"

        with self.assertRaises(WorkerHealthStop) as raised:
            wait_for_post_completion_ready(
                completed_job_id="completed-job",
                diagnostic_receipt_path=self.receipt_path,
                status_reader=lambda: status,
                pool_status_reader=lambda: healthy_pool_status(self.now),
                launch_agent_reader=lambda: self.agent,
                queue_reader=lambda: self.queue,
                utcnow=lambda: self.now,
                monotonic=lambda: self.monotonic_value,
                sleep=lambda delay: None,
            )

        failures = {item["predicate"] for item in raised.exception.receipt["failed_predicates"]}
        self.assertIn("target.GSALES.last_error_code_is_clear", failures)

    def test_missing_launchagent_evidence_fails_closed(self):
        with self.assertRaises(WorkerHealthStop) as raised:
            wait_for_post_completion_ready(
                completed_job_id="completed-job",
                diagnostic_receipt_path=self.receipt_path,
                status_reader=lambda: healthy_status(self.now),
                pool_status_reader=lambda: healthy_pool_status(self.now),
                launch_agent_reader=lambda: None,
                queue_reader=lambda: self.queue,
                utcnow=lambda: self.now,
                monotonic=lambda: self.monotonic_value,
                sleep=lambda delay: None,
            )

        failures = {item["predicate"] for item in raised.exception.receipt["failed_predicates"]}
        self.assertIn("launch_agent.loaded", failures)

    def test_missing_queue_inventory_fails_closed(self):
        with self.assertRaises(WorkerHealthStop) as raised:
            wait_for_post_completion_ready(
                completed_job_id="completed-job",
                diagnostic_receipt_path=self.receipt_path,
                status_reader=lambda: healthy_status(self.now),
                pool_status_reader=lambda: healthy_pool_status(self.now),
                launch_agent_reader=lambda: self.agent,
                queue_reader=lambda: {},
                utcnow=lambda: self.now,
                monotonic=lambda: self.monotonic_value,
                sleep=lambda delay: None,
            )

        failures = {item["predicate"] for item in raised.exception.receipt["failed_predicates"]}
        self.assertIn("lab_queue.readback_valid", failures)
        self.assertIn("lab_queue.completed_receipt_present", failures)

    def test_transition_waits_for_exact_completed_job_queue_cleanup(self):
        transitional_queue = {
            "observed_at": self.now.isoformat(),
            "pending_job_ids": [],
            "running_job_ids": ["completed-job"],
            "completed_job_ids": ["completed-job"],
            "current_job_id": "completed-job",
            "readback_valid": True,
        }
        snapshots = iter((transitional_queue, self.queue))
        reads = []

        report = wait_for_post_completion_ready(
            completed_job_id="completed-job",
            diagnostic_receipt_path=self.receipt_path,
            status_reader=lambda: healthy_status(self.now),
            pool_status_reader=lambda: healthy_pool_status(self.now),
            launch_agent_reader=lambda: self.agent,
            queue_reader=lambda: next(snapshots),
            utcnow=lambda: self.now,
            monotonic=lambda: self.monotonic_value,
            sleep=lambda delay: setattr(self, "monotonic_value", self.monotonic_value + delay),
            poll_interval_seconds=0.01,
            timeout_seconds=0.1,
            on_observation=reads.append,
        )

        self.assertTrue(report["healthy"])
        self.assertEqual(len(reads), 2)
        self.assertEqual(
            reads[0]["observed"]["queue"]["running_job_ids"], ["completed-job"]
        )

    def test_wrong_launchagent_label_fails_closed(self):
        with self.assertRaises(WorkerHealthStop) as raised:
            wait_for_post_completion_ready(
                completed_job_id="completed-job",
                diagnostic_receipt_path=self.receipt_path,
                status_reader=lambda: healthy_status(self.now),
                pool_status_reader=lambda: healthy_pool_status(self.now),
                launch_agent_reader=lambda: {
                    "loaded": True,
                    "label": "other.worker",
                    "observed_at": self.now.isoformat(),
                },
                queue_reader=lambda: self.queue,
                utcnow=lambda: self.now,
                monotonic=lambda: self.monotonic_value,
                sleep=lambda delay: None,
            )
        failures = {item["predicate"] for item in raised.exception.receipt["failed_predicates"]}
        self.assertIn("launch_agent.expected_label", failures)

    def test_stop_receipt_is_sanitized_even_when_source_contains_private_fields(self):
        status = healthy_status(self.now)
        status["response_body"] = "private model output"
        status["api_token"] = "secret-token"
        unhealthy = dict(status, last_error_code="worker_gateway_network_error")

        with self.assertRaises(WorkerHealthStop):
            wait_for_post_completion_ready(
                completed_job_id="completed-job",
                diagnostic_receipt_path=self.receipt_path,
                status_reader=lambda: unhealthy,
                pool_status_reader=lambda: healthy_pool_status(self.now),
                launch_agent_reader=lambda: self.agent,
                queue_reader=lambda: self.queue,
                utcnow=lambda: self.now,
                monotonic=lambda: self.monotonic_value,
                sleep=lambda delay: None,
            )

        saved = self.receipt_path.read_text(encoding="utf-8")
        self.assertNotIn("private model output", saved)
        self.assertNotIn("secret-token", saved)

    def test_private_diagnostic_receipt_is_atomic_owner_only_and_payload_free(self):
        status = healthy_status(self.now)
        status["sensitive_payload"] = "must never be copied into diagnostics"
        report = wait_for_post_completion_ready(
            completed_job_id="completed-job",
            diagnostic_receipt_path=self.receipt_path,
            status_reader=lambda: status,
            pool_status_reader=lambda: healthy_pool_status(self.now),
            launch_agent_reader=lambda: self.agent,
            queue_reader=lambda: self.queue,
            utcnow=lambda: self.now,
            monotonic=lambda: self.monotonic_value,
            sleep=lambda delay: None,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "health.json"
            write_private_diagnostic_receipt(path, report)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(saved["guard"], "worker_unhealthy_or_stale")
            self.assertNotIn("sensitive_payload", path.read_text(encoding="utf-8"))

    def test_status_reader_records_the_atomic_file_mtime_for_staleness_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "worker-status.json"
            path.write_text(json.dumps(healthy_status(self.now)), encoding="utf-8")

            status = read_json_status(path)

            self.assertIn("_controller_readback", status)
            self.assertEqual(status["_controller_readback"]["source"], "worker-status.json")
            self.assertTrue(status["_controller_readback"]["source_mtime_at"].endswith("+00:00"))

    def test_queue_reader_reports_only_terminal_identity_and_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("pending", "running", "receipts"):
                (root / name).mkdir()
            (root / "receipts" / "completed-job.json").write_text(
                json.dumps({
                    "job_id": "completed-job",
                    "status": "completed",
                    "response": "private assistant response",
                }),
                encoding="utf-8",
            )

            snapshot = read_lab_queue_state(root)

        self.assertTrue(snapshot["readback_valid"])
        self.assertEqual(snapshot["completed_job_ids"], ["completed-job"])
        self.assertNotIn("response", snapshot)
        self.assertNotIn("private assistant response", json.dumps(snapshot))


if __name__ == "__main__":
    unittest.main()
