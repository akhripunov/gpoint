from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings


@lru_cache(maxsize=1)
def _engine():
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        connect_args={"server_settings": {"search_path": "agata,public"}},
    )


@lru_cache(maxsize=1)
def _session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(_engine(), expire_on_commit=False, class_=AsyncSession)


class _LazySessionFactory:
    def __call__(self) -> AsyncSession:
        return _session_factory()()


SessionFactory = _LazySessionFactory()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session


async def dispose_engine() -> None:
    if _engine.cache_info().currsize:
        await _engine().dispose()
    _session_factory.cache_clear()
    _engine.cache_clear()


async def set_tenant_context(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(
        text("SELECT set_config(:setting, :tenant, true)"),
        {"setting": "app.tenant_id", "tenant": str(tenant_id)},
    )
