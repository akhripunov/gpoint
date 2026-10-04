import hmac

from fastapi import Depends, FastAPI, Header, HTTPException

from app.config import Settings
from app.db.session import Database


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="G Sales", version="0.1.0")
    app.state.settings = settings
    app.state.db = Database(settings)

    def require_owner(authorization: str | None = Header(default=None)):
        token = settings.owner_api_token
        if (
            token is None
            or not authorization
            or not hmac.compare_digest(authorization, "Bearer " + token.get_secret_value())
        ):
            raise HTTPException(401, detail={"code": "unauthorized"})

    app.state.require_owner = require_owner

    @app.get("/health/live")
    async def live():
        return {"status": "ok", "service": "g-sales"}

    @app.get("/health/ready")
    async def ready():
        try:
            async with app.state.db.transaction() as conn:
                await app.state.db.assert_secure(conn)
        except Exception:
            raise HTTPException(503, detail={"code": "database_security_not_ready"}) from None
        if settings.production_mode or settings.outreach_enabled:
            try:
                await app.state.core.assert_secure()
            except Exception:
                code = "inference_provider_degraded" if settings.production_mode else "agata_security_not_ready"
                raise HTTPException(503, detail={"code": code}) from None
        if settings.outreach_enabled and not getattr(app.state, "telegram", None):
            raise HTTPException(503, detail={"code": "telegram_not_configured"})
        return {"status": "ready", "sends_enabled": settings.outreach_enabled}

    @app.get("/v1/status", dependencies=[Depends(require_owner)])
    async def status():
        inference_status = "disabled"
        if settings.production_mode:
            try:
                await app.state.core.assert_secure()
                inference_status = "ready"
            except Exception:
                inference_status = "degraded"
        return {
            "service": "g-sales",
            "outreach_enabled": settings.outreach_enabled,
            "auto_reply_enabled": settings.auto_reply_enabled,
            "tenant_id": settings.tenant_id,
            "inference_status": inference_status,
        }

    from app.crm import install

    install(app, require_owner)
    from app.telegram.management import install as install_management

    install_management(app)
    from app.worker_gateway import install as install_worker_gateway

    install_worker_gateway(app)
    if settings.secret_dir and settings.secret_master_key:
        from app.telegram.transport import TelegramTransport

        app.state.telegram = TelegramTransport(settings)
    return app


app = create_app()
