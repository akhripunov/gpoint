"""Focused resilience and HTTP-contract regressions for the standalone Mac worker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import gpoint_mac_worker as worker  # noqa: E402
from agata_worker_pool import PoolConfig, PoolPaths, PoolSupervisor  # noqa: E402

TEST_GPOINT_TENANT_ID = "7508f36f-774a-48aa-8c6b-42f6f160415d"


class MacWorkerHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gpoint-worker-http-test-")
        self.async_client_type = httpx.AsyncClient
        self.old_paths = worker.SUPPORT, worker.STATUS
        self.old_heartbeat = worker._last_heartbeat_attempt
        worker._last_heartbeat_attempt = float("-inf")
        worker.SUPPORT = Path(self.temp.name)
        worker.STATUS = worker.SUPPORT / "status.json"

    def tearDown(self):
        worker.SUPPORT, worker.STATUS = self.old_paths
        worker._last_heartbeat_attempt = self.old_heartbeat
        self.temp.cleanup()

    def client_for(self, handler):
        return self.async_client_type(transport=httpx.MockTransport(handler))

    def test_reg_a_json_body_and_bodyless_health_contract(self):
        observed = []

        def handler(request):
            observed.append(request)
            return httpx.Response(200, json={"status": "ready"})

        with patch.dict(os.environ, {
            "GPOINT_AGATA_WORKER_URL": "https://gpoint.amiora.one/api/agata-worker",
            "GPOINT_AGATA_WORKER_TOKEN": "test-secret",
        }), patch.object(worker.httpx, "AsyncClient", side_effect=lambda **_kwargs: self.client_for(handler)):
            self.assertEqual(worker.api_request("health"), {"status": "ready"})
            worker.api_request("complete", {"text": "reply"})

        self.assertEqual(observed[0].method, "POST")
        self.assertEqual(observed[0].content, b"")
        self.assertNotIn("content-type", observed[0].headers)
        self.assertEqual(json.loads(observed[1].content), {"text": "reply"})
        self.assertEqual(observed[1].headers["content-type"], "application/json")
        self.assertEqual(observed[0].headers["authorization"], "Bearer test-secret")

    def test_reg_b_worker_survives_connect_timeout_and_recovers(self):
        calls = []

        def claim():
            calls.append(True)
            if len(calls) == 1:
                raise worker.TransientNetworkError("worker_gateway_network_error")
            return None

        pool = PoolSupervisor(PoolPaths(Path(self.temp.name)), PoolConfig(), {"GPOINT": claim}, clock=lambda: 0)
        try:
            pool.start()
            self.assertEqual(pool.poll_once(now=0), 0)
            self.assertEqual(pool.poll_once(now=1), 0)
            self.assertEqual(pool.poll_once(now=2), 0)
            self.assertEqual(len(calls), 2)
            status = json.loads(pool.paths.status_file.read_text())
            self.assertEqual(status["lanes"]["GPOINT"]["state"], "HEALTHY")
        finally:
            pool.close()

    def test_reg_c_network_failure_during_complete_does_not_fail_or_duplicate_job(self):
        paths = []

        def api(path, body=None):
            paths.append((path, body))
            if path == "health":
                return {"status": "ready"}
            if path == "claim":
                return {"job": {"id": "job-1", "leaseToken": "lease-1", "request": {}}}
            if path == "complete":
                raise worker.TransientNetworkError("worker_gateway_network_error")
            self.fail(f"unexpected {path} request")

        with patch.dict(os.environ, {"GPOINT_AGATA_WORKER_TOKEN": "test-secret", "GPOINT_AGATA_TENANT_ID": TEST_GPOINT_TENANT_ID}), patch.object(
            worker, "api_request", side_effect=api
        ), patch.object(worker, "run_inference", return_value="reply"):
            with self.assertRaises(worker.TransientNetworkError):
                worker.process_one()
        self.assertEqual([path for path, _ in paths], ["health", "claim", "complete"])
        self.assertNotIn("fail", [path for path, _ in paths])
        self.assertIn('"current_job_present":true', worker.STATUS.read_text())

    def test_reg_d_recovery_resets_backoff(self):
        calls = iter((
            worker.TransientNetworkError("network"),
            worker.TransientNetworkError("network"),
            None,
            worker.TransientNetworkError("network"),
            None,
        ))
        def claim():
            value = next(calls)
            if value:
                raise value
            return None

        pool = PoolSupervisor(PoolPaths(Path(self.temp.name)), PoolConfig(), {"GPOINT": claim}, clock=lambda: 0)
        try:
            pool.start()
            for now in (0, 2, 6, 7, 9):
                pool.poll_once(now=now)
            status = json.loads(pool.paths.status_file.read_text())
            self.assertEqual(status["lanes"]["GPOINT"]["state"], "HEALTHY")
        finally:
            pool.close()

    def test_reg_e_completed_job_has_one_completion_and_no_failure_call(self):
        calls = []

        def api(path, body=None):
            calls.append(path)
            if path == "claim":
                return {"job": {"id": "job-2", "leaseToken": "lease-2", "request": {}}}
            return {"completed": True}

        with patch.dict(os.environ, {"GPOINT_AGATA_WORKER_TOKEN": "test-secret", "GPOINT_AGATA_TENANT_ID": TEST_GPOINT_TENANT_ID}), patch.object(
            worker, "api_request", side_effect=api
        ), patch.object(worker, "run_inference", return_value="reply"):
            self.assertTrue(worker.process_one())
        self.assertEqual(calls.count("complete"), 1)
        self.assertNotIn("fail", calls)

    def test_reg_f_fatal_configuration_error_stops_watch(self):
        with self.assertRaises(ValueError):
            PoolConfig.from_env({"AGATA_WORKER_CONCURRENCY": "2"})

    def test_reg_g_network_error_does_not_expose_credentials(self):
        def handler(_request):
            raise httpx.ConnectTimeout("test-secret")

        with patch.dict(os.environ, {
            "GPOINT_AGATA_WORKER_URL": "https://gpoint.amiora.one/api/agata-worker",
            "GPOINT_AGATA_WORKER_TOKEN": "test-secret",
        }), patch.object(worker.httpx, "AsyncClient", side_effect=lambda **_kwargs: self.client_for(handler)):
            with self.assertRaises(worker.TransientNetworkError) as raised:
                worker.api_request("health")
        self.assertNotIn("test-secret", str(raised.exception))

    def test_reg_h_dns_resolution_timeout_is_bounded_and_single_threaded(self):
        worker_directory = str(Path(worker.__file__).resolve().parent)
        child = textwrap.dedent("""
            import asyncio
            import os
            import socket
            import threading
            import gpoint_mac_worker as worker

            stalled = threading.Event()
            calls = []
            def blocked_getaddrinfo(*args, **kwargs):
                calls.append(args[0] if args else None)
                stalled.wait()
                return []

            socket.getaddrinfo = blocked_getaddrinfo
            worker.configured_targets = lambda: {
                "GPOINT": ("https://resolver-stall.invalid/api", "fixture-token")
            }
            original_timeout = asyncio.timeout
            worker.asyncio.timeout = lambda _seconds: original_timeout(0.05)
            for _ in range(3):
                try:
                    worker.api_request("health")
                except worker.TransientNetworkError as exc:
                    if str(exc) != "worker_gateway_network_error":
                        print("unexpected_network_error", str(exc), flush=True)
                        os._exit(3)
                else:
                    print("unexpected_request_success", flush=True)
                    os._exit(4)
            if len(calls) != 1:
                print("resolver_calls", len(calls), flush=True)
                os._exit(5)
            print("bounded_timeouts=3 resolver_calls=1", flush=True)
            os._exit(0)
        """)
        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = os.pathsep.join(filter(None, (
            worker_directory, child_env.get("PYTHONPATH", ""),
        )))
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            child_env.pop(name, None)
        try:
            completed = subprocess.run(
                [sys.executable, "-c", child], cwd=Path(__file__).resolve().parents[3],
                env=child_env, check=False, text=True, capture_output=True, timeout=3,
            )
        except subprocess.TimeoutExpired:
            self.fail("timed-out getaddrinfo kept api_request blocked during asyncio.run shutdown")
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertIn("bounded_timeouts=3 resolver_calls=1", completed.stdout)


if __name__ == "__main__":
    unittest.main()
