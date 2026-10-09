"""No-approval checks for the exact operator entrypoint."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

LAB = Path(__file__).resolve().parents[1]
WORKER_FOR_TESTS = LAB.parents[0] / "worker"
REPO_ROOT = LAB.parents[2]
for path in (LAB / "evaluators", WORKER_FOR_TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import live_e2e
import live_e2e_cli


class LiveEntrypointPreflightTests(unittest.TestCase):
    def _valid_preflight(self, installed_identity=None):
        context = {
            "source_sha": "a" * 40, "lab_bundle_sha256": "b" * 64,
            "naturalness_policy_sha256": "c" * 64, "rubric_sha256": "d" * 64,
            "dataset_id": "agata-conversation-lab-v2", "dataset_split": "DEV",
            "dataset_manifest_sha256": "e" * 64,
        }
        return (
            patch.object(live_e2e.v2_provenance, "live_run_context", return_value=context),
            patch.object(live_e2e.promptfoo_runner, "validate_promptfoo_cli", return_value="0.123.1"),
            patch.object(live_e2e.promptfoo_runner, "resolve_promptfoo_node",
                         return_value=Path("/runtime/node")),
            patch.object(live_e2e.promptfoo_runner, "validate_promptfoo_python",
                         return_value=Path("/runtime/python")),
            patch.object(live_e2e.promptfoo_runner, "validate_live_environment"),
            patch.object(live_e2e.lab_queue, "worker_bundle_sha256", return_value="f" * 64),
            patch.object(live_e2e, "worker_install_bundle_sha256", return_value="g" * 64),
            patch.object(live_e2e, "installed_lab_runtime_identity", return_value=installed_identity or {
                "runtime_source_sha": "1" * 40,
                "worker_install_bundle_sha256": "g" * 64,
                "lab_approval_bundle_sha256": "f" * 64,
            }),
        )

    def test_preflight_only_checks_runner_without_reading_approval_or_creating_run_state(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            promptfoo = root / "promptfoo"
            promptfoo.touch()
            with contextlib.ExitStack() as stack:
                for mocked in self._valid_preflight():
                    stack.enter_context(mocked)
                queue = stack.enter_context(patch.object(live_e2e_cli, "canonical_lab_queue"))
                out = io.StringIO()
                with patch.object(sys, "stdout", out), patch.object(Path, "cwd", return_value=REPO_ROOT):
                    result = live_e2e_cli.main([
                        "--preflight-only", "--scenario-id", "v2-dev-clean-20261006-everyday",
                        "--promptfoo", str(promptfoo), "--expected-source-sha", "a" * 40,
                        "--expected-lab-bundle-sha256", "b" * 64,
                        "--expected-lab-approval-bundle-sha256", "f" * 64,
                        "--expected-worker-install-bundle-sha256", "g" * 64,
                    ])
            self.assertEqual(result, 0)
            summary = json.loads(out.getvalue())
            self.assertEqual(summary["status"], "OFFLINE_PREFLIGHT_PASS")
            self.assertEqual(summary["promptfoo_version"], "0.123.1")
            self.assertEqual(summary["model_contract"], "gpt-6-luna / medium")
            self.assertEqual(summary["lab_approval_bundle_sha256"], "f" * 64)
            self.assertEqual(summary["worker_install_bundle_sha256"], "g" * 64)
            self.assertEqual(summary["installed_lab_runtime_source_sha"], "1" * 40)
            self.assertEqual(summary["installed_lab_approval_bundle_sha256"], "f" * 64)
            self.assertEqual(summary["installed_worker_install_bundle_sha256"], "g" * 64)
            self.assertFalse(summary["approval_read"])
            self.assertFalse(summary["run_state_created"])
            queue.assert_not_called()

    def test_bad_promptfoo_fails_before_run_directory_or_approval_lookup(self):
        with tempfile.TemporaryDirectory() as temp_name:
            support = Path(temp_name) / "support"
            support.mkdir(mode=0o700)
            env = {"GPOINT_AGATA_WORKER_SUPPORT": str(support)}
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(Path, "cwd", return_value=REPO_ROOT), \
                    patch.object(live_e2e.promptfoo_runner, "validate_promptfoo_cli",
                                 side_effect=PermissionError("wrong CLI path")), \
                    patch.object(live_e2e.v2_provenance, "live_run_context", return_value={
                        "source_sha": "a" * 40, "lab_bundle_sha256": "e" * 64,
                        "naturalness_policy_sha256": "c" * 64, "rubric_sha256": "d" * 64,
                        "dataset_id": "agata-conversation-lab-v2", "dataset_split": "DEV",
                        "dataset_manifest_sha256": "e" * 64,
                    }), \
                    patch.object(live_e2e.lab_queue, "worker_bundle_sha256", return_value="f" * 64), \
                    patch.object(live_e2e, "worker_install_bundle_sha256", return_value="g" * 64), \
                    patch.object(live_e2e, "installed_lab_runtime_identity", return_value={
                        "runtime_source_sha": "1" * 40,
                        "worker_install_bundle_sha256": "g" * 64,
                        "lab_approval_bundle_sha256": "f" * 64,
                    }), \
                    patch.object(live_e2e_cli, "canonical_lab_queue") as queue:
                out = io.StringIO()
                with patch.object(sys, "stdout", out):
                    result = live_e2e_cli.main([
                        "--run-id", "agata-v2-preflight-fail-r2", "--scenario-id",
                        "v2-dev-clean-20261006-everyday", "--promptfoo", "/wrong/promptfoo",
                        "--expected-source-sha", "a" * 40,
                        "--expected-lab-bundle-sha256", "e" * 64,
                        "--expected-lab-approval-bundle-sha256", "f" * 64,
                        "--expected-worker-install-bundle-sha256", "g" * 64,
                    ])
            self.assertEqual(result, 2)
            self.assertEqual(json.loads(out.getvalue()), {
                "status": "PRE_INFERENCE_BLOCKED", "stage": "PROMPTFOO_CLI",
                "reason_code": "PINNED_CLI_INVALID", "error_type": "PermissionError",
            })
            self.assertFalse((support / "private/conversation-lab/runs/agata-v2-preflight-fail-r2").exists())
            queue.assert_not_called()

    def test_wrong_working_directory_fails_before_any_runtime_probe(self):
        with tempfile.TemporaryDirectory() as temp_name:
            with patch.object(live_e2e.promptfoo_runner, "validate_promptfoo_cli") as cli_probe:
                with self.assertRaises(PermissionError):
                    live_e2e_cli.validate_offline_runner_prerequisites(
                        "v2-dev-clean-20261006-everyday", "/ignored/promptfoo", {}, cwd=Path(temp_name),
                    )
            cli_probe.assert_not_called()

    def test_exact_source_mismatch_fails_before_promptfoo_probe(self):
        with contextlib.ExitStack() as stack:
            for mocked in self._valid_preflight():
                stack.enter_context(mocked)
            cli_probe = stack.enter_context(patch.object(live_e2e.promptfoo_runner, "validate_promptfoo_cli"))
            with self.assertRaises(live_e2e.OfflinePreflightError) as error:
                live_e2e.validate_offline_runner_prerequisites(
                    "v2-dev-clean-20261006-everyday", "/irrelevant", {}, cwd=REPO_ROOT,
                    expected_source_sha="f" * 40, expected_lab_bundle_sha256="b" * 64,
                    expected_lab_approval_bundle_sha256="f" * 64,
                    expected_worker_install_bundle_sha256="g" * 64,
                )
        self.assertEqual(error.exception.reason_code, "EXACT_SOURCE_MISMATCH")
        cli_probe.assert_not_called()

    def test_installed_lab_bundle_mismatch_blocks_even_when_checkout_bundle_matches(self):
        with contextlib.ExitStack() as stack:
            installed_identity = {
                "runtime_source_sha": "2" * 40,
                "worker_install_bundle_sha256": "x" * 64,
                "lab_approval_bundle_sha256": "y" * 64,
            }
            for mocked in self._valid_preflight(installed_identity):
                stack.enter_context(mocked)
            cli_probe = stack.enter_context(patch.object(live_e2e.promptfoo_runner, "validate_promptfoo_cli"))
            with self.assertRaises(live_e2e.OfflinePreflightError) as error:
                live_e2e.validate_offline_runner_prerequisites(
                    "v2-dev-clean-20261006-everyday", "/irrelevant", {}, cwd=REPO_ROOT,
                    expected_source_sha="a" * 40, expected_lab_bundle_sha256="b" * 64,
                    expected_lab_approval_bundle_sha256="f" * 64,
                    expected_worker_install_bundle_sha256="g" * 64,
                )
        self.assertEqual(error.exception.stage, "INSTALLED_LAB_RUNTIME")
        self.assertEqual(error.exception.reason_code, "INSTALLED_WORKER_BUNDLE_MISMATCH")
        cli_probe.assert_not_called()

    def test_installed_lab_identity_reads_pointer_manifest_and_private_asset_modes(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name) / "support"
            runtime = root / "runtime"
            slot_dir = runtime / "slots/LAB"
            release = runtime / ("a" * 40)
            for directory in (root, runtime, runtime / "slots", slot_dir, release):
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                directory.chmod(0o700)
            asset_hashes = {}
            for name in live_e2e.migrate_worker_runtime.ASSETS:
                asset = release / name
                asset.write_text(f"asset:{name}")
                asset.chmod(0o700 if name == "run_codex_worker.sh" else 0o600)
                asset_hashes[name] = live_e2e.migrate_worker_runtime.sha256(asset)
            install_bundle = hashlib.sha256(
                json.dumps(asset_hashes, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            manifest = {"source_sha": "a" * 40, "assets": asset_hashes, "bundle_sha256": install_bundle}
            manifest_path = release / "bundle-manifest.json"
            manifest_path.write_text(json.dumps(manifest, sort_keys=True))
            manifest_path.chmod(0o600)
            (slot_dir / "current").symlink_to(os.path.relpath(release, slot_dir))

            identity = live_e2e.installed_lab_runtime_identity({"GPOINT_AGATA_WORKER_SUPPORT": str(root)})
            self.assertEqual(identity["runtime_source_sha"], "a" * 40)
            self.assertEqual(identity["worker_install_bundle_sha256"], install_bundle)
            self.assertEqual(identity["lab_approval_bundle_sha256"], live_e2e.lab_queue.worker_bundle_sha256(release))

            unsafe_asset = release / "lab_queue.py"
            unsafe_asset.chmod(0o644)
            with self.assertRaises(PermissionError):
                live_e2e.installed_lab_runtime_identity({"GPOINT_AGATA_WORKER_SUPPORT": str(root)})
            unsafe_asset.chmod(0o600)

            manifest_path.write_text("{}", encoding="utf-8")
            manifest_path.chmod(0o600)
            with self.assertRaises(PermissionError):
                live_e2e.installed_lab_runtime_identity({"GPOINT_AGATA_WORKER_SUPPORT": str(root)})

            manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            manifest_path.chmod(0o600)
            unsafe_asset.unlink()
            unsafe_asset.symlink_to(WORKER_FOR_TESTS / "lab_queue.py")
            with self.assertRaises(PermissionError):
                live_e2e.installed_lab_runtime_identity({"GPOINT_AGATA_WORKER_SUPPORT": str(root)})

            unsafe_asset.unlink()
            unsafe_asset.write_text("asset:lab_queue.py")
            unsafe_asset.chmod(0o600)
            external_runtime_root = root.parent / "external-runtime"
            external_release = external_runtime_root / ("b" * 40)
            external_release.mkdir(mode=0o700, parents=True)
            external_slot = external_runtime_root / "slots/LAB"
            external_slot.mkdir(mode=0o700, parents=True)
            external_pointer = external_slot / "current"
            external_pointer.symlink_to(external_release)
            pointer = slot_dir / "current"
            pointer.unlink()
            pointer.symlink_to(external_release)
            with self.assertRaises(PermissionError):
                live_e2e.installed_lab_runtime_identity({"GPOINT_AGATA_WORKER_SUPPORT": str(root)})

    def test_live_mode_requires_exact_fingerprints_before_queue_or_run_state(self):
        with tempfile.TemporaryDirectory() as temp_name:
            support = Path(temp_name) / "support"
            support.mkdir(mode=0o700)
            with patch.dict(os.environ, {"GPOINT_AGATA_WORKER_SUPPORT": str(support)}, clear=True), \
                    patch.object(Path, "cwd", return_value=REPO_ROOT), \
                    patch.object(live_e2e_cli, "canonical_lab_queue") as queue:
                out = io.StringIO()
                with patch.object(sys, "stdout", out):
                    result = live_e2e_cli.main([
                        "--run-id", "agata-v2-missing-fingerprints", "--scenario-id",
                        "v2-dev-clean-20261006-everyday", "--promptfoo", "/path/not/read",
                    ])
            self.assertEqual(result, 2)
            self.assertEqual(json.loads(out.getvalue())["reason_code"], "EXPECTED_FINGERPRINT_REQUIRED")
            self.assertFalse((support / "private/conversation-lab/runs/agata-v2-missing-fingerprints").exists())
            queue.assert_not_called()

    def test_live_callable_rechecks_prerequisites_before_approval_lookup(self):
        failure = live_e2e.OfflinePreflightError(
            "PROMPTFOO_CLI", "PINNED_CLI_INVALID", PermissionError("invalid CLI"),
        )
        with patch.object(live_e2e, "validate_offline_runner_prerequisites", side_effect=failure), \
                patch.object(live_e2e.lab_queue, "load_owner_approval") as approval_lookup, \
                patch.object(live_e2e.worker_client, "canonical_lab_queue") as queue_lookup:
            with self.assertRaises(live_e2e.OfflinePreflightError):
                live_e2e.run_v2_live_e2e(
                    "v2-dev-clean-20261006-everyday", run_id="agata-v2-preflight-r2",
                    ledger=None, promptfoo_executable="/wrong/promptfoo",
                    receipt_directory=Path("/tmp/unused"), private_directory=Path("/tmp/unused"), env={},
                    expected_source_sha="a" * 40, expected_lab_bundle_sha256="b" * 64,
                    expected_lab_approval_bundle_sha256="c" * 64,
                    expected_worker_install_bundle_sha256="d" * 64,
                )
        approval_lookup.assert_not_called()
        queue_lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
