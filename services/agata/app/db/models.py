from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(schema="agata")


def pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )


class Scope(str, enum.Enum):
    real_world = "real_world"
    roleplay = "roleplay"


class EvidenceState(str, enum.Enum):
    explicit = "explicit"
    confirmed = "confirmed"
    inferred = "inferred"
    disputed = "disputed"


class LifecycleState(str, enum.Enum):
    pending = "pending"
    active = "active"
    historical = "historical"
    deleted = "deleted"


class JobState(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    dead = "dead"


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[uuid.UUID] = pk()
    status: Mapped[str] = mapped_column(String(32), server_default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TenantOwned(Base):
    __abstract__ = True
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    id: Mapped[uuid.UUID] = pk()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class User(TenantOwned):
    __tablename__ = "users"
    source_system: Mapped[str] = mapped_column(String(32), server_default="gpoint", nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    locale: Mapped[str | None] = mapped_column(String(32))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_system", "external_id", name="uq_users_source_external"
        ),
        UniqueConstraint("tenant_id", "id", name="uq_users_tenant_id"),
    )


class Character(TenantOwned):
    __tablename__ = "characters"
    source_system: Mapped[str] = mapped_column(String(32), server_default="gpoint", nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), server_default="active", nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_system", "external_id", name="uq_characters_source_external"
        ),
        UniqueConstraint("tenant_id", "id", name="uq_characters_tenant_id"),
    )


class CharacterProfileVersion(TenantOwned):
    __tablename__ = "character_profile_versions"
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    personality: Mapped[dict] = mapped_column(JSONB, nullable=False)
    speech_style: Mapped[dict] = mapped_column(JSONB, nullable=False)
    backstory: Mapped[dict] = mapped_column(JSONB, nullable=False)
    boundaries: Mapped[dict] = mapped_column(JSONB, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A reviewed admin draft has immutable content but cannot become a response
    # persona until the explicit, optimistic-concurrency activation transition.
    admin_draft: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "character_id", "version", name="uq_profile_version"),
        ForeignKeyConstraint(
            ["tenant_id", "character_id"],
            ["characters.tenant_id", "characters.id"],
            name="fk_profile_character",
        ),
        CheckConstraint(
            "retired_at IS NULL OR retired_at > effective_at", name="ck_profile_interval"
        ),
    )


class UserCharacterSettings(TenantOwned):
    __tablename__ = "user_character_settings"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    preferences: Mapped[dict] = mapped_column(JSONB, server_default="{}", nullable=False)
    # Legacy lifecycle-shaped JSON; runtime intentionally does not read or write it.
    # Explicit communication preferences belong in `preferences`; relationship
    # identity/lifecycle is canonicalized by the relationships table.
    relationship: Mapped[dict] = mapped_column(JSONB, server_default="{}", nullable=False)
    version: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "character_id", name="uq_user_character_settings"),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "character_id"], ["characters.tenant_id", "characters.id"]
        ),
    )


class Consent(TenantOwned):
    __tablename__ = "consents"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_consents_tenant_id"),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= granted_at", name="ck_consent_interval"
        ),
        Index(
            "uq_current_consent_per_purpose",
            "tenant_id",
            "user_id",
            "purpose",
            unique=True,
            postgresql_where=revoked_at.is_(None),
        ),
    )


class RetentionPolicy(TenantOwned):
    __tablename__ = "retention_policies"
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    message_days: Mapped[int | None] = mapped_column(Integer)
    memory_days: Mapped[int | None] = mapped_column(Integer)
    legal_hold: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_retention_tenant_id"),
        CheckConstraint(
            "message_days IS NULL OR message_days >= 0", name="ck_retention_message_days"
        ),
        CheckConstraint("memory_days IS NULL OR memory_days >= 0", name="ck_retention_memory_days"),
    )


class RoleplayScenario(TenantOwned):
    __tablename__ = "roleplay_scenarios"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[dict] = mapped_column(JSONB, server_default="{}", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_roleplay_tenant_id"),
        UniqueConstraint("tenant_id", "id", "user_id", "character_id", name="uq_roleplay_owner"),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "character_id"], ["characters.tenant_id", "characters.id"]
        ),
        CheckConstraint("ended_at IS NULL OR ended_at > started_at", name="ck_scenario_interval"),
    )


class Relationship(TenantOwned):
    """Durable user-character interaction identity in one scope/scenario."""

    __tablename__ = "relationships"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[Scope] = mapped_column(
        Enum(Scope, name="scope_enum", native_enum=True, create_type=False), nullable=False
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scenario_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        Computed(
            "coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)",
            persisted=True,
        ),
    )
    status: Mapped[str] = mapped_column(String(16), server_default="ACTIVE", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_interaction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "character_id", "scope", "scenario_key",
            name="uq_relationship_context",
        ),
        UniqueConstraint(
            "tenant_id", "id", "user_id", "character_id", "scope", "scenario_key",
            name="uq_relationship_owner_scope",
        ),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "character_id"], ["characters.tenant_id", "characters.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "scenario_id", "user_id", "character_id"],
            [
                "roleplay_scenarios.tenant_id", "roleplay_scenarios.id",
                "roleplay_scenarios.user_id", "roleplay_scenarios.character_id",
            ],
            name="fk_relationship_scenario_owner",
        ),
        CheckConstraint(
            "(scope = 'real_world' AND scenario_id IS NULL) OR "
            "(scope = 'roleplay' AND scenario_id IS NOT NULL)",
            name="ck_relationship_scope",
        ),
        CheckConstraint(
            "status IN ('ACTIVE','PAUSED','ENDED')", name="ck_relationship_status"
        ),
        CheckConstraint(
            "(status = 'ENDED' AND ended_at IS NOT NULL) OR "
            "(status <> 'ENDED' AND ended_at IS NULL)",
            name="ck_relationship_ended_at",
        ),
        CheckConstraint(
            "last_interaction_at IS NULL OR last_interaction_at >= started_at",
            name="ck_relationship_interaction_interval",
        ),
    )


class Conversation(TenantOwned):
    __tablename__ = "conversations"
    source_system: Mapped[str] = mapped_column(String(32), server_default="gpoint", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255))
    relationship_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[Scope] = mapped_column(
        Enum(Scope, name="scope_enum", native_enum=True), nullable=False
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scenario_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        Computed(
            "coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)", persisted=True
        ),
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    language_code: Mapped[str] = mapped_column(String(8), server_default="unknown", nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_conversations_tenant_id"),
        UniqueConstraint(
            "tenant_id", "source_system", "external_id", name="uq_conversations_source_external"
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            "user_id",
            "character_id",
            "scope",
            "scenario_key",
            name="uq_conversation_owner_scope",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            "relationship_id",
            "user_id",
            "character_id",
            "scope",
            "scenario_key",
            name="uq_conversation_relationship_owner_scope",
        ),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "character_id"], ["characters.tenant_id", "characters.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "relationships.tenant_id", "relationships.id", "relationships.user_id",
                "relationships.character_id", "relationships.scope", "relationships.scenario_key",
            ],
            name="fk_conversation_relationship_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "scenario_id", "user_id", "character_id"],
            [
                "roleplay_scenarios.tenant_id",
                "roleplay_scenarios.id",
                "roleplay_scenarios.user_id",
                "roleplay_scenarios.character_id",
            ],
            name="fk_export_job_scenario_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "scenario_id", "user_id", "character_id"],
            [
                "roleplay_scenarios.tenant_id",
                "roleplay_scenarios.id",
                "roleplay_scenarios.user_id",
                "roleplay_scenarios.character_id",
            ],
            name="fk_conversation_scenario_owner",
        ),
        CheckConstraint(
            "(scope = 'real_world' AND scenario_id IS NULL) OR (scope = 'roleplay' AND scenario_id IS NOT NULL)",
            name="ck_conversation_scope",
        ),
        CheckConstraint(
            "closed_at IS NULL OR closed_at > started_at", name="ck_conversation_interval"
        ),
        CheckConstraint(
            "language_code IN ('ru','en','mixed','unknown')", name="ck_conversation_language"
        ),
    )


