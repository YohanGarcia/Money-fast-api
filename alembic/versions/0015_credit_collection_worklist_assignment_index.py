"""credit collection worklist assignment index

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-03 12:00:00.000000

Only the partial index that serves the T-013 filter ``assignee_id`` / ``assignment=mine`` over the OPEN assignments
(EXPLAIN evidence in docs/T-013-CREDIT-COLLECTION-WORKLIST-ASSIGNMENT-FILTERS.md). No table, column, permission or data.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0015'
down_revision: Union[str, Sequence[str], None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        'ix_credit_collection_assignments_open_assignee',
        'credit_collection_assignments',
        ['tenant_id', 'assignee_user_id', 'loan_id'],
        unique=False,
        postgresql_where=sa.text('ended_at IS NULL'),
    )


def downgrade() -> None:
    """Only drops the index: no data is lost, so no history guard is needed."""
    op.drop_index(
        'ix_credit_collection_assignments_open_assignee',
        table_name='credit_collection_assignments',
        postgresql_where=sa.text('ended_at IS NULL'),
    )
