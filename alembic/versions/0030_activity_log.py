"""activity log: searchable columns on security_events

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-15 18:00:00.000000

Additive over 0029. ``security_events`` stays append-only (the trigger is untouched, so old rows are NOT
backfilled: queries derive the category of an old row from its ``event_type`` prefix). New rows carry the
category, the actor's role and branch, the affected entity and a Spanish summary so the company can search and
filter its own activity.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0030'
down_revision: Union[str, Sequence[str], None] = '0029'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('security_events', sa.Column('category', sa.String(30), nullable=True))
    op.add_column('security_events', sa.Column('actor_role', sa.String(20), nullable=True))
    op.add_column('security_events', sa.Column('branch_id', sa.Integer(), nullable=True))
    op.add_column('security_events', sa.Column('entity_type', sa.String(40), nullable=True))
    op.add_column('security_events', sa.Column('entity_id', sa.String(40), nullable=True))
    op.add_column('security_events', sa.Column('summary', sa.String(240), nullable=True))
    op.create_index('ix_security_events_tenant_category', 'security_events', ['tenant_id', 'category', 'occurred_at'])
    op.create_index('ix_security_events_tenant_actor', 'security_events', ['tenant_id', 'actor_id', 'occurred_at'])


def downgrade() -> None:
    op.drop_index('ix_security_events_tenant_actor', table_name='security_events')
    op.drop_index('ix_security_events_tenant_category', table_name='security_events')
    for col in ('summary', 'entity_id', 'entity_type', 'branch_id', 'actor_role', 'category'):
        op.drop_column('security_events', col)
