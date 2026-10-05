"""Regression tests for the offline-only AGATA Naturalness experiment."""
import copy
import hashlib
import importlib.util
import json
import re
import socket
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


LAB = Path(__file__).resolve().parents[1]
REPO = LAB.parents[2]
NATURALNESS_CLI = LAB / "evaluators/naturalness_cli.py"
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
    def test_ru_naturalness_dev_preflight_never_decodes_sealed_holdout_scenarios(self):
        original_loads = json.loads
        attempted_holdout_ids = []

        def reject_sealed_scenario(payload, *args, **kwargs):
            if isinstance(payload, str) and '"dialogue_turns"' in payload:
                match = re.search(r'"scenario_id"\s*:\s*"([^"]+)"', payload)
                if match and match.group(1).endswith("-01"):
                    attempted_holdout_ids.append(match.group(1))
                    raise AssertionError("sealed holdout dialogue was decoded")
            return original_loads(payload, *args, **kwargs)

        with patch.object(json, "loads", side_effect=reject_sealed_scenario):
            config = NATURALNESS.load_naturalness_config(LAB)
            baseline, candidate = NATURALNESS.build_ab_invocations(
                LAB, config, "ru_naturalness-02", 0,
                {"baseline": [], "naturalness-v1": []},
            )
            dry_run = NATURALNESS.run_fixture_dry_run(LAB, config)

        self.assertEqual(attempted_holdout_ids, [])
        self.assertEqual(dry_run["status"], "FIXTURE_ONLY")
        self.assertEqual(dry_run["development_scenarios"], 16)
        self.assertEqual(dry_run["live_inference_requests"], 0)
        self.assertTrue(baseline["fixture_only"])
        self.assertTrue(candidate["fixture_only"])
        self.assertEqual(baseline["user_turn"], candidate["user_turn"])
        self.assertEqual(baseline["context"], candidate["context"])
        self.assertEqual(baseline["persona"], candidate["persona"])
        self.assertEqual(baseline["memory"], candidate["memory"])
        self.assertEqual(baseline["history"], candidate["history"])
        self.assertEqual(
            {key: value for key, value in baseline["core_request"].items() if key != "instructions"},
            {key: value for key, value in candidate["core_request"].items() if key != "instructions"},
        )
        self.assertNotEqual(baseline["core_request"]["instructions"], candidate["core_request"]["instructions"])

    def test_documented_fixture_cli_reports_only_offline_preparation(self):
        readme = (LAB / "README.md").read_text(encoding="utf-8")
        command = "python3 services/agata/conversation-lab/evaluators/naturalness_cli.py dry-run"
        self.assertIn(command, readme, "README must document the exact local fixture-only command")
        self.assertTrue(NATURALNESS_CLI.exists(), "documented offline CLI has not been implemented")
        completed = subprocess.run(
            ["python3", str(NATURALNESS_CLI), "dry-run"], cwd=REPO,
            check=True, text=True, capture_output=True, timeout=60,
        )
        report = json.loads(completed.stdout)
        self.assertEqual(report["status"], "FIXTURE_ONLY")
        self.assertEqual(report["development_scenarios"], 16)
        self.assertEqual(report["live_inference_requests"], 0)
        self.assertEqual(report["holdout_scenarios_used"], 0)
        self.assertEqual(report["quality_status"], "NO_MODEL_OUTPUTS_OWNER_REVIEW_NOT_APPLICABLE")

    def test_naturalness_documentation_separates_future_integration_layers(self):
        readme = (LAB / "README.md").read_text(encoding="utf-8")
        for layer in ("SHARED policy", "PER MODEL Persona", "RELATIONSHIP context", "MEMORY"):
            self.assertIn(layer, readme)
        self.assertIn("отдельн", readme.casefold())
        self.assertIn("не запускает модель", readme.casefold())

    def test_phase4_diff_stays_inside_conversation_lab(self):
        changed = subprocess.run(
            ["git", "diff", "--name-only", NATURALNESS.PHASE3_BASE_SHA], cwd=REPO,
            check=True, text=True, capture_output=True,
        ).stdout.splitlines()
        self.assertTrue(changed)
        allowed = "services/agata/conversation-lab/"
        self.assertTrue(all(path.startswith(allowed) for path in changed), f"out-of-scope changed paths: {[path for path in changed if not path.startswith(allowed)]}")

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

    def test_phase3_invocation_snapshot_and_dev_allowlist_are_preserved(self):
        pilot = NATURALNESS._module(LAB, "pilot")
        frozen = pilot.load_frozen_pilot(LAB)
        scenario = pilot.scenario_by_id(LAB, frozen, "long_memory-01")
        history = [
            {"role": "user", "text": scenario["dialogue_turns"][0]["text"]},
            {"role": "assistant", "text": "СИНТЕТИЧЕСКИЙ FIXTURE RESPONSE — не ответ модели."},
        ]
        request = pilot.build_invocation(LAB, frozen, "long_memory-01", "codex-cli-baseline", 1, history)
        encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), "05f2a3fb262f72fda306134dabc5f8945f8eecc43a12b029ecd7ab70a2b4ef0e")
        dev_scenario = pilot._inputs(LAB)[-1]["daily-02"]
        with self.assertRaisesRegex(ValueError, "outside frozen pilot"):
            pilot.build_invocation(LAB, frozen, "daily-02", "codex-cli-baseline", 0, [])
        self.assertTrue(callable(getattr(pilot, "build_fixture_invocation", None)), "pure hash-checked serializer has not been extracted")
        if callable(getattr(pilot, "build_fixture_invocation", None)):
            self.assertEqual(
                pilot.build_fixture_invocation(LAB, scenario, frozen["scenario_hashes"]["long_memory-01"], "codex-cli-baseline", 1, history),
                request,
            )
            with self.assertRaisesRegex(ValueError, "scenario hash"):
                pilot.build_fixture_invocation(LAB, dev_scenario, frozen["scenario_hashes"]["long_memory-01"], "codex-cli-baseline", 0, [])

    def test_ab_builder_is_available_for_only_manifested_development_scenarios(self):
        self.assertTrue(callable(getattr(NATURALNESS, "build_ab_invocations", None)), "Naturalness A/B request builder has not been implemented")

    @unittest.skipUnless(NATURALNESS is not None and callable(getattr(NATURALNESS, "build_ab_invocations", None)), "RED scaffold: A/B builder not implemented yet")
    def test_ab_builder_changes_only_candidate_instructions_and_keeps_histories_separate(self):
        config = NATURALNESS.load_naturalness_config(LAB)
        sid = "long_memory-02"
        pilot = NATURALNESS._module(LAB, "pilot")
        scenario = pilot._inputs(LAB)[-1][sid]
        histories = {
            "baseline": [
                {"role": "user", "text": scenario["dialogue_turns"][0]["text"]},
                {"role": "assistant", "text": "baseline-only previous reply"},
            ],
            "naturalness-v1": [
                {"role": "user", "text": scenario["dialogue_turns"][0]["text"]},
                {"role": "assistant", "text": "candidate-only previous reply"},
            ],
        }
        baseline, candidate = NATURALNESS.build_ab_invocations(LAB, config, sid, 1, histories)
        self.assertEqual((baseline["fixture_only"], candidate["fixture_only"]), (True, True))
        for key in set(baseline) - {"core_request", "history"}:
            self.assertEqual(baseline[key], candidate[key], key)
        self.assertEqual(set(baseline["core_request"]) - {"instructions", "history"}, set(candidate["core_request"]) - {"instructions", "history"})
        for key in set(baseline["core_request"]) - {"instructions", "history"}:
            self.assertEqual(baseline["core_request"][key], candidate["core_request"][key], key)
        self.assertEqual(baseline["history"][1]["text"], "baseline-only previous reply")
        self.assertEqual(candidate["history"][1]["text"], "candidate-only previous reply")
        self.assertNotIn("candidate-only previous reply", baseline["core_request"]["instructions"])
        self.assertIn(NATURALNESS.load_policy(LAB, config), candidate["core_request"]["instructions"])
        for example in NATURALNESS.candidate_visible_examples(NATURALNESS.load_examples(LAB, config)):
            for turn in example["turns"]:
                self.assertIn(turn["text"], candidate["core_request"]["instructions"])
        self.assertNotIn("anti_pattern", candidate["core_request"]["instructions"])
        self.assertNotIn("review_only", candidate["core_request"]["instructions"])
        for example in NATURALNESS.load_examples(LAB, config):
            for review_value in example["review_only"].values():
                self.assertNotIn(review_value, candidate["core_request"]["instructions"])
        with self.assertRaisesRegex(ValueError, "development"):
            NATURALNESS.build_ab_invocations(LAB, config, "long_memory-01", 0, {"baseline": [], "naturalness-v1": []})
        with self.assertRaises(ValueError):
            NATURALNESS.build_ab_invocations(LAB, config, sid, 1, {"baseline": [], "naturalness-v1": []})

    @unittest.skipUnless(NATURALNESS is not None and callable(getattr(NATURALNESS, "run_fixture_dry_run", None)), "RED scaffold: Naturalness fixture runner not implemented yet")
    def test_ab_fixture_run_is_offline_and_marks_outputs_non_evidentiary(self):
        config = NATURALNESS.load_naturalness_config(LAB)
        with patch.object(socket, "create_connection", side_effect=AssertionError("network access is forbidden")), patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess access is forbidden")):
            report = NATURALNESS.run_fixture_dry_run(LAB, config)
        self.assertEqual(report["planned_candidate_turns"], sum(config["development"]["scenario_rounds"].values()) * 2)
        self.assertEqual(report["live_inference_requests"], 0)
        self.assertEqual(report["quality_status"], "NO_MODEL_OUTPUTS_OWNER_REVIEW_NOT_APPLICABLE")
        self.assertTrue(report["fixture_only"])

    def test_blind_fixture_packet_api_is_available(self):
        self.assertTrue(callable(getattr(NATURALNESS, "fixture_ab_result_rows", None)), "fixture transcript fixture has not been implemented")
        self.assertTrue(callable(getattr(NATURALNESS, "package_blind_fixture", None)), "Naturalness blind packet wrapper has not been implemented")

    @unittest.skipUnless(NATURALNESS is not None and callable(getattr(NATURALNESS, "package_blind_fixture", None)), "RED scaffold: blind packet wrapper not implemented yet")
    def test_blind_fixture_packet_is_anonymous_seeded_private_and_not_a_model_result(self):
        config = NATURALNESS.load_naturalness_config(LAB)
        rows = NATURALNESS.fixture_ab_result_rows(LAB, config)
        packet, private = NATURALNESS.package_blind_fixture(LAB, config, rows, 41004)
        repeated, repeated_private = NATURALNESS.package_blind_fixture(LAB, config, rows, 41004)
        self.assertEqual(packet, repeated)
        self.assertEqual(private, repeated_private)
        self.assertEqual(packet["completion_status"], "FIXTURE_ONLY")
        self.assertEqual(packet["quality_status"], "NO_MODEL_OUTPUTS_OWNER_REVIEW_NOT_APPLICABLE")
        self.assertIsNone(packet["winner"])
        self.assertEqual(len(packet["scenarios"]), 16)
        self.assertTrue(all(len(scenario["candidates"]) == 2 for scenario in packet["scenarios"]))
        serialized_packet = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn("baseline", serialized_packet)
        self.assertNotIn("naturalness-v1", serialized_packet)
        self.assertNotIn("codex-cli-baseline", serialized_packet)
        self.assertNotIn("review_only", serialized_packet)
        self.assertNotIn("anti_pattern_id", serialized_packet)
        self.assertTrue(private["private"])
        self.assertEqual(private["mapping_sha256"], NATURALNESS.stable_hash(private["mappings"]))
        self.assertEqual({row["model_id"] for row in private["mappings"]}, {"baseline", "naturalness-v1"})
        self.assertNotIn("model_id", serialized_packet)
        _, seed_change_private = NATURALNESS.package_blind_fixture(LAB, config, rows, 41005)
        order = lambda value: [(entry["scenario_id"], entry["label"], entry["model_id"]) for entry in value["mappings"]]
        self.assertNotEqual(order(private), order(seed_change_private))

        ratings = NATURALNESS._module(LAB, "ratings")
        template = ratings.empty_ratings(packet, LAB)
        self.assertEqual(len(template["ratings"]), 32)
        with self.assertRaisesRegex(ValueError, "OWNER explanation"):
            ratings.lock_ratings(packet, template, cycle_id="naturalness-fixture-no-ratings")

    @unittest.skipUnless(NATURALNESS is not None and callable(getattr(NATURALNESS, "package_blind_fixture", None)), "RED scaffold: blind packet wrapper not implemented yet")
    def test_blind_fixture_rejects_incomplete_duplicate_extra_and_mismatched_transcripts(self):
        config = NATURALNESS.load_naturalness_config(LAB)
        rows = NATURALNESS.fixture_ab_result_rows(LAB, config)
        for mutation in (
            lambda changed: changed.pop(),
            lambda changed: changed.append(copy.deepcopy(changed[0])),
            lambda changed: changed[0]["turns"].append({"role": "assistant", "text": "extra"}),
            lambda changed: changed[0]["turns"].__setitem__(0, {"role": "user", "text": "wrong user"}),
            lambda changed: changed[0].__setitem__("fixture_only", False),
        ):
            changed = copy.deepcopy(rows)
            mutation(changed)
            with self.subTest(rows=len(changed)):
                with self.assertRaises(ValueError):
                    NATURALNESS.package_blind_fixture(LAB, config, changed, 41004)

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
    def test_example_user_turns_are_not_exact_copies_of_development_turns(self):
        config = NATURALNESS.load_naturalness_config(LAB)
        examples = NATURALNESS.load_examples(LAB, config)
        benchmark, _, scenarios, _, _, _ = NATURALNESS._inputs(LAB)
        normalize = lambda value: " ".join(value.casefold().split())
        benchmark_turns = {normalize(turn["text"]) for scenario in scenarios for turn in scenario["dialogue_turns"] if turn["role"] == "user"}
        example_turns = [normalize(turn["text"]) for item in examples for turn in item["candidate_turns"] if turn["role"] == "user"]
        self.assertEqual(len(example_turns), len(set(example_turns)))
        self.assertTrue(set(example_turns).isdisjoint(benchmark_turns))


if __name__ == "__main__":
    unittest.main()
