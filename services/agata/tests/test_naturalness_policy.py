import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx

from app.responses.deepseek import DeepSeekResponseProvider
from app.responses.mac_worker import _worker_request
from app.responses.naturalness import build_naturalness_instructions
from app.responses.pipeline import (
    CommunicationSettings,
    ConversationEntry,
    ResponseContext,
    UntrustedMemoryBlock,
)


def _context() -> ResponseContext:
    memory_id = uuid4()
    return ResponseContext(
        profile_version=3,
        route_tuple=("anymodel", "test-model", "prompt", "context", "policy"),
        current_message="Сегодня наконец выбрался в книжный.",
        personality={"identity_core": {"name": "Мира", "gender": "female"}, "warmth": "reserved"},
        speech_style={"register": "casual", "humor": "dry"},
        backstory={"public_bio": "Мира часто выбирает небольшие книжные магазины."},
        boundaries={"no_personal_addresses": True},
        character_name="Мира",
        relationship_note="Обсуждали книги и прогулки.",
        relationship_summary="Пользователь любит небольшие книжные магазины.",
        conversation_summaries=("Недавно выбирали книгу в дорогу.",),
        current_context_summary="Разговор о сегодняшней прогулке.",
        conversation_history=(
            ConversationEntry("user", "Давно не гулял по центру."),
            ConversationEntry("assistant", "Там иногда попадаются хорошие места."),
        ),
        memories=(
            UntrustedMemoryBlock(
                memory_id=memory_id,
                version=1,
                subject="user",
                predicate="preferred_activity",
                value="quiet bookstores",
            ),
        ),
        communication_settings=CommunicationSettings(
            language="ru", response_length="short", tone="casual", initiative="low"
        ),
    )


def test_policy_and_examples_match_the_frozen_naturalness_v1_candidate():
    rendered = build_naturalness_instructions()
    examples_path = Path(__file__).parents[1] / "app" / "responses" / "naturalness_v1_examples_source.jsonl"
    source_examples = [
        json.loads(line)
        for line in examples_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert "agata-naturalness-v1" in rendered
    assert "Keep the character's own warmth, reserve, humor, opinions, vocabulary, and boundaries" in rendered
    assert "No fixed word limit" in rendered
    assert "Do not invent a shared event, personal biography, emotion, or remembered detail" in rendered
    assert "Сегодня наконец выбрался" not in rendered
    assert "User: День вышел шумный, сейчас наконец дома." in rendered
    assert "Character: Тогда пусть дома будет тихо хотя бы минут десять." in rendered
    assert "review_only" not in rendered
    assert all(
        value not in rendered
        for example in source_examples
        for value in example["review_only"].values()
    )


def test_mac_worker_policy_toggle_changes_only_instructions_and_keeps_context_identical():
    context = _context()
    baseline = _worker_request(context)
    candidate = _worker_request(context, naturalness_policy_enabled=True)

    assert baseline["instructions"] != candidate["instructions"]
    assert "agata-naturalness-v1" not in baseline["instructions"]
    assert "agata-naturalness-v1" in candidate["instructions"]
    assert baseline["persona"]["backstory"] == context.backstory
    assert baseline["communication_settings"] == {
        "language": "ru",
        "response_length": "short",
        "tone": "casual",
        "use_name": None,
        "initiative": "low",
        "roleplay": None,
    }
    assert baseline["working_dialogue_state"] == "Разговор о сегодняшней прогулке."
    assert baseline["dialogue_summaries"] == ["Недавно выбирали книгу в дорогу."]
    assert baseline["memories"][0]["value"] == "quiet bookstores"
    assert {key: value for key, value in baseline.items() if key != "instructions"} == {
        key: value for key, value in candidate.items() if key != "instructions"
    }


def test_deepseek_policy_toggle_adds_only_one_candidate_system_message():
    payloads = []

    async def capture(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"id": "test-response", "choices": [{"message": {"content": "Неплохая прогулка."}}]},
        )

    async def run(enabled: bool) -> None:
        provider = DeepSeekResponseProvider(
            api_key="unit-test-key",
            base_url="https://anymodel.org/v1",
            timeout_seconds=1,
            transport=httpx.MockTransport(capture),
            provider="anymodel",
            model="cx/gpt-5.6-terra",
            naturalness_policy_enabled=enabled,
        )
        await provider.complete(_context())

    asyncio.run(run(False))
    asyncio.run(run(True))
    baseline, candidate = payloads
    base_messages = baseline["messages"]
    candidate_messages = candidate["messages"]
    assert baseline["model"] == candidate["model"] == "cx/gpt-5.6-terra"
    assert len(candidate_messages) == len(base_messages) + 1
    policy_messages = [m for m in candidate_messages if "agata-naturalness-v1" in m["content"]]
    assert len(policy_messages) == 1
    assert policy_messages[0]["role"] == "system"
    assert [m for m in candidate_messages if "agata-naturalness-v1" not in m["content"]] == base_messages


def test_ab_scenario_pack_reuses_sixteen_development_scenarios_without_holdout():
    manifest = Path(__file__).parents[1] / "worker" / "lab_fixture_manifest.json"
    scenarios = json.loads(manifest.read_text(encoding="utf-8"))["scenarios"]

    assert len(scenarios) == 16
    assert all(scenario_id.endswith("-02") for scenario_id in scenarios)
    assert "long_dialogue-02" in scenarios
    assert "ru_naturalness-01" not in scenarios
    assert all(
        len(scenario["user_turns"]) >= 4
        for scenario in scenarios.values()
    )
