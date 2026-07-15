"""add location_pings table

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-07-13

Archives collector GPS positions over time so the admin can review the route
a collector actually traveled on a given day.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "e4f5a6b7c8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "location_pings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("latitude", sa.Numeric(9, 6), nullable=False),
        sa.Column("longitude", sa.Numeric(9, 6), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_location_pings_user_id", "location_pings", ["user_id"], unique=False)
    op.create_index("ix_location_pings_recorded_at", "location_pings", ["recorded_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_location_pings_recorded_at", table_name="location_pings")
    op.drop_index("ix_location_pings_user_id", table_name="location_pings")
    op.drop_table("location_pings")
