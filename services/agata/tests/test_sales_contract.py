import pytest

from app.api.sales import DraftRequest
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


@pytest.mark.parametrize('provider',['mac_worker','local_fake','disabled'])
def test_sales_production_rejects_laptop_or_test_provider(provider):
    import base64
    key=base64.b64encode(b'a'*32).decode()
    with pytest.raises(ValueError,match='always-on'):
        Settings(database_url='postgresql+asyncpg://test',tenant_id='c4d67889-877e-4d1b-a8d8-597c479d638f',
            internal_api_token='test',message_aead_key=key,request_fingerprint_hmac_key=key,
            retrieval_query_hmac_key=key,shadow_candidate_hmac_key=key,response_fingerprint_hmac_key=key,
            source_system='gsales',sales_production_mode=True,llm_provider=provider)
