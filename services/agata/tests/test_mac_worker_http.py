"""Focused resilience and HTTP-contract regressions for the standalone Mac worker."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
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

    def test_async_resolver_uses_bounded_dig_for_macos_synthetic_gateway_hosts(self):
        calls = []

        class DigProcess:
            returncode = 0

            async def communicate(self):
                return b"240.0.0.17\n", b""

        async def create_process(*args, **kwargs):
            calls.append((args, kwargs))
            return DigProcess()

        numeric_results = [(worker.socket.AF_INET, worker.socket.SOCK_STREAM, 6, "", ("240.0.0.17", 443))]
        with patch.object(worker.asyncio, "create_subprocess_exec", side_effect=create_process), \
             patch.object(worker.socket, "getaddrinfo", return_value=numeric_results) as system_resolver:
            result = worker._run_http(worker._bounded_getaddrinfo(
                b"gpoint.amiora.one", 443, family=worker.socket.AF_INET,
                type=worker.socket.SOCK_STREAM,
            ))

        self.assertEqual(result, numeric_results)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][0], "/usr/bin/dig")
        self.assertIn("gpoint.amiora.one", calls[0][0])
        self.assertEqual(calls[0][0][-1], "A")
        system_resolver.assert_called_once()
        self.assertEqual(system_resolver.call_args.args[0], "240.0.0.17")
        self.assertTrue(system_resolver.call_args.args[5] & worker.socket.AI_NUMERICHOST)

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

    def test_queue_status_uses_read_only_get_and_never_claims(self):
        observed = []

        def handler(request):
            observed.append(request)
            return httpx.Response(200, json={
                "source_id": "agata-worker-queue-status-v1:gpoint",
                "lane": "GPOINT", "pending_jobs": 0, "running_jobs": 0,
                "snapshot_at": "2026-10-07T10:00:00+00:00",
            })

        with patch.dict(os.environ, {
            "GPOINT_AGATA_WORKER_URL": "https://gpoint.amiora.one/api/agata-worker",
            "GPOINT_AGATA_WORKER_TOKEN": "test-secret",
        }), patch.object(worker.httpx, "AsyncClient", side_effect=lambda **_kwargs: self.client_for(handler)):
            result = worker.api_request("queue-status")

        self.assertEqual(result["pending_jobs"], 0)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].method, "GET")
        self.assertEqual(observed[0].content, b"")
        self.assertNotIn("claim", observed[0].url.path)

    def test_status_includes_sanitized_per_lane_queue_counts(self):
        status_file = PoolPaths(Path(self.temp.name)).worker_status_file
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text(json.dumps({"targets": {}}))
        pool_status = PoolPaths(Path(self.temp.name)).status_file
        pool_status.write_text(json.dumps({
            "pool": {"state": "HEALTHY", "busy": 0, "total": 1, "idle": 1},
            "updated_at": "2026-10-07T10:00:00+00:00",
            "lanes": {"GPOINT": {"state": "HEALTHY"}, "GSALES": {"state": "HEALTHY"}},
        }))
        queue = {
            "GPOINT": {"source_id": "agata-worker-queue-status-v1:gpoint", "lane": "GPOINT", "pending_jobs": 2,
                       "running_jobs": 0, "snapshot_at": "2026-10-07T10:00:00+00:00",
                       "private_payload": "must-not-appear"},
            "GSALES": {"source_id": "agata-worker-queue-status-v1:gsales", "lane": "GSALES", "pending_jobs": 0,
                       "running_jobs": 0, "snapshot_at": "2026-10-07T10:00:01+00:00"},
        }
        def request(target, path):
            if path == "health":
                return {"status": "ready"}
            if path == "queue-status":
                return queue[target]
            self.fail(f"unexpected request {path}")
        output = io.StringIO()
        with patch.object(worker, "load_worker_configs"), patch.object(worker.shutil, "which", return_value=None), \
             patch.object(worker, "configured_targets", return_value={"GPOINT": ("url", "token"), "GSALES": ("url", "token")}), \
             patch.object(worker, "target_request", side_effect=request), patch("sys.stdout", output):
            self.assertEqual(worker.status(), 0)

        rendered = output.getvalue()
        value = json.loads(rendered)
        self.assertEqual(value["lanes"]["GPOINT"]["pending_inference_jobs"], 2)
        self.assertEqual(value["lanes"]["GSALES"]["pending_inference_jobs"], 0)
        self.assertEqual(value["lanes"]["GPOINT"]["queue_snapshot_state"], "VERIFIED")
        self.assertEqual(value["lanes"]["GPOINT"]["health"], "HEALTHY")
        self.assertEqual(value["pool"]["busy"], 0)
        self.assertNotIn("must-not-appear", rendered)
        self.assertNotIn("private_payload", value["lanes"]["GPOINT"])

    def test_status_preserves_only_safe_queue_endpoint_404_for_version_skew_bootstrap(self):
        output = io.StringIO()

        def request(_target, path):
            if path == "health":
                return {"status": "ready"}
            raise worker.GatewayResponseError("worker_gateway_http_404")

        with patch.object(worker, "load_worker_configs"), \
             patch.object(worker.shutil, "which", return_value=None), \
             patch.object(worker, "configured_targets", return_value={"GPOINT": ("url", "token")}), \
             patch.object(worker, "target_request", side_effect=request), \
             patch("sys.stdout", output):
            self.assertEqual(worker.status(), 0)

        value = json.loads(output.getvalue())
        lane = value["lanes"]["GPOINT"]
        self.assertEqual(lane["health"], "HEALTHY")
        self.assertEqual(lane["queue_snapshot_state"], "UNAVAILABLE")
        self.assertEqual(lane["queue_snapshot_error_code"], "worker_gateway_http_404")
        self.assertNotIn("token", value)

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

    def test_reg_h_dns_failure_is_bounded_and_does_not_use_system_resolver(self):
        calls = []

        class EmptyDigProcess:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def create_process(*args, **kwargs):
            calls.append(args)
            return EmptyDigProcess()

        with patch.object(worker.asyncio, "create_subprocess_exec", side_effect=create_process), \
             patch.object(worker.socket, "getaddrinfo") as system_resolver:
            with self.assertRaises(worker.socket.gaierror):
                worker._run_http(worker._bounded_getaddrinfo(
                    "unresolved.invalid", 443, family=worker.socket.AF_INET,
                    type=worker.socket.SOCK_STREAM,
                ))

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "/usr/bin/dig")
        system_resolver.assert_not_called()

    def test_reg_i_dns_timeout_kills_dig_process(self):
        class SlowDigProcess:
            returncode = None
            killed = False

            async def communicate(self):
                await worker.asyncio.sleep(1)
                return b"", b""

            def kill(self):
                self.killed = True

            async def wait(self):
                self.returncode = -9
                return self.returncode

        process = SlowDigProcess()

        async def create_process(*_args, **_kwargs):
            return process

        with patch.object(worker.asyncio, "create_subprocess_exec", side_effect=create_process), \
             patch.object(worker, "DNS_LOOKUP_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(worker.socket.gaierror):
                worker._run_http(worker._bounded_getaddrinfo(
                    "slow.invalid", 443, family=worker.socket.AF_INET,
                    type=worker.socket.SOCK_STREAM,
                ))

        self.assertTrue(process.killed)
        self.assertEqual(process.returncode, -9)


if __name__ == "__main__":
    unittest.main()
