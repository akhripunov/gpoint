import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE=Path(__file__).resolve().parents[1]/"evaluators/leakage.py"
TEXT="Это полностью синтетическая длинная реплика про фиолетовый воздушный змей над тихим двором"
class LeakageTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.exists(),"leakage detector missing")
        spec=importlib.util.spec_from_file_location("leakage",MODULE);self.l=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.l)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/"dialogs.jsonl"
        self.path.write_text(json.dumps({"dialogue_id":"private-id","source":"fixture","turns":[{"speaker":"a","text":TEXT},{"speaker":"b","text":"Второй синтетический ответ сохраняет необычную тему прогулки и воспоминание об огороде"}]},ensure_ascii=False)+"\n")
    def scenarios(self,text):return [{"scenario_id":"safe-id","dialogue_turns":[{"role":"user","text":text}]}]
    def test_copied_turn_detected_without_exporting_text(self):
        result=self.l.check_leakage(self.scenarios(TEXT.upper()),[self.path])
        self.assertEqual(result["status"],"FAIL");self.assertEqual(result["exact_matches"][0]["scenario_id"],"safe-id")
        self.assertNotIn(TEXT,json.dumps(result,ensure_ascii=False))
    def test_independent_and_short_common_phrases_not_leaks(self):
        self.assertEqual(self.l.check_leakage(self.scenarios("Привет!"),[self.path])["status"],"PASS")
        self.assertEqual(self.l.check_leakage(self.scenarios("Сегодня решили разобрать шкаф, а вечером испечь яблочный пирог"),[self.path])["status"],"PASS")
    def test_near_copy_alert_separate_from_exact_match(self):
        result=self.l.check_leakage(self.scenarios(TEXT+" вечером"),[self.path])
        self.assertEqual(result["status"],"REVIEW_REQUIRED");self.assertTrue(result["near_copy_alerts"])
    def test_future_training_source_collision_checked(self):
        result=self.l.check_leakage(self.scenarios(TEXT),[],training_paths=[self.path])
        self.assertEqual(result["future_training_check"],"FAIL")
        self.assertEqual(self.l.check_leakage(self.scenarios(TEXT),[])["future_training_check"],"NOT_YET_APPLICABLE")
    def test_span_copy_detected_for_short_individual_turns(self):
        a="Тихий зелёный сад рядом с домом";b="Пора собирать спелые яблоки для пирога"
        self.path.write_text(json.dumps({"source":"fixture","turns":[{"text":a},{"text":b}]},ensure_ascii=False)+"\n")
        rows=[{"scenario_id":"pair","dialogue_turns":[{"role":"user","text":a},{"role":"user","text":b}]}]
        result=self.l.check_leakage(rows,[self.path]);self.assertEqual(result["status"],"FAIL")
        self.assertTrue(any(x["kind"]=="span" for x in result["exact_matches"]))

    def test_context_and_memory_payloads_are_checked_for_copies(self):
        rows=self.scenarios("Совсем другой разговор о выборе настольной игры")
        rows[0]["context"]={"setup":TEXT}
        self.assertEqual(self.l.check_leakage(rows,[self.path])["status"],"FAIL")
        rows[0]["context"]={};rows[0]["memory_fixture"]=[{"fact_id":"f","text":TEXT}]
        self.assertEqual(self.l.check_leakage(rows,[self.path])["status"],"FAIL")
