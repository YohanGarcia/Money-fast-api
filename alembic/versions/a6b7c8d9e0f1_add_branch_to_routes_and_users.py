"""add branch_id to routes and users

Revision ID: a6b7c8d9e0f1
Revises: f5a6b7c8d9e0
Create Date: 2026-07-14

Links routes and users (collectors) to a branch so the operation can be
grouped and filtered by branch (sucursal).
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "a6b7c8d9e0f1"
down_revision: Union[str, Sequence[str], None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("routes", schema=None) as batch_op:
        batch_op.add_column(sa.Column("branch_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_routes_branch_id", ["branch_id"], unique=False)
        batch_op.create_foreign_key("fk_routes_branch_id_branches", "branches", ["branch_id"], ["id"])

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("branch_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_users_branch_id", ["branch_id"], unique=False)
        batch_op.create_foreign_key("fk_users_branch_id_branches", "branches", ["branch_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint("fk_users_branch_id_branches", type_="foreignkey")
        batch_op.drop_index("ix_users_branch_id")
        batch_op.drop_column("branch_id")

    with op.batch_alter_table("routes", schema=None) as batch_op:
        batch_op.drop_constraint("fk_routes_branch_id_branches", type_="foreignkey")
        batch_op.drop_index("ix_routes_branch_id")
        batch_op.drop_column("branch_id")
