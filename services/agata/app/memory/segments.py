"""Deterministic medium-term conversation segment hierarchy helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta


def segment_interval(timestamps: Iterable[datetime]) -> tuple[datetime, datetime]:
    """Return the chronological bounds; a single event is a one-microsecond interval."""
    values = tuple(timestamps)
    if not values or any(
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        for value in values
    ):
        raise ValueError("segment_time_invalid")
    ordered = sorted(values, key=lambda value: value.astimezone(UTC))
    started_at, ended_at = ordered[0], ordered[-1]
    if ended_at <= started_at:
        ended_at = started_at + timedelta(microseconds=1)
    return started_at, ended_at


def segment_hierarchy(summary: str, started_at: datetime, ended_at: datetime) -> dict[str, object]:
    """Represent one summary at segment, topic/day and relationship/project levels."""
    if not isinstance(summary, str) or not 1 <= len(summary) <= 1200:
        raise ValueError("summary_invalid")
    if not all(
        isinstance(value, datetime) and value.tzinfo is not None for value in (started_at, ended_at)
    ):
        raise ValueError("segment_time_invalid")
    words = [word.casefold() for word in re.findall(r"[\w-]{4,}", summary, re.UNICODE)]
    keyphrases = list(dict.fromkeys(words))[:12]
    day = ended_at.astimezone(UTC).date().isoformat()
    return {
        "segment": {
            "summary": summary,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
        },
        "topic_day": {"day": day, "topics": keyphrases[:8]},
        "relationship_project": {
            "relationship": "user_character",
            "project_topics": keyphrases[:4],
        },
    }


def summary_relevance(summary: str, query: str, ended_at: datetime, *, now: datetime) -> float:
    """Bounded freshness + token overlap score for deterministic summary retrieval."""
    if (
        not isinstance(summary, str)
        or not isinstance(query, str)
        or not isinstance(ended_at, datetime)
    ):
        return 0.0
    query_words = set(re.findall(r"[\w-]{4,}", query.casefold(), re.UNICODE))
    summary_words = set(re.findall(r"[\w-]{4,}", summary.casefold(), re.UNICODE))
    overlap = len(query_words & summary_words) / max(1, len(query_words))
    age_days = max(0.0, (now - ended_at).total_seconds() / 86400.0)
    freshness = 1.0 / (1.0 + age_days)
    return 0.65 * overlap + 0.35 * freshness
