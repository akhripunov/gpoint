"""Worker readiness must track the current AGATA schema head."""

import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app.memory import worker


class ReadOnlySession:
    def __init__(self, revision: str):
        self.values = [revision, 1]

    @asynccontextmanager
    async def begin(self):
        yield

    async def scalar(self, _statement):
        return self.values.pop(0)


class WorkerHealthcheckTests(unittest.IsolatedAsyncioTestCase):
    async def assert_healthcheck(self, revision: str, expected: int) -> None:
        original_path = worker._HEARTBEAT_PATH
        original_settings = worker.get_settings
        original_factory = worker.SessionFactory
        original_tenant_context = worker.set_tenant_context
        try:
            with TemporaryDirectory() as directory:
                heartbeat = Path(directory) / "heartbeat"
                heartbeat.touch()
                session = ReadOnlySession(revision)

                @asynccontextmanager
                async def session_context():
                    yield session

                async def set_tenant_context(_session, _tenant_id):
                    return None

                worker._HEARTBEAT_PATH = heartbeat
                worker.get_settings = lambda: SimpleNamespace(
                    worker_poll_seconds=15, tenant_id="tenant"
                )
                worker.SessionFactory = session_context
                worker.set_tenant_context = set_tenant_context

                self.assertEqual(await worker._healthcheck(), expected)
        finally:
            worker._HEARTBEAT_PATH = original_path
            worker.get_settings = original_settings
            worker.SessionFactory = original_factory
            worker.set_tenant_context = original_tenant_context

    async def test_current_agata_head_is_healthy(self):
        await self.assert_healthcheck("agata0011", 0)

    async def test_previous_agata_head_is_not_healthy(self):
        await self.assert_healthcheck("agata0008", 1)
