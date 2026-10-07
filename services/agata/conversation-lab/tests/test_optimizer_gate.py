"""Calibration lifecycle and bounded approval checks without optimizer calls."""
import importlib.util
from pathlib import Path
import unittest


MODULE = Path(__file__).resolve().parents[1] / "evaluators" / "optimizer_gate.py"
SPEC = importlib.util.spec_from_file_location("agata_optimizer_gate", MODULE) if MODULE.exists() else None
gate = importlib.util.module_from_spec(SPEC) if SPEC else None
if SPEC and SPEC.loader:
    SPEC.loader.exec_module(gate)

POLICY_HASH = "a" * 64
CHECKS = {
    "execution_parity": True,
    "blind_private_isolation": True,
    "judge_isolation": True,
    "deterministic_aggregation": True,
    "replacement_split_isolation": True,
    "optimizer_holdout_allowlist": True,
    "call_budget_guards": True,
}


def pair(index, *, agree=True, critical=False, missed=False):
    return {
        "pair_id": f"pair-{index}",
        "owner_rated": True,
        "owner_overall_winner": "X",
        "judge_ensemble_overall_winner": "X" if agree else "Y",
        "applicable_critical_dimensions": ["persona_consistency", "emotional_appropriateness", "direct_request_fulfillment_responsiveness"],
        "owner_dimension_winners": {"persona_consistency": "X", "emotional_appropriateness": "X", "direct_request_fulfillment_responsiveness": "X"},
        "judge_dimension_winners": {"persona_consistency": "X", "emotional_appropriateness": "X", "direct_request_fulfillment_responsiveness": "X" if not critical else "Y"},
        "owner_critical_defect_codes": ["USER_FEMININE_ADDRESS"] if critical else [],
        "judge_detected_critical_defect_codes": [] if missed else (["USER_FEMININE_ADDRESS"] if critical else []),
    }


def evidence(count=10):
    return {
        "adapter_implemented": True,
        "checks": dict(CHECKS),
        "owner_rated_pairs": [pair(index) for index in range(count)],
        "naturalness_policy_sha256": POLICY_HASH,
    }


class OptimizerGateTests(unittest.TestCase):
    def require_module(self):
        self.assertIsNotNone(gate, "optimizer lifecycle gate is not implemented")

    def test_implemented_adapter_does_not_require_calibration_or_authorize_calls(self):
        self.require_module()
        result = gate.optimizer_status({"adapter_implemented": True})
        self.assertEqual(result, "OPTIMIZER_ADAPTER_IMPLEMENTED")

    def test_ready_requires_every_technical_check_ten_pairs_and_ninety_percent(self):
        self.require_module()
        value = evidence(10)
        value["owner_rated_pairs"][0]["judge_ensemble_overall_winner"] = "Y"
        self.assertEqual(gate.optimizer_status(value), "OPTIMIZER_READY")
        value = evidence(10)
        value["checks"]["judge_isolation"] = False
        self.assertEqual(gate.optimizer_status(value), "OPTIMIZER_ADAPTER_IMPLEMENTED")
        value = evidence(9)
        self.assertEqual(gate.optimizer_status(value), "OPTIMIZER_ADAPTER_IMPLEMENTED")
        value = evidence(10)
        value["owner_rated_pairs"][0]["judge_ensemble_overall_winner"] = "Y"
        value["owner_rated_pairs"][1]["judge_ensemble_overall_winner"] = "Y"
        self.assertEqual(gate.optimizer_status(value), "OPTIMIZER_ADAPTER_IMPLEMENTED")

    def test_systematic_critical_failure_means_two_distinct_owner_pairs(self):
        self.require_module()
        one = evidence(10)
        one["owner_rated_pairs"][0] = pair(0, critical=True)
        self.assertEqual(gate.optimizer_status(one), "OPTIMIZER_READY")
        two = evidence(10)
        two["owner_rated_pairs"][0] = pair(0, critical=True)
        two["owner_rated_pairs"][1] = pair(1, critical=True)
        self.assertEqual(gate.optimizer_status(two), "OPTIMIZER_ADAPTER_IMPLEMENTED")
        missed = evidence(10)
        missed["owner_rated_pairs"][0] = pair(0, missed=True, critical=True)
        missed["owner_rated_pairs"][1] = pair(1, missed=True, critical=True)
        self.assertEqual(gate.optimizer_status(missed), "OPTIMIZER_ADAPTER_IMPLEMENTED")

    def test_only_exact_bounded_matching_approval_authorizes_optimizer_run(self):
        self.require_module()
        value = evidence(10)
        value["optimization_approval"] = {
            "approved": True, "approval_id": "owner-approval-17", "dataset_id": "agata-v2-dev-2026-10",
            "split": "DEV", "max_candidate_count": 3, "max_model_calls": 20,
            "starting_naturalness_policy_sha256": POLICY_HASH,
        }
        self.assertEqual(gate.optimizer_status(value), "OPTIMIZER_RUN_AUTHORIZED")
        for change in (
            {"split": "SEALED_HOLDOUT"},
            {"dataset_id": ""},
            {"max_candidate_count": 0},
            {"starting_naturalness_policy_sha256": "b" * 64},
        ):
            invalid = evidence(10)
            approval = dict(value["optimization_approval"])
            approval.update(change)
            invalid["optimization_approval"] = approval
            self.assertEqual(gate.optimizer_status(invalid), "OPTIMIZER_READY")

    def test_invalid_calibration_evidence_fails_closed(self):
        self.require_module()
        value = evidence(10)
        value["owner_rated_pairs"][1]["pair_id"] = value["owner_rated_pairs"][0]["pair_id"]
        self.assertEqual(gate.optimizer_status(value), "OPTIMIZER_ADAPTER_IMPLEMENTED")
        with self.assertRaises(ValueError):
            gate.optimizer_status({"adapter_implemented": False})


if __name__ == "__main__":
    unittest.main()
