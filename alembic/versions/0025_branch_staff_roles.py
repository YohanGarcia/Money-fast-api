"""branch manager / notary as real users with branch-scoped roles

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-14 16:00:00.000000

Additive over 0024. ``branches`` gains two nullable references to ``users``: the user holding the "Gerente de
sucursal" role and the one holding the "Notario" role on that branch. The role assignments themselves live in
``user_role_assignments`` (branch scope). ``manager_name`` / ``notary_name`` stay as the displayed name (derived from
the user, or the legacy free text). Nothing is backfilled.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0025'
down_revision: Union[str, Sequence[str], None] = '0024'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('branches', sa.Column('manager_user_id', sa.Integer(), nullable=True))
    op.add_column('branches', sa.Column('notary_user_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_branches_manager_user', 'branches', 'users', ['manager_user_id'], ['id'])
    op.create_foreign_key('fk_branches_notary_user', 'branches', 'users', ['notary_user_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('fk_branches_notary_user', 'branches', type_='foreignkey')
    op.drop_constraint('fk_branches_manager_user', 'branches', type_='foreignkey')
    op.drop_column('branches', 'notary_user_id')
    op.drop_column('branches', 'manager_user_id')
