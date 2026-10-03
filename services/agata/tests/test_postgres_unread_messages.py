"""Unread persistence, migration and later HTTP/accounting regressions."""
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from app.db.models import Message

ROOT = Path(__file__).parents[1]
DSN = os.getenv('AGATA_TEST_DATABASE_URL')


def test_incoming_read_state_has_no_default_that_marks_new_messages_read():
    column = Message.__table__.columns.get('incoming_read_at')
    assert column is not None, 'incoming read state must be persisted on the existing Message'
    assert column.nullable
    assert column.server_default is None


@pytest.mark.skipif(not DSN, reason='AGATA_TEST_DATABASE_URL not set')
def test_migration_baselines_existing_messages_across_tenants_without_marking_new_messages():
    # Dedicated database owned by this test; never reset the shared suite database.
    admin = create_engine(DSN, isolation_level='AUTOCOMMIT')
    db = 'unread_migration_' + uuid4().hex + '_agata_test'
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE {db}'))
    from sqlalchemy.engine import make_url
    url = make_url(DSN).set(database=db)
    engine = create_engine(url)
    env = dict(os.environ, AGATA_DATABASE_URL=url.render_as_string(hide_password=False))
    try:
        with engine.begin() as conn:
            conn.execute(text('CREATE EXTENSION vector; CREATE EXTENSION pgcrypto;'))
        def upgrade(rev):
            subprocess.run([sys.executable, '-m', 'alembic', '-c', 'alembic.ini', 'upgrade', rev], cwd=ROOT, env=env, check=True, capture_output=True, text=True)
        upgrade('agata0009')
        tenants = [env['AGATA_TENANT_ID'], str(uuid4())]
        mids = []
        with engine.begin() as conn:
            # Baseline imports ORM metadata; emulate the already deployed old schema.
            conn.execute(text('ALTER TABLE agata.messages DROP COLUMN IF EXISTS incoming_read_at CASCADE'))
            for tenant in tenants:
                u, c, r, conversation, m = [str(uuid4()) for _ in range(5)]
                conn.execute(text("INSERT INTO agata.tenants(id) VALUES (:t) ON CONFLICT DO NOTHING"), {'t':tenant})
                conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {'t':tenant})
                conn.execute(text("INSERT INTO agata.users(id,tenant_id,external_id) VALUES (CAST(:u AS uuid),:t,CAST(:u AS text))"), {'u':u,'t':tenant})
                conn.execute(text("INSERT INTO agata.characters(id,tenant_id,external_id,name) VALUES (CAST(:c AS uuid),:t,CAST(:c AS text),'Fixture')"), {'c':c,'t':tenant})
                conn.execute(text("INSERT INTO agata.relationships(id,tenant_id,user_id,character_id,scope,status,started_at) VALUES (:r,:t,:u,:c,'real_world','ACTIVE',now())"), {'r':r,'t':tenant,'u':u,'c':c})
                conn.execute(text("INSERT INTO agata.conversations(id,tenant_id,user_id,character_id,relationship_id,scope,external_id,started_at) VALUES (:v,:t,:u,:c,:r,'real_world',:external,now())"), {'v':conversation,'t':tenant,'u':u,'c':c,'r':r,'external':f'gpoint-chat:{u}:{c}'})
                conn.execute(text("INSERT INTO agata.messages(id,tenant_id,user_id,character_id,conversation_id,scope,idempotency_key,role,occurred_at) VALUES (CAST(:m AS uuid),:t,:u,:c,:v,'real_world',CAST(:m AS text),'assistant',now())"), {'m':m,'t':tenant,'u':u,'c':c,'v':conversation})
                mids.append(m)
        upgrade('head')
        assert 'incoming_read_at' in {c['name'] for c in inspect(engine).get_columns('messages', schema='agata')}
        with engine.begin() as conn:
            assert conn.scalar(text('SELECT count(*) FROM agata.messages WHERE id = ANY(CAST(:ids AS uuid[])) AND incoming_read_at IS NOT NULL'), {'ids':mids}) == 2
            new = str(uuid4())
            conn.execute(text("INSERT INTO agata.messages(id,tenant_id,user_id,character_id,conversation_id,scope,idempotency_key,role,occurred_at) SELECT CAST(:new AS uuid),tenant_id,user_id,character_id,conversation_id,scope,CAST(:new AS text),'assistant',now() FROM agata.messages WHERE id=:old"), {'new':new,'old':mids[0]})
            assert conn.scalar(text('SELECT incoming_read_at FROM agata.messages WHERE id=:id'), {'id':new}) is None
            assert conn.scalar(text("SELECT count(*) FROM pg_indexes WHERE schemaname='agata' AND indexname='messages_unread_incoming_idx'")) == 1
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE {db} WITH (FORCE)'))
        admin.dispose()
