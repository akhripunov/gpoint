"""Replace legacy derived views with encrypted, relationship-owned snapshots.

Revision ID: agata0004
Revises: agata0003
"""

from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

from app.messages.crypto import AeadCrypto
from app.messages.protected_content import (
    content_aad,
    decrypt_json_from_storage,
    encrypt_json_for_storage,
    memory_snapshot_content_aad,
)

revision = "agata0004"
down_revision = "agata0003"
branch_labels = None
depends_on = None

SNAPSHOT_TYPES = (
    "relationship_note", "relationship_summary", "user_profile_summary",
    "conversation_summary", "topic_summary", "day_summary", "goal_summary",
    "project_summary", "current_context_summary",
)


def _crypto() -> AeadCrypto:
    key = os.environ.get("AGATA_MESSAGE_AEAD_KEY")
    if not key:
        raise RuntimeError("AGATA_MESSAGE_AEAD_KEY required to migrate derived snapshots")
    return AeadCrypto(
        encoded_key=key,
        key_version=os.environ.get("AGATA_MESSAGE_AEAD_KEY_VERSION", "v1"),
    )


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table, schema="agata")


def _has_column(table: str, column: str) -> bool:
    return any(item["name"] == column for item in sa.inspect(op.get_bind()).get_columns(table, schema="agata"))


