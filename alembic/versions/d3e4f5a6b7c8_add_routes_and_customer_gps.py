"""add routes table and customer route/gps columns

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-07-13

Introduces collection routes (Ruta): a customer belongs to a route, and the
route carries the assigned collector. Also adds GPS coordinates per customer
for navigation ("Cómo llegar").
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, Sequence[str], None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "routes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("zone", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("assigned_collector_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["assigned_collector_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_routes_name", "routes", ["name"], unique=False)
    op.create_index("ix_routes_company_id", "routes", ["company_id"], unique=False)
    op.create_index("ix_routes_assigned_collector_id", "routes", ["assigned_collector_id"], unique=False)

    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("route_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("latitude", sa.Numeric(9, 6), nullable=True))
        batch_op.add_column(sa.Column("longitude", sa.Numeric(9, 6), nullable=True))
        batch_op.create_index("ix_customers_route_id", ["route_id"], unique=False)
        batch_op.create_foreign_key("fk_customers_route_id_routes", "routes", ["route_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.drop_constraint("fk_customers_route_id_routes", type_="foreignkey")
        batch_op.drop_index("ix_customers_route_id")
        batch_op.drop_column("longitude")
        batch_op.drop_column("latitude")
        batch_op.drop_column("route_id")

    op.drop_index("ix_routes_assigned_collector_id", table_name="routes")
    op.drop_index("ix_routes_company_id", table_name="routes")
    op.drop_index("ix_routes_name", table_name="routes")
    op.drop_table("routes")
