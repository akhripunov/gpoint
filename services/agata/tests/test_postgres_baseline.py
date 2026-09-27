"""Explicit integration test for a disposable, initially empty AGATA test database."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.messages.crypto import AeadCrypto
from app.messages.protected_content import (
    content_aad,
    decrypt_json_from_storage,
    decrypt_text_from_storage,
    memory_snapshot_content_aad,
    memory_value_aad,
    segment_keyphrases_aad,
    segment_summary_aad,
)

ROOT = Path(__file__).parents[1]
DATABASE_URL = os.getenv("AGATA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AGATA_TEST_DATABASE_URL not set")


def test_empty_database_baseline_only_adds_agata_schema() -> None:
    assert DATABASE_URL is not None
    url = make_url(DATABASE_URL)
    assert url.database and url.database.endswith("_agata_test"), (
        "refusing migration outside an explicitly disposable *_agata_test database"
    )
    sync_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    engine = create_engine(sync_url)
    with engine.connect() as connection:
        before = set(
            connection.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
        )
        assert (
            connection.scalar(
                text("SELECT 1 FROM information_schema.schemata WHERE schema_name='agata'")
            )
            is None
        ), "test database must begin without schema agata"

    env = dict(os.environ)
    env["AGATA_DATABASE_URL"] = DATABASE_URL
    env.setdefault("AGATA_TENANT_ID", "7508f36f-774a-48aa-8c6b-42f6f160415d")
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "agata0001"],
        cwd=ROOT,
        env=env,
        check=True,
        timeout=60,
        capture_output=True,
        text=True,
    )

    # The baseline migration imports current ORM metadata. Strip only the P2.1
    # relationship layer here to reproduce an agata0002-era disposable schema.
    with engine.begin() as connection:
        for table in (
            "conversations",
            "memory_control_state",
            "memories",
            "suppression_rules",
            "retrieval_runs",
            "retrieval_candidates",
            "response_runs",
            "response_memory_refs",
            "shared_history_episodes",
            "shadow_memory_candidates",
        ):
            connection.execute(
                text(f"ALTER TABLE agata.{table} DROP COLUMN IF EXISTS relationship_id CASCADE")
            )
        connection.execute(text("DROP TABLE IF EXISTS agata.relationships CASCADE"))
        connection.execute(text("DROP TABLE IF EXISTS agata.memory_snapshots CASCADE"))
        for column in ("snapshot_id", "snapshot_generation", "ref_type"):
            connection.execute(text(f"ALTER TABLE agata.response_memory_refs DROP COLUMN IF EXISTS {column} CASCADE"))
        connection.execute(text("ALTER TABLE agata.response_memory_refs ALTER COLUMN memory_id SET NOT NULL"))
        connection.execute(text("ALTER TABLE agata.response_memory_refs ALTER COLUMN memory_version SET NOT NULL"))
        connection.execute(text("""
            CREATE TABLE agata.derived_memory_views (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id uuid NOT NULL,
                user_id uuid NOT NULL,
                character_id uuid NOT NULL,
                scope public.scope_enum NOT NULL,
                scenario_id uuid NULL,
                view_type varchar(64) NOT NULL,
                generation integer NOT NULL DEFAULT 1,
                content jsonb NOT NULL,
                parent_memory_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
                creation_epoch integer NOT NULL DEFAULT 0,
                created_at timestamptz NOT NULL DEFAULT now()
            )
        """))
        connection.execute(text("""
            ALTER TABLE agata.response_memory_refs
            ADD CONSTRAINT fk_response_memory_owner FOREIGN KEY
            (tenant_id,memory_id,user_id,character_id,scope,scenario_key)
            REFERENCES agata.memories(tenant_id,id,user_id,character_id,scope,scenario_key)
        """))

    tenant_id = env["AGATA_TENANT_ID"]
    user_id, character_id, relationship_id, conversation_id, memory_id, segment_id, view_id = (
        uuid4() for _ in range(7)
    )
    sentinel = "legacy-private-synthetic-payload"
    with engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id}
        )
        connection.execute(
            text(
                "INSERT INTO agata.users(id,tenant_id,external_id) "
                "VALUES (:id,:tenant,:external)"
            ),
            {"id": user_id, "tenant": tenant_id, "external": f"migration-{user_id}"},
        )
        connection.execute(
            text(
                "INSERT INTO agata.characters(id,tenant_id,external_id,name) "
                "VALUES (:id,:tenant,:external,'Synthetic migration character')"
            ),
            {"id": character_id, "tenant": tenant_id, "external": f"migration-{character_id}"},
        )
        connection.execute(
            text(
                "INSERT INTO agata.conversations(id,tenant_id,user_id,character_id,scope,scenario_id,started_at) "
                "VALUES (:id,:tenant,:user,:character,CAST('real_world' AS public.scope_enum),NULL,now())"
            ),
            {
                "id": conversation_id,
                "tenant": tenant_id,
                "user": user_id,
                "character": character_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO agata.memories(id,tenant_id,user_id,character_id,kind,scope,scenario_id,"
                "slot_key,predicate,value_text,value_json,search_text,observed_at,evidence_state,"
                "lifecycle_state,confidence,importance) VALUES (:id,:tenant,:user,:character,'semantic_fact',"
                "CAST('real_world' AS public.scope_enum),NULL,'legacy.slot','likes',:value_text,"
                "CAST(:value_json AS jsonb),:search_text,now(),'confirmed','active',0.8,0.5)"
            ),
            {
                "id": memory_id,
                "tenant": tenant_id,
                "user": user_id,
                "character": character_id,
                "value_text": sentinel,
                "value_json": '{"note":"legacy-private-synthetic-payload"}',
                "search_text": sentinel,
            },
        )
        connection.execute(
            text(
                "INSERT INTO agata.conversation_segments(id,tenant_id,conversation_id,started_at,ended_at,summary,keyphrases) "
                "VALUES (:id,:tenant,:conversation,now()-interval '2 minutes',now(),:summary,CAST(:phrases AS jsonb))"
            ),
            {
                "id": segment_id,
                "tenant": tenant_id,
                "conversation": conversation_id,
                "summary": sentinel,
                "phrases": '["legacy-private-synthetic-payload"]',
            },
        )
        connection.execute(
            text(
                "INSERT INTO agata.derived_memory_views(id,tenant_id,user_id,character_id,scope,scenario_id,"
                "view_type,content,parent_memory_ids) VALUES (:id,:tenant,:user,:character,"
                "CAST('real_world' AS public.scope_enum),NULL,'conversation_summary',"
                "CAST(:content AS jsonb),CAST(:parent_ids AS jsonb))"
            ),
            {
                "id": view_id,
                "tenant": tenant_id,
                "user": user_id,
                "character": character_id,
                "content": '{"summary":"legacy-private-synthetic-payload"}',
                "parent_ids": f'["{segment_id}"]',
            },
        )

    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "agata0002"],
        cwd=ROOT,
        env=env,
        check=True,
        timeout=60,
        capture_output=True,
        text=True,
    )
    with engine.connect() as connection:
        legacy_revision = connection.scalar(text("SELECT version_num FROM agata.alembic_version"))
        assert legacy_revision == "agata0002"

    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "agata0005"],
        cwd=ROOT,
        env=env,
        check=True,
        timeout=60,
        capture_output=True,
        text=True,
    )

    with engine.connect() as connection:
        after = set(
            connection.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
        )
        agata_tables = set(
            connection.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='agata'"))
        )
        assert after == before
        assert "alembic_version" in agata_tables
        assert (
            connection.scalar(text("SELECT version_num FROM agata.alembic_version")) == "agata0005"
        )
        assert connection.scalar(text("SELECT count(*) FROM agata.tenants")) == 1
        assert (
            connection.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='vector'")) == 1
        )
        assert (
            connection.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='pgcrypto'"))
            == 1
        )
        assert (
            connection.scalar(text("SELECT count(*) FROM pg_policies WHERE schemaname='agata'"))
            == len(agata_tables) - 1
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname='agata' AND c.relrowsecurity AND c.relforcerowsecurity"
                )
            )
            == len(agata_tables) - 1
        )
        message_guard = connection.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='ck_message_content_envelope'"
            )
        )
        shadow_guard = connection.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='ck_shadow_candidate_envelope'"
            )
        )
        assert message_guard and "content_nonce IS NOT NULL" in message_guard
        assert message_guard and "content_key_version IS NOT NULL" in message_guard
        assert shadow_guard and "value_nonce IS NOT NULL" in shadow_guard
        assert shadow_guard and "value_key_version IS NOT NULL" in shadow_guard
        backfilled_relationship_id = connection.scalar(
            text(
                "SELECT id FROM agata.relationships WHERE tenant_id=:tenant AND user_id=:user "
                "AND character_id=:character AND scope='real_world' AND scenario_id IS NULL"
            ),
            {"tenant": tenant_id, "user": user_id, "character": character_id},
        )
        assert backfilled_relationship_id is not None
        migrated_memory = connection.execute(
            text("SELECT * FROM agata.memories WHERE id=:id"),
            {"id": memory_id},
        ).one()
        assert migrated_memory.relationship_id == backfilled_relationship_id
        assert migrated_memory.value_text.startswith("agata$")
        assert migrated_memory.value_json["_agata_ciphertext"] == "1"
        assert sentinel not in migrated_memory.search_text
        migrated_segment = connection.execute(
            text("SELECT * FROM agata.conversation_segments WHERE id=:id"),
            {"id": segment_id},
        ).one()
        assert migrated_segment.summary.startswith("agata$")
        assert migrated_segment.keyphrases["_agata_ciphertext"] == "1"
        migrated_view = connection.execute(
            text("SELECT * FROM agata.memory_snapshots WHERE id=:id"),
            {"id": view_id},
        ).one()
        assert migrated_view.relationship_id == backfilled_relationship_id
        assert migrated_view.content["_agata_ciphertext"] == "1"
        assert connection.scalar(text("SELECT to_regclass('agata.derived_memory_views')")) is None
        assert migrated_view.snapshot_type == "conversation_summary"
        assert migrated_view.source_segment_ids == [str(segment_id)]
        crypto = AeadCrypto(
            encoded_key=os.environ["AGATA_MESSAGE_AEAD_KEY"],
            key_version=os.getenv("AGATA_MESSAGE_AEAD_KEY_VERSION", "v1"),
        )
        memory_owner = SimpleNamespace(
            tenant_id=UUID(tenant_id),
            user_id=user_id,
            character_id=character_id,
            scope="real_world",
            scenario_id=None,
            id=memory_id,
        )
        conversation_owner = SimpleNamespace(
            tenant_id=UUID(tenant_id),
            user_id=user_id,
            character_id=character_id,
            scope="real_world",
            scenario_id=None,
        )
        assert decrypt_text_from_storage(
            migrated_memory.value_text, memory_value_aad(memory_owner), crypto=crypto
        ) == sentinel
        assert decrypt_json_from_storage(
            migrated_memory.value_json,
            content_aad(
                entity="memories",
                tenant_id=UUID(tenant_id),
                user_id=user_id,
                character_id=character_id,
                scope="real_world",
                scenario_id=None,
                record_id=memory_id,
                field="value_json",
            ),
            crypto=crypto,
        ) == {"note": sentinel}
        segment_owner = SimpleNamespace(
            tenant_id=UUID(tenant_id), id=segment_id
        )
        assert decrypt_text_from_storage(
            migrated_segment.summary,
            segment_summary_aad(segment_owner, conversation_owner),
            crypto=crypto,
        ) == sentinel
        assert decrypt_json_from_storage(
            migrated_segment.keyphrases,
            segment_keyphrases_aad(segment_owner, conversation_owner),
            crypto=crypto,
        ) == [sentinel]
        view_owner = SimpleNamespace(
            tenant_id=UUID(tenant_id), user_id=user_id, character_id=character_id,
            scope="real_world", scenario_id=None, id=view_id,
            snapshot_type=migrated_view.snapshot_type,
            snapshot_key=migrated_view.snapshot_key,
            generation=migrated_view.generation,
        )
        assert decrypt_json_from_storage(
            migrated_view.content, memory_snapshot_content_aad(view_owner), crypto=crypto
        ) == {"summary": sentinel}
        assert connection.scalar(
            text("SELECT count(*) FROM agata.retrieval_candidates WHERE relationship_id IS NULL")
        ) == 0
        assert connection.scalar(text("SELECT count(*) FROM agata.relationships")) == 1
        relationship_id = connection.scalar(
            text("SELECT relationship_id FROM agata.conversations WHERE id=:id"),
            {"id": conversation_id},
        )
        assert relationship_id is not None
        assert connection.scalar(
            text("SELECT relationship_id FROM agata.memories WHERE id=:id"), {"id": memory_id}
        ) == relationship_id
        assert connection.scalar(
            text("SELECT count(*) FROM agata.relationships WHERE id=:id AND status='ACTIVE'"),
            {"id": relationship_id},
        ) == 1
    engine.dispose()
