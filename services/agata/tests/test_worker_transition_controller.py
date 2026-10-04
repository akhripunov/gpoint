"""Transition controller recovery and approval gates use temporary fixture paths only."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/transition-agata-worker-v3.py"
SPEC = importlib.util.spec_from_file_location("agata_worker_transition", SCRIPT)
transition = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(transition)


class WorkerTransitionTests(unittest.TestCase):
    def test_recovery_watchdog_process_registers_ready_without_launchagent_work(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.json"
            state_path.write_text(json.dumps({"complete": False, "parent_pid": os.getpid(), "watchdog_ready": False}))
            state_path.chmod(0o600)
            process = transition._start_watchdog(state_path)
            state = json.loads(state_path.read_text())
            state["complete"] = True
            transition._write_state(state_path, state)
            self.assertEqual(process.wait(timeout=5), 0)
            state = json.loads(state_path.read_text())
            self.assertTrue(state["watchdog_ready"])
            self.assertEqual(state["watchdog_pid"], process.pid)

    def test_legacy_v2_config_resolves_only_the_gpoint_lane_without_fabricated_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "worker.env"
            config.write_text(
                "GPOINT_AGATA_WORKER_URL=https://gpoint.amiora.one/api/agata-worker\n"
                "GPOINT_AGATA_WORKER_TOKEN=withheld\n"
            )
            config.chmod(0o600)
            old_config = transition.CONFIG
            transition.CONFIG = config
            try:
                self.assertEqual(transition._configured_lanes(), ("GPOINT",))
                config.write_text(
                    "GPOINT_AGATA_WORKER_URL=https://other.example/worker\n"
                    "GPOINT_AGATA_WORKER_TOKEN=withheld\n"
                )
                config.chmod(0o600)
                with self.assertRaisesRegex(RuntimeError, "gpoint_worker_config_incomplete"):
                    transition._configured_lanes()
            finally:
                transition.CONFIG = old_config

    def test_preflight_rejects_configured_gpoint_tenant_that_disagrees_with_live_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "worker.env"
            config.write_text(
                "GPOINT_AGATA_WORKER_URL=https://gpoint.amiora.one/api/agata-worker\n"
                "GPOINT_AGATA_WORKER_TOKEN=withheld\n"
                "GPOINT_AGATA_TENANT_ID=7508f36f-774a-48aa-8c6b-42f6f160415d\n"
            )
            config.chmod(0o600)
            old_config = transition.CONFIG
            transition.CONFIG = config
            try:
                with self.assertRaisesRegex(RuntimeError, "gpoint_worker_tenant_mismatch"):
                    transition._validate_configured_gpoint_binding({
                        "tenant_id": "81a4ba28-6de5-4b03-8332-0661ba4d46f4"
                    })
            finally:
                transition.CONFIG = old_config

    def test_authoritative_gpoint_snapshot_stages_hash_bound_nonsecret_tenant_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            old_path = transition.TENANT_BINDINGS
            transition.TENANT_BINDINGS = Path(temp) / "tenant-bindings.json"
            snapshot = {
                "source_system": "gpoint", "tenant_identity_verified": True,
                "transaction_read_only": True, "same_login": True,
                "role_superuser": False, "role_bypassrls": False,
                "tenant_id": "81a4ba28-6de5-4b03-8332-0661ba4d46f4",
                "source_id": "gpoint-vps:" + "b" * 40 + ":api=sha256:" + "1" * 64 + ":agata=sha256:" + "2" * 64,
                "snapshot_at": "2026-10-04T12:00:00Z", "release_sha": "b" * 40,
                "api_build_sha": "b" * 40, "agata_build_sha": "b" * 40,
                "api_image_id": "sha256:" + "1" * 64, "agata_image_id": "sha256:" + "2" * 64,
                "pending_inference_jobs": 0, "running_inference_jobs": 0,
                "effective_inference_ttl_seconds": 420, "observed_monotonic": time.monotonic(),
            }
            candidate = {"candidate_sha": "a" * 40, "assets": {"worker": "c" * 64}}
            try:
                transition._stage_gpoint_tenant_binding(snapshot, candidate)
                self.assertEqual(transition.TENANT_BINDINGS.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(transition.TENANT_BINDINGS.read_text()), {
                    "version": 1, "candidate_sha": "a" * 40, "worker_sha256": "c" * 64,
                    "tenant_id": snapshot["tenant_id"], "source_id": snapshot["source_id"],
                    "snapshot_at": snapshot["snapshot_at"],
                })
            finally:
                transition.TENANT_BINDINGS = old_path

    def test_starting_record_recovers_codex_pid_from_inherited_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record_path = root / "inference-process.json"
            record_path.write_text(json.dumps({"pid": None, "pgid": None, "command": "/opt/codex",
                                              "state": "starting"}))
            record_path.chmod(0o600)
            support = transition.SUPPORT
            transition.SUPPORT = root
            original_run = transition.subprocess.run

            def run(args, **kwargs):
                if args[0] == "lsof":
                    return subprocess.CompletedProcess(args, 0, "4242\n", "")
                if args[:3] == ["ps", "-p", "4242"]:
                    return subprocess.CompletedProcess(args, 0, "/opt/codex exec --ephemeral", "")
                return original_run(args, **kwargs)

            signals = []
            def killpg(pgid, sig):
                signals.append((pgid, sig))
                if sig == 0:
                    raise ProcessLookupError

            try:
                with patch.object(transition.subprocess, "run", side_effect=run), patch.object(
                    transition.os, "killpg", side_effect=killpg
                ):
                    transition._terminate_recorded_inference()
            finally:
                transition.SUPPORT = support
            self.assertEqual(signals, [(4242, 15), (4242, 0)])
            self.assertFalse(record_path.exists())

    def test_caller_supplied_gsales_snapshot_never_authorizes_transition(self):
        point = {"source_id": "gpoint:snapshot", "snapshot_at": "2026-10-04T10:00:00+00:00",
                 "pending_inference_jobs": 0, "running_inference_jobs": 0, "effective_inference_ttl_seconds": 420}
        gsales = {"target": "GSALES", "source_id": "sales:snapshot", "snapshot_at": "2026-10-04T10:00:00+00:00",
                  "pending_inference_jobs": 0, "running_inference_jobs": 0, "effective_inference_ttl_seconds": 420,
                  "transaction_read_only": True, "same_login": True, "role_superuser": False, "role_bypassrls": False}
        # Snapshot recency is evaluated at runtime, so use current UTC.
        from datetime import datetime, timezone
        gsales["snapshot_at"] = datetime.now(timezone.utc).isoformat()  # noqa: UP017
        for candidate in (gsales, gsales | {"pending_inference_jobs": 1}, {"source_id": "forged"}, None):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(RuntimeError, "source_unavailable"):
                    transition.validate_queue_snapshots(point, candidate, ("GPOINT", "GSALES"))

    def test_fresh_poll_means_a_new_authenticated_poll_even_if_job_is_active(self):
        before = {"last_poll_at": "old", "state": "idle", "current_job_present": False, "last_error_code": None}
        current = {"last_poll_at": "new", "state": "running", "current_job_present": True, "last_error_code": None}
        self.assertTrue(transition.authenticated_poll_confirmed(current, before, ("GPOINT",)))

    def test_restore_reinstates_exact_v2_artifacts_and_removes_new_helper(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            installed = {name: root / f"installed-{name}" for name in ("worker", "helper", "wrapper", "plist", "tenant_bindings")}
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
            installed["tenant_bindings"].write_text("staged nonsecret binding")
            original["tenant_bindings"] = None
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
                    transition, "_bootstrap", lambda _deadline=None: state.update(loaded=True, bootstrapped=state["bootstrapped"] + 1)
                ), patch.object(transition, "_wait_for_poll", lambda _before, lanes, _deadline: self.assertEqual(lanes, ("GPOINT",))), patch.object(
                    transition, "_terminate_recorded_inference"
                ), patch.object(transition, "_prove_global_lock_free"):
                    self.assertTrue(transition.restore_v2(state_path))
                self.assertEqual(state["bootstrapped"], 1)
                self.assertFalse(installed["helper"].exists())
                self.assertFalse(installed["tenant_bindings"].exists())
                for name in ("worker", "wrapper", "plist"):
                    self.assertEqual(transition.digest(installed[name]), original[name])
                self.assertTrue(json.loads(state_path.read_text())["restore_confirmed"])
            finally:
                transition.TARGETS = targets

    def test_restore_starts_early_enough_and_does_not_wait_past_cutoff_for_active_state(self):
        actual_restore_at = min(transition.RESTORE_AFTER_SECONDS,
                                transition.MAX_OUTAGE_SECONDS - transition.RESTORE_BUDGET_SECONDS)
        self.assertLessEqual(actual_restore_at + transition.RESTORE_BUDGET_SECONDS,
                             transition.MAX_OUTAGE_SECONDS)
        self.assertTrue(transition._watchdog_restore_due(True, True, 180, 180))
        self.assertTrue(transition._watchdog_restore_due(True, True, 300, 180))
        self.assertTrue(transition._watchdog_restore_due(False, False, 20, 180))


if __name__ == "__main__":
    unittest.main()
