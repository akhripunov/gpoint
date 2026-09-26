"""Canonical resolver and lifecycle boundary for AGATA relationships."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_tenant_context
from app.db.models import Character, Relationship, RoleplayScenario, Scope
from app.domain.principal import PrincipalContext


class RelationshipUnavailable(ValueError):
    """The requested owner context is invalid or not active for interaction."""


async def resolve_relationship(
    session: AsyncSession,
    principal: PrincipalContext,
    character_id: UUID,
    scope: Scope,
    scenario_id: UUID | None,
) -> Relationship:
    """Get or atomically create exactly one relationship for the principal tuple."""
    if (
        not isinstance(principal, PrincipalContext)
        or not isinstance(character_id, UUID)
        or not isinstance(scope, Scope)
        or (scenario_id is not None and not isinstance(scenario_id, UUID))
        or (scope is Scope.real_world and scenario_id is not None)
        or (scope is Scope.roleplay and scenario_id is None)
    ):
        raise RelationshipUnavailable("relationship_context_invalid")

    transaction = nullcontext() if session.in_transaction() else session.begin()
    async with transaction:
        await set_tenant_context(session, str(principal.tenant_id))
        character_exists = await session.scalar(
            select(Character.id).where(
                Character.tenant_id == principal.tenant_id,
                Character.id == character_id,
            )
        )
        if character_exists is None:
            raise RelationshipUnavailable("relationship_character_unavailable")
        if scope is Scope.roleplay:
            scenario_exists = await session.scalar(
                select(RoleplayScenario.id).where(
                    RoleplayScenario.tenant_id == principal.tenant_id,
                    RoleplayScenario.id == scenario_id,
                    RoleplayScenario.user_id == principal.user_id,
                    RoleplayScenario.character_id == character_id,
                    RoleplayScenario.ended_at.is_(None),
                )
            )
            if scenario_exists is None:
                raise RelationshipUnavailable("relationship_scenario_unavailable")

        now = datetime.now(UTC)
        statement = (
            insert(Relationship)
            .values(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                character_id=character_id,
                scope=scope,
                scenario_id=scenario_id,
                status="ACTIVE",
                started_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_relationship_context")
            .returning(Relationship.id)
        )
        await session.scalar(statement)
        relationship = await session.scalar(
            select(Relationship).where(
                Relationship.tenant_id == principal.tenant_id,
                Relationship.user_id == principal.user_id,
                Relationship.character_id == character_id,
                Relationship.scope == scope,
                Relationship.scenario_id == scenario_id,
            )
        )
        if relationship is None:
            raise RelationshipUnavailable("relationship_resolution_failed")
        if relationship.status != "ACTIVE":
            raise RelationshipUnavailable("relationship_not_active")
        return relationship


async def record_accepted_interaction(
    session: AsyncSession,
    principal: PrincipalContext,
    relationship_id: UUID,
    occurred_at: datetime,
) -> None:
    """Advance only the actual accepted-interaction marker, never general updated_at."""
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise RelationshipUnavailable("relationship_interaction_time_invalid")
    transaction = nullcontext() if session.in_transaction() else session.begin()
    async with transaction:
        await set_tenant_context(session, str(principal.tenant_id))
        result = await session.execute(
            update(Relationship)
            .where(
                Relationship.tenant_id == principal.tenant_id,
                Relationship.user_id == principal.user_id,
                Relationship.id == relationship_id,
                Relationship.status == "ACTIVE",
            )
            .values(
                started_at=func.least(Relationship.started_at, occurred_at),
                last_interaction_at=func.greatest(Relationship.last_interaction_at, occurred_at)
            )
        )
        if result.rowcount != 1:
            raise RelationshipUnavailable("relationship_not_active")