class ConversationSegment(TenantOwned):
    __tablename__ = "conversation_segments"
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str | None] = mapped_column(Text)
    # Ciphertext envelope object at rest; decrypted list exists only in process memory.
    keyphrases: Mapped[dict | list | None] = mapped_column(JSONB)
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"], ["conversations.tenant_id", "conversations.id"]
        ),
        CheckConstraint("ended_at IS NULL OR ended_at > started_at", name="ck_segment_interval"),
    )


class Message(TenantOwned):
    __tablename__ = "messages"
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[Scope] = mapped_column(
        Enum(Scope, name="scope_enum", native_enum=True, create_type=False), nullable=False
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scenario_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        Computed(
            "coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)", persisted=True
        ),
    )
    channel_delivery_status: Mapped[str] = mapped_column(String(16), server_default="confirmed", nullable=False)
    channel_message_id: Mapped[str | None] = mapped_column(String(128))
    incoming_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    content_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    content_key_version: Mapped[str | None] = mapped_column(String(32))
    system_message_type: Mapped[str | None] = mapped_column(String(32))
    system_action: Mapped[str | None] = mapped_column(String(64))
    request_fingerprint: Mapped[str | None] = mapped_column(String(128))
    consent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    retention_policy_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    language_code: Mapped[str] = mapped_column(String(8), server_default="unknown", nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_message_idempotency"),
        UniqueConstraint("tenant_id", "id", name="uq_messages_tenant_id"),
        UniqueConstraint(
            "tenant_id",
            "id",
            "user_id",
            "character_id",
            "scope",
            "scenario_key",
            name="uq_message_owner_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"], ["conversations.tenant_id", "conversations.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "conversations.tenant_id",
                "conversations.id",
                "conversations.user_id",
                "conversations.character_id",
                "conversations.scope",
                "conversations.scenario_key",
            ],
            name="fk_message_conversation_owner",
        ),
        ForeignKeyConstraint(["tenant_id", "consent_id"], ["consents.tenant_id", "consents.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "retention_policy_id"],
            ["retention_policies.tenant_id", "retention_policies.id"],
        ),
        CheckConstraint("role IN ('user','assistant','system')", name="ck_message_role"),
        CheckConstraint(
            "(content_ciphertext IS NULL AND content_nonce IS NULL AND content_key_version IS NULL) "
            "OR (content_ciphertext IS NOT NULL AND content_nonce IS NOT NULL "
            "AND content_key_version IS NOT NULL AND octet_length(content_ciphertext) > 16 "
            "AND octet_length(content_nonce) = 12 AND content_key_version IS NOT NULL)",
            name="ck_message_content_envelope",
        ),
        CheckConstraint(
            "language_code IN ('ru','en','mixed','unknown')", name="ck_message_language"
        ),
        Index("messages_conversation_time_idx", "tenant_id", "conversation_id", "occurred_at"),
        Index("messages_unread_incoming_idx", "tenant_id", "user_id", "character_id", "conversation_id",
              postgresql_where=text("role = 'assistant' AND incoming_read_at IS NULL AND deleted_at IS NULL AND scope = 'real_world' AND scenario_id IS NULL")),
        Index(
            "messages_idempotency_fingerprint_idx",
            "tenant_id",
            "idempotency_key",
            "request_fingerprint",
        ),
    )


class MemoryControlState(TenantOwned):
    __tablename__ = "memory_control_state"
    relationship_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[Scope] = mapped_column(
        Enum(Scope, name="scope_enum", native_enum=True, create_type=False), nullable=False
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scenario_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        Computed(
            "coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)", persisted=True
        ),
    )
    deletion_epoch: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_memory_control_tenant_id"),
        UniqueConstraint(
            "tenant_id", "relationship_id", name="uq_memory_control_relationship"
        ),
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "character_id",
            "scope",
            "scenario_id",
            name="uq_memory_control_scope",
            postgresql_nulls_not_distinct=True,
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            "user_id",
            "character_id",
            "scope",
            "scenario_key",
            name="uq_control_owner_scope_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            "relationship_id",
            "user_id",
            "character_id",
            "scope",
            "scenario_key",
            name="uq_control_relationship_owner_scope_key",
        ),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "character_id"], ["characters.tenant_id", "characters.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "relationships.tenant_id", "relationships.id", "relationships.user_id",
                "relationships.character_id", "relationships.scope", "relationships.scenario_key",
            ],
            name="fk_memory_control_relationship_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "scenario_id", "user_id", "character_id"],
            [
                "roleplay_scenarios.tenant_id",
                "roleplay_scenarios.id",
                "roleplay_scenarios.user_id",
                "roleplay_scenarios.character_id",
            ],
            name="fk_control_scenario_owner",
        ),
        CheckConstraint(
            "(scope = 'real_world' AND scenario_id IS NULL) OR (scope = 'roleplay' AND scenario_id IS NOT NULL)",
            name="ck_control_scope",
        ),
    )


