"""add company bank accounts

Revision ID: 3c4d5e6f7a8b
Revises: 24b3c4d5e6f7
Create Date: 2026-09-20

Adds the ``bank_accounts`` table (company-wide accounts customers can transfer
to, managed by an admin) and a nullable ``bank_account_id`` foreign key on
``cash_transfers`` so each counter transfer records which registered account
received it. Purely additive; existing transfers keep their free-text
``destination`` and a null ``bank_account_id``.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "3c4d5e6f7a8b"
down_revision: Union[str, Sequence[str], None] = "24b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "bank_accounts" not in tables:
        op.create_table(
            "bank_accounts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False, index=True),
            sa.Column("bank_name", sa.String(length=120), nullable=False),
            sa.Column("account_number", sa.String(length=60), nullable=False),
            sa.Column("account_holder", sa.String(length=140), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    transfer_columns = {column["name"] for column in inspector.get_columns("cash_transfers")}
    if "bank_account_id" not in transfer_columns:
        with op.batch_alter_table("cash_transfers") as batch_op:
            batch_op.add_column(sa.Column("bank_account_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                "fk_cash_transfers_bank_account_id",
                "bank_accounts",
                ["bank_account_id"],
                ["id"],
            )


def downgrade() -> None:
    with op.batch_alter_table("cash_transfers") as batch_op:
        batch_op.drop_constraint("fk_cash_transfers_bank_account_id", type_="foreignkey")
        batch_op.drop_column("bank_account_id")
    op.drop_table("bank_accounts")
