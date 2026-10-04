"""Regression tests for the offline-only AGATA Naturalness experiment."""
import copy
import hashlib
import importlib.util
import json
import unittest
from pathlib import Path


LAB = Path(__file__).resolve().parents[1]
CATEGORIES = (
    "ru_naturalness", "daily", "humor", "support", "romance", "persona",
    "initiative", "topic_change", "return_after_break", "long_memory",
    "fact_update", "false_memory", "disagreement", "no_excess_advice",
    "long_dialogue", "boundaries",
)
EXPECTED_DEVELOPMENT_IDS = [f"{category}-02" for category in CATEGORIES]
EXPECTED_BENCHMARK_CONFIG_SHA256 = "13f34ec48fbca65c994877550eeebde60a3476c4d9e073888674f53de0186d11"
EXPECTED_PILOT_CONFIG_SHA256 = "ea45217d858d1add4916280b4eb44290e79a7a46ca4c8a34cae94558fecd4adc"
EXPECTED_BENCHMARK_JSONL_SHA256 = "07a1dad1a2411ea8f00741c64a49196561b8debaf43ab999170c4385c5acfd9f"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NATURALNESS_PATH = LAB / "evaluators/naturalness.py"
NATURALNESS = load_module("naturalness", NATURALNESS_PATH) if NATURALNESS_PATH.exists() else None
POLICY_API_AVAILABLE = NATURALNESS is not None and callable(getattr(NATURALNESS, "load_policy", None)) and callable(getattr(NATURALNESS, "load_examples", None))


