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
@pytest.mark.parametrize('restricted_role', [False, True])
def test_migration_baselines_existing_messages_across_tenants_without_marking_new_messages(restricted_role):
    # Dedicated database owned by this test; never reset the shared suite database.
    admin = create_engine(DSN, isolation_level='AUTOCOMMIT')
    db = 'unread_migration_' + uuid4().hex + '_agata_test'
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE {db}'))
    from sqlalchemy.engine import make_url
    url = make_url(DSN).set(database=db)
    engine = create_engine(url)
    migration_url = url
    if restricted_role:
        rls_dsn = os.getenv('AGATA_RLS_TEST_DATABASE_URL')
        assert rls_dsn, 'restricted migration regression requires its dedicated non-bypass role'
        migration_url = make_url(rls_dsn).set(database=db)
        with admin.connect() as conn:
            conn.execute(text(f'ALTER DATABASE {db} OWNER TO {migration_url.username}'))
    env = dict(os.environ, AGATA_DATABASE_URL=migration_url.render_as_string(hide_password=False))
    env.pop('AGATA_MIGRATION_TENANT_IDS', None)
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
        if restricted_role:
            with pytest.raises(subprocess.CalledProcessError) as failure:
                upgrade('head')
            assert 'AGATA_MIGRATION_TENANT_IDS' in failure.value.stderr
            with engine.begin() as conn:
                assert conn.scalar(text('SELECT version_num FROM agata.alembic_version')) == 'agata0009'
            env['AGATA_MIGRATION_TENANT_IDS'] = ','.join(tenants)
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

import asyncio
from datetime import UTC, datetime
from uuid import UUID
import httpx
import pytest_asyncio
from sqlalchemy import select, update
from app.config import get_settings
from app.db.base import SessionFactory, set_tenant_context
from app.db.models import User, Character, Conversation, Relationship, Consent, Scope
from app.main import app
from app.messages.crypto import AeadCrypto
from app.messages.service import MessageIngestionService


