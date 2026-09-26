import importlib.util
import json
import pathlib
import unittest
import urllib.error
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sync_agents_to_linear", ROOT / "scripts" / "sync_agents_to_linear.py"
)
SYNC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC)

DOC_ID = "b0924f59-ceaa-411e-a274-880f802e8f9a"
CONTENT = "# Test AGENTS\n\nsource body\n"


class SyncContractTests(unittest.TestCase):
    def successful_call(self, content=CONTENT, *, update_id=DOC_ID):
        calls = []

        def call(query, variables, key):
            calls.append((query, variables, key))
            if "documentUpdate" in query:
                return {"documentUpdate": {"success": True, "document": {"id": update_id, "title": "AGENTS.md"}}}
            return {"document": {"id": DOC_ID, "title": "AGENTS.md", "content": content}}

        return call, calls

    def test_updates_existing_document_and_reads_back_exact_content(self):
        call, calls = self.successful_call()
        SYNC.sync_document(CONTENT, DOC_ID, "test-key", call)
        self.assertEqual(len(calls), 3)
        self.assertIn("documentUpdate", calls[1][0])
        self.assertNotIn("documentCreate", " ".join(query for query, _, _ in calls))
        self.assertEqual(calls[1][1], {"id": DOC_ID, "input": {"content": CONTENT}})

    def test_missing_secret_fails_before_api_call(self):
        with self.assertRaisesRegex(SYNC.SyncError, "LINEAR_API_KEY is required"):
            SYNC.sync_document(CONTENT, DOC_ID, "", lambda *_: self.fail("API called"))

    def test_invalid_document_id_fails_before_api_call(self):
        with self.assertRaisesRegex(SYNC.SyncError, "must be a UUID"):
            SYNC.sync_document(CONTENT, "wrong-id", "test-key", lambda *_: self.fail("API called"))

    def test_missing_document_fails_closed_without_update(self):
        calls = []

        def call(query, variables, key):
            calls.append(query)
            return {"document": None}

        with self.assertRaisesRegex(SYNC.SyncError, "does not exist"):
            SYNC.sync_document(CONTENT, DOC_ID, "test-key", call)
        self.assertEqual(len(calls), 1)

    def test_wrong_document_id_fails_before_update(self):
        call, calls = self.successful_call(update_id="00000000-0000-0000-0000-000000000001")

        def wrong_initial_id(query, variables, key):
            result = call(query, variables, key)
            if len(calls) == 1:
                result["document"]["id"] = "00000000-0000-0000-0000-000000000001"
            return result

        with self.assertRaisesRegex(SYNC.SyncError, "different document ID"):
            SYNC.sync_document(CONTENT, DOC_ID, "test-key", wrong_initial_id)
        self.assertEqual(len(calls), 1)

    def test_readback_mismatch_fails(self):
        call, calls = self.successful_call(content="wrong content")
        with self.assertRaisesRegex(SYNC.SyncError, "does not match"):
            SYNC.sync_document(CONTENT, DOC_ID, "test-key", call)
        self.assertEqual(len(calls), 3)

    def test_graphql_errors_fail_even_with_http_200(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps({"data": {"document": None}, "errors": [{"message": "denied"}]}).encode()

        with self.assertRaisesRegex(SYNC.SyncError, "GraphQL request failed"):
            SYNC.graphql_request("query { viewer { id } }", {}, "test-key", opener=lambda *_args, **_kwargs: Response(), sleep=lambda _: None)

    def test_network_retries_are_bounded(self):
        attempts = []

        def failing_opener(*_args, **_kwargs):
            attempts.append(1)
            raise urllib.error.URLError("offline")

        with self.assertRaisesRegex(SYNC.SyncError, "after 3 attempts"):
            SYNC.graphql_request("query { viewer { id } }", {}, "test-key", opener=failing_opener, sleep=lambda _: None)
        self.assertEqual(len(attempts), 3)

    def test_http_429_and_5xx_retry_bounded_but_auth_does_not(self):
        for status, expected_attempts in ((429, 3), (503, 3), (403, 1)):
            attempts = []

            def failing_opener(*_args, **_kwargs):
                attempts.append(1)
                raise urllib.error.HTTPError(SYNC.ENDPOINT, status, "failure", None, None)

            with self.subTest(status=status):
                with self.assertRaises(SYNC.SyncError):
                    SYNC.graphql_request(
                        "query { viewer { id } }",
                        {},
                        "test-key",
                        opener=failing_opener,
                        sleep=lambda _: None,
                    )
                self.assertEqual(len(attempts), expected_attempts)

    def test_only_one_terminal_newline_can_be_normalized(self):
        self.assertTrue(SYNC._content_matches("body\n", "body"))
        self.assertFalse(SYNC._content_matches("body\n\n", "body"))

    def test_linear_equivalent_bullet_marker_is_normalized(self):
        self.assertTrue(SYNC._content_matches("- item\n", "* item\n"))
        self.assertFalse(SYNC._content_matches("- original item\n", "* changed item\n"))

    def test_bullet_marker_inside_fenced_code_is_not_normalized(self):
        source = "```text\n- literal code\n```\n"
        returned = "```text\n* literal code\n```\n"
        self.assertFalse(SYNC._content_matches(source, returned))

    def test_documented_linear_markdown_serializations_are_narrowly_normalized(self):
        source = (
            "```text\ncode\n```\n\n"
            "Paragraph before list.\n\n"
            "* `wrapped\n  inline code`\n"
            "[role registry](<docs/ROLE_REGISTRY.md>)\n"
        )
        returned = (
            "```text\ncode\n```\n"
            "Paragraph before list.\n"
            "* `wrapped inline code`\n"
            "[role registry](docs/ROLE_REGISTRY.md)\n"
        )
        self.assertTrue(SYNC._content_matches(source, returned))


if __name__ == "__main__":
    unittest.main()
