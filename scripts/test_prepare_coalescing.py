import importlib.util
from pathlib import Path
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

    def test_all_expensive_prepare_steps_are_gated_before_execution(self):
        workflow = (ROOT / ".github" / "workflows" / "gpoint-prepare.yml").read_text()
        gate = "steps.coalesce.outputs.result == 'WINNER'"
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
        publish_if = workflow[workflow.index("needs: prepare"):workflow.index("runs-on: ubuntu-latest", workflow.index("needs: prepare"))]
        self.assertIn("always() && github.ref == 'refs/heads/main'", publish_if)
        self.assertIn("needs.prepare.outputs.coalescing != 'SUPERSEDED'", publish_if)
        self.assertIn("needs.prepare.result != 'cancelled'", publish_if)
        deploy = (ROOT / ".github" / "workflows" / "gpoint-deploy.yml").read_text()
        self.assertIn("current main", deploy.lower())
        self.assertIn("select-release-run.py", deploy)


if __name__ == "__main__":
    unittest.main()
