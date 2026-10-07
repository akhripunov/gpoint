"""Offline coordination and budget contracts; fixtures are synthetic only."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


LAB = Path(__file__).resolve().parents[1]
COORDINATOR_PATH = LAB / "evaluators" / "run_coordinator.py"
SPEC = importlib.util.spec_from_file_location("agata_run_coordinator", COORDINATOR_PATH) if COORDINATOR_PATH.exists() else None
coordinator = importlib.util.module_from_spec(SPEC) if SPEC else None
if SPEC and SPEC.loader:
    SPEC.loader.exec_module(coordinator)
NATURALNESS_PATH = LAB / "evaluators" / "naturalness.py"
NATURALNESS_SPEC = importlib.util.spec_from_file_location("agata_naturalness_v2_coordinator", NATURALNESS_PATH)
naturalness = importlib.util.module_from_spec(NATURALNESS_SPEC)
NATURALNESS_SPEC.loader.exec_module(naturalness)


def synthetic_scenario():
    return {
        "scenario_id": "synthetic-daily-02",
        "language": "ru",
        "dialogue_turns": [
            {"role": role, "text": text} if role == "user" else {"role": role, "response_slot": True}
            for role, text in [
                ("user", "Синтетическая реплика 1"), ("assistant", ""),
                ("user", "Синтетическая реплика 2"), ("assistant", ""),
                ("user", "Синтетическая реплика 3"), ("assistant", ""),
                ("user", "Синтетическая реплика 4"), ("assistant", ""),
            ]
        ],
    }


def fixtures():
    return {
        "persona": {"name": "Агата", "voice": "synthetic fixed persona"},
        "context": {"setting": "offline fixture"},
        "memory": [],
    }


class RunCoordinatorTests(unittest.TestCase):
    def require_module(self):
        self.assertIsNotNone(coordinator, "parity-checked run coordinator is not implemented")

    def test_compiles_exactly_two_private_arms_with_only_treatment_difference(self):
        self.require_module()
        scenario, initial = synthetic_scenario(), fixtures()
        arms = coordinator.compile_arms(scenario, initial, {"naturalness_policy": "brief synthetic policy"}, run_id="batch-001")
        self.assertEqual(len(arms), 2)
        self.assertEqual({arm["private"]["configuration_id"] for arm in arms}, {"baseline", "naturalness-v1"})
        baseline = next(arm for arm in arms if arm["private"]["configuration_id"] == "baseline")
        candidate = next(arm for arm in arms if arm["private"]["configuration_id"] == "naturalness-v1")
        self.assertEqual(set(baseline), {"private", "model_input", "history"})
        self.assertEqual(set(candidate), {"private", "model_input", "history"})
        baseline_input = dict(baseline["model_input"])
        candidate_input = dict(candidate["model_input"])
        self.assertIsNone(baseline_input.pop("treatment"))
        self.assertEqual(candidate_input.pop("treatment"), {"naturalness_policy": "brief synthetic policy"})
        self.assertEqual(baseline_input, candidate_input)
        self.assertIsNot(baseline["history"], candidate["history"])
        self.assertEqual(baseline["history"], [])
        self.assertEqual(candidate["history"], [])
        self.assertEqual(scenario, synthetic_scenario())
        self.assertEqual(initial, fixtures())

    def test_naturalness_v2_entry_uses_explicit_development_inputs_without_legacy_loader(self):
        self.require_module()
        original_loader = naturalness._inputs
        naturalness._inputs = lambda _lab: (_ for _ in ()).throw(AssertionError("legacy frozen pilot must not load"))
        try:
            scenario = synthetic_scenario()
            pilot = naturalness._module(LAB, "pilot")
            split_manifest = {
                "schema_version": 2,
                "legacy_holdout_v1": {"status": "COMPROMISED_EXCLUDED", "optimizer_access": False,
                                       "scenario_content_read": False, "scenario_content_used_for_v2": False},
                "splits": {"DEV": {"scenario_ids": [scenario["scenario_id"]],
                                    "scenario_hashes": {scenario["scenario_id"]: pilot.stable_hash(scenario)}}},
            }
            arms = naturalness.compile_v2_arms(
                scenario, fixtures(), (LAB/"configs/naturalness-policy-v1.md").read_text(encoding="utf-8"),
                run_id="batch-safe-v2", split_manifest=split_manifest,
                policy_sha256=__import__("hashlib").sha256((LAB/"configs/naturalness-policy-v1.md").read_bytes()).hexdigest(),
            )
        finally:
            naturalness._inputs = original_loader
        self.assertEqual(len(arms), 2)
        self.assertEqual(arms[0]["model_input"]["scenario_id"], "synthetic-daily-02")

    def test_v2_candidate_treatment_must_match_frozen_policy_hash(self):
        import hashlib
        self.require_module()
        scenario=synthetic_scenario()
        pilot=naturalness._module(LAB,"pilot")
        manifest={"schema_version":2,"legacy_holdout_v1":{"status":"COMPROMISED_EXCLUDED","optimizer_access":False,
                  "scenario_content_read":False,"scenario_content_used_for_v2":False},
                  "splits":{"DEV":{"scenario_ids":[scenario["scenario_id"]],"scenario_hashes":{scenario["scenario_id"]:pilot.stable_hash(scenario)}}}}
        treatment=(LAB/"configs/naturalness-policy-v1.md").read_text(encoding="utf-8")
        expected=hashlib.sha256(treatment.encode()).hexdigest()
        arms=naturalness.compile_v2_arms(scenario,fixtures(),treatment,run_id="frozen-policy",split_manifest=manifest,policy_sha256=expected)
        self.assertEqual(arms[1]["model_input"]["treatment"],treatment)
        with self.assertRaises(ValueError):
            naturalness.compile_v2_arms(scenario,fixtures(),"mutated",run_id="frozen-policy",split_manifest=manifest,policy_sha256=expected)

    def test_turn_requests_are_single_turn_and_histories_never_cross_arms(self):
        self.require_module()
        arms = coordinator.compile_arms(synthetic_scenario(), fixtures(), "naturalness treatment", run_id="batch-002")
        baseline = next(arm for arm in arms if arm["private"]["configuration_id"] == "baseline")
        candidate = next(arm for arm in arms if arm["private"]["configuration_id"] == "naturalness-v1")
        with tempfile.TemporaryDirectory() as workdir:
            first_baseline = coordinator.build_turn_request(baseline, 0, working_dir=workdir)
            first_candidate = coordinator.build_turn_request(candidate, 0, working_dir=workdir)
        self.assertEqual(first_baseline["providers"][0]["config"]["model"], "gpt-6-luna")
        self.assertEqual(first_baseline["providers"][0]["config"]["reasoning_effort"], "medium")
        self.assertIn("Синтетическая реплика 1", first_baseline["prompts"][0])
        self.assertNotIn("naturalness treatment", first_baseline["prompts"][0])
        self.assertIn("naturalness treatment", first_candidate["prompts"][0])
        coordinator.record_assistant_reply(baseline, 0, "Только ответ baseline")
        coordinator.record_assistant_reply(candidate, 0, "Только ответ candidate")
        with tempfile.TemporaryDirectory() as workdir:
            second_baseline = coordinator.build_turn_request(baseline, 1, working_dir=workdir)
            second_candidate = coordinator.build_turn_request(candidate, 1, working_dir=workdir)
        self.assertIn("Только ответ baseline", second_baseline["prompts"][0])
        self.assertNotIn("Только ответ baseline", second_candidate["prompts"][0])
        self.assertIn("Только ответ candidate", second_candidate["prompts"][0])
        self.assertIn("Синтетическая реплика 2", second_baseline["prompts"][0])
        with self.assertRaises(ValueError):
            with tempfile.TemporaryDirectory() as workdir:
                coordinator.build_turn_request(baseline, 2, working_dir=workdir)

    def test_arm_limit_tracks_four_baseline_and_four_candidate(self):
        self.require_module()
        ledger = coordinator.CallLedger(max_arms=8, max_generator_invocations=32, max_judge_invocations=15)
        for index in range(4):
            ledger.reserve("arm", f"baseline-{index}", configuration="baseline", metadata={"arm_id":f"baseline-{index}","scenario_id":f"s{index}"})
        for index in range(4):
            ledger.reserve("arm", f"candidate-{index}", configuration="naturalness-v1", metadata={"arm_id":f"candidate-{index}","scenario_id":f"s{index}"})
        with self.assertRaises(OverflowError):
            ledger.reserve("arm", "candidate-overflow", configuration="naturalness-v1")
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["conversation_arms"]["attempted"], 8)
        self.assertEqual(snapshot["conversation_arms"]["by_configuration"], {"baseline": 4, "naturalness-v1": 4})

    def test_generator_and_judge_budgets_are_separate_and_enforced(self):
        self.require_module()
        ledger = coordinator.CallLedger(max_arms=8, max_generator_invocations=32, max_judge_invocations=15)
        for index in range(8):
            config="baseline" if index < 4 else "naturalness-v1";arm=f"arm-{index}";sid=f"s{index%4}"
            ledger.reserve("arm", arm, configuration=config, metadata={"arm_id":arm,"scenario_id":sid})
        for arm_index in range(8):
            config="baseline" if arm_index < 4 else "naturalness-v1";arm=f"arm-{arm_index}";sid=f"s{arm_index%4}"
            for turn in range(4):
                index=arm_index*4+turn
                ledger.reserve("generator", f"gen-{index}", metadata={"arm_id":arm,"turn_index":turn,"scenario_id":sid,"configuration":config})
                ledger.complete("generator", f"gen-{index}", "success")
            ledger.complete("arm", arm, "success")
        for index in range(15):
            ledger.reserve("judge", f"judge-{index}", metadata={"judge_id":f"judge-{index}"})
            ledger.complete("judge", f"judge-{index}", "success")
        with self.assertRaises(OverflowError):
            ledger.reserve("generator", "gen-overflow")
        with self.assertRaises(OverflowError):
            ledger.reserve("judge", "judge-overflow")
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["generator_invocations"]["attempted"], 32)
        self.assertEqual(snapshot["judge_invocations"]["attempted"], 15)
        self.assertEqual(snapshot["conversation_arms"]["attempted"], 8)

    def test_ambiguous_invocation_is_charged_once_and_cannot_be_retried(self):
        self.require_module()
        ledger = coordinator.CallLedger()
        ledger.reserve("arm", "arm", configuration="baseline", metadata={"arm_id":"arm","scenario_id":"scenario"})
        ledger.reserve("generator", "run/scenario/arm/turn-0", metadata={"arm_id":"arm","turn_index":0,"scenario_id":"scenario","configuration":"baseline"})
        ledger.complete("generator", "run/scenario/arm/turn-0", "timeout")
        with self.assertRaises(ValueError):
            ledger.reserve("generator", "run/scenario/arm/turn-1", metadata={"arm_id":"arm","turn_index":1,"scenario_id":"scenario","configuration":"baseline"})
        snapshot = ledger.snapshot()["generator_invocations"]
        self.assertEqual(snapshot["attempted"], 1)
        self.assertEqual(snapshot["outcomes"], {"timeout": 1})
        self.assertEqual(snapshot["in_flight"], 0)

    def test_invalid_stage_duplicate_completion_and_non_sequential_turns_fail_closed(self):
        self.require_module()
        ledger = coordinator.CallLedger()
        with self.assertRaises(ValueError):
            ledger.reserve("unknown", "id")
        ledger.reserve("judge", "j-1", metadata={"judge_id":"j-1"})
        ledger.complete("judge", "j-1", "success")
        with self.assertRaises(ValueError):
            ledger.complete("judge", "j-1", "success")
        arm = coordinator.compile_arms(synthetic_scenario(), fixtures(), "treatment", run_id="batch-3")[0]
        with self.assertRaises(ValueError):
            coordinator.record_assistant_reply(arm, 0, "too early")

    def test_durable_ledger_recovers_interrupted_attempt_and_forbids_retry(self):
        self.require_module()
        with tempfile.TemporaryDirectory() as folder:
            journal=Path(folder)/"calls.jsonl"
            first=coordinator.CallLedger(journal_path=journal)
            first.reserve("arm","arm-1",configuration="baseline",metadata={"arm_id":"arm-1","scenario_id":"s1"})
            first.reserve("generator","attempt-1",metadata={"arm_id":"arm-1","turn_index":0,"scenario_id":"s1","configuration":"baseline"})
            recovered=coordinator.CallLedger(journal_path=journal)
            self.assertTrue(recovered.verify_persisted())
            self.assertEqual(recovered.snapshot()["generator_invocations"]["in_flight"],1)
            with self.assertRaises(ValueError):
                recovered.reserve("generator","retry-1",metadata={"arm_id":"arm-1","turn_index":0,"scenario_id":"s1","configuration":"baseline"})
            recovered.complete("generator","attempt-1","ambiguous")
            again=coordinator.CallLedger(journal_path=journal)
            with self.assertRaises(ValueError):
                again.reserve("generator","retry-2",metadata={"arm_id":"arm-1","turn_index":0,"scenario_id":"s1","configuration":"baseline"})


if __name__ == "__main__":
    unittest.main()
