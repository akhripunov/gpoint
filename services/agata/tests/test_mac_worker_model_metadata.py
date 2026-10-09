"""Codex provider identity parsing stays separate from configured model labels."""
import json
import sys
import unittest
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parents[1] / "worker"
sys.path.insert(0, str(WORKER_DIR))
import gpoint_mac_worker as worker  # noqa: E402


class CodexModelMetadataTests(unittest.TestCase):
    def test_generic_model_fields_and_cli_banner_are_not_provider_identity(self):
        stdout = "\n".join((
            json.dumps({"type": "thread.started", "model": "gpt-6-sol"}),
            json.dumps({"type": "item.completed", "item": {"type": "reasoning", "model_id": "gpt-6-sol"}}),
            "Codex model: gpt-6-sol",
            json.dumps({"type": "turn.completed", "model": "gpt-6-sol", "usage": {"input_tokens": 2}}),
        ))
        metadata = worker._codex_reported_metadata(stdout)
        self.assertIsNone(metadata["actual_model"])
        self.assertIsNone(metadata["actual_model_source"])

    def test_only_provider_server_model_on_terminal_turn_is_identity(self):
        stdout = json.dumps({
            "type": "turn.completed", "server_model": "gpt-6-sol",
            "usage": {"input_tokens": 2},
        })
        metadata = worker._codex_reported_metadata(stdout)
        self.assertEqual(metadata["actual_model"], "gpt-6-sol")
        self.assertEqual(metadata["actual_model_source"], "codex_provider_response_metadata")

    def test_server_model_outside_terminal_event_is_ignored(self):
        stdout = json.dumps({"type": "item.completed", "server_model": "gpt-6-sol"})
        metadata = worker._codex_reported_metadata(stdout)
        self.assertIsNone(metadata["actual_model"])
        self.assertIsNone(metadata["actual_model_source"])


if __name__ == "__main__":
    unittest.main()
