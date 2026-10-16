import importlib.util
import io
import os
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

RUNNER = Path(__file__).resolve().with_name("run_postgres.py")
SPEC = importlib.util.spec_from_file_location("gsales_prepare_runner", RUNNER)
RUNNER_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(RUNNER_MODULE)


class PrepareTimingTests(unittest.TestCase):
    def test_autonomous_timeline_accounts_for_its_own_start(self):
        ticks = iter([10.0, 12.0, 15.0, 16.0, 20.0, 21.0])
        timeline = RUNNER_MODULE.PhaseTimeline(now=lambda: next(ticks))

        with timeline.phase("database_setup"):
            pass
        with timeline.phase("postgres_tests"):
            pass
        summary = timeline.finish()

        self.assertEqual(summary["timing_scope"], "runner_main_entry_to_accounting_snapshot")
        self.assertEqual(summary["total_seconds"], 11.0)
        self.assertEqual(summary["measured_phase_seconds"], 7.0)
        self.assertEqual(summary["unmeasured_gap_seconds"], 4.0)
        self.assertEqual(summary["accounting_residual_seconds"], 0.0)

    def test_timeline_accounts_for_phase_gaps_and_total_runtime(self):
        ticks = iter([12.0, 15.0, 20.0, 24.0, 24.0])
        timeline = RUNNER_MODULE.PhaseTimeline(
            started_at=10.0,
            now=lambda: next(ticks),
        )

        with timeline.phase("database_setup"):
            pass
        with timeline.phase("postgres_tests"):
            pass
        summary = timeline.finish()

        self.assertEqual(summary["total_seconds"], 14.0)
        self.assertEqual(summary["measured_phase_seconds"], 7.0)
        self.assertEqual(summary["unmeasured_gap_seconds"], 7.0)
        self.assertEqual(summary["timing_scope"], "parent_start_to_accounting_snapshot")
        self.assertEqual(summary["accounting_residual_seconds"], 0.0)
        self.assertEqual(
            summary["measured_phase_seconds"] + summary["unmeasured_gap_seconds"],
            summary["total_seconds"],
        )
        emitted_intervals = sum(phase["seconds"] for phase in summary["phases"]) + sum(
            gap["seconds"] for gap in summary["gaps"]
        )
        self.assertAlmostEqual(emitted_intervals, summary["total_seconds"], delta=0.01)
        self.assertEqual(
            summary["gaps"],
            [
                {"before": "database_setup", "seconds": 2.0},
                {"before": "postgres_tests", "seconds": 5.0},
                {"after": "postgres_tests", "seconds": 0.0},
            ],
        )

    def test_failed_phase_is_counted_as_failed_and_timeline_still_closes(self):
        ticks = iter([2.0, 5.0, 7.0])
        timeline = RUNNER_MODULE.PhaseTimeline(started_at=1.0, now=lambda: next(ticks))

        with self.assertRaisesRegex(RuntimeError, "expected test failure"):
            with timeline.phase("postgres_tests"):
                raise RuntimeError("expected test failure")

        summary = timeline.finish()
        self.assertEqual(summary["phases"], [
            {"name": "postgres_tests", "status": "FAIL", "seconds": 3.0}
        ])
        self.assertEqual(summary["total_seconds"], 6.0)
        self.assertEqual(summary["measured_phase_seconds"], 3.0)
        self.assertEqual(summary["unmeasured_gap_seconds"], 3.0)
        self.assertEqual(summary["accounting_residual_seconds"], 0.0)

    def test_timeline_rejects_nested_or_unbalanced_phases(self):
        ticks = iter([1.0, 2.0, 3.0, 4.0])
        timeline = RUNNER_MODULE.PhaseTimeline(started_at=0.0, now=lambda: next(ticks))
        timeline.start_phase("outer")
        with self.assertRaisesRegex(ValueError, "transition"):
            timeline.start_phase("inner")
        timeline.end_phase()
        timeline.finish()
        with self.assertRaisesRegex(ValueError, "transition"):
            timeline.start_phase("after_finish")

    def test_migration_command_is_reported_as_a_named_timed_phase(self):
        ticks = iter([1.0, 3.0, 4.0])
        timeline = RUNNER_MODULE.PhaseTimeline(started_at=0.0, now=lambda: next(ticks))

        result = RUNNER_MODULE.run_timed_command(
            timeline,
            "agata_head_migration",
            ["python", "-m", "alembic", "upgrade", "head"],
            run=lambda *_args, **_kwargs: type("Result", (), {"returncode": 0})(),
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            timeline.finish()["phases"],
            [{"name": "agata_head_migration", "status": "PASS", "seconds": 2.0}],
        )

    def test_container_cleanup_records_nonzero_docker_exit_as_failure(self):
        ticks = iter([1.0, 2.0, 3.0, 4.0])
        timeline = RUNNER_MODULE.PhaseTimeline(started_at=0.0, now=lambda: next(ticks))

        RUNNER_MODULE.cleanup_test_container(
            timeline,
            "disposable-test-container",
            run=lambda *_args, **_kwargs: type("Result", (), {"returncode": 1})(),
        )

        self.assertEqual(
            timeline.finish()["phases"],
            [{"name": "container_cleanup", "status": "FAIL", "seconds": 1.0}],
        )

    def test_parent_start_timestamp_rejects_future_and_non_finite_values(self):
        with self.assertRaisesRegex(RuntimeError, "invalid_parent_start_time"):
            RUNNER_MODULE.parse_parent_start("5.2", now=lambda: 5.0)
        with self.assertRaisesRegex(RuntimeError, "invalid_parent_start_time"):
            RUNNER_MODULE.parse_parent_start("nan", now=lambda: 5.0)

    def test_main_emits_autonomous_accounting_on_failure_without_echoing_exception(self):
        def fail(_timeline):
            raise RuntimeError("private details must not be logged")

        original = RUNNER_MODULE._run
        RUNNER_MODULE._run = fail
        output = io.StringIO()
        try:
            with patch.dict(os.environ):
                os.environ.pop("GPOINT_PREPARE_GSALES_STARTED_MONOTONIC", None)
                with redirect_stdout(output), self.assertRaisesRegex(RuntimeError, "private details"):
                    RUNNER_MODULE.main()
        finally:
            RUNNER_MODULE._run = original

        self.assertIn("GS_POSTGRES_ACCOUNTING=", output.getvalue())
        self.assertIn("GS_POSTGRES_TIMING_SCOPE=runner_main_entry_to_accounting_snapshot", output.getvalue())
        self.assertNotIn("private details must not be logged", output.getvalue())

    def test_main_emits_parent_scoped_accounting_on_failure_without_echoing_exception(self):
        def fail(_timeline):
            raise RuntimeError("private parent-scoped details must not be logged")

        original = RUNNER_MODULE._run
        RUNNER_MODULE._run = fail
        output = io.StringIO()
        try:
            parent_start = str(time.monotonic() - 1.0)
            with patch.dict(os.environ, {"GPOINT_PREPARE_GSALES_STARTED_MONOTONIC": parent_start}):
                with redirect_stdout(output), self.assertRaisesRegex(RuntimeError, "private parent-scoped"):
                    RUNNER_MODULE.main()
        finally:
            RUNNER_MODULE._run = original

        self.assertIn("GS_POSTGRES_ACCOUNTING=", output.getvalue())
        self.assertIn("GS_POSTGRES_TIMING_SCOPE=parent_start_to_accounting_snapshot", output.getvalue())
        self.assertNotIn("private parent-scoped details must not be logged", output.getvalue())


if __name__ == "__main__":
    unittest.main()
