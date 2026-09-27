"""Add evidence-backed, relationship-scoped persistent memory graph.

Revision ID: agata0006
Revises: agata0005
"""
from __future__ import annotations

import os
import re

import sqlalchemy as sa
from alembic import op

revision = "agata0006"
down_revision = "agata0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # agata0001 creates tables from current ORM metadata. If a fresh install
    # already has these three empty shells, replace only those empty shells so
    # the versioned migration remains their sole schema owner. Never discard rows.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in ("memory_entities", "memory_relations", "memory_graph_evidence"):
        if inspector.has_table(table, schema="agata"):
            count = bind.scalar(sa.text(f"SELECT count(*) FROM agata.{table}"))
            if count:
                raise RuntimeError(f"agata0006 refuses pre-existing {table} rows")
    op.execute("DROP TABLE IF EXISTS agata.memory_graph_evidence")
    op.execute("DROP TABLE IF EXISTS agata.memory_relations")
    op.execute("DROP TABLE IF EXISTS agata.memory_entities")
    op.execute("""DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_memory_exact_revision_owner_scope' AND conrelid='agata.memories'::regclass) THEN
        ALTER TABLE agata.memories ADD CONSTRAINT uq_memory_exact_revision_owner_scope
          UNIQUE (tenant_id,id,version,relationship_id,user_id,character_id,scope,scenario_key);
      END IF;
    END $$""")
    op.execute("""
        CREATE TABLE agata.memory_entities (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES agata.tenants(id),
          relationship_id uuid NOT NULL, user_id uuid NOT NULL, character_id uuid NOT NULL,
          scope public.scope_enum NOT NULL, scenario_id uuid,
          scenario_key uuid GENERATED ALWAYS AS (coalesce(scenario_id,'00000000-0000-0000-0000-000000000000'::uuid)) STORED,
          entity_type varchar(32) NOT NULL, entity_key_hmac varchar(80) NOT NULL,
          created_epoch integer NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_memory_entity_owner UNIQUE (tenant_id,id,relationship_id,user_id,character_id,scope,scenario_key),
          CONSTRAINT uq_memory_entity_identity UNIQUE (tenant_id,relationship_id,entity_type,entity_key_hmac),
          CONSTRAINT fk_memory_entity_relationship_owner FOREIGN KEY (tenant_id,relationship_id,user_id,character_id,scope,scenario_key)
            REFERENCES agata.relationships(tenant_id,id,user_id,character_id,scope,scenario_key),
          CONSTRAINT ck_memory_entity_scope CHECK ((scope='real_world' AND scenario_id IS NULL) OR (scope='roleplay' AND scenario_id IS NOT NULL)),
          CONSTRAINT ck_memory_entity_type CHECK (entity_type IN ('user','person','place','organization','activity','preference','goal','event','relationship','emotion','instruction','locale','timezone')),
          CONSTRAINT ck_memory_entity_epoch CHECK (created_epoch >= 0),
          CONSTRAINT ck_memory_entity_hmac CHECK (entity_key_hmac ~ '^hmac-sha256:v1:[0-9a-f]{64}$')
        );
        CREATE INDEX memory_entities_relationship_key_idx ON agata.memory_entities(tenant_id,relationship_id,entity_key_hmac);
        CREATE TABLE agata.memory_relations (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES agata.tenants(id),
          relationship_id uuid NOT NULL, user_id uuid NOT NULL, character_id uuid NOT NULL,
          scope public.scope_enum NOT NULL, scenario_id uuid,
          scenario_key uuid GENERATED ALWAYS AS (coalesce(scenario_id,'00000000-0000-0000-0000-000000000000'::uuid)) STORED,
          source_entity_id uuid NOT NULL, target_entity_id uuid NOT NULL, relation_type varchar(32) NOT NULL,
          created_epoch integer NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_memory_relation_owner UNIQUE (tenant_id,id,relationship_id,user_id,character_id,scope,scenario_key),
          CONSTRAINT uq_memory_relation_topology UNIQUE (tenant_id,relationship_id,source_entity_id,relation_type,target_entity_id),
          CONSTRAINT fk_memory_relation_relationship_owner FOREIGN KEY (tenant_id,relationship_id,user_id,character_id,scope,scenario_key)
            REFERENCES agata.relationships(tenant_id,id,user_id,character_id,scope,scenario_key),
          CONSTRAINT fk_memory_relation_source_owner FOREIGN KEY (tenant_id,source_entity_id,relationship_id,user_id,character_id,scope,scenario_key)
            REFERENCES agata.memory_entities(tenant_id,id,relationship_id,user_id,character_id,scope,scenario_key),
          CONSTRAINT fk_memory_relation_target_owner FOREIGN KEY (tenant_id,target_entity_id,relationship_id,user_id,character_id,scope,scenario_key)
            REFERENCES agata.memory_entities(tenant_id,id,relationship_id,user_id,character_id,scope,scenario_key),
          CONSTRAINT ck_memory_relation_scope CHECK ((scope='real_world' AND scenario_id IS NULL) OR (scope='roleplay' AND scenario_id IS NOT NULL)),
          CONSTRAINT ck_memory_relation_not_self CHECK (source_entity_id <> target_entity_id),
          CONSTRAINT ck_memory_relation_type CHECK (relation_type IN ('preferred_name','likes','dislikes','hobby','locale','timezone','current_activity','goal','event','relationship','emotion','instruction','works_at','lives_in')),
          CONSTRAINT ck_memory_relation_epoch CHECK (created_epoch >= 0)
        );
        CREATE INDEX memory_relations_source_idx ON agata.memory_relations(tenant_id,relationship_id,source_entity_id);
        CREATE INDEX memory_relations_target_idx ON agata.memory_relations(tenant_id,relationship_id,target_entity_id);
        CREATE TABLE agata.memory_graph_evidence (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES agata.tenants(id),
          relationship_id uuid NOT NULL, user_id uuid NOT NULL, character_id uuid NOT NULL,
          scope public.scope_enum NOT NULL, scenario_id uuid,
          scenario_key uuid GENERATED ALWAYS AS (coalesce(scenario_id,'00000000-0000-0000-0000-000000000000'::uuid)) STORED,
          memory_id uuid NOT NULL, memory_version integer NOT NULL, entity_id uuid, relation_id uuid,
          evidence_role varchar(24) NOT NULL, created_epoch integer NOT NULL, invalidated_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_memory_graph_evidence_owner UNIQUE (tenant_id,id,relationship_id,user_id,character_id,scope,scenario_key),
          CONSTRAINT fk_memory_graph_evidence_exact_memory FOREIGN KEY (tenant_id,memory_id,memory_version,relationship_id,user_id,character_id,scope,scenario_key)
            REFERENCES agata.memories(tenant_id,id,version,relationship_id,user_id,character_id,scope,scenario_key),
          CONSTRAINT fk_memory_graph_evidence_entity_owner FOREIGN KEY (tenant_id,entity_id,relationship_id,user_id,character_id,scope,scenario_key)
            REFERENCES agata.memory_entities(tenant_id,id,relationship_id,user_id,character_id,scope,scenario_key),
          CONSTRAINT fk_memory_graph_evidence_relation_owner FOREIGN KEY (tenant_id,relation_id,relationship_id,user_id,character_id,scope,scenario_key)
            REFERENCES agata.memory_relations(tenant_id,id,relationship_id,user_id,character_id,scope,scenario_key),
          CONSTRAINT ck_memory_graph_evidence_scope CHECK ((scope='real_world' AND scenario_id IS NULL) OR (scope='roleplay' AND scenario_id IS NOT NULL)),
          CONSTRAINT ck_memory_graph_evidence_revision_epoch CHECK (memory_version >= 1 AND created_epoch >= 0),
          CONSTRAINT ck_memory_graph_evidence_target_role CHECK ((entity_id IS NOT NULL AND relation_id IS NULL AND evidence_role='defines_entity') OR (entity_id IS NULL AND relation_id IS NOT NULL AND evidence_role='supports_relation')),
          CONSTRAINT ck_memory_graph_evidence_invalidation_time CHECK (invalidated_at IS NULL OR invalidated_at >= created_at)
        );
        CREATE INDEX memory_graph_evidence_memory_idx ON agata.memory_graph_evidence(tenant_id,relationship_id,memory_id);
        CREATE INDEX memory_graph_evidence_relation_idx ON agata.memory_graph_evidence(tenant_id,relationship_id,relation_id) WHERE relation_id IS NOT NULL;
        CREATE INDEX memory_graph_evidence_entity_idx ON agata.memory_graph_evidence(tenant_id,relationship_id,entity_id) WHERE entity_id IS NOT NULL;
        CREATE UNIQUE INDEX uq_memory_graph_evidence_entity ON agata.memory_graph_evidence(tenant_id,memory_id,memory_version,entity_id) WHERE entity_id IS NOT NULL;
        CREATE UNIQUE INDEX uq_memory_graph_evidence_relation ON agata.memory_graph_evidence(tenant_id,memory_id,memory_version,relation_id) WHERE relation_id IS NOT NULL;
    """)
    op.execute("ALTER TABLE agata.outbox_jobs DROP CONSTRAINT IF EXISTS ck_outbox_job_type_allowlist")
    op.execute("ALTER TABLE agata.outbox_jobs ADD CONSTRAINT ck_outbox_job_type_allowlist CHECK (job_type IN ('generic','conversation_summary','relationship_note_refresh','snapshot_refresh','memory_extract','memory_promote','memory_embed','memory_graph_sync','response_retry'))")
    runtime_role = os.environ.get("AGATA_RUNTIME_DB_ROLE")
    if runtime_role:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", runtime_role):
            raise RuntimeError("AGATA_RUNTIME_DB_ROLE must be a simple PostgreSQL role name")
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON agata.memory_entities, agata.memory_relations, agata.memory_graph_evidence TO "{runtime_role}"')
    for table in ("memory_entities", "memory_relations", "memory_graph_evidence"):
        op.execute(f"ALTER TABLE agata.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE agata.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='agata' AND tablename='{table}' AND policyname='{table}_tenant_isolation') THEN CREATE POLICY {table}_tenant_isolation ON agata.{table} USING (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid); END IF; END $$")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agata.memory_graph_evidence")
    op.execute("DROP TABLE IF EXISTS agata.memory_relations")
    op.execute("DROP TABLE IF EXISTS agata.memory_entities")
    op.execute("ALTER TABLE agata.outbox_jobs DROP CONSTRAINT ck_outbox_job_type_allowlist")
    op.execute("ALTER TABLE agata.outbox_jobs ADD CONSTRAINT ck_outbox_job_type_allowlist CHECK (job_type IN ('generic','conversation_summary','relationship_note_refresh','snapshot_refresh','memory_extract','memory_promote','memory_embed','response_retry'))")
    op.drop_constraint("uq_memory_exact_revision_owner_scope", "memories", schema="agata", type_="unique")
