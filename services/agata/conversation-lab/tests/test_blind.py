import copy
import importlib.util
import json
from pathlib import Path
import unittest

MODULE=Path(__file__).resolve().parents[1]/"evaluators/blind.py"
class BlindTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.exists(),"offline blind evaluator missing")
        spec=importlib.util.spec_from_file_location("blind",MODULE);self.b=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.b)
        self.scenarios=[{"scenario_id":"s1","character_context":{"persona_ref":"same"},"memory_fixture":[],"dialogue_turns":[{"role":"user","text":"Поговорим о саде?"},{"role":"assistant","response_slot":True}],"expected_behavior":["gentle"],"failure_patterns":["coercive"]}]
        self.results=[{"scenario_id":"s1","model_id":m,"status":"success","turns":[{"role":"user","text":"Поговорим о саде?"},{"role":"assistant","text":"Да, о саде!"}],"provider_header":"SECRET-HEADER","usage":{"output":42}} for m in ["private-model-alpha","private-model-beta","private-model-gamma"]]
    def test_same_seed_reproduces_and_key_stays_private(self):
        packet,key=self.b.make_blind_packets(self.scenarios,self.results,41)
        self.assertEqual((packet,key),self.b.make_blind_packets(self.scenarios,self.results,41))
        self.assertEqual(len(key["mappings"]),3)
        rendered=json.dumps(packet,ensure_ascii=False)
        self.assertNotIn("private-model",rendered);self.assertNotIn("SECRET-HEADER",rendered);self.assertNotIn('"usage"',rendered)
        self.assertEqual(packet["scenarios"][0]["candidates"][0]["turns"][1]["text"],"Да, о саде!")
    def test_duplicate_result_rejected(self):
        self.results.append(copy.deepcopy(self.results[0]))
        with self.assertRaises(ValueError): self.b.make_blind_packets(self.scenarios,self.results,0)
    def test_inconsistent_model_coverage_rejected(self):
        self.scenarios.append({**self.scenarios[0],"scenario_id":"s2"})
        with self.assertRaises(ValueError): self.b.make_blind_packets(self.scenarios,self.results,0)
    def test_success_requires_complete_matching_user_turns(self):
        self.results[0]["turns"][0]["text"]="wrong user context"
        with self.assertRaises(ValueError): self.b.make_blind_packets(self.scenarios,self.results,0)
    def test_error_is_not_scored_as_success(self):
        self.results[0]["status"]="error";self.results[0]["turns"]=[]
        with self.assertRaises(ValueError):
            self.b.make_blind_packets(self.scenarios,self.results,0)
    def test_seed_changes_order_without_omitting_models(self):
        orders=[]
        for seed in range(8):
            packet,key=self.b.make_blind_packets(self.scenarios,self.results,seed)
            orders.append(tuple(m["model_id"] for m in key["mappings"]))
        self.assertGreater(len(set(orders)),1);self.assertTrue(all(set(x)=={r["model_id"] for r in self.results} for x in orders))

    def test_seed_cannot_reconstruct_private_mapping_from_public_packet(self):
        packet,key=self.b.make_blind_packets(self.scenarios,self.results,982743)
        self.assertNotIn("seed",packet)
        self.assertEqual(key["seed"],982743)

    def test_persona_quality_defect_escalates_privately_without_rewriting_dialogue(self):
        scenario=copy.deepcopy(self.scenarios[0]);scenario["user_gender"]="male"
        results=copy.deepcopy(self.results[:2])
        results[0]["turns"][1]["text"]="Я устал тебя ждать."
        packet,key=self.b.make_blind_packets([scenario],results,81)
        self.assertEqual(packet["quality_status"],"HUMAN_ESCALATION")
        self.assertEqual(key["sanity_findings"][0]["private_quality_findings"][0]["code"],"AGATA_SELF_MASCULINE_GRAMMAR")
        public=json.dumps(packet,ensure_ascii=False)
        self.assertNotIn("AGATA_SELF_MASCULINE_GRAMMAR",public)
        texts=[c["turns"][1]["text"] for c in packet["scenarios"][0]["candidates"]]
        self.assertIn("Я устал тебя ждать.",texts)

    def test_structural_or_label_leak_never_emits_blind_packet(self):
        scenario=copy.deepcopy(self.scenarios[0]);scenario["user_gender"]="male"
        results=copy.deepcopy(self.results[:2])
        results[0]["turns"][1]["text"]="Candidate B звучит лучше."
        with self.assertRaises(ValueError):
            self.b.make_blind_packets([scenario],results,81)
        results=copy.deepcopy(self.results[:2]);results[0]["turns"].pop()
        with self.assertRaises(ValueError):
            self.b.make_blind_packets([scenario],results,81)

    def test_v2_x_y_assignment_is_reproducible_from_immutable_batch_identity(self):
        scenarios=copy.deepcopy(self.scenarios)
        results=copy.deepcopy(self.results[:2])
        results[0]["model_id"]="baseline"
        results[1]["model_id"]="naturalness-v1"
        first=self.b.make_v2_blind_packets(scenarios,results,batch_id="immutable-batch-004")
        second=self.b.make_v2_blind_packets(scenarios,results,batch_id="immutable-batch-004")
        self.assertEqual(first,second)
        self.assertEqual(first[1]["batch_id"],"immutable-batch-004")
        self.assertEqual(first[1]["seed"],second[1]["seed"])

    def test_v2_packet_omits_evaluator_oracle_fields_and_scans_displayed_context(self):
        scenarios=copy.deepcopy(self.scenarios)
        scenarios[0].update({"context":{"summary":"Baseline arm; private"},"evaluation_notes":"judge secret",
                            "expected_behavior":["oracle"],"failure_patterns":["hint"],"category":"daily"})
        results=copy.deepcopy(self.results[:2]);results[0]["model_id"]="baseline";results[1]["model_id"]="naturalness-v1"
        with self.assertRaises(ValueError): self.b.make_v2_blind_packets(scenarios,results,batch_id="metadata-leak")
        scenarios[0]["context"]={"summary":"A calm morning"}
        packet,_=self.b.make_v2_blind_packets(scenarios,results,batch_id="metadata-safe")
        rendered=json.dumps(packet,ensure_ascii=False).casefold()
        self.assertNotIn("evaluation_notes",rendered)
        self.assertNotIn("expected_behavior",rendered)
        self.assertNotIn("failure_patterns",rendered)
        self.assertNotIn("judge secret",rendered)

    def test_v2_packet_rejects_technical_metadata_in_user_turn(self):
        scenarios=copy.deepcopy(self.scenarios)
        scenarios[0]["dialogue_turns"][0]["text"]="Candidate A, run_id=abc"
        results=copy.deepcopy(self.results[:2]);results[0]["model_id"]="baseline";results[1]["model_id"]="naturalness-v1"
        for row in results: row["turns"][0]["text"]="Candidate A, run_id=abc"
        with self.assertRaises(ValueError): self.b.make_v2_blind_packets(scenarios,results,batch_id="user-leak")
