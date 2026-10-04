"""Finite worker RPC gateway; never exposes Core credentials or accepts a tenant."""

import hmac
from uuid import UUID

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.db.session import GateError


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID
    lease_token: str = Field(min_length=32, max_length=64)
    text: str = Field(min_length=1, max_length=4000)
    model: str = Field(min_length=1, max_length=128)


class Failure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID
    lease_token: str = Field(min_length=32, max_length=64)
    error_code: str = Field(pattern=r"^[a-z0-9_]{1,64}$")


def install(app):
    settings = app.state.settings

    def authorize(authorization: str | None = Header(default=None)):
        if not settings.worker_token or not authorization or not hmac.compare_digest(
            authorization, "Bearer " + settings.worker_token.get_secret_value()
        ):
            raise HTTPException(401, detail={"code": "unauthorized"})

    async def rpc(action, payload=None):
        try:
            await app.state.core.assert_secure()
            kwargs = {"json": payload.model_dump(mode="json")} if payload else {}
            result = await app.state.core.request("POST", "/internal/v1/worker/" + action, **kwargs)
            if action == "claim" and result.get("job") is not None:
                job = result["job"]
                if not isinstance(job, dict) or job.get("target") != "GSALES" or job.get("tenant_id") != settings.tenant_id:
                    raise GateError("worker_target_identity_invalid")
            return result
        except GateError as exc:
            suffix = str(exc).removeprefix("agata_contract_rejected_")
            code = int(suffix) if suffix.isdigit() and 400 <= int(suffix) < 500 else 503
            raise HTTPException(code, detail={"code": "worker_core_unavailable"}) from None
        except Exception:
            raise HTTPException(503, detail={"code": "worker_core_unavailable"}) from None

    @app.post("/agata-worker/health", dependencies=[Depends(authorize)])
    async def health():
        try:
            await app.state.core.assert_secure()
        except Exception:
            raise HTTPException(503, detail={"code": "worker_core_unavailable"}) from None
        return {"status": "ready", "target": "GSALES", "tenant_id": settings.tenant_id}

    @app.post("/agata-worker/claim", dependencies=[Depends(authorize)])
    async def claim():
        return await rpc("claim")

    @app.post("/agata-worker/complete", dependencies=[Depends(authorize)])
    async def complete(payload: Completion):
        return await rpc("complete", payload)

    @app.post("/agata-worker/fail", dependencies=[Depends(authorize)])
    async def fail(payload: Failure):
        return await rpc("fail", payload)
