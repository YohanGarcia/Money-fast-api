"""add structured address parts to company settings

Revision ID: 5e6f7a8b9c0d
Revises: 4d5e6f7a8b9c
Create Date: 2026-09-22

Adds province / municipality / sector / street / house_number / address_reference
to ``company_settings`` so a company address can be captured with the same detail
as a customer. The existing one-line ``address`` stays as the composed version.
Purely additive.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "5e6f7a8b9c0d"
down_revision: Union[str, Sequence[str], None] = "4d5e6f7a8b9c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = [
    ("province", sa.String(length=60)),
    ("municipality", sa.String(length=80)),
    ("sector", sa.String(length=120)),
    ("street", sa.String(length=160)),
    ("house_number", sa.String(length=40)),
    ("address_reference", sa.String(length=255)),
]


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("company_settings")}
    with op.batch_alter_table("company_settings") as batch_op:
        for name, col_type in COLUMNS:
            if name not in existing:
                batch_op.add_column(sa.Column(name, col_type, nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("company_settings") as batch_op:
        for name, _ in COLUMNS:
            batch_op.drop_column(name)
