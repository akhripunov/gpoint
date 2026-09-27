import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.base import get_session, set_tenant_context

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    settings = get_settings()
    if not settings.llm_api_key:
        raise HTTPException(status_code=503, detail="response provider is not configured")

    async def check() -> None:
        await session.scalar(text("SELECT 1"))
        schema = await session.scalar(
            text("SELECT 1 FROM information_schema.schemata WHERE schema_name='agata'")
        )
        revision = await session.scalar(text("SELECT version_num FROM agata.alembic_version"))
        vector = await session.scalar(text("SELECT 1 FROM pg_extension WHERE extname='vector'"))
        if schema != 1 or revision != "agata0006" or vector != 1:
            raise RuntimeError("AGATA schema, protected-content revision, or pgvector unavailable")

    try:
        await asyncio.wait_for(check(), timeout=1.5)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database not ready") from exc
    return {"status": "ready"}


@router.get("/health/metrics")
async def metrics(session: AsyncSession = Depends(get_session)) -> dict[str, int | str]:
    """Aggregate-only metrics, explicitly tenant-scoped and content-free."""
    try:
        tenant_id = get_settings().tenant_id
        async with session.begin():
            await set_tenant_context(session, tenant_id)
            messages = await session.scalar(text("SELECT count(*) FROM agata.messages"))
            memories = await session.scalar(
                text("SELECT count(*) FROM agata.memories WHERE lifecycle_state='active'")
            )
            failures = await session.scalar(
                text(
                    "SELECT count(*) FROM agata.response_runs "
                    "WHERE status <> 'completed' AND created_at > now()-interval '10 minutes'"
                )
            )
        return {
            "status": "ok",
            "messages": int(messages or 0),
            "active_memories": int(memories or 0),
            "recent_failed_responses": int(failures or 0),
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