class Memory(TenantOwned):
    __tablename__ = "memories"
    relationship_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[Scope] = mapped_column(
        Enum(Scope, name="scope_enum", native_enum=True, create_type=False), nullable=False
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scenario_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        Computed(
            "coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)", persisted=True
        ),
    )
    slot_key: Mapped[str | None] = mapped_column(String(255))
    slot_schema_version: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    subject_type: Mapped[str | None] = mapped_column(String(64))
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    predicate: Mapped[str | None] = mapped_column(String(128))
    value_text: Mapped[str | None] = mapped_column(Text)
    value_json: Mapped[dict | None] = mapped_column(JSONB)
    search_text: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_state: Mapped[EvidenceState] = mapped_column(
        Enum(EvidenceState, name="evidence_state_enum", native_enum=True), nullable=False
    )
    lifecycle_state: Mapped[LifecycleState] = mapped_column(
        Enum(LifecycleState, name="lifecycle_state_enum", native_enum=True), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    importance: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(32), server_default="normal", nullable=False)
    consent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    retention_policy_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    version: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    created_epoch: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_memories_tenant_id"),
        UniqueConstraint(
            "tenant_id", "id", "relationship_id", "user_id", "character_id", "scope", "scenario_key",
            name="uq_memory_relationship_owner_scope",
        ),
        UniqueConstraint(
            "tenant_id", "id", "version", "relationship_id", "user_id", "character_id", "scope", "scenario_key",
            name="uq_memory_exact_revision_owner_scope",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            "user_id",
            "character_id",
            "scope",
            "scenario_key",
            name="uq_memory_owner_scope",
        ),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "character_id"], ["characters.tenant_id", "characters.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "relationships.tenant_id", "relationships.id", "relationships.user_id",
                "relationships.character_id", "relationships.scope", "relationships.scenario_key",
            ],
            name="fk_memory_relationship_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "scenario_id", "user_id", "character_id"],
            [
                "roleplay_scenarios.tenant_id",
                "roleplay_scenarios.id",
                "roleplay_scenarios.user_id",
                "roleplay_scenarios.character_id",
            ],
            name="fk_memory_scenario_owner",
        ),
        ForeignKeyConstraint(["tenant_id", "consent_id"], ["consents.tenant_id", "consents.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "retention_policy_id"],
            ["retention_policies.tenant_id", "retention_policies.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "supersedes_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "memories.tenant_id",
                "memories.id",
                "memories.relationship_id",
                "memories.user_id",
                "memories.character_id",
                "memories.scope",
                "memories.scenario_key",
            ],
            name="fk_memory_supersedes_owner",
        ),
        CheckConstraint("supersedes_id IS NULL OR supersedes_id <> id", name="ck_memory_not_self_superseding"),
        CheckConstraint("version >= 1", name="ck_memory_version_positive"),
        Index(
            "memories_one_successor_idx", "tenant_id", "relationship_id", "supersedes_id",
            unique=True, postgresql_where=supersedes_id.is_not(None),
        ),
        CheckConstraint(
            "(scope = 'real_world' AND scenario_id IS NULL) OR (scope = 'roleplay' AND scenario_id IS NOT NULL)",
            name="ck_memory_scope",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_memory_confidence"),
        CheckConstraint("importance >= 0 AND importance <= 1", name="ck_memory_importance"),
        CheckConstraint(
            "valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from",
            name="ck_memory_interval",
        ),
        Index(
            "memories_owner_active_idx",
            "tenant_id",
            "user_id",
            "character_id",
            "scope",
            "lifecycle_state",
        ),
        Index(
            "memories_one_active_slot_idx",
            "tenant_id",
            "relationship_id",
            "user_id",
            "character_id",
            "scope",
            "scenario_id",
            "slot_key",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=(lifecycle_state == LifecycleState.active)
            & (kind.in_(["semantic_fact", "episodic"]))
            & slot_key.is_not(None),
        ),
        Index(
            "memories_truth_resolution_idx",
            "tenant_id", "relationship_id", "slot_key", "lifecycle_state", "valid_from", "valid_until",
            postgresql_where=(deleted_at.is_(None)) & (evidence_state != EvidenceState.disputed),
        ),
        Index(
            "memories_slot_version_idx",
            "tenant_id",
            "user_id",
            "character_id",
            "scope",
            "scenario_id",
            "slot_key",
            version.desc(),
        ),
        Index("memories_time_idx", "tenant_id", "valid_from", "valid_until"),
        Index(
            "memories_fts_idx",
            text("to_tsvector('simple', coalesce(search_text,''))"),
            postgresql_using="gin",
        ),
        Index(
            "memories_embedding_hnsw_idx",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=(lifecycle_state == LifecycleState.active),
        ),
        Index(
            "memories_retrievable_fts_idx",
            text("to_tsvector('simple', coalesce(search_text,''))"),
            postgresql_using="gin",
            postgresql_where=(lifecycle_state == LifecycleState.active) & deleted_at.is_(None),
        ),
        Index(
            "memories_retrievable_embedding_hnsw_idx",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=(lifecycle_state == LifecycleState.active)
            & deleted_at.is_(None)
            & embedding.is_not(None),
        ),
    )


class MemoryEntity(TenantOwned):
    __tablename__ = "memory_entities"
    relationship_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[Scope] = mapped_column(Enum(Scope, name="scope_enum", native_enum=True, create_type=False), nullable=False)
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scenario_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), Computed(
        "coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)", persisted=True
    ))
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_key_hmac: Mapped[str] = mapped_column(String(80), nullable=False)
    created_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", "relationship_id", "user_id", "character_id", "scope", "scenario_key", name="uq_memory_entity_owner"),
        UniqueConstraint("tenant_id", "relationship_id", "entity_type", "entity_key_hmac", name="uq_memory_entity_identity"),
        ForeignKeyConstraint(["tenant_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"], ["relationships.tenant_id", "relationships.id", "relationships.user_id", "relationships.character_id", "relationships.scope", "relationships.scenario_key"], name="fk_memory_entity_relationship_owner"),
        CheckConstraint("(scope = 'real_world' AND scenario_id IS NULL) OR (scope = 'roleplay' AND scenario_id IS NOT NULL)", name="ck_memory_entity_scope"),
        CheckConstraint("entity_type IN ('user','person','place','organization','activity','preference','goal','event','relationship','emotion','instruction','locale','timezone')", name="ck_memory_entity_type"),
        CheckConstraint("created_epoch >= 0", name="ck_memory_entity_epoch"),
        CheckConstraint("entity_key_hmac ~ '^hmac-sha256:v1:[0-9a-f]{64}$'", name="ck_memory_entity_hmac"),
        Index("memory_entities_relationship_key_idx", "tenant_id", "relationship_id", "entity_key_hmac"),
    )


class MemoryRelation(TenantOwned):
    __tablename__ = "memory_relations"
    relationship_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[Scope] = mapped_column(Enum(Scope, name="scope_enum", native_enum=True, create_type=False), nullable=False)
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scenario_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), Computed(
        "coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)", persisted=True
    ))
    source_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", "relationship_id", "user_id", "character_id", "scope", "scenario_key", name="uq_memory_relation_owner"),
        UniqueConstraint("tenant_id", "relationship_id", "source_entity_id", "relation_type", "target_entity_id", name="uq_memory_relation_topology"),
        ForeignKeyConstraint(["tenant_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"], ["relationships.tenant_id", "relationships.id", "relationships.user_id", "relationships.character_id", "relationships.scope", "relationships.scenario_key"], name="fk_memory_relation_relationship_owner"),
        ForeignKeyConstraint(["tenant_id", "source_entity_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"], ["memory_entities.tenant_id", "memory_entities.id", "memory_entities.relationship_id", "memory_entities.user_id", "memory_entities.character_id", "memory_entities.scope", "memory_entities.scenario_key"], name="fk_memory_relation_source_owner"),
        ForeignKeyConstraint(["tenant_id", "target_entity_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"], ["memory_entities.tenant_id", "memory_entities.id", "memory_entities.relationship_id", "memory_entities.user_id", "memory_entities.character_id", "memory_entities.scope", "memory_entities.scenario_key"], name="fk_memory_relation_target_owner"),
        CheckConstraint("(scope = 'real_world' AND scenario_id IS NULL) OR (scope = 'roleplay' AND scenario_id IS NOT NULL)", name="ck_memory_relation_scope"),
        CheckConstraint("source_entity_id <> target_entity_id", name="ck_memory_relation_not_self"),
        CheckConstraint("relation_type IN ('preferred_name','likes','dislikes','hobby','locale','timezone','current_activity','goal','event','relationship','emotion','instruction','works_at','lives_in')", name="ck_memory_relation_type"),
        CheckConstraint("created_epoch >= 0", name="ck_memory_relation_epoch"),
        Index("memory_relations_source_idx", "tenant_id", "relationship_id", "source_entity_id"),
        Index("memory_relations_target_idx", "tenant_id", "relationship_id", "target_entity_id"),
    )


