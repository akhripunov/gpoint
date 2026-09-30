"""Lock ownership regressions for the standalone AGATA worker lifecycle."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import gpoint_mac_worker as worker  # noqa: E402


class MacWorkerLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gpoint-worker-lock-test-")
        self.root = Path(self.temp.name)
        self.previous_paths = worker.SUPPORT, worker.STATUS
        worker.SUPPORT = self.root
        worker.STATUS = self.root / "worker-status.json"

    def tearDown(self):
        worker.SUPPORT, worker.STATUS = self.previous_paths
        self.temp.cleanup()

    def test_watch_main_acquires_one_lock_and_enters_polling(self):
        original_watch = worker.run_watch
        polls = []

        def one_cycle(poll_seconds):
            return original_watch(poll_seconds, max_cycles=1, sleep_fn=lambda _delay: None)

        with patch.dict(os.environ, {
            "GPOINT_AGATA_WORKER_URL": "https://gpoint.example/api/agata-worker",
            "GPOINT_AGATA_WORKER_TOKEN": "test-only",
        }), patch.object(sys, "argv", ["gpoint_mac_worker.py", "watch"]), patch.object(
            worker, "load_env_file"
        ), patch.object(worker, "process_one", side_effect=lambda: polls.append(True)), patch.object(
            worker, "run_watch", side_effect=one_cycle
        ), patch.object(worker, "acquire_worker_lock", wraps=worker.acquire_worker_lock) as acquire:
            self.assertEqual(worker.main(), 0)

        self.assertEqual(acquire.call_count, 1)
        self.assertEqual(polls, [True])
        self.assertTrue(worker.STATUS.is_file())

    def test_second_independent_worker_is_refused_while_lock_is_held(self):
        lock = worker.acquire_worker_lock()
        try:
            module_path = Path(worker.__file__).resolve()
            child_code = (
                "import pathlib,sys; sys.path.insert(0,sys.argv[1]); "
                "import gpoint_mac_worker as w; w.SUPPORT=pathlib.Path(sys.argv[2]); "
                "\ntry: w.acquire_worker_lock()\n"
                "except RuntimeError as exc: print(str(exc)); sys.exit(0 if str(exc)=='worker_already_running' else 2)\n"
                "sys.exit(3)\n"
            )
            result = subprocess.run(
                [sys.executable, "-c", child_code, str(module_path.parent), str(self.root)],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        finally:
            lock.close()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "worker_already_running")


if __name__ == "__main__":
    unittest.main()
