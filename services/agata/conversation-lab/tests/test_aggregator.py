"""Deterministic AGATA decision contract for synthetic judge receipts."""
import importlib.util
from pathlib import Path
import unittest


MODULE = Path(__file__).resolve().parents[1] / "evaluators" / "aggregator.py"
SPEC = importlib.util.spec_from_file_location("agata_deterministic_aggregator", MODULE) if MODULE.exists() else None
aggregator = importlib.util.module_from_spec(SPEC) if SPEC else None
if SPEC and SPEC.loader:
    SPEC.loader.exec_module(aggregator)

CORE = ["naturalness", "human_likeness", "emotional_appropriateness", "persona_consistency",
        "initiative", "less_ai_template_like", "conversation_coherence", "direct_request_fulfillment_responsiveness"]


def private_mapping():
    return {"private": True, "mappings": [
        {"scenario_id": "s1", "label": "X", "model_id": "baseline"},
        {"scenario_id": "s1", "label": "Y", "model_id": "naturalness-v1"},
    ]}


def receipts(winner="X", dimensions=None, *, orders=None, raw_positions=None):
    dimensions = dimensions or {name: "X" if winner == "X" else "Y" for name in CORE}
    orders = orders or [["X", "Y"], ["Y", "X"], ["X", "Y"]]
    raw_positions = raw_positions or ["first" if winner == order[0] else "second" for order in orders]
    output = []
    for index in range(3):
        choices = {}
        for name, dimension_winner in dimensions.items():
            choices[name] = "tie" if dimension_winner == "tie" else ("first" if dimension_winner == orders[index][0] else "second")
        output.append({"judge_slot": index + 1, "scenario_id": "s1", "presented_labels": orders[index],
                       "preferred_position": raw_positions[index], "core_dimension_positions": choices,
                       "conditional_evidence": {"memory": "not applicable"}})
    return output


class DeterministicAggregatorTests(unittest.TestCase):
    def require_module(self):
        self.assertIsNotNone(aggregator, "deterministic AGATA aggregator is not implemented")

    def decide(self, rows=None, findings=None):
        self.require_module()
        return aggregator.aggregate(receipts() if rows is None else rows, private_mapping(), [] if findings is None else findings)

    def test_three_of_three_and_six_of_eight_core_dimensions_auto_win(self):
        self.require_module()
        dimensions = {name: ("X" if index < 6 else "Y") for index, name in enumerate(CORE)}
        result = self.decide(receipts(dimensions=dimensions))
        self.assertEqual(result["decision"], "AUTO_WIN_X")
        self.assertEqual(result["core_dimension_majorities"]["X"], 6)
        self.assertEqual(result["normalized_overall_votes"], ["X", "X", "X"])

    def test_five_of_eight_core_dimensions_escalates_and_conditionals_never_count(self):
        dimensions = {name: ("X" if index < 5 else "Y") for index, name in enumerate(CORE)}
        rows = receipts(dimensions=dimensions)
        for row in rows:
            row["conditional_evidence"] = {"memory": "Y strongly preferred", "humor_flirt": "Y strongly preferred"}
        result = self.decide(rows)
        self.assertEqual(result["decision"], "HUMAN_ESCALATION")
        self.assertIn("CORE_DIMENSION_THRESHOLD_NOT_MET", result["reason_codes"])

    def test_two_one_tie_uncertainty_and_missing_judge_all_escalate(self):
        rows = receipts(); rows[2]["preferred_position"] = "second"
        results = [self.decide(rows)]
        rows = receipts(); rows[0]["preferred_position"] = "tie"
        results.append(self.decide(rows))
        rows = receipts(); rows[0]["preferred_position"] = "uncertain"
        results.append(self.decide(rows))
        results.append(self.decide(receipts()[:2]))
        self.assertTrue(all(item["decision"] == "HUMAN_ESCALATION" for item in results))

    def test_order_sensitivity_is_detected_after_position_normalization(self):
        rows = receipts(orders=[["X", "Y"], ["Y", "X"], ["X", "Y"]], raw_positions=["first", "first", "first"])
        result = self.decide(rows)
        self.assertEqual(result["decision"], "HUMAN_ESCALATION")
        self.assertIn("ORDER_SENSITIVE", result["reason_codes"])

    def test_critical_sanity_findings_private_mapping_or_out_of_rubric_escalate(self):
        self.assertIn("CRITICAL_SANITY_DEFECT", self.decide(findings=[{"code": "USER_FEMININE_ADDRESS"}])["reason_codes"])
        self.require_module()
        self.assertEqual(aggregator.aggregate(receipts(), {"private": False, "mappings": []}, [])["decision"], "HUMAN_ESCALATION")
        rows = receipts(); rows[0]["core_dimension_positions"]["unexpected"] = "first"
        self.assertIn("OUT_OF_RUBRIC_CORE_DIMENSIONS", self.decide(rows)["reason_codes"])

    def test_declared_position_sensitivity_and_critical_defect_escalate(self):
        rows = receipts()
        rows[0]["position_sensitivity"] = True
        result = self.decide(rows)
        self.assertEqual(result["decision"], "HUMAN_ESCALATION")
        self.assertIn("ORDER_SENSITIVE", result["reason_codes"])
        rows = receipts()
        rows[2]["critical_defects"] = ["PERSONA_CONTRADICTION"]
        result = self.decide(rows)
        self.assertEqual(result["decision"], "HUMAN_ESCALATION")
        self.assertIn("CONDITIONAL_CRITICAL_EVIDENCE", result["reason_codes"])


if __name__ == "__main__":
    unittest.main()
