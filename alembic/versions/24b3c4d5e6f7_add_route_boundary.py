"""add route boundary polygon

Revision ID: 24b3c4d5e6f7
Revises: 13a2b3c4d5e6
Create Date: 2026-09-18

Adds a JSON ``boundary`` column to ``routes`` -- a list of [lat, lng] points
forming the polygon an admin draws on the map to define the zone a route
covers. Used by /routes/suggest as one more (still suggestion-only) signal:
if a customer's GPS point falls inside the polygon, that route is suggested.
Purely additive; existing routes get an empty boundary ("[]").
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "24b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "13a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("routes")}
    if "boundary" in existing:
        return
    with op.batch_alter_table("routes") as batch_op:
        batch_op.add_column(
            sa.Column("boundary", sa.JSON(), nullable=False, server_default="[]")
        )


def downgrade() -> None:
    with op.batch_alter_table("routes") as batch_op:
        batch_op.drop_column("boundary")
