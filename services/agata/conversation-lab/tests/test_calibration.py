"""Batch OWNER calibration must preserve blinding and verify lock before comparison."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "evaluators"))
import calibration
import judges
import ratings
import receipts
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
        "batch_id": f"judge-batch-{pair['pair_id']}", "dataset_split": "DEV",
        "judge_receipts": [{
            "judge_slot": slot, "scenario_id": pair["pair_id"],
            "invocation_id": f"judge-{pair['pair_id']}-{slot}",
            "batch_id": f"judge-batch-{pair['pair_id']}",
            "worker_receipt_sha256": str(slot) * 64,
            "promptfoo_result_sha256": str(slot + 3) * 64,
            "model": "gpt-6-luna", "reasoning_effort": "medium",
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
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.generation_paths, self.judge_paths = {}, {}
        for pair in self.pairs:
            self._write_evidence(pair)

    def _write_once(self, name, value):
        path = Path(self.temp.name) / name
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        path.chmod(0o600)
        return str(path)

    def _write_evidence(self, pair):
        run_id, pair_id = pair["generation_run_id"], pair["pair_id"]
        arms, generators, mappings = [], [], []
        for label, configuration, model_id in (("X", "baseline", "baseline"), ("Y", "naturalness-v1", "naturalness-v1")):
            arm_id = f"{run_id}:{label}"
            arms.append({"invocation_id": f"arm-{arm_id}", "configuration": configuration,
                         "outcome": "success", "metadata": {"scenario_id": pair["source_scenario_id"], "arm_id": arm_id}})
            for turn in range(4):
                generators.append({"invocation_id": f"gen-{arm_id}-{turn}", "outcome": "success",
                                   "metadata": {"arm_id": arm_id, "turn_index": turn}})
            candidate = next(row for row in pair["candidates"] if row["label"] == label)
            mappings.append({"scenario_id": pair["source_scenario_id"], "label": label,
                             "model_id": model_id, "scenario_hash": pair["scenario_commitment"],
                             "result_hash": str(len(mappings) + 1) * 64,
                             "blind_candidate_sha256": calibration._sha(candidate)})
        generation = {
            "private": True, "schema_version": 2, "batch_id": run_id,
            "requested_model": "gpt-6-luna", "requested_reasoning": "medium",
            "scenario_commitments": {pair["source_scenario_id"]: pair["scenario_commitment"]},
            "private_mapping": {"private": True, "dataset_split": "DEV", "mappings": mappings,
                                "mapping_sha256": receipts.fingerprint(mappings)},
            "call_ledger": {"durable": True, "journal_sha256": "a" * 64,
                            "conversation_arms": {"invocations": arms},
                            "generator_invocations": {"invocations": generators}},
        }
        generation["receipt_sha256"] = receipts.fingerprint(generation)
        self.generation_paths[run_id] = self._write_once(run_id + ".generation.json", generation)
        assessment = self.assessments[pair_id]
        judge_rows = assessment["judge_receipts"]
        judge_ledger = [{"invocation_id": row["invocation_id"], "outcome": "success",
                         "metadata": {"judge_slot": row["judge_slot"], "scenario_id": pair_id,
                                      "batch_id": assessment["batch_id"], "model": "gpt-6-luna",
                                      "reasoning_effort": "medium"}}
                        for row in judge_rows]
        judged = {"private": True, "schema_version": 1, "batch_id": assessment["batch_id"],
                  "dataset_split": "DEV", "decision": "AUTO_DECISION",
                  "scenarios": [{"scenario_id": pair_id, "decision": "AUTO_WIN_X",
                                 "judge_receipts": judge_rows}],
                  "call_ledger": {"durable": True,
                                  "judge_invocations": {"invocations": judge_ledger}}}
        judged["receipt_sha256"] = receipts.fingerprint(judged)
        self.judge_paths[pair_id] = self._write_once(pair_id + ".judges.json", judged)

    def _prepare(self, packet_id, pairs_to_use=None, assessment_map=None):
        return calibration.prepare_owner_calibration_packet(
            self.pairs if pairs_to_use is None else pairs_to_use,
            self.assessments if assessment_map is None else assessment_map,
            packet_id=packet_id, dataset_split="DEV", rubric=self.rubric,
            generation_receipt_paths=self.generation_paths, judge_receipt_paths=self.judge_paths,
        )

    def test_builds_one_blind_packet_for_ten_pairs_and_keeps_judge_evidence_private(self):
        prepared = self._prepare("calibration-batch-001")
        packet = prepared["owner_packet"]
        self.assertEqual(len(packet["scenarios"]), 10)
        self.assertNotIn("judge_receipts", json.dumps(packet))
        self.assertNotIn("model_id", json.dumps(packet))
        self.assertFalse(prepared["private_judge_manifest"]["owner_packet_includes_judge_scores"])
        self.assertEqual(len(prepared["owner_rating_form"]["ratings"]), 20)

    def test_judge_owner_agreement_is_computed_only_after_exact_rating_lock(self):
        packet = self._prepare("calibration-batch-002")["owner_packet"]
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
            self._prepare("calibration-small", self.pairs[:9])
        contaminated = [*self.pairs]
        contaminated[0] = {**contaminated[0], "private_mapping": {"X": "baseline"}}
        with self.assertRaises(PermissionError):
            self._prepare("calibration-private", contaminated)

    def test_rejects_sealed_split_and_non_v2_source(self):
        with self.assertRaises(PermissionError):
            calibration.prepare_owner_calibration_packet(
                self.pairs, self.assessments, packet_id="calibration-sealed",
                dataset_split="SEALED_HOLDOUT", rubric=self.rubric,
                generation_receipt_paths=self.generation_paths, judge_receipt_paths=self.judge_paths,
            )
        old_source = [dict(pair, dataset_version="V1") for pair in self.pairs]
        with self.assertRaises(PermissionError):
            self._prepare("calibration-legacy", old_source)

    def test_caller_asserted_split_without_manifest_commitment_is_rejected(self):
        forged = [dict(pair, scenario_commitment="f" * 64) for pair in self.pairs]
        with self.assertRaises(PermissionError):
            self._prepare("calibration-forged", forged)

    def test_rejects_unreceipted_dialogue_or_fabricated_judge_assessment(self):
        with self.assertRaises(PermissionError):
            calibration.prepare_owner_calibration_packet(
                self.pairs, self.assessments, packet_id="calibration-no-receipts",
                dataset_split="DEV", rubric=self.rubric,
                generation_receipt_paths={}, judge_receipt_paths={},
            )
        forged = dict(self.assessments)
        forged[self.pairs[0]["pair_id"]] = dict(forged[self.pairs[0]["pair_id"]], decision="AUTO_WIN_Y")
        with self.assertRaises(PermissionError):
            self._prepare("calibration-forged-judge", assessment_map=forged)


if __name__ == "__main__":
    unittest.main()
