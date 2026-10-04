from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GSALES_TENANT_ID: Literal["c4d67889-877e-4d1b-a8d8-597c479d638f"] = (
    "c4d67889-877e-4d1b-a8d8-597c479d638f"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GSALES_", extra="forbid")

    tenant_id: Literal["c4d67889-877e-4d1b-a8d8-597c479d638f"] = GSALES_TENANT_ID
    database_url: SecretStr | None = None
    db_role: Literal["gsales_runtime"] = "gsales_runtime"
    owner_api_token: SecretStr | None = None
    worker_token: SecretStr | None = None
    owner_telegram_id: int | None = None
    production_mode: bool = False
    outreach_enabled: bool = False
    auto_reply_enabled: bool = False
    management_enabled: bool = False
    telegram_listener_enabled: bool = False
    agata_url: str = "http://127.0.0.1:8000"
    agata_api_token: SecretStr | None = None
    gpoint_registration_contract_enabled: bool = False
    gpoint_url: str | None = None
    gpoint_api_token: SecretStr | None = None
    management_bot_token: SecretStr | None = None
    management_bot_id: int | None = None
    management_webhook_secret: SecretStr | None = None
    secret_dir: str | None = None
    secret_master_key: SecretStr | None = None
    max_auto_replies: int = Field(default=10, ge=1, le=50)
    send_timeout_seconds: float = Field(default=10, ge=1, le=30)

    @model_validator(mode="after")
    def distinct_worker_credentials(self):
        if self.worker_token and any(
            token and token.get_secret_value() == self.worker_token.get_secret_value()
            for token in (self.agata_api_token, self.owner_api_token, self.management_bot_token)
        ):
            raise ValueError("worker_credentials_must_be_distinct")
        return self
