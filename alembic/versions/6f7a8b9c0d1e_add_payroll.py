"""add payroll config and payments

Revision ID: 6f7a8b9c0d1e
Revises: 5e6f7a8b9c0d
Create Date: 2026-09-22

Adds ``payroll_configs`` (per-employee salary + commission rules) and
``payroll_payments`` (ledger of payments drawn from Caja or Capital).
Purely additive.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "6f7a8b9c0d1e"
down_revision: Union[str, Sequence[str], None] = "5e6f7a8b9c0d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "payroll_configs" not in tables:
        op.create_table(
            "payroll_configs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False, index=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, unique=True, index=True),
            sa.Column("salary_amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
            sa.Column("salary_frequency", sa.String(length=20), nullable=False, server_default="monthly"),
            sa.Column("commissions", sa.JSON(), nullable=False, server_default="[]"),
        )
    if "payroll_payments" not in tables:
        op.create_table(
            "payroll_payments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False, index=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
            sa.Column("amount", sa.Numeric(12, 2), nullable=False),
            sa.Column("source", sa.String(length=20), nullable=False),
            sa.Column("period_start", sa.Date(), nullable=True),
            sa.Column("period_end", sa.Date(), nullable=True),
            sa.Column("salary_part", sa.Numeric(12, 2), nullable=False, server_default="0"),
            sa.Column("commission_part", sa.Numeric(12, 2), nullable=False, server_default="0"),
            sa.Column("breakdown", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("notes", sa.Text(), nullable=False, server_default=""),
            sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("branch_id", sa.Integer(), sa.ForeignKey("branches.id"), nullable=True),
            sa.Column("cash_movement_id", sa.Integer(), sa.ForeignKey("cash_movements.id"), nullable=True),
            sa.Column("capital_movement_id", sa.Integer(), sa.ForeignKey("capital_movements.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )


def downgrade() -> None:
    op.drop_table("payroll_payments")
    op.drop_table("payroll_configs")
