"""Offline coordinator contracts: ledger-before-CLI, private receipts, deterministic outcomes."""
import importlib.util
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

LAB = Path(__file__).resolve().parents[1]
PATH = LAB / "evaluators" / "judge_coordinator.py"
SPEC = importlib.util.spec_from_file_location("agata_judge_coordinator", PATH) if PATH.exists() else None
coordinator = importlib.util.module_from_spec(SPEC) if SPEC else None
if SPEC and SPEC.loader:
    SPEC.loader.exec_module(coordinator)


def packet_and_mapping():
    blind_spec = importlib.util.spec_from_file_location("judge_test_blind", LAB / "evaluators" / "blind.py")
    blind = importlib.util.module_from_spec(blind_spec); blind_spec.loader.exec_module(blind)
    scenario = json.loads((LAB / "data/golden/synthetic/v2-dev-scenarios.jsonl").read_text().splitlines()[0])
    turns = [{"role": turn["role"], "text": turn["text"]} if turn["role"] == "user"
             else {"role": "assistant", "text": "Хорошо, спасибо, что спросил."}
             for turn in scenario["dialogue_turns"]]
    results = [{"scenario_id": scenario["scenario_id"], "model_id": model, "status": "success", "turns": turns}
               for model in ("baseline", "naturalness-v1")]
    return blind.make_v2_blind_packets([scenario], results, batch_id="judge-test-generation")


def valid_receipt(prompt_record):
    from judges import CORE_IDS, parse_judge_output
    winner_position = "first" if prompt_record["presented_labels"][0] == "X" else "second"
    value = {"overall": winner_position, "core_dimensions": {key: winner_position for key in CORE_IDS},
             "conditional_evidence": {}, "reasons": ["First is more responsive."], "defects": [],
             "uncertainty": False, "position_sensitivity": False}
    return parse_judge_output(json.dumps(value), judge_slot=prompt_record["judge_slot"],
                              scenario_id=packet_and_mapping()[0]["scenarios"][0]["scenario_id"],
                              presented_labels=prompt_record["presented_labels"])