class MemoryGraphEvidence(TenantOwned):
    __tablename__ = "memory_graph_evidence"
    relationship_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[Scope] = mapped_column(Enum(Scope, name="scope_enum", native_enum=True, create_type=False), nullable=False)
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scenario_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), Computed(
        "coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)", persisted=True
    ))
    memory_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    memory_version: Mapped[int] = mapped_column(Integer, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    relation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    evidence_role: Mapped[str] = mapped_column(String(24), nullable=False)
    created_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", "relationship_id", "user_id", "character_id", "scope", "scenario_key", name="uq_memory_graph_evidence_owner"),
        ForeignKeyConstraint(["tenant_id", "memory_id", "memory_version", "relationship_id", "user_id", "character_id", "scope", "scenario_key"], ["memories.tenant_id", "memories.id", "memories.version", "memories.relationship_id", "memories.user_id", "memories.character_id", "memories.scope", "memories.scenario_key"], name="fk_memory_graph_evidence_exact_memory"),
        ForeignKeyConstraint(["tenant_id", "entity_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"], ["memory_entities.tenant_id", "memory_entities.id", "memory_entities.relationship_id", "memory_entities.user_id", "memory_entities.character_id", "memory_entities.scope", "memory_entities.scenario_key"], name="fk_memory_graph_evidence_entity_owner"),
        ForeignKeyConstraint(["tenant_id", "relation_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"], ["memory_relations.tenant_id", "memory_relations.id", "memory_relations.relationship_id", "memory_relations.user_id", "memory_relations.character_id", "memory_relations.scope", "memory_relations.scenario_key"], name="fk_memory_graph_evidence_relation_owner"),
        CheckConstraint("(scope = 'real_world' AND scenario_id IS NULL) OR (scope = 'roleplay' AND scenario_id IS NOT NULL)", name="ck_memory_graph_evidence_scope"),
        CheckConstraint("memory_version >= 1 AND created_epoch >= 0", name="ck_memory_graph_evidence_revision_epoch"),
        CheckConstraint("(entity_id IS NOT NULL AND relation_id IS NULL AND evidence_role='defines_entity') OR (entity_id IS NULL AND relation_id IS NOT NULL AND evidence_role='supports_relation')", name="ck_memory_graph_evidence_target_role"),
        CheckConstraint("invalidated_at IS NULL OR invalidated_at >= created_at", name="ck_memory_graph_evidence_invalidation_time"),
        Index("memory_graph_evidence_memory_idx", "tenant_id", "relationship_id", "memory_id"),
        Index("memory_graph_evidence_relation_idx", "tenant_id", "relationship_id", "relation_id", postgresql_where=text("relation_id IS NOT NULL")),
        Index("memory_graph_evidence_entity_idx", "tenant_id", "relationship_id", "entity_id", postgresql_where=text("entity_id IS NOT NULL")),
        Index("uq_memory_graph_evidence_entity", "tenant_id", "memory_id", "memory_version", "entity_id", unique=True, postgresql_where=text("entity_id IS NOT NULL")),
        Index("uq_memory_graph_evidence_relation", "tenant_id", "memory_id", "memory_version", "relation_id", unique=True, postgresql_where=text("relation_id IS NOT NULL")),
    )


class MemorySource(TenantOwned):
    __tablename__ = "memory_sources"
    memory_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[Scope] = mapped_column(
        Enum(Scope, name="scope_enum", native_enum=True, create_type=False), nullable=False
    )
    scenario_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    relation: Mapped[str] = mapped_column(String(32), nullable=False)
    source_role: Mapped[str] = mapped_column(String(32), server_default="user", nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "memory_id", "message_id", "relation", name="uq_memory_source"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "memory_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "memories.tenant_id",
                "memories.id",
                "memories.user_id",
                "memories.character_id",
                "memories.scope",
                "memories.scenario_key",
            ],
            name="fk_memory_source_memory_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "message_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "messages.tenant_id",
                "messages.id",
                "messages.user_id",
                "messages.character_id",
                "messages.scope",
                "messages.scenario_key",
            ],
            name="fk_memory_source_message_owner",
        ),
    )


class MemoryDependency(TenantOwned):
    __tablename__ = "memory_dependencies"
    parent_memory_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    child_memory_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[Scope] = mapped_column(
        Enum(Scope, name="scope_enum", native_enum=True, create_type=False), nullable=False
    )
    scenario_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    relation: Mapped[str] = mapped_column(String(32), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "parent_memory_id",
            "child_memory_id",
            "relation",
            name="uq_memory_dependency",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "parent_memory_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "memories.tenant_id",
                "memories.id",
                "memories.user_id",
                "memories.character_id",
                "memories.scope",
                "memories.scenario_key",
            ],
            name="fk_memory_dependency_parent_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "child_memory_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "memories.tenant_id",
                "memories.id",
                "memories.user_id",
                "memories.character_id",
                "memories.scope",
                "memories.scenario_key",
            ],
            name="fk_memory_dependency_child_owner",
        ),
        CheckConstraint("parent_memory_id <> child_memory_id", name="ck_dependency_not_self"),
        Index("memory_dependencies_parent_idx", "tenant_id", "parent_memory_id"),
        Index("memory_dependencies_child_idx", "tenant_id", "child_memory_id"),
    )


class SuppressionRule(TenantOwned):
    __tablename__ = "suppression_rules"
    relationship_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[Scope] = mapped_column(
        Enum(Scope, name="scope_enum", native_enum=True, create_type=False), nullable=False
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scenario_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        Computed(
            "coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)",
            persisted=True,
        ),
    )
    rule_type: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_version: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    slot_hmac: Mapped[str | None] = mapped_column(String(128))
    value_hmac: Mapped[str | None] = mapped_column(String(128))
    entity_hmac: Mapped[str | None] = mapped_column(String(128))
    source_message_id_hmac: Mapped[str | None] = mapped_column(String(128))
    key_version: Mapped[str] = mapped_column(String(32), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(255))
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "character_id"], ["characters.tenant_id", "characters.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "relationships.tenant_id", "relationships.id", "relationships.user_id",
                "relationships.character_id", "relationships.scope", "relationships.scenario_key",
            ],
            name="fk_suppression_relationship_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "scenario_id", "user_id", "character_id"],
            [
                "roleplay_scenarios.tenant_id",
                "roleplay_scenarios.id",
                "roleplay_scenarios.user_id",
                "roleplay_scenarios.character_id",
            ],
            name="fk_suppression_scenario_owner",
        ),
        CheckConstraint(
            "(scope = 'real_world' AND scenario_id IS NULL) OR (scope = 'roleplay' AND scenario_id IS NOT NULL)",
            name="ck_suppression_scope",
        ),
        CheckConstraint("rule_type IN ('slot_value')", name="ck_suppression_stage2_type"),
        CheckConstraint(
            "slot_hmac IS NOT NULL AND value_hmac IS NOT NULL AND entity_hmac IS NULL AND source_message_id_hmac IS NULL",
            name="ck_suppression_hmac_only",
        ),
    )


