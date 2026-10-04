import pytest

from app.api.sales import DraftRequest
from app.api.sales import sales_inference_ready
from app.config import Settings


def test_draft_contract_rejects_external_tenant_and_mutable_ownership():
    with pytest.raises(ValueError):
        DraftRequest(request_id="x", character_external_id="seller", tenant_id="foreign")


def test_product_source_is_finite_and_defaults_to_gpoint():
    assert Settings.model_fields["source_system"].default == "gpoint"
    assert Settings.model_fields["sales_drafts_enabled"].default is False
    assert (
        DraftRequest(request_id="x", character_external_id="seller", text="approved draft").text
        == "approved draft"
    )


@pytest.mark.parametrize('provider',['local_fake','disabled','none'])
def test_sales_production_rejects_unapproved_test_providers(provider):
    import base64
    key=base64.b64encode(b'a'*32).decode()
    with pytest.raises(ValueError,match='approved inference provider'):
        Settings(database_url='postgresql+asyncpg://test',tenant_id='c4d67889-877e-4d1b-a8d8-597c479d638f',
            internal_api_token='test',message_aead_key=key,request_fingerprint_hmac_key=key,
            retrieval_query_hmac_key=key,shadow_candidate_hmac_key=key,response_fingerprint_hmac_key=key,
            source_system='gsales',sales_production_mode=True,llm_provider=provider)


def test_sales_production_accepts_owner_approved_shared_mac_worker():
    import base64
    key=base64.b64encode(b'a'*32).decode()
    settings = Settings(database_url='postgresql+asyncpg://test',tenant_id='c4d67889-877e-4d1b-a8d8-597c479d638f',
        internal_api_token='test',message_aead_key=key,request_fingerprint_hmac_key=key,
        retrieval_query_hmac_key=key,shadow_candidate_hmac_key=key,response_fingerprint_hmac_key=key,
        source_system='gsales',sales_production_mode=True,llm_provider='mac_worker',codex_local_enabled=True)
    assert settings.llm_provider == 'mac_worker'


def test_sales_production_rejects_mac_worker_without_shared_worker_enabled():
    import base64
    key=base64.b64encode(b'a'*32).decode()
    with pytest.raises(ValueError,match='worker'):
        Settings(database_url='postgresql+asyncpg://test',tenant_id='c4d67889-877e-4d1b-a8d8-597c479d638f',
            internal_api_token='test',message_aead_key=key,request_fingerprint_hmac_key=key,
            retrieval_query_hmac_key=key,shadow_candidate_hmac_key=key,response_fingerprint_hmac_key=key,
            source_system='gsales',sales_production_mode=True,llm_provider='mac_worker',codex_local_enabled=False)


def test_sales_mac_readiness_requires_fresh_authenticated_worker_poll():
    from types import SimpleNamespace

    configured = SimpleNamespace(
        source_system="gsales",
        sales_production_mode=True,
        llm_provider="mac_worker",
        codex_local_enabled=True,
        codex_worker_poll_ttl_seconds=30,
    )
    assert not sales_inference_ready(configured, None, now=100)
    assert sales_inference_ready(configured, 80, now=100)
    assert not sales_inference_ready(configured, 69, now=100)
    assert not sales_inference_ready(configured, 101, now=100)
