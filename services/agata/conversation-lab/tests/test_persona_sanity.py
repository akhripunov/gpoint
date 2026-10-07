"""Deterministic no-rewrite Persona/grammar sanity checks on synthetic turns."""
import importlib.util
from pathlib import Path
import unittest


MODULE = Path(__file__).resolve().parents[1] / "evaluators" / "persona_sanity.py"
SPEC = importlib.util.spec_from_file_location("agata_persona_sanity", MODULE) if MODULE.exists() else None
sanity = importlib.util.module_from_spec(SPEC) if SPEC else None
if SPEC and SPEC.loader:
    SPEC.loader.exec_module(sanity)


def scenario(user_gender="male"):
    return {
        "scenario_id": "synthetic-persona-02",
        "user_gender": user_gender,
        "dialogue_turns": [
            {"role": "user", "text": "Синтетический вопрос?"},
            {"role": "assistant", "response_slot": True},
            {"role": "user", "text": "Синтетическое продолжение?"},
            {"role": "assistant", "response_slot": True},
        ],
    }


def arms(first="Я рада тебя слышать.", second="Ты сегодня хорошо выглядишь.", *, user_gender="male"):
    return [
        {"configuration_id": "baseline", "turns": [
            {"role": "user", "text": "Синтетический вопрос?"},
            {"role": "assistant", "text": first},
            {"role": "user", "text": "Синтетическое продолжение?"},
            {"role": "assistant", "text": second},
        ]},
        {"configuration_id": "naturalness-v1", "turns": [
            {"role": "user", "text": "Синтетический вопрос?"},
            {"role": "assistant", "text": "Рада, что спросил."},
            {"role": "user", "text": "Синтетическое продолжение?"},
            {"role": "assistant", "text": "Расскажи, как прошёл день."},
        ]},
    ]


class PersonaSanityTests(unittest.TestCase):
    def require_module(self):
        self.assertIsNotNone(sanity, "Persona/grammar sanity validator is not implemented")

    def test_female_agata_self_reference_violation_is_private_quality_finding(self):
        self.require_module()
        transcript = arms(first="Я устал тебя ждать.")[0]["turns"]
        report = sanity.validate_transcripts(scenario(), arms(first="Я устал тебя ждать."))
        self.assertEqual(report["structural_status"], "PASS")
        self.assertIn("AGATA_SELF_MASCULINE_GRAMMAR", {item["code"] for item in report["private_quality_findings"]})
        self.assertNotIn("Я устала", json_dump(report))
        self.assertEqual(transcript[1]["text"], "Я устал тебя ждать.")
        self.assertNotIn("rewritten_turns", report)

    def test_known_male_user_is_not_addressed_in_feminine_grammatical_gender(self):
        self.require_module()
        report = sanity.validate_transcripts(scenario("male"), arms(second="Ты уже слышала эту историю?"))
        self.assertIn("USER_FEMININE_ADDRESS", {item["code"] for item in report["private_quality_findings"]})

    def test_unknown_gender_is_not_inferred_from_name_and_gendered_address_is_flagged(self):
        self.require_module()
        unknown = scenario(None)
        unknown["display_name"] = "Алекс"
        report = sanity.validate_transcripts(unknown, arms(second="Ты был там вчера?"))
        self.assertIn("UNKNOWN_USER_GENDERED_ADDRESS", {item["code"] for item in report["private_quality_findings"]})
        neutral = sanity.validate_transcripts(unknown, arms(second="Приятно тебя читать."))
        self.assertEqual(neutral["private_quality_findings"], [])

    def test_turn_count_role_order_and_user_turn_drift_are_structural_failures(self):
        self.require_module()
        malformed = arms()
        malformed[0]["turns"].pop()
        result = sanity.validate_transcripts(scenario(), malformed)
        self.assertEqual(result["structural_status"], "FAIL")
        self.assertIn("TURN_COUNT_MISMATCH", {item["code"] for item in result["structural_defects"]})
        malformed = arms()
        malformed[1]["turns"][0]["text"] = "Подменённый вопрос"
        result = sanity.validate_transcripts(scenario(), malformed)
        self.assertIn("USER_TURN_MISMATCH", {item["code"] for item in result["structural_defects"]})
        malformed = arms()
        malformed[1]["turns"][1]["role"] = "user"
        result = sanity.validate_transcripts(scenario(), malformed)
        self.assertIn("TURN_ROLE_ORDER_MISMATCH", {item["code"] for item in result["structural_defects"]})

    def test_blind_labels_and_technical_metadata_fail_closed(self):
        self.require_module()
        for text, expected in (("Candidate B звучит лучше", "BLIND_LABEL_LEAK"),
                               ("run_id=abc provider=codex", "TECHNICAL_METADATA_LEAK")):
            with self.subTest(text=text):
                result_arms = arms(first=text)
                report = sanity.validate_transcripts(scenario(), result_arms)
                self.assertEqual(report["structural_status"], "FAIL")
                self.assertIn(expected, {item["code"] for item in report["structural_defects"]})

    def test_persona_contradiction_is_recorded_without_rewriting_any_text(self):
        self.require_module()
        transcript = arms(first="Я мужчина и рад тебя видеть.")[0]["turns"]
        before = json_dump(transcript)
        report = sanity.validate_transcripts(scenario(), arms(first="Я мужчина и рад тебя видеть."))
        self.assertIn("AGATA_PERSONA_CONTRADICTION", {item["code"] for item in report["private_quality_findings"]})
        self.assertEqual(json_dump(transcript), before)


def json_dump(value):
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


if __name__ == "__main__":
    unittest.main()
