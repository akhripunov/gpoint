"""Private V2 receipt and OWNER lock contracts; synthetic fixtures only."""
import importlib.util
import json
import tempfile
import atexit
import unittest
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


receipts = load("agata_v2_receipts", LAB / "evaluators/receipts.py") if (LAB / "evaluators/receipts.py").exists() else None
ratings = load("agata_v2_ratings", LAB / "evaluators/ratings.py")
judges = load("agata_v2_judges", LAB / "evaluators/judges.py")
coordinator = load("agata_v2_coordinator", LAB / "evaluators/run_coordinator.py")
blind = load("agata_v2_blind", LAB / "evaluators/blind.py")
RUBRIC = json.loads((LAB / "configs/owner-preference-rubric-v1.json").read_text(encoding="utf-8"))
PACKET = {"schema_version": 1, "scenarios": [{"scenario_id": "synthetic-dev-001", "category": "daily", "candidates": [
    {"label": "X", "status": "success", "turns": [{"role": "user", "text": "Привет"}, {"role": "assistant", "text": "Здравствуйте"}]},
    {"label": "Y", "status": "success", "turns": [{"role": "user", "text": "Привет"}, {"role": "assistant", "text": "Привет!"}]},
]}]}
MAPPING = {"private": True, "mappings": [
    {"scenario_id": "synthetic-dev-001", "label": "X", "model_id": "baseline", "scenario_hash": "a" * 64, "result_hash": "b" * 64},
    {"scenario_id": "synthetic-dev-001", "label": "Y", "model_id": "naturalness-v1", "scenario_hash": "a" * 64, "result_hash": "c" * 64},
]}
MAPPING["mapping_sha256"] = judges.fingerprint(MAPPING["mappings"])
EMPTY_MAPPING = {"private": True, "mappings": []}
EMPTY_MAPPING["mapping_sha256"] = judges.fingerprint([])


_LEDGER_DIRS = []
atexit.register(lambda: [__import__("shutil").rmtree(path, ignore_errors=True) for path in _LEDGER_DIRS])