@pytest_asyncio.fixture(loop_scope='module')
async def dialogue():
    tenant = UUID(get_settings().tenant_id)
    user, chars, conversations = uuid4(), [uuid4(), uuid4()], [uuid4(), uuid4()]
    external, now = 'unread-' + uuid4().hex, datetime.now(UTC)
    async with SessionFactory() as session, session.begin():
        await set_tenant_context(session, str(tenant))
        session.add(User(id=user, tenant_id=tenant, external_id=external))
        session.add_all([Character(id=c, tenant_id=tenant, external_id=str(c), name='Unread fixture') for c in chars])
        await session.flush()
        session.add(Consent(tenant_id=tenant, user_id=user, purpose='chat_history', granted_at=now, policy_version='test'))
        for char, conv in zip(chars, conversations):
            rel = uuid4()
            session.add(Relationship(id=rel, tenant_id=tenant, user_id=user, character_id=char, scope=Scope.real_world, status='ACTIVE', started_at=now))
            await session.flush()
            session.add(Conversation(id=conv, tenant_id=tenant, user_id=user, character_id=char, relationship_id=rel, scope=Scope.real_world, external_id=f'gpoint-chat:{external}:{char}', started_at=now))
            await session.flush()
    async def add(index=0, role='assistant', deleted=False):
        mid = uuid4()
        crypto = AeadCrypto(encoded_key=get_settings().message_aead_key.get_secret_value(), key_version='v1')
        encrypted = crypto.encrypt('Fixture incoming', MessageIngestionService._aad_for(tenant, user, conversations[index], mid))
        async with SessionFactory() as session, session.begin():
            await set_tenant_context(session, str(tenant))
            session.add(Message(id=mid, tenant_id=tenant, user_id=user, character_id=chars[index], conversation_id=conversations[index], scope=Scope.real_world, idempotency_key=str(mid), role=role, occurred_at=datetime.now(UTC), content_ciphertext=encrypted.ciphertext, content_nonce=encrypted.nonce, content_key_version=encrypted.key_version, deleted_at=now if deleted else None))
        return str(mid)
    mids=[await add(),await add(),await add(1)]
    await add(role='user'); await add(role='system'); await add(deleted=True)
    headers={'authorization':'Bearer '+get_settings().internal_api_token.get_secret_value()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',headers=headers) as client:
        yield client, external, chars, mids, add


@pytest.mark.skipif(not DSN, reason='AGATA_TEST_DATABASE_URL not set')
@pytest.mark.asyncio(loop_scope='module')
async def test_counts_are_incoming_only_and_history_fetch_does_not_mark_read(dialogue):
    client,user,chars,mids,add=dialogue
    result=await client.get(f'/internal/v1/users/{user}/unread')
    assert result.status_code==200,result.text
    assert {i['gpointCharacterId']:i['unreadCount'] for i in result.json()['items']}=={str(chars[0]):2,str(chars[1]):1}
    history=await client.get(f'/internal/v1/users/{user}/characters/{chars[0]}/messages')
    assert history.status_code==200,history.text
    assert all(i['incomingReadAt'] is None for i in history.json()['items'])
    inbox=await client.get(f'/internal/v1/users/{user}/conversations?limit=1')
    assert inbox.status_code==200 and 'unreadCount' in inbox.json()['items'][0]
    assert (await client.get(f'/internal/v1/users/{user}/unread')).json()==result.json()


@pytest.mark.skipif(not DSN, reason='AGATA_TEST_DATABASE_URL not set')
@pytest.mark.asyncio(loop_scope='module')
async def test_explicit_ack_is_idempotent_and_concurrent_arrival_remains_unread(dialogue):
    client,user,chars,mids,add=dialogue
    path=f'/internal/v1/users/{user}/characters/{chars[0]}/messages/read'
    read,new=await asyncio.gather(client.post(path,json={'messageIds':[mids[0],mids[1],mids[0]]}),add())
    assert read.status_code==200,read.text
    assert set(read.json()['acknowledgedMessageIds'])==set(mids[:2])
    async def timestamps():
        async with SessionFactory() as session,session.begin():
            await set_tenant_context(session,get_settings().tenant_id)
            return (await session.scalars(select(Message.incoming_read_at).where(Message.id.in_([UUID(x) for x in mids[:2]])).order_by(Message.id))).all()
    before=await timestamps(); assert all(before)
    assert (await client.post(path,json={'messageIds':mids[:2]})).status_code==200
    assert await timestamps()==before
    summary=(await client.get(f'/internal/v1/users/{user}/unread')).json()
    assert {i['gpointCharacterId']:i['unreadCount'] for i in summary['items']}=={str(chars[0]):1,str(chars[1]):1}


@pytest.mark.skipif(not DSN, reason='AGATA_TEST_DATABASE_URL not set')
@pytest.mark.asyncio(loop_scope='module')
async def test_foreign_batch_cannot_partially_acknowledge_owned_messages(dialogue):
    client,user,chars,mids,add=dialogue
    path=f'/internal/v1/users/{user}/characters/{chars[0]}/messages/read'
    result=await client.post(path,json={'messageIds':[mids[0],mids[2]]})
    assert result.status_code==404,result.text
    async with SessionFactory() as session,session.begin():
        await set_tenant_context(session,get_settings().tenant_id)
        assert await session.scalar(select(Message.incoming_read_at).where(Message.id==UUID(mids[0]))) is None
    for payload in [{'messageIds':[]},{'messageIds':['malformed']},{'messageIds':[str(uuid4()) for _ in range(101)]}]:
        assert (await client.post(path,json=payload)).status_code==422
    assert (await client.post(path,json={'messageIds':mids[:1]},headers={'authorization':'Bearer wrong'})).status_code==401
    assert (await client.post(f'/internal/v1/users/other/characters/{chars[0]}/messages/read',json={'messageIds':mids[:1]})).status_code==404


@pytest.mark.skipif(not DSN, reason='AGATA_TEST_DATABASE_URL not set')
@pytest.mark.asyncio(loop_scope='module')
async def test_unread_requires_consent_and_excludes_inactive_characters(dialogue):
    client,user,chars,mids,add=dialogue
    async with SessionFactory() as session,session.begin():
        await set_tenant_context(session,get_settings().tenant_id)
        char=await session.get(Character,chars[0]); char.status='inactive'
    result=await client.get(f'/internal/v1/users/{user}/unread')
    assert result.status_code==200,result.text
    assert result.json()['items']==[{'gpointCharacterId':str(chars[1]),'unreadCount':1}]
    async with SessionFactory() as session,session.begin():
        await set_tenant_context(session,get_settings().tenant_id)
        owner=await session.scalar(select(User).where(User.external_id==user))
        await session.execute(update(Consent).where(Consent.user_id==owner.id).values(revoked_at=datetime.now(UTC)))
    assert (await client.get(f'/internal/v1/users/{user}/unread')).status_code==403


@pytest.mark.skipif(not DSN, reason='AGATA_TEST_DATABASE_URL not set')
@pytest.mark.asyncio(loop_scope='module')
async def test_summary_is_not_limited_to_the_first_inbox_page(dialogue):
    client,user,chars,mids,add=dialogue
    tenant=UUID(get_settings().tenant_id)
    async with SessionFactory() as session,session.begin():
        await set_tenant_context(session,str(tenant))
        owner=await session.scalar(select(User).where(User.external_id==user))
        for _ in range(33):
            char,rel,conv,mid=[uuid4() for _ in range(4)]
            session.add(Character(id=char,tenant_id=tenant,external_id=str(char),name='Paging'))
            await session.flush()
            session.add(Relationship(id=rel,tenant_id=tenant,user_id=owner.id,character_id=char,scope=Scope.real_world,status='ACTIVE',started_at=datetime.now(UTC)))
            await session.flush()
            session.add(Conversation(id=conv,tenant_id=tenant,user_id=owner.id,character_id=char,relationship_id=rel,scope=Scope.real_world,external_id=f'gpoint-chat:{user}:{char}',started_at=datetime.now(UTC)))
            await session.flush()
            session.add(Message(id=mid,tenant_id=tenant,user_id=owner.id,character_id=char,conversation_id=conv,scope=Scope.real_world,idempotency_key=str(mid),role='assistant',occurred_at=datetime.now(UTC)))
    result=await client.get(f'/internal/v1/users/{user}/unread')
    assert result.status_code==200,result.text
    assert len(result.json()['items'])==35
    assert sum(i['unreadCount'] for i in result.json()['items'])==36
    # A separate HTTP client/new session gets the same persisted state.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://reopen',headers=client.headers) as fresh:
        assert (await fresh.get(f'/internal/v1/users/{user}/unread')).json()==result.json()

@pytest.mark.skipif(not DSN, reason='AGATA_TEST_DATABASE_URL not set')
@pytest.mark.asyncio(loop_scope='module')
async def test_matching_external_id_in_another_tenant_cannot_leak_or_partially_ack(dialogue):
    from app.db.models import Tenant
    client,user,chars,mids,add=dialogue
    tenant,u,char,rel,conv,foreign=[uuid4() for _ in range(6)]
    now=datetime.now(UTC)
    async with SessionFactory() as session,session.begin():
        await set_tenant_context(session,str(tenant))
        session.add(Tenant(id=tenant));await session.flush()
        session.add(User(id=u,tenant_id=tenant,external_id=user))
        session.add(Character(id=char,tenant_id=tenant,external_id=str(chars[0]),name='Other tenant'))
        await session.flush()
        session.add(Relationship(id=rel,tenant_id=tenant,user_id=u,character_id=char,scope=Scope.real_world,status='ACTIVE',started_at=now));await session.flush()
        session.add(Conversation(id=conv,tenant_id=tenant,user_id=u,character_id=char,relationship_id=rel,scope=Scope.real_world,external_id=f'gpoint-chat:{user}:{chars[0]}',started_at=now));await session.flush()
        session.add(Message(id=foreign,tenant_id=tenant,user_id=u,character_id=char,conversation_id=conv,scope=Scope.real_world,idempotency_key=str(foreign),role='assistant',occurred_at=now))
    summary=await client.get(f'/internal/v1/users/{user}/unread')
    assert summary.status_code==200,summary.text
    assert sum(row['unreadCount'] for row in summary.json()['items'])==3
    ack=await client.post(f'/internal/v1/users/{user}/characters/{chars[0]}/messages/read',json={'messageIds':[mids[0],str(foreign)]})
    assert ack.status_code==404,ack.text
    async with SessionFactory() as session,session.begin():
        await set_tenant_context(session,get_settings().tenant_id)
        assert await session.scalar(select(Message.incoming_read_at).where(Message.id==UUID(mids[0]))) is None
        await set_tenant_context(session,str(tenant))
        assert await session.scalar(select(Message.incoming_read_at).where(Message.id==foreign)) is None
