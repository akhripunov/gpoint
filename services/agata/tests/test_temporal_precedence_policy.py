"""Deterministic, database-free tests for P2.3 policy boundaries."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.db.models import EvidenceState, Scope
from app.memory.precedence import (
    MemoryPrecedenceService,
    PrecedenceOutcome,
    TemporalMode,
    build_slot_identity,
    effective_at,
    resolve_as_of,
)
from app.memory.retrieval import HybridRetrievalService, RetrievalRequest
from app.responses.pipeline import QueryPlanner


def test_slot_identity_keeps_singletons_and_hides_multivalue_plaintext():
    key = b"k" * 32
    assert build_slot_identity("user.preferred_name", "Alexander", hmac_key=key) == "user.preferred_name"
    coffee = build_slot_identity("user.preference", "Coffee", hmac_key=key)
    jazz = build_slot_identity("user.preference", "Jazz", hmac_key=key)
    assert coffee != jazz
    assert coffee.startswith("user.preference#v1:")
    assert "Coffee" not in coffee and "Jazz" not in jazz


def test_effective_interval_uses_inclusive_start_and_exclusive_end():
    memory = SimpleNamespace(
        valid_from=datetime(2026, 9, 1, tzinfo=UTC),
        valid_until=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert not effective_at(memory, datetime(2026, 8, 31, 23, 59, tzinfo=UTC))
    assert effective_at(memory, memory.valid_from)
    assert effective_at(memory, memory.valid_until - timedelta(microseconds=1))
    assert not effective_at(memory, memory.valid_until)
    assert effective_at(SimpleNamespace(valid_from=None, valid_until=None), datetime.now(UTC))
    assert resolve_as_of(memory, memory.valid_from) == (True, "interval_confirmed")
    assert resolve_as_of(SimpleNamespace(valid_from=None, valid_until=None), memory.valid_from) == (
        False, "temporal_boundary_unknown",
    )


def test_evidence_class_precedes_recency_and_disputed_is_never_a_candidate():
    current = SimpleNamespace(
        evidence_state=EvidenceState.explicit,
        observed_at=datetime(2026, 9, 26, tzinfo=UTC),
    )
    inferred = MemoryPrecedenceService.compare_candidate(
        current, candidate_value="Alex", current_value="Alexander",
        candidate_evidence=EvidenceState.inferred,
        candidate_claimed_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    disputed = MemoryPrecedenceService.compare_candidate(
        None, candidate_value="Alex", current_value=None,
        candidate_evidence=EvidenceState.disputed,
        candidate_claimed_at=datetime.now(UTC),
    )
    assert inferred.outcome == PrecedenceOutcome.CONFLICT
    assert inferred.reason_code == "weaker_evidence"
    assert disputed.outcome == PrecedenceOutcome.CONFLICT


def test_out_of_order_and_equal_authority_conflicts_are_deterministic():
    current = SimpleNamespace(
        evidence_state=EvidenceState.confirmed,
        observed_at=datetime(2026, 9, 26, tzinfo=UTC),
    )
    old = MemoryPrecedenceService.compare_candidate(
        current, candidate_value="Company A", current_value="Company B",
        candidate_evidence=EvidenceState.confirmed,
        candidate_claimed_at=datetime(2025, 9, 26, tzinfo=UTC),
        current_claimed_at=current.observed_at,
    )
    tie = MemoryPrecedenceService.compare_candidate(
        current, candidate_value="Company C", current_value="Company B",
        candidate_evidence=EvidenceState.confirmed,
        candidate_claimed_at=current.observed_at,
        current_claimed_at=current.observed_at,
    )
    assert old.reason_code == "out_of_order_evidence"
    assert tie.reason_code == "equal_authority_conflict"


def test_query_planner_defaults_current_and_never_emits_unbounded_mode():
    planner = QueryPlanner()
    now = datetime.now(UTC)
    assert planner.plan("Где я работаю?", now).temporal_mode is TemporalMode.CURRENT
    assert planner.plan("Где я работал раньше?", now).temporal_mode is TemporalMode.HISTORICAL
    invalid = planner.plan("", now)
    assert invalid.abstain and invalid.temporal_mode is TemporalMode.CURRENT


def test_hard_filter_places_truth_eligibility_in_the_sql_for_each_mode():
    request = RetrievalRequest(
        character_id=uuid4(), relationship_id=uuid4(), scope=Scope.real_world,
        scenario_id=None, query="works_at", as_of=datetime.now(UTC),
    )
    principal = SimpleNamespace(tenant_id=uuid4(), user_id=uuid4())
    service = object.__new__(HybridRetrievalService)
    current_sql = str(HybridRetrievalService._hard_filter(service, principal, request).compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    ))
    assert "lifecycle_state = 'active'" in current_sql
    assert "valid_from" in current_sql and "valid_until" in current_sql
    assert "disputed" in current_sql

    historical = replace(request, temporal_mode=TemporalMode.HISTORICAL)
    historical_sql = str(HybridRetrievalService._hard_filter(service, principal, historical).compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    ))
    assert "lifecycle_state = 'historical'" in historical_sql
    assert "valid_until" in historical_sql


@pytest.mark.parametrize("value", ["current", "historical"])
def test_temporal_modes_are_finite(value):
    assert TemporalMode(value) in {TemporalMode.CURRENT, TemporalMode.HISTORICAL}
