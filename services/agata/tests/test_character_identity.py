import asyncio

from app.responses.deepseek import DeepSeekResponseProvider
from app.responses.pipeline import ResponseContext


def _identity(name: str) -> str:
    context = ResponseContext(
        profile_version=1,
        route_tuple=("provider", "model", "prompt", "context", "policy"),
        current_message="Как тебя зовут?",
        personality={"identity_core": {"name": name, "gender": "female"}},
        character_name=name,
    )
    provider = DeepSeekResponseProvider(
        api_key="unit-test-key",
        base_url="https://anymodel.org/v1",
        timeout_seconds=1,
    )
    envelope = asyncio.run(provider.complete(context))
    return envelope.text


def test_response_identity_comes_from_the_selected_character_profile() -> None:
    first = _identity("Anna")
    second = _identity("Maria")
    assert "Anna" in first
    assert "Maria" in second
    assert first != second


def test_engine_name_is_not_hardcoded_as_a_character_identity() -> None:
    assert "Агата" not in _identity("Anna")
