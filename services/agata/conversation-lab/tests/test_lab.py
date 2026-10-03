import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "pipelines" / "lab.py"
SPEC = importlib.util.spec_from_file_location("agata_conversation_lab", MODULE)
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)


class NormalizationTests(unittest.TestCase):
    def test_dialogsum_person_markers_keep_turn_order(self):
        source = lab.get_source("dialogsum-ru")
        item = lab.dialogue_from_row({"id": "x", "dialogue": "#Person1#: привет\n#Person2#: здравствуй"}, source, "train")
        self.assertEqual([x["speaker"] for x in item["turns"]], ["speaker_1", "speaker_2"])
        self.assertEqual(len(item["turns"]), 2)

    def test_dialogsum_list_markers_move_into_speaker_field(self):
        source = lab.get_source("dialogsum-ru")
        item = lab.dialogue_from_row({"dialogue": ["#Person1#: привет", "#Person2#: здравствуй"]}, source, "train")
        self.assertEqual(item["turns"], [{"speaker":"speaker_1","text":"привет"},{"speaker":"speaker_2","text":"здравствуй"}])

    def test_empty_turns_are_dropped_and_pii_is_flagged(self):
        row = {"dialog": ["hello", "", "reach me at person@example.com"]}
        item = lab.normalize_record(lab.dialogue_from_row(row, lab.get_source("daily-dialog"), "train"))
        self.assertEqual(len(item["turns"]), 2)
        self.assertIn("pii_suspected", item["quality_flags"])

    def test_contact_detection_avoids_grouped_currency_amount_false_positive(self):
        self.assertTrue(lab.is_pii("call +7 (999) 123-45-67"))
        self.assertTrue(lab.is_pii("mail person@example.com"))
        self.assertTrue(lab.is_pii("call 415 555 2671"))
        self.assertFalse(lab.is_pii("18 500 000 000 рублей"))
        self.assertFalse(lab.is_pii("reference 2019-01-02-03"))

    def test_russian_dialogues_jsonl_sample_field_is_dialogue(self):
        item = lab.dialogue_from_row({"sample": ["один", "два", "три"]}, lab.get_source("russian-dialogues-2"), "unspecified")
        self.assertEqual([x["text"] for x in item["turns"]], ["один", "два", "три"])

    def test_empathetic_marker_tokens_restore_punctuation(self):
        self.assertEqual(lab.clean_text("hello_comma_ world_period_"), "hello, world.")

    def test_resume_skips_splits_before_saved_cursor(self):
        plan = lab.split_resume_plan([("default", "train"), ("default", "validation"), ("default", "test")], {"config":"default", "split":"validation", "offset":400})
        self.assertEqual(plan, [("default", "validation", 400), ("default", "test", 0)])

    def test_resume_rejects_missing_saved_split(self):
        with self.assertRaises(RuntimeError):
            lab.split_resume_plan([("default", "train")], {"config":"default", "split":"test", "offset":400})

    def test_convokit_keeps_single_turn_rows_for_filter_counting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "extracted" / "movie-corpus"
            root.mkdir(parents=True)
            (root / "utterances.jsonl").write_text(json.dumps({"id":"c1","conversation_id":"c1","text":"only line","speaker":"u1","reply-to":None})+"\n",encoding="utf-8")
            (root / "conversations.json").write_text(json.dumps({"c1":{"meta":{}}}),encoding="utf-8")
            rows = list(lab.iter_convokit(Path(tmp)))
            self.assertEqual(len(rows),1)
            self.assertEqual(len(rows[0]["turns"]),1)

    def test_archive_traversal_rejected(self):
        for name in ("../escape", "/absolute", "folder/../../escape"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                lab.safe_rel(name)

    def test_extended_is_not_in_default_download_selection(self):
        starter = [x["id"] for x in lab.sources() if x["group"] == "starter"]
        extended = [x["id"] for x in lab.sources() if x["group"] == "extended"]
        self.assertEqual(len(starter), 6)
        self.assertEqual(extended, ["russian-conversations-rldd", "toloka-persona-chat-ru"])

    def test_extended_download_is_rejected_before_network_access(self):
        with self.assertRaises(PermissionError):
            lab.main(["download", "russian-conversations-rldd"])


if __name__ == "__main__":
    unittest.main()
