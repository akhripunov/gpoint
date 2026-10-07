"""Offline-only V2 integration checks; never open legacy or sealed scenario data."""
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

LAB = Path(__file__).resolve().parents[1]
REPO = LAB.parents[2]
CLI = LAB / "evaluators/naturalness_cli.py"


class NaturalnessV2OfflineTests(unittest.TestCase):
    def test_dry_run_is_v2_only_and_makes_zero_provider_calls(self):
        runner = importlib.util.spec_from_file_location("agata_naturalness_cli_test", CLI)
        module = importlib.util.module_from_spec(runner)
        runner.loader.exec_module(module)
        with patch("socket.create_connection", side_effect=AssertionError("network is forbidden")), \
             patch("subprocess.Popen", side_effect=AssertionError("external provider process is forbidden")), \
             patch("subprocess.run", side_effect=AssertionError("provider invocation is forbidden")):
            code = module.main(["dry-run"])
        self.assertEqual(code, 0)

    def test_cli_report_discloses_no_sealed_ids_and_no_live_invocations(self):
        completed = subprocess.run(["python3", str(CLI), "dry-run"], cwd=REPO,
                                   check=True, capture_output=True, text=True, timeout=30)
        report = json.loads(completed.stdout)
        self.assertEqual(report["dataset_id"], "agata-conversation-lab-v2")
        self.assertIn(report["status"], {"READY_FOR_DEV_FIXTURES", "SPLITS_NOT_POPULATED"})
        self.assertEqual(report["live_invocations"], 0)
        self.assertEqual(report["conversation_arms"], 0)
        self.assertEqual(report["generator_provider_invocations"], 0)
        self.assertEqual(report["judge_provider_invocations"], 0)
        self.assertNotIn("sealed_scenario_ids", report)
        self.assertNotIn("SEALED_HOLDOUT", json.dumps(report))

    def test_cli_source_does_not_reference_legacy_loaders_or_live_entrypoints(self):
        source = CLI.read_text(encoding="utf-8")
        for forbidden in ("load_naturalness_config", "run_fixture_dry_run", "load_frozen_pilot",
                          "run_promptfoo_eval", "promptfoo optimize", "scenario_by_id"):
            self.assertNotIn(forbidden, source)

    def test_dry_run_fails_closed_if_sealed_identifiers_enter_the_repo_manifest(self):
        spec = importlib.util.spec_from_file_location("agata_v2_cli_manifest_test", CLI)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        manifest = json.loads((LAB / "configs/splits-v2.json").read_text(encoding="utf-8"))
        manifest["splits"]["SEALED_HOLDOUT"]["scenario_ids"] = ["sealed-private-id"]
        manifest["splits"]["SEALED_HOLDOUT"]["scenario_hashes"] = {"sealed-private-id": "a" * 64}
        with self.assertRaises(PermissionError):
            module._validate_manifest(manifest)

    def test_legacy_read_incident_is_recorded_but_cannot_be_used_for_v2(self):
        spec = importlib.util.spec_from_file_location("agata_v2_cli_incident_test", CLI)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        manifest = json.loads((LAB / "configs/splits-v2.json").read_text(encoding="utf-8"))
        self.assertIs(manifest["legacy_holdout_v1"]["scenario_content_read"], True)
        self.assertIs(manifest["legacy_holdout_v1"]["scenario_content_used_for_v2"], False)
        self.assertEqual(module._validate_manifest(manifest), (4, 4))
        manifest["legacy_holdout_v1"]["scenario_content_used_for_v2"] = True
        with self.assertRaises(PermissionError):
            module._validate_manifest(manifest)
        manifest["legacy_holdout_v1"]["scenario_content_used_for_v2"] = False
        manifest["legacy_holdout_v1"].pop("access_incident")
        with self.assertRaises(PermissionError):
            module._validate_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
