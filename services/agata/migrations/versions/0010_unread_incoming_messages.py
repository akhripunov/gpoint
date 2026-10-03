"""Add incoming read accounting; old history is an accounting baseline, not view evidence.

Revision ID: agata0010
Revises: agata0009
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text

revision = 'agata0010'
down_revision = 'agata0009'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {c['name'] for c in inspect(bind).get_columns('messages', schema='agata')}
    if 'incoming_read_at' not in columns:
        op.add_column('messages', sa.Column('incoming_read_at', sa.DateTime(timezone=True), nullable=True), schema='agata')
    # Serialize baseline with ingestion until this migration commits. RLS stays
    # enabled: explicitly enter each tenant, including non-default tenants.
    bind.execute(text('LOCK TABLE agata.messages IN SHARE ROW EXCLUSIVE MODE'))
    prior = bind.scalar(text("SELECT current_setting('app.tenant_id', true)"))
    try:
        for tenant in bind.scalars(text('SELECT id FROM agata.tenants')):
            bind.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {'tenant': str(tenant)})
            bind.execute(text("""
                UPDATE agata.messages AS m SET incoming_read_at = transaction_timestamp()
                FROM agata.conversations AS c
                WHERE m.tenant_id = :tenant AND c.tenant_id = m.tenant_id
                  AND c.id = m.conversation_id AND c.source_system = 'gpoint'
                  AND m.role = 'assistant' AND m.scope = 'real_world'
                  AND m.scenario_id IS NULL AND m.incoming_read_at IS NULL
            """), {'tenant': tenant})
    finally:
        bind.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {'tenant': prior or ''})
    if 'messages_unread_incoming_idx' not in {i['name'] for i in inspect(bind).get_indexes('messages', schema='agata')}:
        op.create_index('messages_unread_incoming_idx', 'messages', ['tenant_id', 'user_id', 'character_id', 'conversation_id'], schema='agata',
                        postgresql_where=text("role = 'assistant' AND incoming_read_at IS NULL AND deleted_at IS NULL AND scope = 'real_world' AND scenario_id IS NULL"))


def downgrade():
    raise RuntimeError('agata0010 is forward-only: downgrade would discard incoming read state')
