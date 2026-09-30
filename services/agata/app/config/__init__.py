import base64
import binascii
import re
import uuid
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGATA_", extra="ignore")

    database_url: str
    tenant_id: str
    internal_api_token: SecretStr
    message_aead_key: SecretStr
    message_aead_key_version: str = "v1"
    request_fingerprint_hmac_key: SecretStr
    retrieval_query_hmac_key: SecretStr
    shadow_candidate_hmac_key: SecretStr
    response_fingerprint_hmac_key: SecretStr
    embedding_api_key: SecretStr | None = None
    llm_provider: str = "disabled"
    llm_model: str = "codex-default"
    llm_api_key: SecretStr | None = None
    llm_api_base_url: str = "https://anymodel.org/v1"
    llm_timeout_seconds: int = Field(default=12, ge=1, le=30)
    worker_wait_seconds: float = Field(default=6.0, ge=0.5, le=10.0)
    codex_local_enabled: bool = False
    inference_job_ttl_seconds: int = Field(default=420, ge=60, le=3600)
    max_pending_inference_jobs: int = Field(default=100, ge=1, le=10000)
    runtime_db_role: str | None = None
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=100)
    worker_poll_seconds: float = Field(default=2.0, ge=0.1, le=60)
    worker_batch_size: int = Field(default=4, ge=1, le=64)

    @field_validator("tenant_id")
    @classmethod
    def valid_tenant_id(cls, value: str) -> str:
        try:
            uuid.UUID(value)
        except ValueError as exc:
            raise ValueError("AGATA_TENANT_ID must be a UUID") from exc
        return value

    @field_validator("message_aead_key")
    @classmethod
    def valid_aead_key(cls, value: SecretStr) -> SecretStr:
        try:
            decoded = base64.b64decode(value.get_secret_value(), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("AGATA_MESSAGE_AEAD_KEY must be base64") from exc
        if len(decoded) != 32:
            raise ValueError("AGATA_MESSAGE_AEAD_KEY must decode to exactly 32 bytes")
        return value

    @field_validator("message_aead_key_version")
    @classmethod
    def valid_aead_key_version(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", value):
            raise ValueError("AGATA_MESSAGE_AEAD_KEY_VERSION is invalid")
        return value

    @field_validator(
        "request_fingerprint_hmac_key",
        "retrieval_query_hmac_key",
        "shadow_candidate_hmac_key",
        "response_fingerprint_hmac_key",
    )
    @classmethod
    def valid_hmac_key(cls, value: SecretStr) -> SecretStr:
        try:
            decoded = base64.b64decode(value.get_secret_value(), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("AGATA HMAC keys must be base64") from exc
        if len(decoded) < 32:
            raise ValueError("AGATA HMAC keys must decode to at least 32 bytes")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


__all__ = ["Settings", "get_settings"]
