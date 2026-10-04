"""Transition controller recovery and approval gates use temporary fixture paths only."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/transition-agata-worker-v3.py"
SPEC = importlib.util.spec_from_file_location("agata_worker_transition", SCRIPT)
transition = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(transition)


class WorkerTransitionTests(unittest.TestCase):
    def test_gsales_snapshot_must_be_authoritative_empty_fresh_and_ttl_safe(self):
        point = {"source_id": "gpoint:snapshot", "snapshot_at": "2026-10-04T10:00:00+00:00",
                 "pending_inference_jobs": 0, "running_inference_jobs": 0, "effective_inference_ttl_seconds": 420}
        gsales = {"target": "GSALES", "source_id": "sales:snapshot", "snapshot_at": "2026-10-04T10:00:00+00:00",
                  "pending_inference_jobs": 0, "running_inference_jobs": 0, "effective_inference_ttl_seconds": 420,
                  "transaction_read_only": True, "same_login": True, "role_superuser": False, "role_bypassrls": False}
        # Snapshot recency is evaluated at runtime, so use current UTC.
        from datetime import datetime, timezone
        gsales["snapshot_at"] = datetime.now(timezone.utc).isoformat()
        self.assertIn("GSALES", transition.validate_queue_snapshots(point, gsales, ("GPOINT", "GSALES")))
        for bad in (
            gsales | {"pending_inference_jobs": 1},
            gsales | {"effective_inference_ttl_seconds": 419},
            gsales | {"transaction_read_only": False},
            gsales | {"role_bypassrls": True},
            gsales | {"snapshot_at": "2000-01-01T00:00:00+00:00"},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(RuntimeError):
                    transition.validate_queue_snapshots(point, bad, ("GPOINT", "GSALES"))
        with self.assertRaisesRegex(RuntimeError, "required"):
            transition.validate_queue_snapshots(point, None, ("GPOINT", "GSALES"))

    def test_fresh_poll_means_a_new_authenticated_poll_even_if_job_is_active(self):
        before = {"last_poll_at": "old", "state": "idle", "current_job_present": False, "last_error_code": None}
        current = {"last_poll_at": "new", "state": "running", "current_job_present": True, "last_error_code": None}
        self.assertTrue(transition.authenticated_poll_confirmed(current, before, ("GPOINT",)))

    def test_restore_reinstates_exact_v2_artifacts_and_removes_new_helper(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            installed = {name: root / f"installed-{name}" for name in ("worker", "helper", "wrapper", "plist")}
            backup = root / "backup"
            backup.mkdir(mode=0o700)
            original = {}
            for name in ("worker", "wrapper", "plist"):
                content = f"original-v2-{name}".encode()
                installed[name].write_bytes(content)
                original[name] = transition.hashlib.sha256(content).hexdigest()
                (backup / name).write_bytes(content)
            installed["helper"].write_text("new v3 helper")
            original["helper"] = None
            state_path = backup / "state.json"
            state_path.write_text(json.dumps({
                "version": 1, "backup_dir": str(backup), "original_hashes": original,
                "before_status": {"last_poll_at": "old"}, "v2_lanes": ["GPOINT"],
                "complete": False, "restore_confirmed": False,
            }))
            targets = transition.TARGETS
            transition.TARGETS = installed
            state = {"loaded": False, "bootstrapped": 0}
            try:
                with patch.object(transition, "loaded", lambda: state["loaded"]), patch.object(
                    transition, "_bootstrap", lambda: state.update(loaded=True, bootstrapped=state["bootstrapped"] + 1)
                ), patch.object(transition, "_wait_for_poll", lambda _before, lanes, _deadline: self.assertEqual(lanes, ("GPOINT",))):
                    self.assertTrue(transition.restore_v2(state_path))
                self.assertEqual(state["bootstrapped"], 1)
                self.assertFalse(installed["helper"].exists())
                for name in ("worker", "wrapper", "plist"):
                    self.assertEqual(transition.digest(installed[name]), original[name])
                self.assertTrue(json.loads(state_path.read_text())["restore_confirmed"])
            finally:
                transition.TARGETS = targets


if __name__ == "__main__":
    unittest.main()
