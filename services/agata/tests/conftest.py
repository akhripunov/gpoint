"""Keep async PostgreSQL connection pools bound to their pytest event loop."""

import pytest_asyncio

from app.db.base import dispose_engine


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def dispose_async_database_pool_between_modules():
    await dispose_engine()
    yield
    await dispose_engine()
