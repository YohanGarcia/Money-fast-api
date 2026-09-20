"""add customer address detail fields

Revision ID: 13a2b3c4d5e6
Revises: 02f5c1e35ba4
Create Date: 2026-09-17

Adds structured address-detail columns to customers (province, house
number, building/residencial, apartment, and a free-text reference note)
to support the redesigned customer-registration form. These are purely
additive/nullable and do not change existing sector/calle/barrio/city
semantics -- city continues to hold the municipio value and
sector/barrio continue to feed route-suggestion matching.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "13a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "02f5c1e35ba4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("customers")}
    with op.batch_alter_table("customers") as batch_op:
        if "province" not in existing:
            batch_op.add_column(sa.Column("province", sa.String(120), nullable=True))
        if "house_number" not in existing:
            batch_op.add_column(sa.Column("house_number", sa.String(60), nullable=True))
        if "building" not in existing:
            batch_op.add_column(sa.Column("building", sa.String(160), nullable=True))
        if "apartment" not in existing:
            batch_op.add_column(sa.Column("apartment", sa.String(60), nullable=True))
        if "reference_note" not in existing:
            batch_op.add_column(sa.Column("reference_note", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("customers") as batch_op:
        batch_op.drop_column("reference_note")
        batch_op.drop_column("apartment")
        batch_op.drop_column("building")
        batch_op.drop_column("house_number")
        batch_op.drop_column("province")
