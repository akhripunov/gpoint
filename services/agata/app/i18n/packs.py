"""Canonical, versioned Locale Pack contents.

Each pack is the finite, complete, mandatory system string set for its locale.
en-US and en-GB are deliberately separate packs even where a subset of strings
is currently identical: language and locale are distinct, and collapsing them
would silently erase the difference.

Only system texts live here. There is no romantic content, no Identity Core,
no humour, no cultural stereotype and no LLM prompt. These strings are an
engineering scaffold, not a statement of cultural validation: a future native
review, cultural review and product copy approval pass is required before any
external rollout.
"""

from __future__ import annotations

from typing import Final

from app.i18n.contracts import (
    DEFAULT_LOCALE_CODE,
    SUPPORTED_LOCALE_CODES,
    LocalePack,
    LocalePackError,
    compute_content_hash,
)

_SCHEMA_VERSION: Final[int] = 1

_RU_RU_MESSAGES: Final[dict[str, str]] = {
    "ai_disclosure": (
        "Я — AI-персонаж GPTGirls, а не человек. Это важно учитывать в нашем общении."
    ),
    "ai_disclosure_staging": (
        "Я — AI-персонаж GPTGirls, а не человек. Это staging/experimental-среда для тестирования."
    ),
    "onboarding_welcome": (
        "Привет! Я {character_name}. Прежде чем начать, выполним несколько коротких шагов."
    ),
    "onboarding_age_required": (
        "Чтобы продолжить, нужно подтвердить возраст: эта функция доступна только совершеннолетним."
    ),
    "onboarding_consent_required": (
        "Для продолжения нужно согласие на обработку переписки и применение политики безопасности."
    ),
    "onboarding_locale_selected": "Язык системных сообщений выбран: русский (ru-RU).",
    "onboarding_character_required": (
        "Сначала нужно выбрать персонажа, прежде чем продолжать общение."
    ),
    "onboarding_ready": (
        "Привет! Рада познакомиться и буду внимательно слушать. "
        "О чём тебе хотелось бы поговорить сегодня?"
    ),
    "proactive_check_in": (
        "Привет! Ненавязчиво заглядываю узнать, как ты. "
        "Если сейчас не до разговора, отвечать не нужно."
    ),
    "help_overview": (
        "Доступные темы: помощь, приватность, память, экспорт данных и удаление. "
        "Выберите нужную, и я расскажу подробнее."
    ),
    "privacy_overview": (
        "Вы управляете своими данными: их можно посмотреть, экспортировать, исправить "
        "или удалить. Удалённое не возвращается в ответы."
    ),
    "memory_overview": (
        "Важные факты сохраняются с указанием владельца, области и срока хранения. "
        "Их можно скрыть или забыть."
    ),
    "unsupported_action": (
        "Это действие пока не поддерживается. Попробуйте другую доступную команду."
    ),
    "request_rejected": ("Запрос отклонён политикой безопасности. Подробнее — в разделе помощи."),
    "temporarily_unavailable": ("Сервис временно недоступен. Пожалуйста, попробуйте чуть позже."),
    "generic_retry": "Что-то пошло не так. Пожалуйста, повторите попытку.",
    "deletion_requested": ("Запрос на удаление принят. Удалённые данные не возвращаются в ответы."),
    "export_requested": (
        "Запрос на экспорт данных принят. Готовый экспорт появится в личном кабинете."
    ),
}

_EN_US_MESSAGES: Final[dict[str, str]] = {
    "ai_disclosure": (
        "I am a GPTGirls AI character, not a human. Please keep that in mind as we talk."
    ),
    "ai_disclosure_staging": (
        "I am a GPTGirls AI character, not a human. This is a staging/experimental environment for testing."
    ),
    "onboarding_welcome": (
        "Hi! I'm {character_name}. Before we begin, let's complete a few short steps."
    ),
    "onboarding_age_required": (
        "To continue, you need to confirm your age: this feature is available to adults only."
    ),
    "onboarding_consent_required": (
        "To continue, you need to consent to processing this conversation and to the safety policy."
    ),
    "onboarding_locale_selected": "System message language selected: English (en-US).",
    "onboarding_character_required": (
        "You need to choose a character before continuing the conversation."
    ),
    "onboarding_ready": (
        "Hi! I'm glad to meet you and I'm here to listen. What would you like to talk about today?"
    ),
    "proactive_check_in": (
        "Hi! I'm checking in gently to see how you are. "
        "If now is not a good time to talk, there is no need to reply."
    ),
    "help_overview": (
        "Available topics: help, privacy, memory, data export and deletion. Pick one and "
        "I'll explain it in more detail."
    ),
    "privacy_overview": (
        "You control your data: you can view, export, correct or delete it. Deleted data "
        "does not return in responses."
    ),
    "memory_overview": (
        "Important facts are stored with an owner, scope and retention period. They can be "
        "hidden or forgotten."
    ),
    "unsupported_action": ("This action is not supported yet. Try another available command."),
    "request_rejected": (
        "The request was rejected by the safety policy. See the help section for details."
    ),
    "temporarily_unavailable": (
        "The service is temporarily unavailable. Please try again shortly."
    ),
    "generic_retry": "Something went wrong. Please try again.",
    "deletion_requested": ("Deletion request accepted. Deleted data does not return in responses."),
    "export_requested": (
        "Data export request accepted. The finished export will appear in your account."
    ),
}

