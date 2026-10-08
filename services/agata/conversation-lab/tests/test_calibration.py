"""Batch OWNER calibration must preserve blinding and verify lock before comparison."""
import json
from pathlib import Path
import sys
import unittest

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "evaluators"))
import calibration
import judges
import ratings
import v2_provenance
from optimizer_gate import systematic_critical_dimension_failure as systematic_gate


def pairs(count=10):
    sources = [json.loads(line) for line in
               (LAB / "data/golden/synthetic/v2-dev-scenarios.jsonl").read_text().splitlines()]
    result = []
    for index in range(count):
        source = sources[index % len(sources)]
        candidates = []
        for label, reply in (("X", "Мне кажется, это хороший план."),
                             ("Y", "Есть несколько способов.")):
            turns = []
            for turn in source["dialogue_turns"]:
                turns.append({"role": "user", "text": turn["text"]} if turn["role"] == "user"
                             else {"role": "assistant", "text": reply})
            candidates.append({"label": label, "turns": turns})
        result.append({"pair_id": f"calibration-pair-{index:02d}",
                       "generation_run_id": f"calibration-run-{index:02d}",
                       "source_scenario_id": source["scenario_id"],
                       "scenario_commitment": v2_provenance.stable_hash(source),
                       "dataset_split": "DEV", "dataset_version": "V2", "candidates": candidates})
    return result


def assessments(items):
    return {pair["pair_id"]: {
        "scenario_id": pair["pair_id"], "decision": "AUTO_WIN_X",
        "judge_receipts": [{
            "judge_slot": slot, "scenario_id": pair["pair_id"],
            "presented_labels": ["X", "Y"], "preferred_position": "first",
            "core_dimension_positions": {dimension: "first" for dimension in judges.CORE_IDS},
            "critical_defects": [],
        } for slot in (1, 2, 3)],
    } for pair in items}


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.rubric = json.loads((LAB / "configs/owner-preference-rubric-v1.json").read_text())
        self.pairs = pairs()
        self.assessments = assessments(self.pairs)

    def test_builds_one_blind_packet_for_ten_pairs_and_keeps_judge_evidence_private(self):
        prepared = calibration.prepare_owner_calibration_packet(
            self.pairs, self.assessments, packet_id="calibration-batch-001", dataset_split="DEV", rubric=self.rubric,
        )
        packet = prepared["owner_packet"]
        self.assertEqual(len(packet["scenarios"]), 10)
        self.assertNotIn("judge_receipts", json.dumps(packet))
        self.assertNotIn("model_id", json.dumps(packet))
        self.assertFalse(prepared["private_judge_manifest"]["owner_packet_includes_judge_scores"])
        self.assertEqual(len(prepared["owner_rating_form"]["ratings"]), 20)

    def test_judge_owner_agreement_is_computed_only_after_exact_rating_lock(self):
        packet = calibration.prepare_owner_calibration_packet(
            self.pairs, self.assessments, packet_id="calibration-batch-002", dataset_split="DEV", rubric=self.rubric,
        )["owner_packet"]
        owner = ratings.empty_ratings(packet, rubric=self.rubric)
        core = [row["id"] for row in self.rubric["core_dimensions"]]
        supplemental = list(self.rubric["conditional_dimensions"])
        for row in owner["ratings"]:
            row["scores"] = {dimension: (5 if row["label"] == "X" else 4) for dimension in core}
            row["supplemental"] = {dimension: None for dimension in supplemental}
            row["not_applicable"] = {dimension: "This batch does not test this condition." for dimension in supplemental}
        for pref in owner["pairwise_preferences"]:
            pref["preferred_label"] = "X"
        lock = ratings.lock_ratings(packet, owner, cycle_id="calibration-cycle-002", rubric=self.rubric)
        result = calibration.compare_locked_owner_ratings(
            packet, owner, lock, self.assessments, rubric=self.rubric,
        )
        self.assertEqual(result["agreement_percent"], 100.0)
        self.assertFalse(result["systematic_critical_dimension_failure"])
        self.assertFalse(systematic_gate(result["owner_rated_pairs"]))
        self.assertFalse(result["unblinded"])
        owner["pairwise_preferences"][0]["preferred_label"] = "Y"
        with self.assertRaises(PermissionError):
            calibration.compare_locked_owner_ratings(packet, owner, lock, self.assessments, rubric=self.rubric)

    def test_rejects_less_than_ten_pairs_and_any_private_mapping(self):
        with self.assertRaises(ValueError):
            calibration.prepare_owner_calibration_packet(
                self.pairs[:9], self.assessments, packet_id="calibration-small", dataset_split="DEV", rubric=self.rubric,
            )
        contaminated = [*self.pairs]
        contaminated[0] = {**contaminated[0], "private_mapping": {"X": "baseline"}}
        with self.assertRaises(PermissionError):
            calibration.prepare_owner_calibration_packet(
                contaminated, self.assessments, packet_id="calibration-private", dataset_split="DEV", rubric=self.rubric,
            )

    def test_rejects_sealed_split_and_non_v2_source(self):
        with self.assertRaises(PermissionError):
            calibration.prepare_owner_calibration_packet(
                self.pairs, self.assessments, packet_id="calibration-sealed",
                dataset_split="SEALED_HOLDOUT", rubric=self.rubric,
            )
        old_source = [dict(pair, dataset_version="V1") for pair in self.pairs]
        with self.assertRaises(PermissionError):
            calibration.prepare_owner_calibration_packet(
                old_source, self.assessments, packet_id="calibration-legacy",
                dataset_split="DEV", rubric=self.rubric,
            )

    def test_caller_asserted_split_without_manifest_commitment_is_rejected(self):
        forged = [dict(pair, scenario_commitment="f" * 64) for pair in self.pairs]
        with self.assertRaises(PermissionError):
            calibration.prepare_owner_calibration_packet(
                forged, self.assessments, packet_id="calibration-forged",
                dataset_split="DEV", rubric=self.rubric,
            )


if __name__ == "__main__":
    unittest.main()
