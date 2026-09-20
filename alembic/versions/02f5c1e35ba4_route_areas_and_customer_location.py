"""route areas and customer structured location

Revision ID: 02f5c1e35ba4
Revises: e0f1a2b3c4d5
Create Date: 2026-09-17

Adds a route_areas table (sector/calle/barrio tags a route covers) and
structured sector/calle/barrio columns on customers, so a route can be
suggested for a customer by matching their address components against the
areas a route declares it covers. The match is a suggestion only -- a
customer's route assignment stays a manual choice either way.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "02f5c1e35ba4"
down_revision: Union[str, Sequence[str], None] = "e0f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "route_areas",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("route_id", sa.Integer(), nullable=False),
        sa.Column("area_type", sa.String(20), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("normalized_name", sa.String(120), nullable=False),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_route_areas_route_id", "route_areas", ["route_id"], unique=False)
    op.create_index(
        "ix_route_areas_normalized_name", "route_areas", ["normalized_name"], unique=False
    )

    with op.batch_alter_table("customers") as batch_op:
        batch_op.add_column(sa.Column("sector", sa.String(120), nullable=True))
        batch_op.add_column(sa.Column("calle", sa.String(120), nullable=True))
        batch_op.add_column(sa.Column("barrio", sa.String(120), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("customers") as batch_op:
        batch_op.drop_column("barrio")
        batch_op.drop_column("calle")
        batch_op.drop_column("sector")

    op.drop_index("ix_route_areas_normalized_name", table_name="route_areas")
    op.drop_index("ix_route_areas_route_id", table_name="route_areas")
    op.drop_table("route_areas")
