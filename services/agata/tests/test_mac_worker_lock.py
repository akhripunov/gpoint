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
        polls = []

        class FakeSupervisor:
            def __init__(self, root):
                self.config = type("Config", (), {"poll_seconds": 3.0})()
                self.paths = worker.PoolPaths(root)

            def start(self):
                pass

            def poll_once(self):
                polls.append(True)

            def close(self):
                pass

        original_run_pool = worker.run_pool

        def one_cycle(**kwargs):
            return original_run_pool(max_cycles=1, sleep_fn=lambda _delay: None)

        with patch.dict(os.environ, {
            "GPOINT_AGATA_WORKER_URL": "https://gpoint.one/api/agata-worker",
            "GPOINT_AGATA_WORKER_TOKEN": "test-only",
            "GPOINT_AGATA_TENANT_ID": "7508f36f-774a-48aa-8c6b-42f6f160415d",
        }), patch.object(sys, "argv", ["gpoint_mac_worker.py", "watch"]), patch.object(
            worker, "load_env_file"
        ), patch.object(worker, "build_pool_supervisor", return_value=FakeSupervisor(self.root)), patch.object(
            worker, "run_pool", side_effect=one_cycle
        ):
            self.assertEqual(worker.main(), 0)

        self.assertEqual(polls, [True])
        self.assertTrue((self.root / "queues/lab/pending").is_dir())

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