class OutboxJob(TenantOwned):
    __tablename__ = "outbox_jobs"
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    memory_control_state_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state: Mapped[JobState] = mapped_column(
        Enum(JobState, name="job_state_enum", native_enum=True),
        server_default="pending",
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(64))
    creation_epoch: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_outbox_dedupe"),
        ForeignKeyConstraint(
            ["tenant_id", "memory_control_state_id"],
            ["memory_control_state.tenant_id", "memory_control_state.id"],
            name="fk_outbox_memory_control",
        ),
        CheckConstraint(
            "job_type NOT LIKE 'memory_%' OR memory_control_state_id IS NOT NULL",
            name="ck_outbox_memory_control_required",
        ),
        CheckConstraint(
            "job_type IN ('generic','conversation_summary','relationship_note_refresh','snapshot_refresh','memory_extract','memory_promote','memory_embed','memory_graph_sync','response_retry','inference','sales_draft_inference')",
            name="ck_outbox_job_type_allowlist",
        ),
        Index(
            "outbox_ready_idx", "available_at", "id", postgresql_where=(state == JobState.pending)
        ),
        Index(
            "outbox_claimed_idx", "lease_expires_at", postgresql_where=(state == JobState.running)
        ),
    )


class ResponseMemoryRef(TenantOwned):
    __tablename__ = "response_memory_refs"
    assistant_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    relationship_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    memory_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[Scope] = mapped_column(
        Enum(Scope, name="scope_enum", native_enum=True, create_type=False), nullable=False
    )
    scenario_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    memory_version: Mapped[int | None] = mapped_column(Integer)
    snapshot_generation: Mapped[int | None] = mapped_column(Integer)
    ref_type: Mapped[str] = mapped_column(String(16), server_default="memory", nullable=False)
    usage_type: Mapped[str] = mapped_column(String(32), nullable=False)
    response_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "tenant_id",
                "assistant_message_id",
                "user_id",
                "character_id",
                "scope",
                "scenario_key",
            ],
            [
                "messages.tenant_id",
                "messages.id",
                "messages.user_id",
                "messages.character_id",
                "messages.scope",
                "messages.scenario_key",
            ],
            name="fk_response_message_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "memory_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "memories.tenant_id", "memories.id", "memories.relationship_id",
                "memories.user_id", "memories.character_id", "memories.scope", "memories.scenario_key",
            ],
            name="fk_response_memory_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "snapshot_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
            ["memory_snapshots.tenant_id", "memory_snapshots.id", "memory_snapshots.relationship_id", "memory_snapshots.user_id", "memory_snapshots.character_id", "memory_snapshots.scope", "memory_snapshots.scenario_key"],
            name="fk_response_snapshot_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
            ["relationships.tenant_id", "relationships.id", "relationships.user_id", "relationships.character_id", "relationships.scope", "relationships.scenario_key"],
            name="fk_response_ref_relationship_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "response_run_id"],
            ["response_runs.tenant_id", "response_runs.id"],
            name="fk_response_ref_run",
        ),
        CheckConstraint("usage_type IN ('supplied','cited')", name="ck_response_ref_usage"),
        CheckConstraint(
            "(ref_type='memory' AND memory_id IS NOT NULL AND snapshot_id IS NULL) OR (ref_type='snapshot' AND snapshot_id IS NOT NULL AND memory_id IS NULL)",
            name="ck_response_ref_type",
        ),
        CheckConstraint("(memory_id IS NOT NULL) <> (snapshot_id IS NOT NULL)", name="ck_response_ref_one_source"),
        CheckConstraint("(memory_id IS NOT NULL AND memory_version IS NOT NULL AND snapshot_generation IS NULL) OR (snapshot_id IS NOT NULL AND snapshot_generation IS NOT NULL AND memory_version IS NULL)", name="ck_response_ref_source_version"),
        Index("response_refs_run_idx", "tenant_id", "response_run_id", "usage_type"),
    )


class RetrievalRun(TenantOwned):
    __tablename__ = "retrieval_runs"
    relationship_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[Scope] = mapped_column(
        Enum(Scope, name="scope_enum", native_enum=True, create_type=False), nullable=False
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scenario_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        Computed(
            "coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)", persisted=True
        ),
    )
    query_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    retrieval_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    index_version: Mapped[str] = mapped_column(String(32), nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    result_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    selected_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    latency_bucket: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_retrieval_run_tenant_id"),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        UniqueConstraint(
            "tenant_id", "id", "relationship_id", name="uq_retrieval_run_relationship"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "character_id"], ["characters.tenant_id", "characters.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "relationships.tenant_id", "relationships.id", "relationships.user_id",
                "relationships.character_id", "relationships.scope", "relationships.scenario_key",
            ],
            name="fk_retrieval_run_relationship_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "scenario_id", "user_id", "character_id"],
            [
                "roleplay_scenarios.tenant_id",
                "roleplay_scenarios.id",
                "roleplay_scenarios.user_id",
                "roleplay_scenarios.character_id",
            ],
        ),
        CheckConstraint(
            "(scope='real_world' AND scenario_id IS NULL) OR (scope='roleplay' AND scenario_id IS NOT NULL)",
            name="ck_retrieval_run_scope",
        ),
        CheckConstraint(
            "result_count>=0 AND selected_count>=0 AND selected_count<=result_count",
            name="ck_retrieval_run_counts",
        ),
        CheckConstraint(
            "latency_bucket IN ('lt_10ms','lt_100ms','lt_1s','gte_1s')",
            name="ck_retrieval_run_latency",
        ),
        CheckConstraint(
            "reason_code IN ('ok','vector_degraded','no_results','budget_limited','invalid_request')",
            name="ck_retrieval_run_reason",
        ),
        Index(
            "retrieval_runs_owner_time_idx",
            "tenant_id",
            "user_id",
            "character_id",
            "scope",
            "scenario_key",
            text("created_at DESC"),
        ),
    )


