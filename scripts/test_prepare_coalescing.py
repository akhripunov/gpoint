import importlib.util
from pathlib import Path
import urllib.error
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepare_coalescing", ROOT / "scripts" / "prepare_coalescing.py"
)
COALESCING = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COALESCING)


class PrepareCoalescingTests(unittest.TestCase):
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
        checkout = controller.split("uses: actions/checkout@v4", 1)[1].split("      - name:", 1)[0]
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


if __name__ == "__main__":
    unittest.main()
