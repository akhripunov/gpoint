import copy
import importlib.util
import json
from pathlib import Path
import unittest

LAB=Path(__file__).resolve().parents[1]
MODULE=LAB/"evaluators/benchmark.py"
CATEGORIES=["ru_naturalness","daily","humor","support","romance","persona","initiative","topic_change","return_after_break","long_memory","fact_update","false_memory","disagreement","no_excess_advice","long_dialogue","boundaries"]

def fixture():
    out=[]
    for cat in CATEGORIES:
        for n in range(8):
            rounds=12 if cat=="long_dialogue" else 4
            out.append({"scenario_id":cat+"-"+str(n),"category":cat,"language":"ru","context":{"setup":"isolated synthetic "+cat+str(n)},"character_context":{"persona_ref":"agata-eval-v1"},"memory_fixture":[],"dialogue_turns":[t for i in range(rounds) for t in [{"role":"user","text":"Синтетическая тема "+cat+str(n)+" поворот "+str(i)},{"role":"assistant","response_slot":True}]],"expected_behavior":["observe anchored rubric"],"failure_patterns":["unsupported invented facts"],"evaluation_notes":"Synthetic fixture, not generated results"})
    return out

class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.exists(),"benchmark validator missing")
        spec=importlib.util.spec_from_file_location("benchmark",MODULE);self.b=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.b)
        self.rows=fixture()

    def test_accepts_complete_coverage(self):
        report=self.b.validate_scenarios(self.rows)
        self.assertEqual(report["count"],128);self.assertEqual(report["category_counts"]["long_memory"],8)

    def test_missing_field_rejected(self):
        del self.rows[0]["memory_fixture"]
        with self.assertRaises(ValueError): self.b.validate_scenarios(self.rows)

    def test_wrong_coverage_rejected(self):
        with self.assertRaises(ValueError): self.b.validate_scenarios(self.rows[:-1])
        self.rows[0]["category"]="unknown"
        with self.assertRaises(ValueError): self.b.validate_scenarios(self.rows)

    def test_duplicate_id_rejected(self):
        self.rows[1]["scenario_id"]=self.rows[0]["scenario_id"]
        with self.assertRaises(ValueError): self.b.validate_scenarios(self.rows)

    def test_duplicate_dialogue_rejected_after_normalizing(self):
        self.rows[1]["dialogue_turns"]=copy.deepcopy(self.rows[0]["dialogue_turns"])
        self.rows[1]["dialogue_turns"][0]["text"]="  "+self.rows[1]["dialogue_turns"][0]["text"].upper()+"  "
        with self.assertRaises(ValueError): self.b.validate_scenarios(self.rows)

    def test_premature_rounds_and_fixed_assistant_reply_rejected(self):
        self.rows[0]["dialogue_turns"]=self.rows[0]["dialogue_turns"][:6]
        with self.assertRaises(ValueError): self.b.validate_scenarios(self.rows)
        self.rows=fixture();self.rows[0]["dialogue_turns"][1]={"role":"assistant","text":"a fabricated successful reply"}
        with self.assertRaises(ValueError): self.b.validate_scenarios(self.rows)

    def test_long_dialogue_requires_twelve_rounds(self):
        next(r for r in self.rows if r["category"]=="long_dialogue")["dialogue_turns"]=self.rows[0]["dialogue_turns"]
        with self.assertRaises(ValueError): self.b.validate_scenarios(self.rows)

    def test_memory_duplicates_and_unknown_supersession_rejected(self):
        self.rows[0]["memory_fixture"]=[{"fact_id":"a","text":"fiction","supersedes":"missing"}]
        with self.assertRaises(ValueError): self.b.validate_scenarios(self.rows)

    def test_prompt_does_not_include_evaluator_answers(self):
        packet=self.b.prompt_fixture(self.rows[0])
        self.assertNotIn("expected_behavior",packet);self.assertNotIn("failure_patterns",packet);self.assertNotIn("evaluation_notes",packet)
        self.assertEqual(packet["character_context"],{"persona_ref":"agata-eval-v1"})

    def test_actual_benchmark_exists_and_validates(self):
        path=LAB/"data/golden/synthetic/agata-benchmark-v1.jsonl"
        self.assertTrue(path.exists(),"128 synthetic scenarios missing")
        self.assertEqual(self.b.validate_scenarios(self.b.load_scenarios(path))["count"],128)

    def test_story_setup_is_not_sent_as_future_oracle(self):
        self.rows[0]["context"]={"setup":"The user will announce tomorrow that the secret choice is blue","as_of":"2026-10-04","model_context":"Current session begins after a walk"}
        out=self.b.prompt_fixture(self.rows[0])
        self.assertNotIn("secret choice",json.dumps(out))
        self.assertEqual(out["context"]["model_context"],"Current session begins after a walk")

    def test_prompt_contains_only_current_user_turn_not_future_script(self):
        self.rows[0]["dialogue_turns"][-2]["text"]="SECRET FUTURE CORRECTION"
        out=self.b.prompt_fixture(self.rows[0])
        self.assertNotIn("SECRET FUTURE",json.dumps(out))

    def test_manifest_tampering_rejected(self):
        self.assertTrue(hasattr(self.b,"validate_manifest"),"manifest verifier missing")
        rows=self.b.load_scenarios(LAB/"data/golden/synthetic/agata-benchmark-v1.jsonl")
        manifest=json.loads((LAB/"data/golden/synthetic/manifest-v1.json").read_text())
        config=json.loads((LAB/"configs/benchmark-v1.json").read_text())
        manifest["scenario_hashes"][rows[0]["scenario_id"]]="bad"
        with self.assertRaises(ValueError):self.b.validate_manifest(rows,manifest,config)

    def test_malformed_context_and_evaluation_types_rejected(self):
        self.rows[0]["context"]["setup"]=123
        with self.assertRaises(ValueError):self.b.validate_scenarios(self.rows)

    def test_name_only_near_duplicate_alerted(self):
        rows=copy.deepcopy(self.rows[:2])
        rows[0]["dialogue_turns"]= [{"role":"user","text":"Я вчера гуляла с Мариной по старому парку, потом мы обсуждали новый фильм за чаем."}]
        rows[1]["dialogue_turns"]= [{"role":"user","text":"Я вчера гуляла с Ириной по старому парку, потом мы обсуждали новый фильм за чаем!"}]
        alerts=self.b.scenario_similarity_alerts(rows)
        self.assertEqual(len(alerts),1)

    def test_actual_benchmark_has_no_lexical_near_duplicates(self):
        rows=self.b.load_scenarios(LAB/"data/golden/synthetic/agata-benchmark-v1.jsonl")
        self.assertEqual(self.b.scenario_similarity_alerts(rows),[])

    def test_malformed_temporal_metadata_rejected(self):
        self.rows[0]["memory_fixture"]=[{"fact_id":"a","text":"prior fact","valid_from":{"not":"date"}}]
        with self.assertRaises(ValueError):self.b.validate_scenarios(self.rows)

    def test_reversed_user_timestamps_rejected(self):
        self.rows[0]["dialogue_turns"][0]["at"]="2026-10-04T18:00:00+03:00"
        self.rows[0]["dialogue_turns"][2]["at"]="2026-10-03T18:00:00+03:00"
        with self.assertRaises(ValueError):self.b.validate_scenarios(self.rows)

    def test_supersession_cycle_rejected(self):
        self.rows[0]["memory_fixture"]=[{"fact_id":"a","text":"old","supersedes":"b"},{"fact_id":"b","text":"new","supersedes":"a"}]
        with self.assertRaises(ValueError):self.b.validate_scenarios(self.rows)

    def test_v1_initial_memory_is_available_before_session(self):
        self.rows[0]["context"]["as_of"]="2026-10-04"
        self.rows[0]["memory_fixture"]=[{"fact_id":"a","text":"unknown future fact","valid_from":"2035-01-01"}]
        with self.assertRaises(ValueError):self.b.validate_scenarios(self.rows)
