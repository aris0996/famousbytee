"""add provider metadata to notification history

Revision ID: f2a1c9d3e7b4
Revises: d83b36d7a9eb
"""

from alembic import op
import sqlalchemy as sa


revision = 'f2a1c9d3e7b4'
down_revision = 'd83b36d7a9eb'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {column['name'] for column in inspector.get_columns('notification_history')}
    existing_indexes = {index['name'] for index in inspector.get_indexes('notification_history')}
    with op.batch_alter_table('notification_history', schema=None) as batch_op:
        if 'provider' not in existing_columns:
            batch_op.add_column(sa.Column('provider', sa.String(length=30), nullable=True))
        if 'provider_message_id' not in existing_columns:
            batch_op.add_column(sa.Column('provider_message_id', sa.String(length=120), nullable=True))
        if 'webhook_event_id' not in existing_columns:
            batch_op.add_column(sa.Column('webhook_event_id', sa.String(length=120), nullable=True))
        if 'ix_notification_history_provider_message_id' not in existing_indexes:
            batch_op.create_index('ix_notification_history_provider_message_id', ['provider_message_id'], unique=False)
        if 'ix_notification_history_webhook_event_id' not in existing_indexes:
            batch_op.create_index('ix_notification_history_webhook_event_id', ['webhook_event_id'], unique=False)


def downgrade():
    with op.batch_alter_table('notification_history', schema=None) as batch_op:
        batch_op.drop_index('ix_notification_history_webhook_event_id')
        batch_op.drop_index('ix_notification_history_provider_message_id')
        batch_op.drop_column('webhook_event_id')
        batch_op.drop_column('provider_message_id')
        batch_op.drop_column('provider')
