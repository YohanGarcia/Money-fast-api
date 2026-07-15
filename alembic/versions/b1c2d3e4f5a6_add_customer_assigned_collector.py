"""add assigned_collector_id to customers

Revision ID: b1c2d3e4f5a6
Revises: 075a77ebbfb8
Create Date: 2026-07-13

Adds the collector (asesor) assignment so each customer belongs to a
collector's portfolio, enabling per-collector scoping of customers,
loans and payments.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "075a77ebbfb8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("assigned_collector_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            "ix_customers_assigned_collector_id", ["assigned_collector_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_customers_assigned_collector_id_users",
            "users",
            ["assigned_collector_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.drop_constraint("fk_customers_assigned_collector_id_users", type_="foreignkey")
        batch_op.drop_index("ix_customers_assigned_collector_id")
        batch_op.drop_column("assigned_collector_id")
