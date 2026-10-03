"""Queries over the existing message store; no event-driven counter state."""
from uuid import UUID
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Character, Conversation, Message, Scope


def incoming_messages(tenant_id: UUID, user_id: UUID, external_user_id: str):
    return select(Message).join(Conversation, and_(
        Conversation.tenant_id == Message.tenant_id,
        Conversation.id == Message.conversation_id,
        Conversation.user_id == Message.user_id,
        Conversation.character_id == Message.character_id,
    )).join(Character, and_(
        Character.tenant_id == Message.tenant_id, Character.id == Message.character_id,
    )).where(
        Message.tenant_id == tenant_id, Message.user_id == user_id,
        Message.role == 'assistant', Message.deleted_at.is_(None),
        Message.scope == Scope.real_world, Message.scenario_id.is_(None),
        Conversation.source_system == 'gpoint', Conversation.scope == Scope.real_world,
        Conversation.scenario_id.is_(None), Character.source_system == 'gpoint',
        Character.status == 'active',
        Conversation.external_id == func.concat('gpoint-chat:', external_user_id, ':', Character.external_id),
    )


async def unread_counts(session: AsyncSession, tenant_id: UUID, user_id: UUID, external_user_id: str, characters: list[str] | None = None) -> dict[str, int]:
    query = incoming_messages(tenant_id, user_id, external_user_id).with_only_columns(
        Character.external_id, func.count(Message.id),
    ).where(Message.incoming_read_at.is_(None))
    if characters is not None:
        if not characters:
            return {}
        query = query.where(Character.external_id.in_(characters))
    rows = await session.execute(query.group_by(Character.external_id))
    return {character: count for character, count in rows}