def ledger_evidence(arms, generators, judges_count):
    folder = Path(tempfile.mkdtemp(prefix="agata-v2-ledger-test-")); _LEDGER_DIRS.append(folder)
    ledger = coordinator.CallLedger(journal_path=folder / "calls.jsonl")
    configs = ["baseline", "naturalness-v1"] * min(arms // 2, 4)
    for i, config in enumerate(configs):
        arm_id = f"arm-{i}"
        ledger.reserve("arm", arm_id, configuration=config, metadata={"arm_id": arm_id, "scenario_id": "synthetic-dev-001"})
    for i in range(generators):
        arm_index = i // 4
        if arm_index >= len(configs): break
        arm_id = f"arm-{arm_index}"; turn = i % 4
        ledger.reserve("generator", f"gen-{i}", metadata={"arm_id": arm_id, "turn_index": turn,
                                                              "scenario_id": "synthetic-dev-001", "configuration": configs[arm_index]})
        ledger.complete("generator", f"gen-{i}", "success")
    for arm_index in range(len(configs)):
        if sum(1 for i in range(generators) if i//4 == arm_index) == 4:
            ledger.complete("arm", f"arm-{arm_index}", "success")
    for i in range(judges_count):
        ledger.reserve("judge", f"judge-{i}", metadata={"judge_id": f"judge-{i}"})
        ledger.complete("judge", f"judge-{i}", "success")
    return ledger


class PrivateReceiptTests(unittest.TestCase):
    def require_module(self):
        self.assertIsNotNone(receipts, "V2 private receipt API is not implemented")

    def test_run_identity_is_deterministic_and_binds_immutable_inputs(self):
        self.require_module()
        args = dict(batch_id="v2-batch-001", candidate_sha256="d" * 64,
                    scenario_commitments={"synthetic-dev-001": "a" * 64}, naturalness_policy_sha256="c" * 64)
        first = receipts.run_identity(**args)
        self.assertEqual(first, receipts.run_identity(**args))
        changed = dict(args, candidate_sha256="e" * 64)
        self.assertNotEqual(first, receipts.run_identity(**changed))

    def test_private_receipt_enforces_model_budgets_and_keeps_mapping_private(self):
        self.require_module()
        receipt = receipts.build_private_receipt(
            batch_id="v2-batch-001", candidate_sha256="d" * 64,
            scenario_commitments={"synthetic-dev-001": "a" * 64}, naturalness_policy_sha256="c" * 64,
            blind_packet=PACKET, rubric=RUBRIC,
            counters={"conversation_arms": 2, "generator_provider_invocations": 8, "judge_provider_invocations": 0},
            call_ledger=ledger_evidence(2, 8, 0),
            private_mapping=MAPPING, randomization={"seed": 123, "owner_control_scenario_id": "synthetic-dev-001", "x_y_orders": [["X", "Y"]]},
            judge_receipts=[], sanity_findings=[], aggregate_status="HUMAN_ESCALATION")
        self.assertTrue(receipt["private"])
        self.assertEqual(receipt["requested_model"], "gpt-6-luna")
        self.assertEqual(receipt["requested_reasoning"], "medium")
        self.assertEqual(receipt["owner_review"]["unblinded"], False)
        self.assertIn("private_mapping", receipt)
        with self.assertRaises(ValueError):
            receipts.build_private_receipt(
                batch_id="v2-batch-001", candidate_sha256="d" * 64,
                scenario_commitments={"synthetic-dev-001": "a" * 64}, naturalness_policy_sha256="c" * 64,
                blind_packet=PACKET, rubric=RUBRIC,
                counters={"conversation_arms": 9, "generator_provider_invocations": 0, "judge_provider_invocations": 0},
                call_ledger=ledger_evidence(9, 0, 0),
                private_mapping=MAPPING, randomization={}, judge_receipts=[], sanity_findings=[], aggregate_status="HUMAN_ESCALATION")

    def test_private_receipt_is_atomic_restrictive_and_write_once(self):
        self.require_module()
        receipt = receipts.build_private_receipt(
            batch_id="v2-batch-001", candidate_sha256="d" * 64,
            scenario_commitments={"synthetic-dev-001": "a" * 64}, naturalness_policy_sha256="c" * 64,
            blind_packet=PACKET, rubric=RUBRIC,
            counters={"conversation_arms": 0, "generator_provider_invocations": 0, "judge_provider_invocations": 0},
            call_ledger=ledger_evidence(0, 0, 0),
            private_mapping=EMPTY_MAPPING, randomization={"seed": 123, "owner_control_scenario_id": "synthetic-dev-001", "x_y_orders": []},
            judge_receipts=[], sanity_findings=[], aggregate_status="NOT_RUN")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "receipt.json"
            receipts.write_private_receipt(path, receipt)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["receipt_sha256"], receipt["receipt_sha256"])
            with self.assertRaises(FileExistsError):
                receipts.write_private_receipt(path, receipt)

    def test_owner_packet_stays_blind_and_contains_no_private_receipt_or_judge_verdict(self):
        scenario = json.loads((LAB / "data/golden/synthetic/v2-dev-scenarios.jsonl").read_text().splitlines()[0])
        results = [
            {"scenario_id": scenario["scenario_id"], "model_id": model, "status": "success",
             "turns": [{"role": turn["role"], "text": turn["text"]} if turn["role"] == "user"
                       else {"role": "assistant", "text": reply} for turn in scenario["dialogue_turns"]]}
            for model, reply in (("baseline", "Привет."), ("naturalness-v1", "Привет!"))]
        packet, private = blind.make_v2_blind_packets([scenario], results, batch_id="receipt-blind-batch")
        public_text = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn("model_id", public_text)
        self.assertNotIn("baseline", public_text)
        self.assertNotIn("naturalness-v1", public_text)
        self.assertNotIn("judge_receipts", public_text)
        self.assertNotIn("preferred_position", public_text)
        self.assertTrue(private["private"])
        self.assertEqual(private["mapping_sha256"], judges.fingerprint(private["mappings"]))

    def test_owner_lock_binds_packet_and_rubric_before_unblind(self):
        self.require_module()
        owner_ratings = ratings.empty_ratings(PACKET, rubric=RUBRIC)
        for row in owner_ratings["ratings"]:
            row["scores"] = {dimension["id"]: 4 for dimension in RUBRIC["core_dimensions"]}
            row["supplemental"] = {dimension: 4 for dimension in RUBRIC["conditional_dimensions"]}
        for row in owner_ratings["pairwise_preferences"]:
            row["preferred_label"] = "tie"
        lock = receipts.lock_owner_ratings(PACKET, RUBRIC, owner_ratings, cycle_id="owner-cycle-1")
        self.assertEqual(lock["packet_sha256"], receipts.fingerprint(PACKET))
        self.assertEqual(lock["rubric_sha256"], judges.fingerprint(RUBRIC))
        private_receipt = receipts.build_private_receipt(
            batch_id="v2-batch-001", candidate_sha256="d" * 64,
            scenario_commitments={"synthetic-dev-001": "a" * 64}, naturalness_policy_sha256="c" * 64,
            blind_packet=PACKET, rubric=RUBRIC,
            counters={"conversation_arms": 2, "generator_provider_invocations": 8, "judge_provider_invocations": 0},
            call_ledger=ledger_evidence(2, 8, 0),
            private_mapping=MAPPING, randomization={"seed": 123, "owner_control_scenario_id": "synthetic-dev-001", "x_y_orders": [["X", "Y"]]},
            judge_receipts=[], sanity_findings=[], aggregate_status="HUMAN_ESCALATION")
        with tempfile.TemporaryDirectory() as folder:
            receipt_path = Path(folder) / "receipt.json"
            receipts.write_private_receipt(receipt_path, private_receipt)
            with self.assertRaises(ValueError):
                receipts.persist_owner_unblind(receipt_path, PACKET, RUBRIC, owner_ratings, lock, MAPPING)
            receipts.persist_owner_lock(receipt_path, PACKET, RUBRIC, owner_ratings, lock)
            revealed = receipts.persist_owner_unblind(receipt_path, PACKET, RUBRIC, owner_ratings, lock, MAPPING)
            self.assertEqual(len(revealed["revealed"]), 2)
            self.assertEqual([row["event"] for row in receipts.load_review_events(receipt_path)], ["ratings-locked", "unblinded"])
        changed_rubric = dict(RUBRIC, rubric_id="changed")
        with self.assertRaises((ValueError, TypeError)):
            receipts.unblind_owner_ratings(PACKET, changed_rubric, owner_ratings, lock, MAPPING)
        changed_packet = json.loads(json.dumps(PACKET)); changed_packet["scenarios"][0]["category"] = "changed"
        with self.assertRaises((ValueError, TypeError)):
            receipts.unblind_owner_ratings(changed_packet, RUBRIC, owner_ratings, lock, MAPPING)

    def test_owner_lock_rejects_legacy_dimension_schema(self):
        self.require_module()
        legacy = ratings.empty_ratings(PACKET)
        for row in legacy["ratings"]:
            row["scores"] = {dimension: 4 for dimension in ratings.DIMENSIONS}
            row["supplemental"] = {dimension: 4 for dimension in ratings.SUPPLEMENTAL}
        for row in legacy["pairwise_preferences"]:
            row["preferred_label"] = "tie"
        with self.assertRaises(ValueError):
            receipts.lock_owner_ratings(PACKET, RUBRIC, legacy, cycle_id="legacy-schema")

    def test_private_receipt_rejects_counters_not_supported_by_evidence(self):
        self.require_module()
        with self.assertRaises(ValueError):
            receipts.build_private_receipt(
                batch_id="v2-batch-001", candidate_sha256="d" * 64,
                scenario_commitments={"synthetic-dev-001": "a" * 64}, naturalness_policy_sha256="c" * 64,
                blind_packet=PACKET, rubric=RUBRIC,
                counters={"conversation_arms": 0, "generator_provider_invocations": 0, "judge_provider_invocations": 0},
                call_ledger=ledger_evidence(0, 0, 0),
                private_mapping=MAPPING, randomization={"seed": 123, "owner_control_scenario_id": "synthetic-dev-001", "x_y_orders": [["X", "Y"]]},
                judge_receipts=[], sanity_findings=[], aggregate_status="NOT_RUN")

    def test_lock_and_unblind_lifecycle_events_are_persisted_write_once(self):
        self.require_module()
        owner_ratings = ratings.empty_ratings(PACKET, rubric=RUBRIC)
        for row in owner_ratings["ratings"]:
            row["scores"] = {dimension["id"]: 4 for dimension in RUBRIC["core_dimensions"]}
            row["supplemental"] = {dimension: 4 for dimension in RUBRIC["conditional_dimensions"]}
        for row in owner_ratings["pairwise_preferences"]:
            row["preferred_label"] = "tie"
        lock = receipts.lock_owner_ratings(PACKET, RUBRIC, owner_ratings, cycle_id="persist-cycle")
        receipt = receipts.build_private_receipt(
            batch_id="v2-batch-001", candidate_sha256="d" * 64,
            scenario_commitments={"synthetic-dev-001": "a" * 64}, naturalness_policy_sha256="c" * 64,
            blind_packet=PACKET, rubric=RUBRIC,
            counters={"conversation_arms": 2, "generator_provider_invocations": 8, "judge_provider_invocations": 0},
            call_ledger=ledger_evidence(2, 8, 0),
            private_mapping=MAPPING, randomization={"seed": 1, "owner_control_scenario_id": "synthetic-dev-001", "x_y_orders": [["X", "Y"]]},
            judge_receipts=[], sanity_findings=[], aggregate_status="HUMAN_ESCALATION")
        with tempfile.TemporaryDirectory() as folder:
            receipt_path = Path(folder) / "receipt.json"
            receipts.write_private_receipt(receipt_path, receipt)
            receipts.persist_owner_lock(receipt_path, PACKET, RUBRIC, owner_ratings, lock)
            receipts.persist_owner_unblind(receipt_path, PACKET, RUBRIC, owner_ratings, lock, MAPPING)
            events = sorted((Path(folder) / "events").rglob("*.json"))
            self.assertEqual([event.name for event in events], ["00-ratings-locked.json", "01-unblinded.json"])
            with self.assertRaises(FileExistsError):
                receipts.persist_owner_lock(receipt_path, PACKET, RUBRIC, owner_ratings, lock)


if __name__ == "__main__":
    unittest.main()