_EN_GB_MESSAGES: Final[dict[str, str]] = {
    "ai_disclosure": (
        "I am a GPTGirls AI character, not a human. Please keep that in mind as we talk."
    ),
    "ai_disclosure_staging": (
        "I am a GPTGirls AI character, not a human. This is a staging/experimental environment for testing."
    ),
    "onboarding_welcome": (
        "Hello! I'm {character_name}. Before we begin, let's complete a few short steps."
    ),
    "onboarding_age_required": (
        "To continue, you need to confirm your age: this feature is available to adults only."
    ),
    "onboarding_consent_required": (
        "To continue, you need to consent to processing this conversation and to the safety policy."
    ),
    "onboarding_locale_selected": "System message language selected: English (en-GB).",
    "onboarding_character_required": (
        "You need to choose a character before continuing the conversation."
    ),
    "onboarding_ready": (
        "Hello! I'm glad to meet you and I'm here to listen. "
        "What would you like to talk about today?"
    ),
    "proactive_check_in": (
        "Hello! I'm checking in gently to see how you are. "
        "If now is not a good time to talk, there is no need to reply."
    ),
    "help_overview": (
        "Available topics: help, privacy, memory, data export and deletion. Pick one and "
        "I'll explain it in more detail."
    ),
    "privacy_overview": (
        "You control your data: you can view, export, correct or delete it. Deleted data "
        "does not return in responses."
    ),
    "memory_overview": (
        "Important facts are stored with an owner, scope and retention period. They can be "
        "hidden or forgotten."
    ),
    "unsupported_action": ("This action is not supported yet. Try another available command."),
    "request_rejected": (
        "The request was rejected by the safety policy. See the help section for details."
    ),
    "temporarily_unavailable": (
        "The service is temporarily unavailable. Please try again shortly."
    ),
    "generic_retry": "Something went wrong. Please try again.",
    "deletion_requested": ("Deletion request accepted. Deleted data does not return in responses."),
    "export_requested": (
        "Data export request accepted. The finished export will appear in your account."
    ),
}

_PACK_CONTENT: Final[dict[str, dict[str, str]]] = {
    "ru-RU": _RU_RU_MESSAGES,
    "en-US": _EN_US_MESSAGES,
    "en-GB": _EN_GB_MESSAGES,
}

_PACK_VERSIONS: Final[dict[str, int]] = {"ru-RU": 1, "en-US": 1, "en-GB": 1}

_PACK_STATUSES: Final[dict[str, str]] = {
    "ru-RU": "enabled",
    "en-US": "enabled",
    "en-GB": "enabled",
}


def build_pack(locale_code: str) -> LocalePack:
    """Construct a validated Locale Pack from the canonical content constants.

    The content hash is computed from all validated fields via the single
    canonical ``compute_content_hash`` function.  ``LocalePack.__post_init__``
    independently recomputes and compares it, so a forged or stale hash never
    reaches the runtime.
    """
    if locale_code not in SUPPORTED_LOCALE_CODES:
        raise LocalePackError("invalid_locale_code")
    messages = _PACK_CONTENT[locale_code]
    pack_version = _PACK_VERSIONS[locale_code]
    status = _PACK_STATUSES[locale_code]
    content_hash = compute_content_hash(
        schema_version=_SCHEMA_VERSION,
        locale_code=locale_code,
        pack_version=pack_version,
        status=status,
        messages=messages,
    )
    return LocalePack(
        schema_version=_SCHEMA_VERSION,
        locale_code=locale_code,
        pack_version=pack_version,
        status=status,
        messages=dict(messages),
        content_hash=content_hash,
    )


def default_locale_code() -> str:
    return DEFAULT_LOCALE_CODE
