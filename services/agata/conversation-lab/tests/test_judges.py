"""Offline prompt isolation and reproducible randomization contracts."""
import importlib.util
import json
from pathlib import Path
import unittest


LAB = Path(__file__).resolve().parents[1]
MODULE = LAB / "evaluators" / "judges.py"
SPEC = importlib.util.spec_from_file_location("agata_lab_judges", MODULE) if MODULE.exists() else None
judges = importlib.util.module_from_spec(SPEC) if SPEC else None
if SPEC and SPEC.loader:
    SPEC.loader.exec_module(judges)
RUBRIC = LAB / "configs" / "owner-preference-rubric-v1.json"


def packet(count=2):
    return {"scenarios": [
        {"scenario_id": f"dev-{index}", "category": "daily", "context": {"setting": "synthetic"},
         "candidates": [
             {"label": "X", "turns": [{"role": "user", "text": "Вопрос?"}, {"role": "assistant", "text": f"Ответ X {index}"}]},
             {"label": "Y", "turns": [{"role": "user", "text": "Вопрос?"}, {"role": "assistant", "text": f"Ответ Y {index}"}]},
         ]}
        for index in range(count)
    ]}


class JudgeIsolationTests(unittest.TestCase):
    def require_module(self):
        self.assertIsNotNone(judges, "isolated Promptfoo judge compiler is not implemented")

    def test_rubric_has_exactly_eight_core_dimensions_and_conditional_evidence(self):
        self.require_module()
        rubric = json.loads(RUBRIC.read_text(encoding="utf-8"))
        ids = [dimension["id"] for dimension in rubric["core_dimensions"]]
        self.assertEqual(ids, [
            "naturalness", "human_likeness", "emotional_appropriateness", "persona_consistency",
            "initiative", "less_ai_template_like", "conversation_coherence", "direct_request_fulfillment_responsiveness",
        ])
        self.assertEqual(rubric["core_threshold"]["denominator"], 8)
        self.assertEqual(rubric["core_threshold"]["minimum_winning_dimensions"], 6)
        self.assertNotIn("memory", ids)
        self.assertIn("memory", rubric["conditional_dimensions"])

    def test_judges_get_fresh_minimal_blind_x_y_pair_without_private_fields(self):
        self.require_module()
        prompts, private = judges.judge_inputs(packet(), json.loads(RUBRIC.read_text()), batch_id="batch-immutable-1")
        self.assertEqual(len(prompts), 6)
        serialized = json.dumps(prompts, ensure_ascii=False).casefold()
        for forbidden in ("baseline", "naturalness-v1", "candidate_sha", "model_id", "gpt-6-luna", "treatment", "judge_outputs", "provider metadata"):
            self.assertNotIn(forbidden, serialized)
        self.assertNotEqual(prompts[0]["prompt"], prompts[1]["prompt"])
        self.assertTrue(all("scenario_id" not in item["prompt"] for item in prompts))
        self.assertNotIn("judge_outputs", private)
        self.assertTrue(private["private"])
        self.assertEqual(private["judge_invocation_count"], 6)

    def test_private_receipt_records_exact_x_y_orders_and_reproducible_control(self):
        self.require_module()
        first = judges.judge_inputs(packet(4), json.loads(RUBRIC.read_text()), batch_id="batch-seed-42")
        second = judges.judge_inputs(packet(4), json.loads(RUBRIC.read_text()), batch_id="batch-seed-42")
        self.assertEqual(first, second)
        prompts, private = first
        self.assertEqual(len(prompts), 12)
        by_scenario = {}
        for row in private["judge_plan"]:
            by_scenario.setdefault(row["scenario_id"], []).append(row["presented_labels"])
        for orders in by_scenario.values():
            self.assertEqual(orders[0], ["X", "Y"])
            self.assertEqual(orders[1], ["Y", "X"])
            self.assertEqual(set(orders[2]), {"X", "Y"})
        self.assertIn(private["owner_control_scenario_id"], {f"dev-{i}" for i in range(4)})
        changed = judges.judge_inputs(packet(4), json.loads(RUBRIC.read_text()), batch_id="different-batch")
        self.assertNotEqual(private["seed"], changed[1]["seed"])

    def test_order_normalization_maps_presented_choice_back_to_blind_labels(self):
        self.require_module()
        self.assertEqual(judges.normalize_choice("first", ["X", "Y"]), "X")
        self.assertEqual(judges.normalize_choice("first", ["Y", "X"]), "Y")
        self.assertEqual(judges.normalize_choice("tie", ["Y", "X"]), "tie")
        with self.assertRaises(ValueError):
            judges.normalize_choice("Z", ["X", "Y"])

    def test_private_packet_fields_and_excessive_batch_size_fail_closed(self):
        self.require_module()
        contaminated = packet(1)
        contaminated["private_mapping"] = {"secret": "baseline"}
        with self.assertRaises(ValueError):
            judges.judge_inputs(contaminated, json.loads(RUBRIC.read_text()), batch_id="batch-1")
        contaminated = packet(1)
        contaminated["scenarios"][0]["candidates"][1]["turns"][1]["text"] = "Candidate B: naturalness-v1"
        with self.assertRaises(ValueError):
            judges.judge_inputs(contaminated, json.loads(RUBRIC.read_text()), batch_id="batch-text-leak")
        with self.assertRaises(ValueError):
            judges.judge_inputs(packet(6), json.loads(RUBRIC.read_text()), batch_id="batch-too-large")

    def test_structured_verdict_is_validated_and_normalized_without_private_mapping(self):
        self.require_module()
        dimensions = {key: "first" for key in judges.CORE_IDS}
        raw = json.dumps({
            "overall": "first",
            "core_dimensions": dimensions,
            "conditional_evidence": {"memory": {"applicable": False, "winner": "not_applicable", "reason": "not tested"}},
            "reasons": ["The first conversation follows the request directly."],
            "defects": [],
            "uncertainty": False,
            "position_sensitivity": False,
        })
        receipt = judges.parse_judge_output(raw, judge_slot=1, scenario_id="dev-a", presented_labels=["X", "Y"])
        self.assertEqual(receipt["preferred_position"], "first")
        self.assertEqual(receipt["core_dimension_positions"], dimensions)
        self.assertEqual(receipt["judge_slot"], 1)
        self.assertEqual(receipt["critical_defects"], [])

    def test_missing_dimension_or_extra_private_output_fails_closed(self):
        self.require_module()
        raw = {"overall": "first", "core_dimensions": {key: "first" for key in judges.CORE_IDS[:-1]},
               "conditional_evidence": {}, "reasons": ["ok"], "defects": [],
               "uncertainty": False, "position_sensitivity": False}
        with self.assertRaises(ValueError):
            judges.parse_judge_output(json.dumps(raw), judge_slot=1, scenario_id="dev-a", presented_labels=["X", "Y"])
        raw["core_dimensions"][judges.CORE_IDS[-1]] = "first"
        raw["private_mapping"] = {"X": "baseline"}
        with self.assertRaises(ValueError):
            judges.parse_judge_output(json.dumps(raw), judge_slot=1, scenario_id="dev-a", presented_labels=["X", "Y"])

    def test_defect_uncertainty_and_position_sensitivity_are_receipt_evidence(self):
        self.require_module()
        raw = {"overall": "uncertain", "core_dimensions": {key: "tie" for key in judges.CORE_IDS},
               "conditional_evidence": {}, "reasons": ["The comparison is uncertain."],
               "defects": [{"code": "PERSONA_CONTRADICTION", "severity": "critical", "position": "first", "reason": "Identity changed."}],
               "uncertainty": True, "position_sensitivity": True}
        receipt = judges.parse_judge_output(json.dumps(raw), judge_slot=2, scenario_id="dev-a", presented_labels=["Y", "X"])
        self.assertEqual(receipt["critical_defects"], ["PERSONA_CONTRADICTION"])
        self.assertTrue(receipt["uncertainty"])
        self.assertTrue(receipt["position_sensitivity"])


if __name__ == "__main__":
    unittest.main()