class RetrievalCandidate(TenantOwned):
    __tablename__ = "retrieval_candidates"
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    relationship_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    memory_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    memory_version: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[Scope] = mapped_column(
        Enum(Scope, name="scope_enum", native_enum=True, create_type=False), nullable=False
    )
    scenario_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    exact_rank: Mapped[int | None] = mapped_column(Integer)
    fts_rank: Mapped[int | None] = mapped_column(Integer)
    vector_rank: Mapped[int | None] = mapped_column(Integer)
    temporal_rank: Mapped[int | None] = mapped_column(Integer)
    fused_score: Mapped[float] = mapped_column(Numeric(12, 8), nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "run_id", "memory_id", name="uq_retrieval_candidate"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "relationship_id"],
            ["retrieval_runs.tenant_id", "retrieval_runs.id", "retrieval_runs.relationship_id"],
            name="fk_retrieval_candidate_run_relationship",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "memory_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "memories.tenant_id", "memories.id", "memories.relationship_id",
                "memories.user_id", "memories.character_id", "memories.scope", "memories.scenario_key",
            ],
            name="fk_retrieval_candidate_memory_relationship",
        ),
        CheckConstraint("memory_version>0", name="ck_retrieval_candidate_version"),
        CheckConstraint(
            "(exact_rank IS NULL OR exact_rank>0) AND (fts_rank IS NULL OR fts_rank>0) AND (vector_rank IS NULL OR vector_rank>0) AND (temporal_rank IS NULL OR temporal_rank>0)",
            name="ck_retrieval_candidate_ranks",
        ),
        CheckConstraint(
            "reason_code IN ('rrf_selected','rrf_candidate','budget_excluded')",
            name="ck_retrieval_candidate_reason",
        ),
        Index("retrieval_candidates_run_idx", "tenant_id", "run_id", "selected"),
    )


class ResponseRoutePolicy(TenantOwned):
    """Operator-controlled version tuple; runtime may read but never enable routes."""

    __tablename__ = "response_route_policies"
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    streaming_allowed: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_response_route_tenant_id"),
        UniqueConstraint(
            "tenant_id",
            "character_id",
            "profile_version",
            "provider",
            "model",
            "prompt_version",
            "schema_version",
            "policy_version",
            name="uq_response_route_tuple",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "character_id", "profile_version"],
            [
                "character_profile_versions.tenant_id",
                "character_profile_versions.character_id",
                "character_profile_versions.version",
            ],
            name="fk_response_route_profile",
        ),
        CheckConstraint(
            "provider IN ('local_fake','external_disabled','deepseek','anymodel','mac_worker','disabled')",
            name="ck_response_route_provider",
        ),
        CheckConstraint("NOT streaming_allowed", name="ck_response_route_no_streaming"),
    )


class ResponseRun(TenantOwned):
    """Opaque authoritative record for a committed assistant response."""

    __tablename__ = "response_runs"
    relationship_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    assistant_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scope: Mapped[Scope] = mapped_column(
        Enum(Scope, name="scope_enum", native_enum=True, create_type=False), nullable=False
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scenario_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        Computed(
            "coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)", persisted=True
        ),
    )
    route_policy_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    retrieval_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    request_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    language_code: Mapped[str] = mapped_column(String(8), nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_response_run_tenant_id"),
        UniqueConstraint(
            "tenant_id",
            "id",
            "user_id",
            "character_id",
            "scope",
            "scenario_key",
            name="uq_response_run_owner_scope",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            "relationship_id",
            "user_id",
            "character_id",
            "scope",
            "scenario_key",
            name="uq_response_run_relationship_owner_scope",
        ),
        UniqueConstraint(
            "tenant_id", "user_id", "request_fingerprint", name="uq_response_run_idempotency"
        ),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "relationships.tenant_id", "relationships.id", "relationships.user_id",
                "relationships.character_id", "relationships.scope", "relationships.scenario_key",
            ],
            name="fk_response_run_relationship_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "conversations.tenant_id",
                "conversations.id",
                "conversations.user_id",
                "conversations.character_id",
                "conversations.scope",
                "conversations.scenario_key",
            ],
            name="fk_response_run_conversation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "conversations.tenant_id", "conversations.id", "conversations.relationship_id",
                "conversations.user_id", "conversations.character_id", "conversations.scope",
                "conversations.scenario_key",
            ],
            name="fk_response_run_conversation_relationship",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_message_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "messages.tenant_id",
                "messages.id",
                "messages.user_id",
                "messages.character_id",
                "messages.scope",
                "messages.scenario_key",
            ],
            name="fk_response_run_user_message",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "assistant_message_id",
                "user_id",
                "character_id",
                "scope",
                "scenario_key",
            ],
            [
                "messages.tenant_id",
                "messages.id",
                "messages.user_id",
                "messages.character_id",
                "messages.scope",
                "messages.scenario_key",
            ],
            name="fk_response_run_assistant_message",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "route_policy_id"],
            ["response_route_policies.tenant_id", "response_route_policies.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "retrieval_run_id", "relationship_id"],
            ["retrieval_runs.tenant_id", "retrieval_runs.id", "retrieval_runs.relationship_id"],
            name="fk_response_run_retrieval_relationship",
        ),
        CheckConstraint(
            "(scope='real_world' AND scenario_id IS NULL) OR (scope='roleplay' AND scenario_id IS NOT NULL)",
            name="ck_response_run_scope",
        ),
        CheckConstraint(
            "status IN ('completed','abstained','rejected')", name="ck_response_run_status"
        ),
        CheckConstraint(
            "reason_code IN ('ok','insufficient_evidence','route_disabled','provider_invalid','provider_failure','citation_invalid','guard_denied','idempotency_conflict')",
            name="ck_response_run_reason",
        ),
        CheckConstraint(
            "language_code IN ('ru','en','mixed','unknown')", name="ck_response_run_language"
        ),
        Index(
            "response_runs_owner_time_idx",
            "tenant_id",
            "user_id",
            "character_id",
            "scope",
            "scenario_key",
            text("created_at DESC"),
        ),
    )


class MemorySnapshot(TenantOwned):
    __tablename__ = "memory_snapshots"
    relationship_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[Scope] = mapped_column(
        Enum(Scope, name="scope_enum", native_enum=True, create_type=False), nullable=False
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scenario_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        Computed(
            "coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)",
            persisted=True,
        ),
    )
    snapshot_type: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_key: Mapped[str] = mapped_column(String(255), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    generation: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    source_memory_ids: Mapped[list] = mapped_column(JSONB, server_default="[]", nullable=False)
    source_segment_ids: Mapped[list] = mapped_column(JSONB, server_default="[]", nullable=False)
    source_message_ids: Mapped[list] = mapped_column(JSONB, server_default="[]", nullable=False)
    creation_epoch: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "id", "relationship_id", "user_id", "character_id", "scope", "scenario_key",
            name="uq_memory_snapshot_owner_scope",
        ),
        UniqueConstraint(
            "tenant_id", "relationship_id", "snapshot_type", "snapshot_key", name="uq_memory_snapshot_identity"
        ),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "character_id"], ["characters.tenant_id", "characters.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "relationships.tenant_id", "relationships.id", "relationships.user_id",
                "relationships.character_id", "relationships.scope", "relationships.scenario_key",
            ],
            name="fk_memory_snapshot_relationship_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "scenario_id", "user_id", "character_id"],
            [
                "roleplay_scenarios.tenant_id",
                "roleplay_scenarios.id",
                "roleplay_scenarios.user_id",
                "roleplay_scenarios.character_id",
            ],
            name="fk_memory_snapshot_scenario_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
            ["conversations.tenant_id", "conversations.id", "conversations.relationship_id", "conversations.user_id", "conversations.character_id", "conversations.scope", "conversations.scenario_key"],
            name="fk_memory_snapshot_conversation_owner",
        ),
        CheckConstraint("snapshot_type IN ('relationship_note','relationship_summary','user_profile_summary','conversation_summary','topic_summary','day_summary','goal_summary','project_summary','current_context_summary')", name="ck_memory_snapshot_type"),
        CheckConstraint("generation >= 1 AND creation_epoch >= 0", name="ck_memory_snapshot_generation_epoch"),
        CheckConstraint("length(source_fingerprint) BETWEEN 1 AND 128", name="ck_memory_snapshot_fingerprint"),
        CheckConstraint(
            "((snapshot_type IN ('conversation_summary','current_context_summary') AND conversation_id IS NOT NULL AND snapshot_key = 'conversation:' || conversation_id::text) OR (snapshot_type NOT IN ('conversation_summary','current_context_summary') AND conversation_id IS NULL))",
            name="ck_memory_snapshot_conversation_key",
        ),
        CheckConstraint(
            "(scope = 'real_world' AND scenario_id IS NULL) OR (scope = 'roleplay' AND scenario_id IS NOT NULL)",
            name="ck_memory_snapshot_scope",
        ),
    )


