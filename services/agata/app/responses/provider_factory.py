"""Configuration-driven inference provider boundary for AGATA response flows."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.messages.crypto import AeadCrypto
from app.responses.deepseek import DeepSeekResponseProvider
from app.responses.mac_worker import MacWorkerResponseProvider
from app.responses.pipeline import ResponseProvider


def build_response_provider(
    session: AsyncSession, settings: Settings | None = None
) -> ResponseProvider | None:
    configured = settings or get_settings()
    if configured.llm_provider in {"disabled", "none"}:
        return None
    if configured.llm_provider == "mac_worker":
        if not configured.codex_local_enabled:
            return None
        return MacWorkerResponseProvider(
            session,
            crypto=AeadCrypto(
                encoded_key=configured.message_aead_key.get_secret_value(),
                key_version=configured.message_aead_key_version,
            ),
            tenant_id=UUID(configured.tenant_id),
            model=configured.llm_model,
            wait_seconds=configured.worker_wait_seconds,
        )
    if not configured.llm_api_key:
        raise HTTPException(status_code=503, detail={"code": "provider_unconfigured"})
    return DeepSeekResponseProvider(
        api_key=configured.llm_api_key.get_secret_value(),
        base_url=configured.llm_api_base_url,
        timeout_seconds=configured.llm_timeout_seconds,
        provider=configured.llm_provider,
        model=configured.llm_model,
    )
