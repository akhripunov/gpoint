from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.internal import router as internal_router
from app.api.sales import router as sales_router
from app.db.base import dispose_engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


app = FastAPI(title="AGATA Core", version="1.0.0", lifespan=lifespan)
app.include_router(health_router)
app.include_router(internal_router)
app.include_router(sales_router)