class AuditLog(TenantOwned):
    __tablename__ = "audit_log"
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    metadata_json: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )


class SharedHistoryEpisode(TenantOwned):
    """Bounded, source-linked relationship continuity; text stays with Memory."""

    __tablename__ = "shared_history_episodes"
    relationship_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[Scope] = mapped_column(
        Enum(Scope, name="scope_enum", native_enum=True, create_type=False), nullable=False
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scenario_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        Computed(
            "coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)", persisted=True
        ),
    )
    memory_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    state: Mapped[str] = mapped_column(String(16), server_default="active", nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    creation_epoch: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "character_id",
            "scope",
            "scenario_key",
            "memory_id",
            name="uq_shared_history_memory",
        ),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "character_id"], ["characters.tenant_id", "characters.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "relationships.tenant_id", "relationships.id", "relationships.user_id",
                "relationships.character_id", "relationships.scope", "relationships.scenario_key",
            ],
            name="fk_shared_history_relationship_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "memory_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "memories.tenant_id",
                "memories.id",
                "memories.user_id",
                "memories.character_id",
                "memories.scope",
                "memories.scenario_key",
            ],
            name="fk_shared_history_memory_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_message_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "messages.tenant_id",
                "messages.id",
                "messages.user_id",
                "messages.character_id",
                "messages.scope",
                "messages.scenario_key",
            ],
            name="fk_shared_history_source_owner",
        ),
        CheckConstraint(
            "(scope = 'real_world' AND scenario_id IS NULL) OR (scope = 'roleplay' AND scenario_id IS NOT NULL)",
            name="ck_shared_history_scope",
        ),
        CheckConstraint(
            "state IN ('active','historical','deleted')", name="ck_shared_history_state"
        ),
        CheckConstraint(
            "retired_at IS NULL OR retired_at >= confirmed_at", name="ck_shared_history_interval"
        ),
        Index(
            "shared_history_active_idx",
            "tenant_id",
            "user_id",
            "character_id",
            "scope",
            "scenario_key",
            text("confirmed_at DESC"),
            postgresql_where=(state == "active"),
        ),
    )


class ExplicitCommandReceipt(TenantOwned):
    """Durable idempotency guard; it deliberately contains no plaintext command values."""

    __tablename__ = "explicit_command_receipts"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    command_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    command_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    result_memory_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    result_status: Mapped[str] = mapped_column(String(32), nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "command_id", name="uq_explicit_command_receipt"),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "result_memory_id"], ["memories.tenant_id", "memories.id"]
        ),
        CheckConstraint(
            "operation IN ('remember','correct','forget')", name="ck_command_receipt_operation"
        ),
        CheckConstraint(
            "result_status IN ('completed','already_forgotten')", name="ck_command_receipt_status"
        ),
    )


class ShadowMemoryCandidate(TenantOwned):
    """Encrypted, evaluation-only extraction output; never an authoritative memory."""

    __tablename__ = "shadow_memory_candidates"
    relationship_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[Scope] = mapped_column(
        Enum(Scope, name="scope_enum", native_enum=True, create_type=False), nullable=False
    )
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scenario_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        Computed(
            "coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)", persisted=True
        ),
    )
    source_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    memory_control_state_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str] = mapped_column(String(32), nullable=False)
    predicate: Mapped[str] = mapped_column(String(64), nullable=False)
    value_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    value_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    value_key_version: Mapped[str | None] = mapped_column(String(32))
    value_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    temporal_hint: Mapped[str] = mapped_column(String(16), server_default="none", nullable=False)
    extractor_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    extractor_model: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    extractor_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    source_deletion_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), server_default="shadow", nullable=False)
    consent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    retention_policy_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promoted_memory_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_shadow_candidate_tenant_id"),
        UniqueConstraint(
            "tenant_id",
            "source_message_id",
            "extractor_provider",
            "extractor_model",
            "extractor_prompt_version",
            "extractor_schema_version",
            "value_fingerprint",
            name="uq_shadow_candidate_versioned_dedupe",
        ),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "character_id"], ["characters.tenant_id", "characters.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "relationships.tenant_id", "relationships.id", "relationships.user_id",
                "relationships.character_id", "relationships.scope", "relationships.scenario_key",
            ],
            name="fk_shadow_candidate_relationship_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_message_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "messages.tenant_id",
                "messages.id",
                "messages.user_id",
                "messages.character_id",
                "messages.scope",
                "messages.scenario_key",
            ],
            name="fk_shadow_candidate_source_owner",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "memory_control_state_id",
                "relationship_id",
                "user_id",
                "character_id",
                "scope",
                "scenario_key",
            ],
            [
                "memory_control_state.tenant_id",
                "memory_control_state.id",
                "memory_control_state.relationship_id",
                "memory_control_state.user_id",
                "memory_control_state.character_id",
                "memory_control_state.scope",
                "memory_control_state.scenario_key",
            ],
            name="fk_shadow_candidate_control_owner",
        ),
        ForeignKeyConstraint(["tenant_id", "consent_id"], ["consents.tenant_id", "consents.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "retention_policy_id"],
            ["retention_policies.tenant_id", "retention_policies.id"],
        ),
        CheckConstraint(
            "(scope = 'real_world' AND scenario_id IS NULL) OR (scope = 'roleplay' AND scenario_id IS NOT NULL)",
            name="ck_shadow_candidate_scope",
        ),
        CheckConstraint(
            "category IN ('preferred_name','preference','hobby','broad_locale','current_activity','goal','dated_event','relationship','emotional_episode','explicit_instruction')",
            name="ck_shadow_candidate_category",
        ),
        CheckConstraint("subject IN ('user')", name="ck_shadow_candidate_subject"),
        CheckConstraint(
            "predicate IN ('preferred_name','likes','dislikes','hobby','locale','timezone','current_activity','goal','event','relationship','emotion','instruction')",
            name="ck_shadow_candidate_predicate",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_shadow_candidate_confidence"
        ),
        CheckConstraint(
            "temporal_hint IN ('none','stable','temporary')", name="ck_shadow_candidate_temporal"
        ),
        CheckConstraint(
            "status IN ('shadow','promoting','promoted','conflict','invalidated','rejected')",
            name="ck_shadow_candidate_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "promoted_memory_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "memories.tenant_id",
                "memories.id",
                "memories.user_id",
                "memories.character_id",
                "memories.scope",
                "memories.scenario_key",
            ],
            name="fk_shadow_promoted_memory_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "promoted_memory_id", "relationship_id", "user_id", "character_id", "scope", "scenario_key"],
            [
                "memories.tenant_id", "memories.id", "memories.relationship_id",
                "memories.user_id", "memories.character_id", "memories.scope", "memories.scenario_key",
            ],
            name="fk_shadow_promoted_relationship_owner",
        ),
        CheckConstraint(
            "(value_ciphertext IS NULL AND value_nonce IS NULL AND value_key_version IS NULL) "
            "OR (value_ciphertext IS NOT NULL AND value_nonce IS NOT NULL "
            "AND value_key_version IS NOT NULL AND length(value_ciphertext) > 16 "
            "AND octet_length(value_nonce) = 12 AND value_key_version IS NOT NULL)",
            name="ck_shadow_candidate_envelope",
        ),
        Index("shadow_candidate_source_idx", "tenant_id", "source_message_id", "status"),
    )


