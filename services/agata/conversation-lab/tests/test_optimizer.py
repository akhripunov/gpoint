"""V2 split allowlists and validation-only optimizer authorization tests."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


LAB = Path(__file__).resolve().parents[1]
MODULE = LAB / "evaluators" / "optimizer.py"
SPEC = importlib.util.spec_from_file_location("agata_v2_optimizer", MODULE) if MODULE.exists() else None
optimizer = importlib.util.module_from_spec(SPEC) if SPEC else None
if SPEC and SPEC.loader:
    SPEC.loader.exec_module(optimizer)
NAT_SPEC = importlib.util.spec_from_file_location("agata_optimizer_naturalness", LAB / "evaluators" / "naturalness.py")
naturalness = importlib.util.module_from_spec(NAT_SPEC)
NAT_SPEC.loader.exec_module(naturalness)
PILOT_SPEC = importlib.util.spec_from_file_location("agata_optimizer_pilot", LAB / "evaluators" / "pilot.py")
pilot = importlib.util.module_from_spec(PILOT_SPEC)
PILOT_SPEC.loader.exec_module(pilot)

POLICY_HASH = "c" * 64
SCENARIO = {
    "scenario_id": "v2-dev-daily-001", "language": "ru",
    "dialogue_turns": [
        {"role": "user", "text": "Вопрос 1"}, {"role": "assistant", "response_slot": True},
        {"role": "user", "text": "Вопрос 2"}, {"role": "assistant", "response_slot": True},
        {"role": "user", "text": "Вопрос 3"}, {"role": "assistant", "response_slot": True},
        {"role": "user", "text": "Вопрос 4"}, {"role": "assistant", "response_slot": True},
    ],
}


def manifest():
    digest = pilot.stable_hash(SCENARIO)
    return {
        "schema_version": 2,
        "dataset_id": "synthetic-v2-fixture-set",
        "legacy_holdout_v1": {"status": "COMPROMISED_EXCLUDED", "optimizer_access": False,
                               "scenario_content_read": False, "scenario_content_used_for_v2": False},
        "optimizer_allowlist": {"training_splits": ["DEV"], "validation_splits": ["VALIDATION"], "forbidden_splits": ["SEALED_HOLDOUT"]},
        "splits": {
            "DEV": {"scenario_ids": [SCENARIO["scenario_id"]], "scenario_hashes": {SCENARIO["scenario_id"]: digest}},
            "VALIDATION": {"scenario_ids": ["v2-validation-one"], "scenario_hashes": {"v2-validation-one": "b" * 64}},
            "SEALED_HOLDOUT": {"scenario_ids": [], "scenario_hashes": {}, "plaintext_committed": False},
        },
    }


class OptimizerSplitTests(unittest.TestCase):
    def require_module(self):
        self.assertIsNotNone(optimizer, "V2 allowlisted optimizer adapter is not implemented")

    def test_checked_in_v2_dev_and_validation_payloads_match_disjoint_hash_allowlists(self):
        split_manifest = json.loads((LAB / "configs/splits-v2.json").read_text(encoding="utf-8"))
        payload_paths = {
            "DEV": LAB / "data/golden/synthetic/v2-dev-scenarios.jsonl",
            "VALIDATION": LAB / "data/golden/synthetic/v2-validation-scenarios.jsonl",
        }
        loaded_ids = {}
        for split, path in payload_paths.items():
            scenarios = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            loaded_ids[split] = [scenario["scenario_id"] for scenario in scenarios]
            self.assertEqual(loaded_ids[split], split_manifest["splits"][split]["scenario_ids"])
            self.assertEqual(len(scenarios), 4)
            for scenario in scenarios:
                self.assertTrue(pilot.validate_v2_scenario(scenario, split_manifest, split)["validated"])
                self.assertEqual(scenario["user_gender"], "male")
                self.assertEqual(sum(turn["role"] == "user" for turn in scenario["dialogue_turns"]), 4)
                self.assertEqual(sum(turn.get("response_slot") is True for turn in scenario["dialogue_turns"]), 4)
        self.assertFalse(set(loaded_ids["DEV"]) & set(loaded_ids["VALIDATION"]))
        self.assertEqual(split_manifest["optimizer_allowlist"]["training_splits"], ["DEV"])
        self.assertEqual(split_manifest["optimizer_allowlist"]["validation_splits"], ["VALIDATION"])
        self.assertEqual(split_manifest["optimizer_allowlist"]["forbidden_splits"], ["SEALED_HOLDOUT"])
        self.assertEqual(split_manifest["splits"]["SEALED_HOLDOUT"]["scenario_ids"], [])
        self.assertEqual(split_manifest["splits"]["SEALED_HOLDOUT"]["scenario_hashes"], {})

    def test_legacy_read_incident_cannot_authorize_using_legacy_content(self):
        self.require_module()
        value = manifest()
        value["legacy_holdout_v1"].update({"scenario_content_read": True,
            "access_incident": "test incident", "v2_fixture_provenance": "fresh synthetic data",
            "scenario_content_used_for_v2": False})
        self.assertEqual(optimizer._manifest_splits(value)["DEV"], [SCENARIO["scenario_id"]])
        value["legacy_holdout_v1"]["scenario_content_used_for_v2"] = True
        with self.assertRaises(PermissionError):
            optimizer._manifest_splits(value)
        value["legacy_holdout_v1"]["scenario_content_used_for_v2"] = False
        value["legacy_holdout_v1"].pop("access_incident")
        with self.assertRaises(PermissionError):
            optimizer._manifest_splits(value)

    def test_v2_scenario_validator_rejects_legacy_content_reuse(self):
        split_manifest = json.loads((LAB / "configs/splits-v2.json").read_text(encoding="utf-8"))
        scenario = json.loads((LAB / "data/golden/synthetic/v2-dev-scenarios.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(pilot.validate_v2_scenario(scenario, split_manifest, "DEV")["validated"])
        split_manifest["legacy_holdout_v1"]["scenario_content_used_for_v2"] = True
        with self.assertRaises(PermissionError):
            pilot.validate_v2_scenario(scenario, split_manifest, "DEV")

    def test_dev_and_validation_only_are_in_optimizer_config_and_sealed_is_absent(self):
        self.require_module()
        config = optimizer.build_optimizer_config(manifest(), policy_hash=POLICY_HASH, candidate_limit=3)
        self.assertEqual(config["training_split"], "DEV")
        self.assertEqual(config["training_scenario_ids"], [SCENARIO["scenario_id"]])
        self.assertEqual(config["validation_split"], "VALIDATION")
        self.assertEqual(config["validation_scenario_ids"], ["v2-validation-one"])
        serialized = str(config)
        self.assertNotIn("SEALED_HOLDOUT", serialized)
        self.assertNotIn("-01", serialized)
        self.assertFalse(config["execution_enabled"])

    def test_sealed_split_and_compromised_legacy_v1_fail_closed(self):
        self.require_module()
        value = manifest()
        value["optimizer_allowlist"]["training_splits"] = ["SEALED_HOLDOUT"]
        with self.assertRaises(PermissionError):
            optimizer.build_optimizer_config(value, policy_hash=POLICY_HASH, candidate_limit=3)
        value = manifest()
        value["legacy_holdout_v1"]["optimizer_access"] = True
        with self.assertRaises(PermissionError):
            optimizer.build_optimizer_config(value, policy_hash=POLICY_HASH, candidate_limit=3)
        value = manifest()
        value["splits"]["SEALED_HOLDOUT"]["scenario_ids"] = ["sealed-private-id"]
        with self.assertRaises(PermissionError):
            optimizer.build_optimizer_config(value, policy_hash=POLICY_HASH, candidate_limit=3)

    def test_v2_scenario_validation_requires_exact_split_membership_and_hash(self):
        value = manifest()
        self.assertTrue(pilot.validate_v2_scenario(SCENARIO, value, "DEV")["validated"])
        with self.assertRaises(PermissionError):
            pilot.validate_v2_scenario(SCENARIO, value, "VALIDATION")
        malformed = manifest()
        malformed["splits"]["DEV"]["scenario_hashes"][SCENARIO["scenario_id"]] = "not-a-hash"
        with self.assertRaises(ValueError):
            pilot.validate_v2_scenario(SCENARIO, malformed, "DEV")
        malformed = manifest()
        malformed["splits"]["DEV"]["scenario_ids"].append({"unexpected": "object"})
        with self.assertRaises(ValueError):
            pilot.validate_v2_scenario(SCENARIO, malformed, "DEV")

    def test_policy_hash_and_candidate_limit_are_pinned(self):
        self.require_module()
        config = optimizer.build_optimizer_config(manifest(), policy_hash=POLICY_HASH, candidate_limit=2)
        self.assertEqual(config["starting_naturalness_policy_sha256"], POLICY_HASH)
        self.assertEqual(config["candidate_limit"], 2)
        for count in (0, 101):
            with self.subTest(count=count), self.assertRaises(ValueError):
                optimizer.build_optimizer_config(manifest(), policy_hash=POLICY_HASH, candidate_limit=count)
        with self.assertRaises(ValueError):
            optimizer.build_optimizer_config(manifest(), policy_hash="not-a-sha", candidate_limit=2)

    def test_optimizer_approval_is_required_and_must_match_dataset_split_hash_and_budgets(self):
        self.require_module()
        config = optimizer.build_optimizer_config(manifest(), policy_hash=POLICY_HASH, candidate_limit=3)
        with self.assertRaises(PermissionError):
            optimizer.authorize_optimizer_run(config, None)
        approval = {"approved": True, "approval_id": "owner-approval-v2-1", "dataset_id": "synthetic-v2-fixture-set",
                    "split": "DEV", "max_candidate_count": 2, "max_model_calls": 8,
                    "starting_naturalness_policy_sha256": POLICY_HASH}
        authorized = optimizer.authorize_optimizer_run(config, approval)
        self.assertTrue(authorized["authorized"])
        self.assertFalse(authorized["execution_enabled"])
        self.assertEqual(authorized["authorization"]["max_model_calls"], 8)
        for field, value in (("split", "SEALED_HOLDOUT"), ("dataset_id", "other-dataset"),
                             ("max_candidate_count", 4), ("max_model_calls", 10001),
                             ("starting_naturalness_policy_sha256", "e" * 64)):
            invalid = dict(approval); invalid[field] = value
            with self.subTest(field=field), self.assertRaises(PermissionError):
                optimizer.authorize_optimizer_run(config, invalid)
        tampered = dict(config)
        tampered["training_scenario_ids"] = ["v2-sealed-one"]
        with self.assertRaises(PermissionError):
            optimizer.authorize_optimizer_run(tampered, approval)

    def test_naturalness_v2_compile_path_does_not_call_legacy_frozen_pilot_loaders(self):
        self.require_module()
        class GuardPilot:
            @staticmethod
            def validate_v2_scenario(scenario, split_manifest, split):
                return pilot.validate_v2_scenario(scenario, split_manifest, split)
            @staticmethod
            def load_frozen_pilot(*args, **kwargs):
                raise AssertionError("legacy frozen pilot loader was called")
            @staticmethod
            def _inputs(*args, **kwargs):
                raise AssertionError("legacy Benchmark V1 loader was called")

        original_module = naturalness._module
        def guarded_module(lab, name):
            if name == "pilot":
                return GuardPilot
            return original_module(lab, name)

        with patch.object(naturalness, "_inputs", side_effect=AssertionError("legacy Naturalness input loader was called")), \
             patch.object(naturalness, "_module", side_effect=guarded_module):
            arms = naturalness.compile_v2_arms(
                SCENARIO, {"persona": {"name": "Агата"}, "context": {}, "memory": []},
                (LAB/"configs/naturalness-policy-v1.md").read_text(encoding="utf-8"),
                run_id="offline-v2-run", split_manifest=manifest(),
                policy_sha256=__import__("hashlib").sha256((LAB/"configs/naturalness-policy-v1.md").read_bytes()).hexdigest(),
            )
        self.assertEqual(len(arms), 2)
        with self.assertRaises(PermissionError):
            pilot.validate_v2_scenario({**SCENARIO, "scenario_id": "daily-01"}, manifest(), "DEV")


if __name__ == "__main__":
    unittest.main()
