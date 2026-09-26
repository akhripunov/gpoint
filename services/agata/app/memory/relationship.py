"""Privacy-bounded relationship and emotional projections.

These projections never invent a diagnosis or relationship claim.  They only
surface active memories whose evidence is explicit (or an already-authorized
inferred record) and discard deleted/historical rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RelationshipState:
    facts: tuple[str, ...]
    emotional_episodes: tuple[str, ...]


def relationship_state_from_memories(rows: Iterable[object]) -> RelationshipState:
    facts: list[str] = []
    emotions: list[str] = []
    for row in rows:
        predicate = getattr(row, "predicate", None)
        value = getattr(row, "value_text", None)
        memory_id = getattr(row, "id", None)
        lifecycle = getattr(row, "lifecycle_state", None)
        deleted_at = getattr(row, "deleted_at", None)
        evidence = getattr(row, "evidence_state", None)
        if (
            not isinstance(memory_id, UUID)
            or not isinstance(value, str)
            or lifecycle not in (None, "active")
            or deleted_at is not None
            or evidence not in (None, "explicit", "inferred")
            or not value
        ):
            continue
        if predicate == "relationship":
            facts.append(value[:500])
        elif predicate == "emotion":
            emotions.append(value[:500])
    return RelationshipState(tuple(facts[:20]), tuple(emotions[:20]))