class JudgeCoordinatorTests(unittest.TestCase):
    def require_coordinator(self):
        self.assertIsNotNone(coordinator, "three-judge AGATA coordinator is not implemented")

    def test_coordinator_preserves_configured_model_contract_and_rejects_identity_conflicts(self):
        self.require_coordinator()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); root.chmod(0o700)
            expected = {"run_id": "batch-a", "batch_id": "batch-a",
                        "promptfoo_invocation_id": "judge-invocation",
                        "lab_ledger_attempt_id": "judge-invocation", "worker_job_id": "lab-job",
                        "judge_slot": 1, "scenario_id": "dev-v2-01", "dataset_split": "DEV",
                        "presentation_labels": ["X", "Y"]}
            normalized = {"judge_slot": 1, "scenario_id": "dev-v2-01", "presented_labels": ["X", "Y"],
                          "preferred_position": "first", "core_dimension_positions": {},
                          "conditional_evidence": {}, "reasons": [], "defects": [],
                          "critical_defects": [], "uncertainty": False, "position_sensitivity": False}
            common = {**expected, "outcome": "success", "worker_terminal_status": "completed",
                      "worker_receipt_sha256": "a" * 64, "model": "gpt-6-luna",
                      "reasoning_effort": "medium", "actual_model": None,
                      "model_identity_status": "MODEL_IDENTITY_UNVERIFIED",
                      "normalized_judge_receipt": normalized}
            cli = {"run_id": "batch-a", "batch_id": "batch-a",
                   "promptfoo_invocation_id": "judge-invocation", "worker_job_id": "lab-job",
                   "worker_receipt_sha256": "a" * 64, "promptfoo_success": True,
                   "result_count": 1}
            (root / "judge-invocation.promptfoo-receipt.json").write_text(json.dumps(cli))
            receipt_path = root / "judge-invocation.judge-receipt.json"
            receipt_path.write_text(json.dumps(common))
            expected_adapter = {**expected, "presentation_labels": ["X", "Y"]}
            receipt = coordinator._judge_receipt(root, "judge-invocation", expected_adapter)
            self.assertEqual(receipt["model_identity_status"], "MODEL_IDENTITY_UNVERIFIED")
            self.assertIsNone(receipt["actual_model"])

            for changes in (
                {"model_identity_status": "MODEL_IDENTITY_MISMATCH", "actual_model": "gpt-6-sol"},
                {"model_identity_status": "MODEL_IDENTITY_UNVERIFIED", "actual_model": "gpt-6-sol"},
                {"model_identity_status": "MODEL_IDENTITY_VERIFIED", "actual_model": None},
                {"model": "gpt-6-sol"},
                {"reasoning_effort": "high"},
            ):
                with self.subTest(changes=changes):
                    receipt_path.write_text(json.dumps({**common, **changes}))
                    self.assertIsNone(coordinator._judge_receipt(root, "judge-invocation", expected_adapter))

    def test_reserves_before_promptfoo_and_aggregates_three_structured_receipts(self):
        self.require_coordinator()
        from run_coordinator import CallLedger
        blind_packet, private_mapping = packet_and_mapping()
        scenario_id = blind_packet["scenarios"][0]["scenario_id"]
        rubric = json.loads((LAB / "configs/owner-preference-rubric-v1.json").read_text())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); root.chmod(0o700)
            ledger = CallLedger(journal_path=root / "ledger.jsonl")
            receipt_dir = root / "receipts"; receipt_dir.mkdir(mode=0o700)
            calls = []

            def fake_promptfoo(prompt, *, config, receipt_directory, call_ledger, **_kwargs):
                invocation_id = config["invocation_id"]
                job = coordinator.worker_client.build_judge_job(
                    prompt, batch_id=config["batch_id"], scenario_id=config["scenario_id"],
                    dataset_split=config["dataset_split"], judge_slot=config["judge_slot"],
                    presented_labels=config["presented_labels"],
                )
                snapshot = call_ledger.__class__(journal_path=call_ledger.journal_path).snapshot()
                row = next(item for item in snapshot["judge_invocations"]["invocations"]
                           if item["invocation_id"] == invocation_id)
                self.assertIsNone(row["outcome"], "ledger reservation must precede Promptfoo")
                calls.append({"prompt": prompt, "config": config})
                evidence = {"normalized_judge_receipt": valid_receipt(config), "outcome": "success",
                            "run_id": "batch-a", "batch_id": "batch-a",
                            "model": "gpt-6-luna", "reasoning_effort": "medium",
                            "actual_model": None, "model_identity_status": "MODEL_IDENTITY_UNVERIFIED",
                            "promptfoo_invocation_id": invocation_id,
                            "lab_ledger_attempt_id": invocation_id,
                            "worker_job_id": job["job_id"],
                            "worker_receipt_sha256": "a" * 64,
                            "worker_terminal_status": "completed",
                            "judge_slot": config["judge_slot"], "scenario_id": scenario_id,
                            "dataset_split": "DEV", "presentation_labels": config["presented_labels"]}
                (receipt_directory / (invocation_id + ".judge-receipt.json")).write_text(json.dumps(evidence))
                cli = {"run_id": "batch-a", "batch_id": "batch-a",
                       "promptfoo_invocation_id": invocation_id,
                       "worker_job_id": evidence["worker_job_id"],
                       "worker_receipt_sha256": "a" * 64,
                       "promptfoo_success": True, "result_count": 1}
                (receipt_directory / (invocation_id + ".promptfoo-receipt.json")).write_text(json.dumps(cli))
                call_ledger.complete("judge", invocation_id, "success")
                return 0

            fake_approval = {"approval_id": "mock-approval", "phase": "JUDGES", "run_id": "batch-a", "max_calls": 3,
                             "experiment_ids": ["agata-lab-judge-v1"],
                             "configuration_ids": ["judge-1", "judge-2", "judge-3"],
                             "scenario_ids": [scenario_id]}
            with patch.object(coordinator.worker_client, "validate_owner_approval"), \
                 patch.object(coordinator.lab_queue, "load_owner_approval", return_value=fake_approval), \
                 patch.object(coordinator.lab_queue, "owner_approval_usage_calls", return_value=0):
                result = coordinator.run_judge_batch(
                    blind_packet, rubric, batch_id="batch-a", dataset_split="DEV", ledger=ledger,
                    private_mapping=private_mapping, sanity_findings=[], receipt_directory=receipt_dir,
                    private_directory=root, env={"GPOINT_AGATA_WORKER_SUPPORT": str(root / "support")},
                    promptfoo_executable="promptfoo", _invoke_promptfoo=fake_promptfoo,
                )
            self.assertEqual(len(calls), 3)
            self.assertEqual(result["scenarios"][0]["decision"], "AUTO_WIN_X")
            self.assertEqual(result["decision_type"], "AUTO_DECISION")
            self.assertTrue(all(row["model"] == "gpt-6-luna" and row["reasoning_effort"] == "medium"
                                and row["actual_model"] is None
                                and row["model_identity_status"] == "MODEL_IDENTITY_UNVERIFIED"
                                for row in result["scenarios"][0]["judge_receipts"]))
            self.assertEqual(ledger.snapshot()["judge_invocations"]["attempted"], 3)
            self.assertTrue((root / "batch-a.judge-plan.json").exists())
            for call in calls:
                serialized = call["prompt"].casefold()
                self.assertNotIn("baseline", serialized)
                self.assertNotIn("naturalness-v1", serialized)
                self.assertNotIn("model_id", serialized)

    def test_missing_judge_receipt_escalates_and_never_invents_verdict(self):
        self.require_coordinator()
        from run_coordinator import CallLedger
        blind_packet, private_mapping = packet_and_mapping()
        scenario_id = blind_packet["scenarios"][0]["scenario_id"]
        rubric = json.loads((LAB / "configs/owner-preference-rubric-v1.json").read_text())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); root.chmod(0o700)
            receipts = root / "receipts"; receipts.mkdir(mode=0o700)
            ledger = CallLedger(journal_path=root / "ledger.jsonl")

            def missing(_prompt, *, config, call_ledger, **_kwargs):
                call_ledger.complete("judge", config["invocation_id"], "ambiguous")
                return 1

            fake_approval = {"approval_id": "mock-approval", "phase": "JUDGES", "run_id": "batch-missing", "max_calls": 3,
                             "experiment_ids": ["agata-lab-judge-v1"],
                             "configuration_ids": ["judge-1", "judge-2", "judge-3"],
                             "scenario_ids": [scenario_id]}
            with patch.object(coordinator.worker_client, "validate_owner_approval"), \
                 patch.object(coordinator.lab_queue, "load_owner_approval", return_value=fake_approval), \
                 patch.object(coordinator.lab_queue, "owner_approval_usage_calls", return_value=0):
                result = coordinator.run_judge_batch(
                    blind_packet, rubric, batch_id="batch-missing", dataset_split="DEV", ledger=ledger,
                    private_mapping=private_mapping, sanity_findings=[], receipt_directory=receipts,
                    private_directory=root, env={"GPOINT_AGATA_WORKER_SUPPORT": str(root / "support")},
                    promptfoo_executable="promptfoo", _invoke_promptfoo=missing,
                )
            self.assertEqual(result["decision"], "HUMAN_ESCALATION")
            self.assertIn("MISSING_OR_INVALID_JUDGE_OUTPUT", result["scenarios"][0]["reason_codes"])
            self.assertEqual(ledger.snapshot()["judge_invocations"]["attempted"], 3)

    def test_sealed_split_and_three_pair_batch_fail_before_promptfoo_or_reservation(self):
        self.require_coordinator()
        from run_coordinator import CallLedger
        blind_packet, private_mapping = packet_and_mapping()
        rubric = json.loads((LAB / "configs/owner-preference-rubric-v1.json").read_text())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); root.chmod(0o700)
            receipts = root / "receipts"; receipts.mkdir(mode=0o700)
            ledger = CallLedger(journal_path=root / "ledger.jsonl")
            fake_approval = {"approval_id": "mock-approval", "phase": "JUDGES", "run_id": "sealed", "max_calls": 3,
                             "experiment_ids": ["agata-lab-judge-v1"],
                             "configuration_ids": ["judge-1", "judge-2", "judge-3"],
                             "scenario_ids": [blind_packet["scenarios"][0]["scenario_id"]]}
            with self.assertRaises(PermissionError):
                with patch.object(coordinator.lab_queue, "load_owner_approval", return_value=fake_approval):
                    coordinator.run_judge_batch(blind_packet, rubric, batch_id="sealed", dataset_split="SEALED_HOLDOUT",
                        ledger=ledger, private_mapping=private_mapping, sanity_findings=[], receipt_directory=receipts,
                        private_directory=root, env={}, promptfoo_executable="promptfoo",
                        _invoke_promptfoo=lambda *args, **kwargs: self.fail("Promptfoo must not run"))
            self.assertEqual(ledger.snapshot()["judge_invocations"]["attempted"], 0)

    def test_tampered_dialogue_or_caller_asserted_v2_split_fails_before_reservation(self):
        self.require_coordinator()
        from run_coordinator import CallLedger
        blind_packet, private_mapping = packet_and_mapping()
        rubric = json.loads((LAB / "configs/owner-preference-rubric-v1.json").read_text())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); root.chmod(0o700)
            receipts = root / "receipts"; receipts.mkdir(mode=0o700)
            ledger = CallLedger(journal_path=root / "ledger.jsonl")
            tampered = copy.deepcopy(blind_packet)
            tampered["scenarios"][0]["candidates"][0]["turns"][0]["text"] = "Legacy or substituted user data"
            with self.assertRaises(PermissionError):
                coordinator.run_judge_batch(
                    tampered, rubric, batch_id="tampered", dataset_split="DEV", ledger=ledger,
                    private_mapping=private_mapping, sanity_findings=[], receipt_directory=receipts,
                    private_directory=root, env={}, promptfoo_executable="promptfoo",
                    _invoke_promptfoo=lambda *args, **kwargs: self.fail("Promptfoo must not run"),
                )
            self.assertEqual(ledger.snapshot()["judge_invocations"]["attempted"], 0)


if __name__ == "__main__":
    unittest.main()
