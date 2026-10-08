"""Batch OWNER calibration packaging and post-lock judge agreement analysis."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from typing import Mapping

import judges
import ratings
import receipts as private_receipts
from optimizer_gate import systematic_critical_dimension_failure
import v2_provenance

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _sha(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _validate_execution_evidence(pair, receipt):
    private_mapping = receipt.get("private_mapping", {})
    mapping_rows = private_mapping.get("mappings", [])
    if (receipt.get("batch_id") != pair.get("generation_run_id")
            or receipt.get("requested_model") != private_receipts.MODEL
            or receipt.get("requested_reasoning") != private_receipts.REASONING
            or private_mapping.get("dataset_split") != pair.get("dataset_split")
            or private_mapping.get("private") is not True
            or private_mapping.get("mapping_sha256") != private_receipts.fingerprint(mapping_rows)
            or receipt.get("scenario_commitments", {}).get(pair.get("source_scenario_id")) != pair.get("scenario_commitment")):
        raise PermissionError("generation receipt does not bind this pair to its committed run/split")
    candidates = pair.get("candidates")
    mappings = [row for row in mapping_rows
                if row.get("scenario_id") == pair.get("source_scenario_id")]
    if len(mappings) != 2 or {row.get("label") for row in mappings} != {"X", "Y"}:
        raise PermissionError("generation receipt is missing the calibrated blind arm mapping")
    for candidate in candidates:
        mapping = next((row for row in mappings if row.get("label") == candidate.get("label")), None)
        if (mapping is None or mapping.get("blind_candidate_sha256") != _sha(candidate)
                or mapping.get("scenario_hash") != pair.get("scenario_commitment")
                or not isinstance(mapping.get("result_hash"), str)
                or len(mapping["result_hash"]) != 64):
            raise PermissionError("calibration transcript differs from its persisted generated result hash")
    ledger = receipt.get("call_ledger", {})
    if (ledger.get("durable") is not True or not isinstance(ledger.get("journal_sha256"), str)
            or len(ledger["journal_sha256"]) != 64):
        raise PermissionError("generation receipt lacks a durable call-ledger journal commitment")
    arms = ledger.get("conversation_arms", {}).get("invocations", [])
    generators = ledger.get("generator_invocations", {}).get("invocations", [])
    pair_arms = [row for row in arms if row.get("metadata", {}).get("scenario_id") == pair["source_scenario_id"]]
    if len(pair_arms) != 2 or {row.get("configuration") for row in pair_arms} != {"baseline", "naturalness-v1"}:
        raise PermissionError("durable generation ledger does not prove both calibrated arms")
    for arm in pair_arms:
        arm_id = arm.get("metadata", {}).get("arm_id")
        mapping = next((row for row in mappings
                        if row.get("model_id") == arm.get("configuration")
                        and row.get("scenario_id") == pair["source_scenario_id"]), None)
        label = next((candidate.get("label") for candidate in candidates
                      if mapping and mapping.get("blind_candidate_sha256") == _sha(candidate)), None)
        if label is None:
            raise PermissionError("private arm mapping does not bind each generated model to the durable arm ledger")
        turns = [row for row in generators if row.get("metadata", {}).get("arm_id") == arm_id]
        if (arm.get("outcome") != "success" or len(turns) != 4
                or {row.get("metadata", {}).get("turn_index") for row in turns} != {0, 1, 2, 3}
                or any(row.get("outcome") != "success" for row in turns)):
            raise PermissionError("each calibrated arm must have four successful ledger-backed generator turns")


def _validate_judge_evidence(pair_id, assessment, path):
    evidence = private_receipts.load_private_receipt(path)
    if (evidence.get("schema_version") != 1 or evidence.get("batch_id") != assessment.get("batch_id")
            or evidence.get("dataset_split") != assessment.get("dataset_split")
            or evidence.get("decision") not in {"AUTO_DECISION", "HUMAN_ESCALATION"}):
        raise PermissionError("persisted judge outcome receipt does not match its assessment")
    rows = [row for row in evidence.get("scenarios", [])
            if isinstance(row, Mapping) and row.get("scenario_id") == pair_id]
    if (len(rows) != 1 or rows[0].get("judge_receipts") != assessment.get("judge_receipts")
            or rows[0].get("decision") != assessment.get("decision")):
        raise PermissionError("judge assessment is not the exact persisted outcome for this pair")
    receipts = rows[0]["judge_receipts"]
    slots = [row.get("judge_slot") for row in receipts if isinstance(row, Mapping)]
    invocation_ids = [row.get("invocation_id") for row in receipts if isinstance(row, Mapping)]
    if len(slots) != 3 or set(slots) != {1, 2, 3}:
        raise PermissionError("persisted judge receipts must cover slots 1, 2, and 3 exactly once")
    if len(invocation_ids) != 3 or len(set(invocation_ids)) != 3:
        raise PermissionError("each judge receipt must have a unique durable invocation identity")
    for row in receipts:
        if (row.get("scenario_id") != pair_id or row.get("batch_id") != evidence["batch_id"]
                or row.get("model") != private_receipts.MODEL
                or row.get("reasoning_effort") != private_receipts.REASONING
                or not isinstance(row.get("worker_receipt_sha256"), str)
                or len(row["worker_receipt_sha256"]) != 64
                or not isinstance(row.get("promptfoo_result_sha256"), str)
                or len(row["promptfoo_result_sha256"]) != 64):
            raise PermissionError("judge receipts lack matching Worker/Promptfoo/model evidence")
    ledger_rows = evidence.get("call_ledger", {}).get("judge_invocations", {}).get("invocations", [])
    by_id = {row.get("invocation_id"): row for row in ledger_rows}
    if (evidence.get("call_ledger", {}).get("durable") is not True
            or len(by_id) != len(ledger_rows)
            or any(invocation_id not in by_id or by_id[invocation_id].get("outcome") != "success"
                   for invocation_id in invocation_ids)):
        raise PermissionError("judge verdicts are not backed by successful durable ledger attempts")
    for row in receipts:
        ledger_row = by_id[row["invocation_id"]]
        metadata = ledger_row.get("metadata", {})
        if (metadata.get("judge_slot") != row["judge_slot"]
                or metadata.get("scenario_id") != pair_id
                or metadata.get("batch_id") != evidence["batch_id"]
                or metadata.get("model") != private_receipts.MODEL
                or metadata.get("reasoning_effort") != private_receipts.REASONING):
            raise PermissionError("judge receipt and durable invocation ledger do not match")
    return evidence


def prepare_owner_calibration_packet(pairs: list[Mapping[str, object]], judge_assessments: Mapping[str, object], *,
                                     packet_id: str, dataset_split: str,
                                     rubric: Mapping[str, object],
                                     generation_receipt_paths: Mapping[str, str],
                                     judge_receipt_paths: Mapping[str, str]) -> dict:
    """Create one blind 10+ pair OWNER packet; keep judge evidence in a separate private sidecar.

    Input pairs must already be anonymized. Model mappings, policy text, provider
    metadata and receipts are rejected rather than copied into the OWNER packet.
    """
    judges.validate_rubric(rubric)
    if not isinstance(packet_id, str) or not ID_RE.fullmatch(packet_id):
        raise ValueError("immutable calibration packet ID is required")
    if dataset_split not in {"DEV", "VALIDATION"}:
        raise PermissionError("OWNER calibration cannot read SEALED HOLDOUT or legacy Benchmark V1")
    if not isinstance(pairs, list) or len(pairs) < 10:
        raise ValueError("OWNER calibration requires at least ten blind pairs")
    scenarios, private_assessments, seen, run_ids = [], [], set(), set()
    for pair in pairs:
        if not isinstance(pair, Mapping):
            raise ValueError("calibration pair is malformed")
        if any(key in pair for key in ("private_mapping", "mapping", "model_ids", "provider_metadata", "private_receipts")):
            raise PermissionError("calibration OWNER packet cannot contain private arm mapping or receipts")
        pair_id = pair.get("pair_id")
        run_id = pair.get("generation_run_id")
        if not isinstance(pair_id, str) or not ID_RE.fullmatch(pair_id) or pair_id in seen:
            raise ValueError("calibration pair IDs must be unique and immutable")
        if not isinstance(run_id, str) or not ID_RE.fullmatch(run_id) or run_id in run_ids:
            raise ValueError("calibration pairs must come from distinct immutable generation runs")
        source = v2_provenance.validate_calibration_pair(pair, dataset_split)
        generation_path = generation_receipt_paths.get(run_id)
        judge_path = judge_receipt_paths.get(pair_id)
        if not isinstance(generation_path, str) or not isinstance(judge_path, str):
            raise PermissionError("calibration requires persisted generation and judge receipt paths")
        generation_receipt = private_receipts.load_private_receipt(generation_path)
        _validate_execution_evidence(pair, generation_receipt)
        # Pair IDs are opaque to OWNER; source IDs and commitments stay in the private sidecar.
        public_pair = {"scenario_id": pair_id, "candidates": copy.deepcopy(pair.get("candidates"))}
        judges._validate_pair(public_pair)
        assessment = judge_assessments.get(pair_id)
        if (not isinstance(assessment, Mapping) or assessment.get("scenario_id") != pair_id
                or assessment.get("decision") not in {"AUTO_WIN_X", "AUTO_WIN_Y", "HUMAN_ESCALATION"}
                or not isinstance(assessment.get("judge_receipts"), list)
                or len(assessment["judge_receipts"]) != 3):
            raise ValueError("each calibration pair needs the complete private three-judge preassessment")
        judge_receipt = _validate_judge_evidence(pair_id, assessment, judge_path)
        seen.add(pair_id)
        run_ids.add(run_id)
        scenarios.append(public_pair)
        private_assessments.append({"pair_id": pair_id, **source,
                                    "generation_run_id_sha256": _sha(run_id),
                                    "generation_receipt_sha256": generation_receipt["receipt_sha256"],
                                    "judge_receipt_sha256": judge_receipt["receipt_sha256"],
                                    "assessment_sha256": _sha(assessment)})
    packet = {"schema_version": 1, "packet_id": packet_id,
              "rubric_id": rubric["rubric_id"], "scenarios": scenarios}
    owner_form = ratings.empty_ratings(packet, rubric=rubric)
    return {
        "owner_packet": packet,
        "owner_rating_form": owner_form,
        "private_judge_manifest": {
            "private": True, "packet_id": packet_id,
            "packet_sha256": _sha(packet), "assessment_hashes": private_assessments,
            "owner_packet_includes_judge_scores": False,
        },
    }


def _owner_preference(ratings_body: Mapping[str, object], scenario_id: str) -> str:
    rows = [row for row in ratings_body["pairwise_preferences"] if row["scenario_id"] == scenario_id]
    if len(rows) != 1:
        raise ValueError("calibration requires one locked OWNER preference per pair")
    return rows[0]["preferred_label"]


def _judge_consensus(assessment: Mapping[str, object]) -> str:
    if assessment.get("decision") in {"AUTO_WIN_X", "AUTO_WIN_Y"}:
        return assessment["decision"][-1]
    receipts = assessment["judge_receipts"]
    votes = []
    for row in receipts:
        order = row.get("presented_labels")
        position = row.get("preferred_position")
        votes.append(judges.normalize_choice(position, order))
    counts = Counter(votes)
    winner, count = counts.most_common(1)[0]
    return winner if count == 3 and winner in {"X", "Y", "tie"} else "uncertain"


def compare_locked_owner_ratings(packet: Mapping[str, object], owner_ratings: Mapping[str, object],
                                 lock: Mapping[str, object], judge_assessments: Mapping[str, object], *,
                                 rubric: Mapping[str, object]) -> dict:
    """Measure calibration only after verifying the immutable OWNER ratings lock."""
    if not ratings.verify_lock(lock, packet, owner_ratings, rubric=rubric):
        raise PermissionError("OWNER ratings must be complete and locked before judge comparison")
    scenarios = packet.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) < 10:
        raise ValueError("calibration requires at least ten OWNER-rated pairs")
    comparisons, critical_evidence = [], []
    owner_rows = owner_ratings["ratings"]
    for scenario in scenarios:
        scenario_id = scenario["scenario_id"]
        assessment = judge_assessments.get(scenario_id)
        if not isinstance(assessment, Mapping) or len(assessment.get("judge_receipts", [])) != 3:
            raise ValueError("locked calibration pair is missing its complete judge ensemble")
        owner_winner = _owner_preference(owner_ratings, scenario_id)
        judge_winner = _judge_consensus(assessment)
        candidate_rows = [row for row in owner_rows if row["scenario_id"] == scenario_id]
        by_label = {row["label"]: row for row in candidate_rows}
        applicable = []
        owner_dimension_winners = {}
        judge_dimension_winners = {}
        for dimension in judges.CORE_IDS:
            left, right = by_label["X"]["scores"][dimension], by_label["Y"]["scores"][dimension]
            if left is None or right is None:
                continue
            applicable.append(dimension)
            owner_dimension_winners[dimension] = "X" if left > right else "Y" if right > left else "tie"
            votes = []
            for receipt in assessment["judge_receipts"]:
                votes.append(judges.normalize_choice(receipt["core_dimension_positions"][dimension],
                                                     receipt["presented_labels"]))
            count = Counter(votes)
            winner, n = count.most_common(1)[0]
            judge_dimension_winners[dimension] = winner if n >= 2 else "uncertain"
        owner_defects = sorted({item for row in candidate_rows for item in row["critical_failures"]})
        judge_defects = sorted({item for receipt in assessment["judge_receipts"]
                                for item in receipt.get("critical_defects", [])})
        evidence = {
            "pair_id": scenario_id, "owner_rated": True,
            "applicable_critical_dimensions": [dim for dim in applicable if dim in {
                "persona_consistency", "emotional_appropriateness",
                "direct_request_fulfillment_responsiveness"}],
            "owner_dimension_winners": owner_dimension_winners,
            "judge_dimension_winners": judge_dimension_winners,
            "owner_critical_defect_codes": owner_defects,
            "judge_detected_critical_defect_codes": judge_defects,
        }
        critical_evidence.append(evidence)
        comparisons.append({**evidence, "owner_overall_winner": owner_winner,
                            "judge_ensemble_overall_winner": judge_winner})
    agreement = sum(row["owner_overall_winner"] == row["judge_ensemble_overall_winner"]
                    for row in comparisons) / len(comparisons)
    return {
        "owner_rated_pairs": comparisons,
        "overall_agreement": round(agreement, 6),
        "agreement_percent": round(agreement * 100, 2),
        "systematic_critical_dimension_failure": systematic_critical_dimension_failure(comparisons),
        "critical_dimension_evidence": critical_evidence,
        "ratings_lock_sha256": lock["lock_sha256"],
        "packet_sha256": _sha(packet),
        "private_mapping_accessed": False,
        "unblinded": False,
    }