def _has_constraint(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    constraints = (
        inspector.get_foreign_keys(table, schema="agata")
        + inspector.get_check_constraints(table, schema="agata")
    )
    return any(item.get("name") == name for item in constraints)


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("""
        CREATE TABLE IF NOT EXISTS agata.memory_snapshots (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES agata.tenants(id),
            relationship_id uuid NOT NULL,
            user_id uuid NOT NULL,
            character_id uuid NOT NULL,
            scope public.scope_enum NOT NULL,
            scenario_id uuid NULL,
            scenario_key uuid GENERATED ALWAYS AS
                (coalesce(scenario_id, '00000000-0000-0000-0000-000000000000'::uuid)) STORED,
            snapshot_type varchar(64) NOT NULL,
            snapshot_key varchar(255) NOT NULL,
            conversation_id uuid NULL,
            generation integer NOT NULL DEFAULT 1,
            content jsonb NOT NULL,
            source_fingerprint varchar(128) NOT NULL,
            source_memory_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            source_segment_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            source_message_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            creation_epoch integer NOT NULL DEFAULT 0,
            generated_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NULL,
            invalidated_at timestamptz NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_memory_snapshot_owner_scope UNIQUE
                (tenant_id,id,relationship_id,user_id,character_id,scope,scenario_key),
            CONSTRAINT uq_memory_snapshot_identity UNIQUE
                (tenant_id,relationship_id,snapshot_type,snapshot_key),
            CONSTRAINT fk_memory_snapshot_user FOREIGN KEY (tenant_id,user_id)
                REFERENCES agata.users(tenant_id,id),
            CONSTRAINT fk_memory_snapshot_character FOREIGN KEY (tenant_id,character_id)
                REFERENCES agata.characters(tenant_id,id),
            CONSTRAINT fk_memory_snapshot_relationship_owner FOREIGN KEY
                (tenant_id,relationship_id,user_id,character_id,scope,scenario_key)
                REFERENCES agata.relationships(tenant_id,id,user_id,character_id,scope,scenario_key),
            CONSTRAINT fk_memory_snapshot_scenario_owner FOREIGN KEY
                (tenant_id,scenario_id,user_id,character_id)
                REFERENCES agata.roleplay_scenarios(tenant_id,id,user_id,character_id),
            CONSTRAINT fk_memory_snapshot_conversation_owner FOREIGN KEY
                (tenant_id,conversation_id,relationship_id,user_id,character_id,scope,scenario_key)
                REFERENCES agata.conversations(tenant_id,id,relationship_id,user_id,character_id,scope,scenario_key),
            CONSTRAINT ck_memory_snapshot_type CHECK (snapshot_type IN
                ('relationship_note','relationship_summary','user_profile_summary',
                 'conversation_summary','topic_summary','day_summary','goal_summary',
                 'project_summary','current_context_summary')),
            CONSTRAINT ck_memory_snapshot_generation_epoch CHECK (generation >= 1 AND creation_epoch >= 0),
            CONSTRAINT ck_memory_snapshot_fingerprint CHECK (length(source_fingerprint) BETWEEN 1 AND 128),
            CONSTRAINT ck_memory_snapshot_conversation_key CHECK
                (((snapshot_type IN ('conversation_summary','current_context_summary'))
                   AND conversation_id IS NOT NULL
                   AND snapshot_key = 'conversation:' || conversation_id::text)
                 OR ((snapshot_type NOT IN ('conversation_summary','current_context_summary'))
                   AND conversation_id IS NULL)),
            CONSTRAINT ck_memory_snapshot_scope CHECK
                ((scope='real_world' AND scenario_id IS NULL) OR
                 (scope='roleplay' AND scenario_id IS NOT NULL))
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS memory_snapshots_relationship_idx ON agata.memory_snapshots (tenant_id,relationship_id,snapshot_type)")
    op.execute("CREATE INDEX IF NOT EXISTS memory_snapshots_conversation_idx ON agata.memory_snapshots (tenant_id,conversation_id,snapshot_type) WHERE conversation_id IS NOT NULL")

    source_rows = list(bind.execute(text("""
        SELECT v.id,v.tenant_id,v.relationship_id,v.user_id,v.character_id,v.scope,
               v.scenario_id,v.view_type,v.generation,v.content,v.parent_memory_ids,
               v.creation_epoch,r.scenario_key
        FROM agata.derived_memory_views v
        JOIN agata.relationships r ON r.tenant_id=v.tenant_id AND r.id=v.relationship_id
          AND r.user_id=v.user_id AND r.character_id=v.character_id
          AND r.scope=v.scope AND r.scenario_key=v.scenario_key
        ORDER BY v.tenant_id,v.id
    """))) if _has_table("derived_memory_views") else []
    crypto = _crypto() if source_rows else None
    migrated = 0
    for row in source_rows:
        if row.view_type not in SNAPSHOT_TYPES:
            raise RuntimeError(f"agata0004 unsupported legacy snapshot type: {row.view_type}")
        old_aad = content_aad(
            entity="derived_memory_views", tenant_id=row.tenant_id, user_id=row.user_id,
            character_id=row.character_id, scope=row.scope, scenario_id=row.scenario_id,
            record_id=row.id, field="content",
        )
        # Empty legacy objects were permitted before P1 encryption; all other rows
        # must authenticate using the exact historical AAD with no plaintext fallback.
        if row.content == {}:
            plaintext = {}
        else:
            if crypto is None:
                raise RuntimeError("agata0004 AEAD key unavailable for legacy encrypted rows")
            plaintext = decrypt_json_from_storage(row.content, old_aad, crypto=crypto)
        if not isinstance(plaintext, dict):
            raise RuntimeError("agata0004 legacy snapshot payload is not an object")

        segment_ids = row.parent_memory_ids
        if not isinstance(segment_ids, list) or any(not isinstance(value, str) for value in segment_ids):
            raise RuntimeError("agata0004 legacy source provenance is invalid")
        segment_ids = list(dict.fromkeys(segment_ids))
        conversation_ids: set[str] = set()
        resolved_memory_ids: set[str] = set()
        resolved_segment_ids: set[str] = set()
        if segment_ids:
            mapped_segments = list(bind.execute(text("""
                SELECT id::text AS id,conversation_id::text AS conversation_id
                FROM agata.conversation_segments
                WHERE tenant_id=:tenant AND id = ANY(CAST(:ids AS uuid[]))
            """), {"tenant": row.tenant_id, "ids": segment_ids}))
            mapped_memories = list(bind.execute(text("""
                SELECT id::text AS id
                FROM agata.memories
                WHERE tenant_id=:tenant AND id = ANY(CAST(:ids AS uuid[]))
                  AND relationship_id=:relationship AND user_id=:user
                  AND character_id=:character AND scope=:scope
                  AND scenario_id IS NOT DISTINCT FROM :scenario
            """), {
                "tenant": row.tenant_id, "ids": segment_ids,
                "relationship": row.relationship_id, "user": row.user_id,
                "character": row.character_id, "scope": row.scope, "scenario": row.scenario_id,
            }))
            resolved_segment_ids = {item.id for item in mapped_segments}
            resolved_memory_ids = {item.id for item in mapped_memories}
            if resolved_segment_ids | resolved_memory_ids != set(segment_ids):
                raise RuntimeError("agata0004 legacy source provenance does not resolve exactly")
            conversation_ids = {item.conversation_id for item in mapped_segments}
        elif row.view_type == "conversation_summary":
            related_conversations = bind.execute(text("""
                SELECT id::text FROM agata.conversations
                WHERE tenant_id=:tenant AND relationship_id=:relationship
                  AND user_id=:user AND character_id=:character
                  AND scope=:scope AND scenario_id IS NOT DISTINCT FROM :scenario
            """), {
                "tenant": row.tenant_id, "relationship": row.relationship_id,
                "user": row.user_id, "character": row.character_id,
                "scope": row.scope, "scenario": row.scenario_id,
            })
            conversation_ids = {item.id for item in related_conversations}
        if row.view_type == "conversation_summary":
            # The old row was relationship-wide. Preserve it as a relationship summary
            # when it spans conversations; use conversation scope only when provenance
            # proves it came from exactly one conversation.
            snapshot_type = "conversation_summary" if len(conversation_ids) == 1 else "relationship_summary"
            conversation_id = next(iter(conversation_ids), None) if len(conversation_ids) == 1 else None
            snapshot_key = f"conversation:{conversation_id}" if conversation_id else "relationship"
        else:
            snapshot_type = row.view_type
            conversation_id = None
            snapshot_key = "relationship"

        snapshot = SimpleNamespace(
            id=row.id, tenant_id=row.tenant_id, user_id=row.user_id,
            character_id=row.character_id, scope=row.scope, scenario_id=row.scenario_id,
            snapshot_type=snapshot_type, snapshot_key=snapshot_key, generation=row.generation,
        )
        encrypted = encrypt_json_for_storage(
            plaintext, memory_snapshot_content_aad(snapshot), crypto=crypto
        )
        bind.execute(text("""
            INSERT INTO agata.memory_snapshots
              (id,tenant_id,relationship_id,user_id,character_id,scope,scenario_id,
               snapshot_type,snapshot_key,conversation_id,generation,content,source_fingerprint,
               source_memory_ids,source_segment_ids,source_message_ids,creation_epoch)
            VALUES (:id,:tenant,:relationship,:user,:character,:scope,:scenario,:type,:key,
                    :conversation,:generation,CAST(:content AS jsonb),:fingerprint,
                    CAST(:memories AS jsonb),CAST(:segments AS jsonb),'[]'::jsonb,:epoch)
        """), {
            "id": row.id, "tenant": row.tenant_id, "relationship": row.relationship_id,
            "user": row.user_id, "character": row.character_id, "scope": row.scope,
            "scenario": row.scenario_id, "type": snapshot_type, "key": snapshot_key,
            "conversation": conversation_id, "generation": row.generation,
            "content": json.dumps(encrypted),
            "fingerprint": f"legacy:{row.id}:{row.generation}",
            "memories": json.dumps(sorted(resolved_memory_ids)),
            "segments": json.dumps(sorted(resolved_segment_ids)), "epoch": row.creation_epoch,
        })
        migrated += 1

        check = bind.execute(text("SELECT content FROM agata.memory_snapshots WHERE tenant_id=:tenant AND id=:id"), {"tenant": row.tenant_id, "id": row.id}).one()
        check_obj = snapshot
        if crypto is None:
            raise RuntimeError("agata0004 AEAD key unavailable for post-migration verification")
        decoded = decrypt_json_from_storage(check.content, memory_snapshot_content_aad(check_obj), crypto=crypto)
        if decoded != plaintext:
            raise RuntimeError("agata0004 post-migration decrypt verification failed")
        if row.generation < 1:
            raise RuntimeError("agata0004 generation validation failed")

    if migrated != len(source_rows):
        raise RuntimeError(f"agata0004 row count mismatch: source={len(source_rows)} migrated={migrated}")

    # Preserve every existing memory reference while making the owner relationship explicit.
    if not _has_column("response_memory_refs", "relationship_id"):
        op.add_column("response_memory_refs", sa.Column("relationship_id", sa.UUID(), nullable=True), schema="agata")
    if not _has_column("response_memory_refs", "snapshot_id"):
        op.add_column("response_memory_refs", sa.Column("snapshot_id", sa.UUID(), nullable=True), schema="agata")
    if not _has_column("response_memory_refs", "snapshot_generation"):
        op.add_column("response_memory_refs", sa.Column("snapshot_generation", sa.Integer(), nullable=True), schema="agata")
    if not _has_column("response_memory_refs", "ref_type"):
        op.add_column("response_memory_refs", sa.Column("ref_type", sa.String(length=16), server_default="memory", nullable=False), schema="agata")
    bind.execute(text("""
        UPDATE agata.response_memory_refs ref
        SET relationship_id=COALESCE(
            (SELECT run.relationship_id FROM agata.response_runs run
             WHERE run.tenant_id=ref.tenant_id AND run.id=ref.response_run_id),
            (SELECT memory.relationship_id FROM agata.memories memory
             WHERE memory.tenant_id=ref.tenant_id AND memory.id=ref.memory_id)
        )
    """))
    orphan_refs = bind.scalar(text("SELECT count(*) FROM agata.response_memory_refs WHERE relationship_id IS NULL"))
    if orphan_refs:
        raise RuntimeError(f"agata0004 cannot establish relationship for {orphan_refs} memory refs")
    if any(item["name"] == "relationship_id" and item["nullable"] for item in sa.inspect(bind).get_columns("response_memory_refs", schema="agata")):
        op.alter_column("response_memory_refs", "relationship_id", schema="agata", nullable=False)
    op.alter_column("response_memory_refs", "memory_version", schema="agata", nullable=True)
    if _has_constraint("response_memory_refs", "fk_response_memory_owner"):
        op.drop_constraint("fk_response_memory_owner", "response_memory_refs", schema="agata", type_="foreignkey")
    op.create_foreign_key(
        "fk_response_memory_owner", "response_memory_refs", "memories",
        ["tenant_id","memory_id","relationship_id","user_id","character_id","scope","scenario_key"],
        ["tenant_id","id","relationship_id","user_id","character_id","scope","scenario_key"],
        source_schema="agata", referent_schema="agata",
    )
    if _has_constraint("response_memory_refs", "fk_response_snapshot_owner"):
        op.drop_constraint("fk_response_snapshot_owner", "response_memory_refs", schema="agata", type_="foreignkey")
    op.create_foreign_key(
        "fk_response_snapshot_owner", "response_memory_refs", "memory_snapshots",
        ["tenant_id","snapshot_id","relationship_id","user_id","character_id","scope","scenario_key"],
        ["tenant_id","id","relationship_id","user_id","character_id","scope","scenario_key"],
        source_schema="agata", referent_schema="agata",
    )
    if _has_constraint("response_memory_refs", "fk_response_ref_relationship_owner"):
        op.drop_constraint("fk_response_ref_relationship_owner", "response_memory_refs", schema="agata", type_="foreignkey")
    op.create_foreign_key(
        "fk_response_ref_relationship_owner", "response_memory_refs", "relationships",
        ["tenant_id","relationship_id","user_id","character_id","scope","scenario_key"],
        ["tenant_id","id","user_id","character_id","scope","scenario_key"],
        source_schema="agata", referent_schema="agata",
    )
    if _has_constraint("response_memory_refs", "ck_response_ref_one_source"):
        op.drop_constraint("ck_response_ref_one_source", "response_memory_refs", schema="agata", type_="check")
    op.create_check_constraint(
        "ck_response_ref_one_source", "response_memory_refs",
        "(memory_id IS NOT NULL) <> (snapshot_id IS NOT NULL)", schema="agata",
    )
    if _has_constraint("response_memory_refs", "ck_response_ref_source_version"):
        op.drop_constraint("ck_response_ref_source_version", "response_memory_refs", schema="agata", type_="check")
    op.create_check_constraint(
        "ck_response_ref_source_version", "response_memory_refs",
        "(memory_id IS NOT NULL AND memory_version IS NOT NULL AND snapshot_generation IS NULL) OR "
        "(snapshot_id IS NOT NULL AND snapshot_generation IS NOT NULL AND memory_version IS NULL)", schema="agata",
    )
    if _has_constraint("response_memory_refs", "ck_response_ref_type"):
        op.drop_constraint("ck_response_ref_type", "response_memory_refs", schema="agata", type_="check")
    op.create_check_constraint(
        "ck_response_ref_type", "response_memory_refs",
        "(ref_type='memory' AND memory_id IS NOT NULL AND snapshot_id IS NULL) OR "
        "(ref_type='snapshot' AND snapshot_id IS NOT NULL AND memory_id IS NULL)",
        schema="agata",
    )

    # P1's guard originally accepted only selected Memory rows. Extend it so a
    # snapshot reference is proven to be the exact current generation and owned
    # by the same ResponseRun/Relationship, while preserving the Memory path.
    op.execute("""
        CREATE OR REPLACE FUNCTION agata.enforce_response_ref_supplied()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.snapshot_id IS NOT NULL THEN
            IF NOT EXISTS (
              SELECT 1 FROM agata.memory_snapshots s
              WHERE s.tenant_id=NEW.tenant_id AND s.id=NEW.snapshot_id
                AND s.relationship_id=NEW.relationship_id
                AND s.user_id=NEW.user_id AND s.character_id=NEW.character_id
                AND s.scope=NEW.scope AND s.scenario_key=NEW.scenario_key
                AND s.generation=NEW.snapshot_generation
                AND s.invalidated_at IS NULL
            ) THEN
              RAISE EXCEPTION 'response snapshot reference is stale or not owned';
            END IF;
            IF NEW.response_run_id IS NOT NULL AND NOT EXISTS (
              SELECT 1 FROM agata.response_runs r
              WHERE r.tenant_id=NEW.tenant_id AND r.id=NEW.response_run_id
                AND r.relationship_id=NEW.relationship_id
                AND r.user_id=NEW.user_id AND r.character_id=NEW.character_id
                AND r.scope=NEW.scope AND r.scenario_key=NEW.scenario_key
            ) THEN
              RAISE EXCEPTION 'response snapshot reference belongs to another run';
            END IF;
          ELSIF NEW.response_run_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM agata.response_runs r JOIN agata.retrieval_candidates c
              ON c.tenant_id=r.tenant_id AND c.run_id=r.retrieval_run_id
             AND c.memory_id=NEW.memory_id AND c.memory_version=NEW.memory_version AND c.selected
            WHERE r.tenant_id=NEW.tenant_id AND r.id=NEW.response_run_id
              AND r.relationship_id=NEW.relationship_id
              AND r.user_id=NEW.user_id AND r.character_id=NEW.character_id
              AND r.scope=NEW.scope AND r.scenario_key=NEW.scenario_key
          ) THEN
            RAISE EXCEPTION 'response reference is not a supplied retrieval candidate';
          END IF;
          RETURN NEW;
        END $$
    """)

    if _has_constraint("outbox_jobs", "ck_outbox_job_type_allowlist"):
        op.drop_constraint("ck_outbox_job_type_allowlist", "outbox_jobs", schema="agata", type_="check")
    op.create_check_constraint(
        "ck_outbox_job_type_allowlist", "outbox_jobs",
        "job_type IN ('generic','conversation_summary','relationship_note_refresh','snapshot_refresh','memory_extract','memory_promote','memory_embed','response_retry')",
        schema="agata",
    )

    op.execute("ALTER TABLE agata.memory_snapshots ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agata.memory_snapshots FORCE ROW LEVEL SECURITY")
    op.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='agata' AND tablename='memory_snapshots' AND policyname='memory_snapshots_tenant_isolation') THEN CREATE POLICY memory_snapshots_tenant_isolation ON agata.memory_snapshots "
               "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
               "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid); END IF; END $$")
    runtime_role = os.environ.get("AGATA_RUNTIME_DB_ROLE")
    if runtime_role:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", runtime_role):
            raise RuntimeError("AGATA_RUNTIME_DB_ROLE must be a simple PostgreSQL role name")
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON agata.memory_snapshots TO "{runtime_role}"')

    if _has_table("derived_memory_views"):
        op.drop_table("derived_memory_views", schema="agata")


def downgrade() -> None:
    raise RuntimeError("agata0004 is irreversible: derived snapshots contain protected user data")