class ShadowCandidateLabel(TenantOwned):
    __tablename__ = "shadow_candidate_labels"
    candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    label: Mapped[str] = mapped_column(String(16), nullable=False)
    evaluator_code: Mapped[str] = mapped_column(String(32), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "candidate_id", "evaluator_code", name="uq_shadow_candidate_label"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["shadow_memory_candidates.tenant_id", "shadow_memory_candidates.id"],
        ),
        CheckConstraint(
            "label IN ('positive','negative','ambiguous')", name="ck_shadow_candidate_label"
        ),
    )


class AutoWriteReleasePolicy(TenantOwned):
    __tablename__ = "autowrite_release_policies"
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    extractor_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    extractor_model: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    extractor_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    min_confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    min_precision: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    min_labelled_support: Mapped[int] = mapped_column(Integer, nullable=False)
    measured_precision: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    measured_support: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_autowrite_release_tenant_id"),
        UniqueConstraint(
            "tenant_id",
            "category",
            "extractor_provider",
            "extractor_model",
            "extractor_prompt_version",
            "extractor_schema_version",
            "policy_version",
            name="uq_autowrite_release",
        ),
        CheckConstraint(
            "category IN ('preferred_name','preference','hobby','current_activity','goal','dated_event','relationship','emotional_episode','explicit_instruction')",
            name="ck_release_category",
        ),
        CheckConstraint(
            "min_confidence>=0 AND min_confidence<=1 AND min_precision>=0 AND min_precision<=1 AND measured_precision>=0 AND measured_precision<=1 AND min_labelled_support>0 AND measured_support>=0",
            name="ck_release_metrics",
        ),
        CheckConstraint(
            "NOT enabled OR (measured_precision>=min_precision AND measured_support>=min_labelled_support)",
            name="ck_release_enabled_evidence",
        ),
    )


class CandidatePromotionReceipt(TenantOwned):
    __tablename__ = "candidate_promotion_receipts"
    candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    release_policy_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    promotion_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    memory_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "candidate_id", name="uq_candidate_promotion_receipt"),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["shadow_memory_candidates.tenant_id", "shadow_memory_candidates.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "release_policy_id"],
            ["autowrite_release_policies.tenant_id", "autowrite_release_policies.id"],
        ),
        ForeignKeyConstraint(["tenant_id", "memory_id"], ["memories.tenant_id", "memories.id"]),
        CheckConstraint(
            "status IN ('promoted','identical','denied','conflict')",
            name="ck_promotion_receipt_status",
        ),
    )


class RegionalPolicyProfile(TenantOwned):
    __tablename__ = "regional_policy_profiles"
    jurisdiction: Mapped[str] = mapped_column(String(32), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rules: Mapped[dict] = mapped_column(JSONB, nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_policy_profile_tenant_id"),
        UniqueConstraint("tenant_id", "id", "version", name="uq_policy_profile_id_version"),
        UniqueConstraint(
            "tenant_id",
            "jurisdiction",
            "market",
            "version",
            name="uq_policy_profile_version",
        ),
        CheckConstraint("status IN ('draft','active','retired')", name="ck_policy_profile_status"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_policy_profile_interval",
        ),
        Index(
            "policy_profile_active_idx",
            "tenant_id",
            "jurisdiction",
            "market",
            "effective_from",
            postgresql_where=(status == "active"),
        ),
        Index(
            "policy_profile_one_active_idx",
            "tenant_id",
            "jurisdiction",
            "market",
            unique=True,
            postgresql_where=(status == "active"),
        ),
    )


class AgeAssurance(TenantOwned):
    __tablename__ = "age_assurances"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    age_band: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    jurisdiction: Mapped[str] = mapped_column(String(32), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    asserted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    retention_policy_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_age_assurance_tenant_id"),
        UniqueConstraint(
            "tenant_id", "user_id", "jurisdiction", "version", name="uq_age_assurance_version"
        ),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "retention_policy_id"],
            ["retention_policies.tenant_id", "retention_policies.id"],
        ),
        CheckConstraint("age_band IN ('unknown','minor','adult')", name="ck_age_assurance_band"),
        CheckConstraint(
            "status IN ('pending','verified','self_attested','expired','rejected')",
            name="ck_age_assurance_status",
        ),
        CheckConstraint(
            "status <> 'verified' OR verified_at IS NOT NULL", name="ck_age_assurance_verified_at"
        ),
        CheckConstraint(
            "expires_at IS NULL OR verified_at IS NULL OR expires_at > verified_at",
            name="ck_age_assurance_interval",
        ),
    )


class AIDisclosureReceipt(TenantOwned):
    __tablename__ = "ai_disclosure_receipts"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    policy_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    receipt_key: Mapped[str] = mapped_column(String(255), nullable=False)
    displayed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    memory_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    memory_opt_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "receipt_key", name="uq_disclosure_receipt_key"),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "policy_profile_id", "policy_version"],
            [
                "regional_policy_profiles.tenant_id",
                "regional_policy_profiles.id",
                "regional_policy_profiles.version",
            ],
        ),
        Index("disclosure_receipt_lookup_idx", "tenant_id", "user_id", "displayed_at"),
    )


class UserCharacterLocalePreference(TenantOwned):
    __tablename__ = "user_character_locale_preferences"
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    locale_code: Mapped[str] = mapped_column(String(8), nullable=False)
    version: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "character_id", name="uq_locale_pref_owner"),
        UniqueConstraint("tenant_id", "id", name="uq_locale_pref_tenant_id"),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "character_id"], ["characters.tenant_id", "characters.id"]
        ),
        CheckConstraint("locale_code IN ('ru-RU','en-US','en-GB')", name="ck_locale_pref_code"),
        CheckConstraint("version > 0", name="ck_locale_pref_version"),
    )
