"""add user live location columns

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-07-13

Stores each user's last known GPS position for live collector tracking
(opt-in from the mobile app).
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, Sequence[str], None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_lat", sa.Numeric(9, 6), nullable=True))
        batch_op.add_column(sa.Column("last_lng", sa.Numeric(9, 6), nullable=True))
        batch_op.add_column(sa.Column("last_location_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("last_location_at")
        batch_op.drop_column("last_lng")
        batch_op.drop_column("last_lat")
