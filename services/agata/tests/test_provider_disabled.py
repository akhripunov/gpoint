"""Provider-disabled deployments stay healthy without queuing fake work."""

from base64 import b64encode
from types import SimpleNamespace
from uuid import uuid4

from pydantic import SecretStr

from app.config import Settings
from app.responses.provider_factory import build_response_provider


def test_disabled_provider_needs_no_llm_key_or_database_session():
    configured = SimpleNamespace(
        llm_provider="disabled", llm_api_key=None, codex_local_enabled=True
    )

    assert build_response_provider(None, configured) is None


def test_provider_and_codex_local_default_to_disabled():
    key = SecretStr(b64encode(b"k" * 32).decode())
    configured = Settings(
        database_url="postgresql+asyncpg://test:test@localhost/test",
        tenant_id=str(uuid4()),
        internal_api_token=SecretStr("service-token"),
        message_aead_key=key,
        request_fingerprint_hmac_key=key,
        retrieval_query_hmac_key=key,
        shadow_candidate_hmac_key=key,
        response_fingerprint_hmac_key=key,
    )

    assert configured.llm_provider == "disabled"
    assert configured.codex_local_enabled is False
    assert configured.naturalness_policy_enabled is False
    assert configured.inference_job_ttl_seconds == 420
    assert configured.max_pending_inference_jobs == 100


def test_mac_worker_is_disabled_unless_explicitly_enabled():
    configured = SimpleNamespace(llm_provider="mac_worker", codex_local_enabled=False)

    assert build_response_provider(None, configured) is None


def test_mac_worker_uses_existing_provider_only_when_explicitly_enabled():
    configured = SimpleNamespace(
        llm_provider="mac_worker",
        codex_local_enabled=True,
        message_aead_key=SecretStr(b64encode(b"k" * 32).decode()),
        message_aead_key_version="v1",
        tenant_id=str(uuid4()),
        llm_model="codex-default",
        worker_wait_seconds=6.0,
        naturalness_policy_enabled=True,
    )

    provider = build_response_provider(None, configured)

    assert provider is not None
    assert provider.provider == "mac_worker"
    assert provider.naturalness_policy_enabled is True


def test_anymodel_provider_receives_the_opt_in_naturalness_toggle():
    key = SecretStr(b64encode(b"k" * 32).decode())
    configured = Settings(
        database_url="postgresql+asyncpg://test:test@localhost/test",
        tenant_id=str(uuid4()),
        internal_api_token=SecretStr("service-token"),
        message_aead_key=key,
        request_fingerprint_hmac_key=key,
        retrieval_query_hmac_key=key,
        shadow_candidate_hmac_key=key,
        response_fingerprint_hmac_key=key,
        llm_provider="anymodel",
        llm_model="cx/gpt-5.6-terra",
        llm_api_key=SecretStr("unit-test-key"),
        naturalness_policy_enabled=True,
    )

    provider = build_response_provider(None, configured)

    assert provider is not None
    assert provider.provider == "anymodel"
    assert provider.naturalness_policy_enabled is True
