"""Provider-disabled deployments stay healthy without queuing fake work."""

from types import SimpleNamespace

from app.responses.provider_factory import build_response_provider


def test_disabled_provider_needs_no_llm_key_or_database_session():
    configured = SimpleNamespace(llm_provider="disabled", llm_api_key=None)

    assert build_response_provider(None, configured) is None