class NaturalnessConfigTests(unittest.TestCase):
    def test_naturalness_config_api_is_available(self):
        self.assertIsNotNone(NATURALNESS, "offline Naturalness config loader has not been implemented")
        self.assertTrue(callable(getattr(NATURALNESS, "create_naturalness_config", None)))
        self.assertTrue(callable(getattr(NATURALNESS, "validate_naturalness_config", None)))

    @unittest.skipIf(NATURALNESS is None, "RED scaffold: loader not implemented yet")
    def test_development_set_is_each_category_second_scenario(self):
        config = NATURALNESS.create_naturalness_config(LAB)
        self.assertEqual(config["development"]["scenario_ids"], EXPECTED_DEVELOPMENT_IDS)
        self.assertEqual(config["holdout"]["scenario_ids"], [f"{category}-01" for category in CATEGORIES])
        self.assertTrue(set(config["development"]["scenario_ids"]).isdisjoint(config["holdout"]["scenario_ids"]))

    @unittest.skipIf(NATURALNESS is None, "RED scaffold: loader not implemented yet")
    def test_long_dialogue_development_fixture_keeps_twelve_rounds(self):
        config = NATURALNESS.create_naturalness_config(LAB)
        self.assertEqual(config["development"]["scenario_rounds"]["long_dialogue-02"], 12)

    @unittest.skipIf(NATURALNESS is None, "RED scaffold: loader not implemented yet")
    def test_config_pins_existing_benchmark_and_pilot_bytes(self):
        config = NATURALNESS.create_naturalness_config(LAB)
        hashes = config["source_hashes"]
        self.assertEqual(hashes["benchmark_config_sha256"], EXPECTED_BENCHMARK_CONFIG_SHA256)
        self.assertEqual(hashes["pilot_config_sha256"], EXPECTED_PILOT_CONFIG_SHA256)
        self.assertEqual(hashes["benchmark_scenarios_sha256"], EXPECTED_BENCHMARK_JSONL_SHA256)

    @unittest.skipIf(NATURALNESS is None, "RED scaffold: loader not implemented yet")
    def test_config_has_hashes_for_all_development_and_holdout_scenarios(self):
        config = NATURALNESS.create_naturalness_config(LAB)
        self.assertEqual(set(config["development"]["scenario_hashes"]), set(EXPECTED_DEVELOPMENT_IDS))
        self.assertEqual(set(config["holdout"]["scenario_hashes"]), set(config["holdout"]["scenario_ids"]))
        self.assertTrue(all(len(value) == 64 for value in config["development"]["scenario_hashes"].values()))

    @unittest.skipIf(NATURALNESS is None, "RED scaffold: loader not implemented yet")
    def test_validation_rejects_duplicate_missing_and_tampered_scenarios(self):
        original = NATURALNESS.create_naturalness_config(LAB)
        for mutate in (
            lambda value: value["development"]["scenario_ids"].__setitem__(1, value["development"]["scenario_ids"][0]),
            lambda value: value["development"]["scenario_ids"].pop(),
            lambda value: value["development"]["scenario_hashes"].__setitem__("daily-02", "0" * 64),
        ):
            changed = copy.deepcopy(original)
            mutate(changed)
            with self.subTest(changed=changed["development"]["scenario_ids"]):
                with self.assertRaises(ValueError):
                    NATURALNESS.validate_naturalness_config(LAB, changed)

    @unittest.skipIf(NATURALNESS is None, "RED scaffold: loader not implemented yet")
    def test_candidate_manifest_contains_only_ids_and_hashes(self):
        config = NATURALNESS.create_naturalness_config(LAB)
        serialized = json.dumps(config, ensure_ascii=False)
        for forbidden in ("expected_behavior", "failure_patterns", "evaluation_notes", "dialogue_turns"):
            self.assertNotIn(forbidden, serialized)

    @unittest.skipIf(NATURALNESS is None, "RED scaffold: loader not implemented yet")
    def test_load_revalidates_the_checked_in_manifest(self):
        config = NATURALNESS.load_naturalness_config(LAB)
        self.assertEqual(NATURALNESS.validate_naturalness_config(LAB, config), config)

    def test_naturalness_policy_and_example_api_is_available(self):
        self.assertTrue(POLICY_API_AVAILABLE, "versioned policy and synthetic example validators have not been implemented")

    @unittest.skipUnless(POLICY_API_AVAILABLE, "RED scaffold: policy/examples not implemented yet")
    def test_policy_hash_is_bound_to_the_frozen_manifest(self):
        config = NATURALNESS.load_naturalness_config(LAB)
        self.assertEqual(config["artifact_hashes"]["policy_sha256"], hashlib.sha256((LAB / "configs/naturalness-policy-v1.md").read_bytes()).hexdigest())
        changed = copy.deepcopy(config)
        changed["artifact_hashes"]["policy_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            NATURALNESS.validate_naturalness_config(LAB, changed)

    @unittest.skipUnless(POLICY_API_AVAILABLE, "RED scaffold: policy/examples not implemented yet")
    def test_examples_are_authored_synthetic_and_have_distinct_ids(self):
        config = NATURALNESS.load_naturalness_config(LAB)
        examples = NATURALNESS.load_examples(LAB, config)
        self.assertEqual(len(examples), 15)
        self.assertEqual(len({item["example_id"] for item in examples}), 15)
        self.assertTrue(all(item["provenance"] == "synthetic_authored" for item in examples))
        expected_patterns = {
            "excess-sympathy", "repetitive-understanding", "unsolicited-advice", "psychological-analysis",
            "over-literary-russian", "parroting", "mandatory-question-ending", "forced-positivity",
            "no-humor-or-character", "repeated-answer-structure", "excessive-length", "awkward-topic-transition",
            "excessive-initiative", "empty-agreement", "fabricated-memory",
        }
        self.assertEqual({item["review_only"]["anti_pattern_id"] for item in examples}, expected_patterns)

    @unittest.skipUnless(POLICY_API_AVAILABLE, "RED scaffold: policy/examples not implemented yet")
    def test_positive_examples_exclude_evaluator_only_anti_patterns_and_rationale(self):
        config = NATURALNESS.load_naturalness_config(LAB)
        policy = NATURALNESS.load_policy(LAB, config)
        examples = NATURALNESS.load_examples(LAB, config)
        candidate_examples = NATURALNESS.candidate_visible_examples(examples)
        candidate_json = json.dumps(candidate_examples, ensure_ascii=False)
        full_json = json.dumps(examples, ensure_ascii=False)
        self.assertEqual(len(candidate_examples), 15)
        self.assertNotIn("Evidence map", policy)
        self.assertIn("supplied context and memory", policy)
        self.assertNotIn("anti_pattern", candidate_json)
        self.assertNotIn("rationale", candidate_json)
        self.assertIn("anti_pattern", full_json)
        self.assertIn("rationale", full_json)

    @unittest.skipUnless(POLICY_API_AVAILABLE, "RED scaffold: policy/examples not implemented yet")
    def test_example_user_turns_are_not_exact_copies_of_benchmark_turns(self):
        config = NATURALNESS.load_naturalness_config(LAB)
        examples = NATURALNESS.load_examples(LAB, config)
        benchmark = NATURALNESS._module(LAB, "benchmark")
        scenario_path = LAB / "data/golden/synthetic/agata-benchmark-v1.jsonl"
        scenarios = benchmark.load_scenarios(scenario_path)
        normalize = lambda value: " ".join(value.casefold().split())
        benchmark_turns = {normalize(turn["text"]) for scenario in scenarios for turn in scenario["dialogue_turns"] if turn["role"] == "user"}
        example_turns = [normalize(turn["text"]) for item in examples for turn in item["candidate_turns"] if turn["role"] == "user"]
        self.assertEqual(len(example_turns), len(set(example_turns)))
        self.assertTrue(set(example_turns).isdisjoint(benchmark_turns))


if __name__ == "__main__":
    unittest.main()
