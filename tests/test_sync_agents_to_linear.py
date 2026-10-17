import importlib.util
import json
import pathlib
import re
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
    def successful_call(self, content=None, *, update_id=DOC_ID):
        calls = []
        existing_content = content if content is not None else "previous AGENTS content\n"
        readback_content = content if content is not None else CONTENT

        def call(query, variables, key):
            calls.append((query, variables, key))
            if "documentUpdate" in query:
                return {"documentUpdate": {"success": True, "document": {"id": update_id, "title": "AGENTS.md"}}}
            if any("documentUpdate" in previous_query for previous_query, _, _ in calls[:-1]):
                document_content = readback_content
            else:
                document_content = existing_content
            return {"document": {"id": DOC_ID, "title": "AGENTS.md", "content": document_content}}

        return call, calls

    def test_updates_existing_document_and_reads_back_exact_content(self):
        call, calls = self.successful_call()
        result = SYNC.sync_document(CONTENT, DOC_ID, "test-key", call)
        self.assertTrue(result)
        self.assertEqual(len(calls), 3)
        self.assertIn("documentUpdate", calls[1][0])
        self.assertNotIn("documentCreate", " ".join(query for query, _, _ in calls))
        self.assertEqual(calls[1][1], {"id": DOC_ID, "input": {"content": CONTENT}})

    def test_matching_document_is_not_rewritten(self):
        call, calls = self.successful_call(content=CONTENT)
        result = SYNC.sync_document(CONTENT, DOC_ID, "test-key", call)
        self.assertFalse(result)
        self.assertEqual(len(calls), 1)
        self.assertFalse(any("documentUpdate" in query for query, _, _ in calls))

    def test_workflow_rechecks_main_pushes_after_stale_agents_event(self):
        workflow = (ROOT / ".github" / "workflows" / "sync-agents-to-linear.yml").read_text()
        push_trigger = re.search(r"(?ms)^on:\n(.*?)(?=^permissions:)", workflow)
        self.assertIsNotNone(push_trigger)
        self.assertIn("  push:\n    branches: [main]\n", push_trigger.group(1))
        self.assertNotIn("paths:", push_trigger.group(1))

    def test_agents_sync_workflow_uses_available_python3_executable(self):
        workflow = (ROOT / ".github" / "workflows" / "sync-agents-to-linear.yml").read_text()
        self.assertIn("run: python3 -m py_compile scripts/sync_agents_to_linear.py", workflow)
        self.assertIn("run: python3 scripts/sync_agents_to_linear.py", workflow)
        self.assertNotRegex(workflow, r"run: python(?:\s|$)")

    def test_current_main_check_runs_immediately_before_linear_update(self):
        call, calls = self.successful_call()
        checks = []
        SYNC.sync_document(CONTENT, DOC_ID, "test-key", call,
                           before_update=lambda: checks.append(len(calls)))
        self.assertEqual(checks, [1])
        self.assertEqual(len(calls), 3)

    def test_stale_source_is_rejected_before_linear_mutation(self):
        call, calls = self.successful_call()
        def stale_source():
            raise SYNC.SyncError("source commit is no longer current main")
        with self.assertRaisesRegex(SYNC.SyncError, "no longer current main"):
            SYNC.sync_document(CONTENT, DOC_ID, "test-key", call, before_update=stale_source)
        self.assertEqual(len(calls), 1)
        self.assertFalse(any("documentUpdate" in query for query, _, _ in calls))

    def test_current_main_probe_requires_exact_sha_and_token(self):
        with self.assertRaisesRegex(SYNC.SyncError, "full commit SHA"):
            SYNC.verify_current_main_sha("short", "test-token", opener=lambda *_a, **_k: self.fail("API called"))
        with self.assertRaisesRegex(SYNC.SyncError, "GITHUB_TOKEN"):
            SYNC.verify_current_main_sha("a" * 40, "", opener=lambda *_a, **_k: self.fail("API called"))

    def test_current_main_probe_fails_closed_on_drift_without_leaking_token(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, _size):
                return json.dumps({"object": {"sha": "b" * 40}}).encode()

        seen = []
        def opener(request, **kwargs):
            seen.append((request, kwargs))
            return Response()
        with self.assertRaisesRegex(SYNC.SyncError, "no longer current main") as failure:
            SYNC.verify_current_main_sha("a" * 40, "secret-token", opener=opener)
        self.assertEqual(seen[0][0].get_method(), "GET")
        self.assertEqual(seen[0][0].get_header("Authorization"), "Bearer secret-token")
        self.assertNotIn("secret-token", str(failure.exception))

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

    def test_graphql_machine_codes_are_safe_and_transients_retry_bounded(self):
        for code, expected in [('INTERNAL_SERVER_ERROR', 3),
                               ('RATELIMITED', 3), ('BAD_USER_INPUT', 1),
                               ('SECRET_DO_NOT_LOG', 1)]:
            calls = []
            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *_): return False
                def read(self):
                    return json.dumps({'errors': [{
                        'message': 'sensitive query and token', 'extensions': {'code': code}}]}).encode()
            def opener(*_args, **_kwargs):
                calls.append(1)
                return Response()
            with self.subTest(code=code), self.assertRaises(SYNC.SyncError) as failure:
                SYNC.graphql_request('query { viewer { id } }', {}, 'test-key',
                                     opener=opener, sleep=lambda _: None)
            self.assertEqual(len(calls), expected)
            self.assertNotIn('sensitive', str(failure.exception))
            self.assertNotIn('SECRET_DO_NOT_LOG', str(failure.exception))
            self.assertIn('code=', str(failure.exception))

    def test_network_retries_are_bounded(self):
        attempts = []

        def failing_opener(*_args, **_kwargs):
            attempts.append(1)
            raise urllib.error.URLError("offline")

        with self.assertRaisesRegex(SYNC.SyncError, "after 3 attempts"):
            SYNC.graphql_request("query { viewer { id } }", {}, "test-key", opener=failing_opener, sleep=lambda _: None)
        self.assertEqual(len(attempts), 3)

    def test_socket_timeout_retries_are_bounded_and_safe(self):
        attempts = []
        def failing_opener(*args, **kwargs):
            attempts.append(1)
            raise TimeoutError("sensitive internal endpoint")
        with self.assertRaisesRegex(SYNC.SyncError, "after 3 attempts") as failure:
            SYNC.graphql_request("query { viewer { id } }", {}, "test-key", opener=failing_opener, sleep=lambda _: None)
        self.assertEqual(len(attempts), 3)
        self.assertNotIn("sensitive", str(failure.exception))

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
