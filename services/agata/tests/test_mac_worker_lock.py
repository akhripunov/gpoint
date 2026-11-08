"""Lock ownership regressions for the standalone AGATA worker lifecycle."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
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

    def test_watch_main_acquires_one_lock_and_enters_gpoint_polling(self):
        polls = []
        original_run = worker.run_single_worker

        def one_cycle(**kwargs):
            return original_run(max_cycles=1, sleep_fn=lambda _delay: None, **kwargs)

        with patch.dict(os.environ, {
            "GPOINT_EROS_WORKER_URL": "https://gpoint.one/api/gpoint-worker",
            "GPOINT_EROS_WORKER_TOKEN": "test-only",
            "GPOINT_EROS_TENANT_ID": "7508f36f-774a-48aa-8c6b-42f6f160415d",
        }), patch.object(sys, "argv", ["gpoint_mac_worker.py", "watch"]), patch.object(
            worker, "load_worker_configs"
        ), patch.object(worker, "validate_startup_configuration", return_value={"state": "PASS"}), patch.object(
            worker, "process_one", side_effect=lambda target: polls.append(target) or False
        ), patch.object(worker, "run_single_worker", side_effect=one_cycle), patch.dict(
            os.environ, {"GPOINT_EROS_WORKER_SUPPORT": str(self.root)}, clear=False
        ):
            self.assertEqual(worker.main(), 0)

        self.assertEqual(polls, ["GPOINT"])
        self.assertTrue((self.root / "locks/worker.lock").is_file())
        self.assertFalse((self.root / "queues").exists())

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

    def test_orphan_codex_generation_keeps_the_single_worker_lock_until_exit(self):
        fake_codex = self.root / "codex"
        pid_file = self.root / "codex.pid"
        fake_codex.write_text(
            "#!/usr/bin/env python3\n"
            "import os,sys,time\n"
            f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n"
        )
        fake_codex.chmod(0o700)
        child_code = (
            "import pathlib,sys; sys.path.insert(0,sys.argv[1]); "
            "import gpoint_mac_worker as w; "
            "w.SUPPORT=pathlib.Path(sys.argv[2]); "
            "w.INFERENCE_PROCESS=w.SUPPORT/'state/inference-process.json'; "
            "lock=w.acquire_worker_lock(); "
            "import os; os.environ['GPOINT_CODEX_CLI']=sys.argv[3]; "
            "w.run_inference({'request':{'user_message':'fixture'}})"
        )
        parent = subprocess.Popen(
            [sys.executable, "-c", child_code, str(Path(worker.__file__).resolve().parent),
             str(self.root), str(fake_codex), str(pid_file)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        codex_pid = None
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and not pid_file.exists():
                time.sleep(0.02)
            self.assertTrue(pid_file.exists(), "fake Codex child did not start")
            codex_pid = int(pid_file.read_text())
            parent.kill()
            parent.wait(timeout=3)
            with self.assertRaisesRegex(RuntimeError, "worker_already_running"):
                worker.acquire_worker_lock()

            os.kill(codex_pid, 15)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    lock = worker.acquire_worker_lock()
                    lock.close()
                    worker._WORKER_LOCK_FD = None
                    break
                except RuntimeError as exc:
                    if str(exc) != "worker_already_running":
                        raise
                    time.sleep(0.05)
            else:
                self.fail("orphan Codex child retained the worker lock after exit")
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=3)
            if codex_pid is not None:
                try:
                    os.kill(codex_pid, 9)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
