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
        packet,key=self.b.make_blind_packets(self.scenarios,self.results,0)
        self.assertEqual(packet["completion_status"],"INCOMPLETE")
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
