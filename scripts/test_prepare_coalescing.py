import importlib.util
import contextlib
import io
import json
import os
import errno
from pathlib import Path
import socket
import ssl
import tempfile
import urllib.error
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepare_coalescing", ROOT / "scripts" / "prepare_coalescing.py"
)
COALESCING = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COALESCING)
from network_retry import RetryBudget
PREFLIGHT_SPEC = importlib.util.spec_from_file_location(
    "github_api_preflight", ROOT / "scripts" / "github_api_preflight.py"
)
PREFLIGHT = importlib.util.module_from_spec(PREFLIGHT_SPEC)
PREFLIGHT_SPEC.loader.exec_module(PREFLIGHT)


class PrepareCoalescingTests(unittest.TestCase):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"object": {"sha": self.payload}}).encode()

    def test_final_read_timeout_gets_one_transport_retry_and_can_win(self):
        sha = "a" * 40
        events = [self.Response(sha), TimeoutError("private transport detail"), self.Response(sha)]
        calls, sleeps = [], []

        def opener(request, timeout):
            calls.append((request.full_url, timeout))
            event = events.pop(0)
            if isinstance(event, Exception):
                raise event
            return event

        def read():
            return COALESCING.read_current_main_sha("secret-token", opener=opener, retry_sleep=sleeps.append)

        self.assertEqual(COALESCING.wait_for_quiet_main(sha, read, sleep=lambda _: None), "WINNER")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [0.25])

    def test_final_read_timeout_retry_that_sees_new_main_supersedes_candidate(self):
        old, new = "a" * 40, "b" * 40
        events = [self.Response(old), socket.timeout("private detail"), self.Response(new)]
        calls = []

        def opener(request, timeout):
            calls.append(timeout)
            event = events.pop(0)
            if isinstance(event, Exception):
                raise event
            return event

        read = lambda: COALESCING.read_current_main_sha(
            "secret-token", opener=opener, retry_sleep=lambda _: None
        )
        self.assertEqual(COALESCING.wait_for_quiet_main(old, read, sleep=lambda _: None), "SUPERSEDED")
        self.assertEqual(len(calls), 3)

    def test_second_transient_failure_blocks_after_exactly_one_retry(self):
        calls, sleeps = [], []

        def opener(*_args, **_kwargs):
            calls.append(1)
            raise urllib.error.URLError(ConnectionResetError("private reset detail"))

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(COALESCING.CoalescingError) as raised:
            COALESCING.read_current_main_sha(
                "secret-token", opener=opener, retry_sleep=sleeps.append
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [0.25])
        self.assertNotIn("private reset detail", str(raised.exception))
        self.assertNotIn("private reset detail", stderr.getvalue())

    def test_transport_eof_gets_one_retry_without_relying_on_shared_core_wrapping(self):
        sha = "d" * 40
        for failure in (EOFError("private EOF detail"), ssl.SSLEOFError("private TLS EOF detail"),
                        OSError(errno.ETIMEDOUT, "private timeout detail")):
            with self.subTest(failure=type(failure).__name__):
                calls = []

                def opener(*_args, **_kwargs):
                    calls.append(1)
                    if len(calls) == 1:
                        raise failure
                    return self.Response(sha)

                self.assertEqual(
                    COALESCING.read_current_main_sha(
                        "secret-token", opener=opener, retry_sleep=lambda _: None
                    ),
                    sha,
                )
                self.assertEqual(len(calls), 2)

    def test_http_errors_are_never_retried(self):
        for status in (401, 403, 503):
            calls = []

            def opener(*_args, **_kwargs):
                calls.append(1)
                raise urllib.error.HTTPError("https://api.github.com/private", status, "private body", {}, None)

            with self.subTest(status=status), self.assertRaises(COALESCING.CoalescingError):
                COALESCING.read_current_main_sha("secret-token", opener=opener, retry_sleep=lambda _: None)
            self.assertEqual(len(calls), 1)

    def test_certificate_verification_failure_is_never_retried(self):
        calls = []

        def opener(*_args, **_kwargs):
            calls.append(1)
            raise urllib.error.URLError(ssl.SSLCertVerificationError("private cert detail"))

        with self.assertRaises(COALESCING.CoalescingError):
            COALESCING.read_current_main_sha("secret-token", opener=opener, retry_sleep=lambda _: None)
        self.assertEqual(len(calls), 1)

    def test_malformed_json_and_invalid_sha_are_never_retried(self):
        class BadResponse(self.Response):
            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

        for response in (BadResponse(b"{"), BadResponse(b'{"object":{"sha":"bad"}}')):
            calls = []

            def opener(*_args, **_kwargs):
                calls.append(1)
                return response

            with self.subTest(response=response.body), self.assertRaises(COALESCING.CoalescingError):
                COALESCING.read_current_main_sha("secret-token", opener=opener, retry_sleep=lambda _: None)
            self.assertEqual(len(calls), 1)

    def test_unknown_errors_and_missing_token_are_never_retried(self):
        calls = []

        def opener(*_args, **_kwargs):
            calls.append(1)
            raise RuntimeError("private unknown detail")

        with self.assertRaises(RuntimeError):
            COALESCING.read_current_main_sha("secret-token", opener=opener, retry_sleep=lambda _: None)
        with self.assertRaises(COALESCING.CoalescingError):
            COALESCING.read_current_main_sha("", opener=opener, retry_sleep=lambda _: None)
        self.assertEqual(len(calls), 1)

    def test_retry_diagnostic_uses_fixed_category_and_hides_private_details(self):
        sha = "a" * 40
        events = [TimeoutError("secret-token private host"), self.Response(sha)]

        def opener(*_args, **_kwargs):
            event = events.pop(0)
            if isinstance(event, Exception):
                raise event
            return event

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = COALESCING.read_current_main_sha(
                "secret-token", opener=opener, retry_sleep=lambda _: None
            )
        self.assertEqual(result, sha)
        self.assertIn("category=TIMEOUT", stderr.getvalue())
        self.assertNotIn("secret-token", stderr.getvalue())
        self.assertNotIn("private host", stderr.getvalue())

    def test_shared_budget_recovers_after_multiple_timeouts_over_old_preflight_limit(self):
        sha = "e" * 40
        now = [0.0]
        calls = []

        def opener(*_args, **_kwargs):
            calls.append(1)
            if len(calls) <= 3:
                now[0] += 6.0
                raise TimeoutError("private timeout detail")
            return self.Response(sha)

        budget = RetryBudget(seconds=60, clock=lambda: now[0])
        with contextlib.redirect_stderr(io.StringIO()):
            result = COALESCING.read_current_main_sha(
                "redacted-token", opener=opener, budget=budget,
                retry_sleep=lambda delay: now.__setitem__(0, now[0] + delay),
            )
        self.assertEqual(result, sha)
        self.assertEqual(len(calls), 4)
        self.assertGreaterEqual(now[0], 18.0)

    def test_shared_budget_never_retries_auth_certificate_or_invalid_responses(self):
        class BadResponse(self.Response):
            def read(self):
                return b"not-json"

        failures = (
            urllib.error.HTTPError("https://api.github.com", 401, "private body", {}, None),
            urllib.error.HTTPError("https://api.github.com", 403, "private body", {}, None),
            urllib.error.URLError(ssl.SSLCertVerificationError("private certificate")),
        )
        for failure in failures:
            calls = []

            def opener(*_args, **_kwargs):
                calls.append(1)
                raise failure

            with self.subTest(failure=type(failure).__name__), \
                    self.assertRaises(COALESCING.CoalescingError), \
                    contextlib.redirect_stderr(io.StringIO()):
                COALESCING.read_current_main_sha(
                    "redacted-token", opener=opener,
                    budget=RetryBudget(seconds=60), retry_sleep=lambda _: None,
                )
            self.assertEqual(len(calls), 1)

        calls = []
        with self.assertRaises(COALESCING.CoalescingError), contextlib.redirect_stderr(io.StringIO()):
            COALESCING.read_current_main_sha(
                "redacted-token", opener=lambda *_a, **_k: (calls.append(1) or BadResponse("a" * 40)),
                budget=RetryBudget(seconds=60), retry_sleep=lambda _: None,
            )
        self.assertEqual(len(calls), 1)

    def test_shared_retry_budget_exhaustion_fails_closed(self):
        now = [0.0]
        calls = []

        def opener(*_args, **_kwargs):
            calls.append(1)
            now[0] += 6.0
            raise TimeoutError("private timeout detail")

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(COALESCING.CoalescingError):
            COALESCING.read_current_main_sha(
                "redacted-token", opener=opener,
                budget=RetryBudget(seconds=10, clock=lambda: now[0]),
                retry_sleep=lambda _: None,
            )
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(now[0], 10.0)

    def test_retry_budget_state_is_shared_between_preflight_and_coalescing_reads(self):
        sha = "f" * 40
        now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "run-attempt.state"
            budget = RetryBudget(seconds=60, clock=lambda: now[0], state_path=state)
            calls = []

            def opener(*_args, **_kwargs):
                calls.append(1)
                if len(calls) == 1:
                    now[0] += 9.0
                    raise TimeoutError("private timeout")
                return self.Response(sha)

            with contextlib.redirect_stderr(io.StringIO()):
                COALESCING.read_current_main_sha(
                    "redacted-token", opener=opener, budget=budget,
                    retry_sleep=lambda delay: now.__setitem__(0, now[0] + delay),
                )
            subsequent_budget = RetryBudget(seconds=60, clock=lambda: now[0], state_path=state)
            self.assertEqual(subsequent_budget.spent, float(state.read_text().strip()))
            self.assertGreaterEqual(subsequent_budget.spent, 9.0)

    def test_preflight_loads_the_run_scoped_shared_budget_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "run-attempt.state"
            state.write_text("17.000000\n")
            observed = []

            def reader(token, *, budget):
                self.assertEqual(token, "redacted-token")
                observed.append(budget.spent)
                return "a" * 40

            with mock.patch.dict(os.environ, {
                "GITHUB_TOKEN": "redacted-token",
                "GITHUB_REPOSITORY": "akhripunov/gpoint",
                "NETWORK_RETRY_BUDGET_STATE": str(state),
            }), mock.patch.object(PREFLIGHT, "read_current_main_sha", side_effect=reader), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(PREFLIGHT.main(), 0)
            self.assertEqual(observed, [17.0])

    def test_main_orchestrates_through_adapter_reader_and_preserves_outputs(self):
        sha = "c" * 40
        reads = []

        def reader(token, *, budget=None):
            reads.append(token)
            return sha

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            "CANDIDATE_SHA": sha,
            "GITHUB_TOKEN": "redacted-token",
            "GITHUB_OUTPUT": str(Path(directory) / "output"),
            "GITHUB_STEP_SUMMARY": str(Path(directory) / "summary"),
        }), mock.patch.object(COALESCING.core, "load_project_config", side_effect=[{}, {"repository": "akhripunov/gpoint", "project": "GPoint"}]), \
             mock.patch.object(COALESCING.core, "validate_standard_contract"), \
             mock.patch.object(COALESCING.core, "validate_project_config"), \
             mock.patch.object(COALESCING.core.time, "sleep"), \
             mock.patch.object(COALESCING, "read_current_main_sha", side_effect=reader):
            result = COALESCING.main()
            output = Path(os.environ["GITHUB_OUTPUT"]).read_text()
            summary = Path(os.environ["GITHUB_STEP_SUMMARY"]).read_text()
        self.assertEqual(result, 0)
        self.assertEqual(reads, ["redacted-token"] * 2)
        self.assertIn("result=WINNER", output)
        self.assertIn("Candidate SHA: " + sha, summary)

    def test_single_current_sha_waits_for_bounded_window_then_wins(self):
        sha = "a" * 40
        reads = iter([sha, sha])
        sleeps = []
        result = COALESCING.wait_for_quiet_main(
            sha, lambda: next(reads), sleep=sleeps.append, window_seconds=60
        )
        self.assertEqual(result, "WINNER")
        self.assertEqual(sleeps, [60])

    def test_rapid_pushes_coalesce_expensive_prepare_to_latest_sha(self):
        shas = [letter * 40 for letter in "abcd"]
        current = [shas[0]]
        expensive = []

        def read_current():
            return current[0]

        def advance_to_latest(_seconds):
            current[0] = shas[-1]

        outcomes = []
        for candidate in shas[:-1]:
            outcome = COALESCING.wait_for_quiet_main(
                candidate, read_current, sleep=advance_to_latest, window_seconds=60
            )
            outcomes.append((candidate, outcome))
            if outcome == "WINNER":
                expensive.append(candidate)
        final = COALESCING.wait_for_quiet_main(
            shas[-1], read_current, sleep=lambda _: None, window_seconds=60
        )
        outcomes.append((shas[-1], final))
        if final == "WINNER":
            expensive.append(shas[-1])

        self.assertEqual([outcome for _, outcome in outcomes], [
            "SUPERSEDED", "SUPERSEDED", "SUPERSEDED", "WINNER"
        ])
        self.assertEqual(expensive, [shas[-1]])

    def test_new_sha_during_debounce_supersedes_candidate(self):
        old, new = "a" * 40, "b" * 40
        current = [old]
        sleeps = []

        def advance(_seconds):
            sleeps.append(_seconds)
            current[0] = new

        self.assertEqual(
            COALESCING.wait_for_quiet_main(
                old, lambda: current[0], sleep=advance, window_seconds=60
            ),
            "SUPERSEDED",
        )
        self.assertEqual(sleeps, [60])

    def test_quiet_window_remains_bounded_and_urgent_candidate_is_not_held_longer(self):
        with self.assertRaises(COALESCING.CoalescingError):
            COALESCING.wait_for_quiet_main(
                "a" * 40, lambda: "a" * 40, sleep=lambda _: None, window_seconds=301
            )
        self.assertEqual(COALESCING.QUIET_WINDOW_SECONDS, 60)

    def test_new_sha_during_expensive_prepare_cancels_run_and_cannot_publish(self):
        workflow = (ROOT / ".github" / "workflows" / "gpoint-prepare.yml").read_text()
        concurrency = workflow[workflow.index("concurrency:"):workflow.index("jobs:")]
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'push' }}", concurrency)
        publish = workflow[workflow.index("  publish:"):]
        self.assertIn("needs.prepare.result != 'cancelled'", publish)

    def test_new_sha_after_artifact_exists_blocks_stale_controller_preflight(self):
        controller = (ROOT / ".github" / "workflows" / "gpoint-auto-preflight.yml").read_text()
        self.assertIn("github.event.workflow_run.head_sha == github.sha", controller)
        orchestrator = (ROOT / "scripts" / "release-orchestrator.mjs").read_text()
        self.assertIn("current main", orchestrator)
        self.assertIn("!== sha", orchestrator)

    def test_release_controller_checks_full_history_for_durable_authorization(self):
        controller = (ROOT / ".github" / "workflows" / "gpoint-auto-preflight.yml").read_text()
        checkout_ref = "uses: actions/checkout@"
        self.assertIn(checkout_ref, controller)
        checkout_start = controller.index(checkout_ref)
        checkout = controller[checkout_start:controller.index("      - name:", checkout_start)]
        action_version = checkout.split(checkout_ref, 1)[1].splitlines()[0]
        self.assertRegex(action_version, r"^[0-9a-f]{40}$",
                         "release controller actions must remain pinned to a full commit SHA")
        self.assertIn("fetch-depth: 0", checkout,
                      "authorization scope validation needs the production-baseline and source commit ancestry")

    def test_mixed_ui_and_backend_changes_still_run_full_prepare(self):
        workflow = (ROOT / ".github" / "workflows" / "gpoint-prepare.yml").read_text()
        triggers = workflow[workflow.index("on:"):workflow.index("permissions:")]
        self.assertIn("  push:", triggers)
        self.assertIn("branches: [main]", triggers)
        self.assertIn("paths-ignore:", triggers)
        self.assertNotIn("paths:", triggers)
        self.assertNotIn("workflow_dispatch:", triggers)
        self.assertIn("Tests, disposable database rehearsal and immutable Docker build", workflow)

    def test_worker_only_skip_policy_remains_scoped_to_existing_paths(self):
        workflow = (ROOT / ".github" / "workflows" / "gpoint-prepare.yml").read_text()
        triggers = workflow[workflow.index("on:"):workflow.index("permissions:")]
        self.assertIn("services/agata/worker/**", triggers)
        self.assertIn("services/agata/conversation-lab/**", triggers)
        self.assertIn("services/agata/tests/test_worker_*.py", triggers)
        self.assertNotIn("apps/", triggers)

    def test_stale_retry_is_rejected_before_wait_or_expensive_work(self):
        old, current = "a" * 40, "b" * 40
        sleeps, expensive = [], []
        outcome = COALESCING.wait_for_quiet_main(
            old,
            lambda: current,
            sleep=sleeps.append,
            window_seconds=60,
        )
        if outcome == "WINNER":
            expensive.append(old)
        self.assertEqual(outcome, "SUPERSEDED")
        self.assertEqual(sleeps, [])
        self.assertEqual(expensive, [])

    def test_current_main_lookup_failure_fails_closed(self):
        with self.assertRaises(COALESCING.CoalescingError):
            COALESCING.wait_for_quiet_main(
                "a" * 40,
                lambda: (_ for _ in ()).throw(OSError("offline")),
                sleep=lambda _: None,
                window_seconds=60,
            )

    def test_malformed_github_ref_payload_fails_closed(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"[]"

        with self.assertRaises(COALESCING.CoalescingError):
            COALESCING.read_current_main_sha("redacted-test-token", opener=lambda *_a, **_k: Response())

    def test_github_http_error_fails_closed_without_echoing_credentials(self):
        def reject(*_args, **_kwargs):
            raise urllib.error.HTTPError("https://api.github.com", 403, "Forbidden", {}, None)

        with self.assertRaises(COALESCING.CoalescingError) as raised:
            COALESCING.read_current_main_sha("redacted-test-token", opener=reject)
        self.assertIn("HTTP 403", str(raised.exception))
        self.assertNotIn("redacted-test-token", str(raised.exception))

    def test_invalid_sha_from_github_ref_fails_closed(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"object":{"sha":"invalid"}}'

        with self.assertRaises(COALESCING.CoalescingError):
            COALESCING.read_current_main_sha("redacted-test-token", opener=lambda *_a, **_k: Response())

    def test_all_expensive_prepare_steps_are_gated_before_execution(self):
        workflow = (ROOT / ".github" / "workflows" / "gpoint-prepare.yml").read_text()
        gate = "needs.coalesce.outputs.result == 'WINNER'"
        self.assertIn("needs: coalesce", workflow)
        coalesce_job = workflow[workflow.index("  coalesce:"):workflow.index("  prepare:")]
        self.assertIn("runs-on: [self-hosted, macOS, gpoint-production]", coalesce_job)
        self.assertIn("outputs:", coalesce_job)
        self.assertIn("id: coalesce", coalesce_job)
        prepare_job = workflow[workflow.index("  prepare:"):workflow.index("  publish:")]
        self.assertIn("runs-on: ubuntu-latest", prepare_job)
        self.assertIn("contents: read", coalesce_job)
        self.assertIn("persist-credentials: false", coalesce_job)
        self.assertLess(workflow.index("  coalesce:"), workflow.index("  prepare:"))
        prepare = workflow[workflow.index("  prepare:"):workflow.index("  publish:")]
        self.assertIn("always()", prepare)
        self.assertIn("needs.coalesce.outputs.result == 'WINNER'", prepare)
        self.assertNotIn("needs.coalesce.result == 'failure'", prepare)
        self.assertIn("needs.coalesce.result == 'success'", prepare)
        self.assertNotIn("Fail closed when main coalescing did not complete", prepare)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'push' }}", workflow)
        self.assertLess(workflow.index("id: coalesce"), workflow.index("actions/setup-node"))
        self.assertLess(workflow.index("id: coalesce"), workflow.index("prepare-release.py"))
        for expensive in (
            "actions/setup-node",
            "Pin production Docker version",
            "Install AGATA test dependencies",
            "Tests, disposable database rehearsal and immutable Docker build",
        ):
            segment = workflow[workflow.index(expensive):]
            if "if:" not in segment[:500]:
                self.fail(f"{expensive} must be gated on the coalescing winner")
            self.assertIn(gate, segment[:500], expensive)
        self.assertIn("github.event.workflow_run.head_sha == github.sha", (
            ROOT / ".github" / "workflows" / "gpoint-auto-preflight.yml"
        ).read_text())
        publish_job = workflow[workflow.index("  publish:"):]
        self.assertIn("runs-on: ubuntu-latest", publish_job)
        self.assertIn("persist-credentials: false", publish_job)
        publish_if = publish_job
        self.assertIn("always() && github.ref == 'refs/heads/main'", publish_if)
        self.assertIn("needs.coalesce.outputs.result == 'WINNER'", publish_if)
        self.assertIn("needs.prepare.result != 'cancelled'", publish_if)
        self.assertIn("needs.coalesce.result == 'success'", publish_if)
        deploy = (ROOT / ".github" / "workflows" / "gpoint-deploy.yml").read_text()
        self.assertIn("current main", deploy.lower())
        self.assertIn("select-release-run.py", deploy)
        validate_job = deploy[deploy.index("  validate:"):deploy.index("  deploy:")]
        self.assertIn("runs-on: ubuntu-latest", validate_job)
        self.assertIn("python scripts/select-release-run.py", validate_job)
        deploy_publish = deploy[deploy.index("  publish:"):]
        self.assertIn("runs-on: ubuntu-latest", deploy_publish)
        self.assertIn("persist-credentials: false", deploy_publish)

    def test_eros_pilot_has_a_separate_v5_deploy_job(self):
        workflow = (ROOT / ".github" / "workflows" / "gpoint-deploy.yml").read_text()
        self.assertIn("options: [preflight, production, task-scoped-auto, reconcile-authorization, eros-pilot, eros-acl-plan, eros-acl-apply, eros-acl-rollback, eros-acl-reopen]", workflow)
        self.assertIn("approve_eros_pilot:", workflow)
        self.assertIn("group: gpoint-production-release", workflow)
        production = workflow[workflow.index("  deploy:"):workflow.index("  deploy-eros-pilot:")]
        self.assertIn("inputs.mode != 'eros-pilot'", production)
        self.assertNotIn("eros-pilot", production[production.index("run: node scripts/automated-release.mjs"):])
        self.assertIn("  deploy-eros-pilot:", workflow)
        pilot = workflow[workflow.index("  deploy-eros-pilot:"):]
        self.assertIn("if: inputs.mode == 'eros-pilot'", pilot)
        self.assertIn("environment: gpoint-eros-pilot", pilot)
        self.assertIn("node scripts/automated-eros-pilot-release.mjs", pilot)
        self.assertNotIn("node scripts/automated-release.mjs", pilot)
        self.assertNotIn("release:gpoint --apply", pilot)
        self.assertNotIn("--force-recreate", pilot)
        executor = (ROOT / "scripts" / "automated-eros-pilot-release.mjs").read_text()
        self.assertIn("PILOT_SERVICES", executor)
        self.assertIn("createErosPilotOciPullScript", executor)
        pilot_job = workflow[workflow.index("  deploy-eros-pilot:"):workflow.index("  reconcile-authorization:")]
        job_env = pilot_job[pilot_job.index("    env:"):pilot_job.index("    steps:")]
        self.assertNotIn("EROS_PILOT_ENV_B64", job_env)
        self.assertIn("EROS_PILOT_ENV_B64:", pilot_job[pilot_job.index("id: execute"):])


if __name__ == "__main__":
    unittest.main()
