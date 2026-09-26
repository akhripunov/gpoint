"""Tenant-scoped projections for relationship and memory-graph retrieval.

The graph is a derived view: PostgreSQL memories remain authoritative and the
projection never creates facts on its own.  Only active, consented memory rows
provided by the caller may become nodes or edges.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class MemoryGraphNode:
    id: UUID
    kind: str
    label: str


@dataclass(frozen=True, slots=True)
class MemoryGraphEdge:
    source: UUID
    relation: str
    target: UUID


@dataclass(frozen=True, slots=True)
class MemoryGraph:
    nodes: tuple[MemoryGraphNode, ...]
    edges: tuple[MemoryGraphEdge, ...]


def build_memory_graph(rows: Iterable[object]) -> MemoryGraph:
    """Build a bounded graph from already-authorized memory records.

    ``rows`` is deliberately duck-typed so the projection can consume ORM
    objects without exposing a second persistence model.  Unknown, deleted,
    or malformed rows are ignored fail-closed.
    """

    nodes: dict[UUID, MemoryGraphNode] = {}
    edges: list[MemoryGraphEdge] = []
    for row in rows:
        memory_id = getattr(row, "id", getattr(row, "memory_id", None))
        value = getattr(row, "value_text", None)
        predicate = getattr(row, "predicate", None)
        lifecycle = getattr(row, "lifecycle_state", None)
        if not isinstance(memory_id, UUID) or not isinstance(value, str):
            continue
        if lifecycle not in (None, "active") or not value or len(value) > 2000:
            continue
        if predicate not in {
            "preferred_name",
            "likes",
            "goal",
            "event",
            "relationship",
            "emotion",
            "instruction",
            "current_activity",
        }:
            continue
        kind = {
            "likes": "preference",
            "event": "dated_event",
            "emotion": "emotional_episode",
            "instruction": "explicit_instruction",
        }.get(predicate, predicate)
        nodes[memory_id] = MemoryGraphNode(memory_id, kind, value)

    by_kind: dict[str, list[UUID]] = {}
    for node in nodes.values():
        by_kind.setdefault(node.kind, []).append(node.id)
    for source_kind, target_kind, relation in (
        ("goal", "dated_event", "related_event"),
        ("relationship", "goal", "supports_goal"),
        ("emotional_episode", "goal", "about_goal"),
    ):
        for source in by_kind.get(source_kind, ()):
            for target in by_kind.get(target_kind, ()):
                edges.append(MemoryGraphEdge(source, relation, target))
    return MemoryGraph(tuple(nodes.values()), tuple(edges))
