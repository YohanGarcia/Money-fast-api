"""payroll from Caja: administrator authorizes, cashier executes (FM-FUNCTIONAL-018-R1)

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-14 14:00:00.000000

Additive over 0024. ``payroll_payments`` gains ``status`` (authorized | paid | cancelled, every existing row is
``paid``), ``executed_by`` / ``executed_at`` and ``cancelled_by`` / ``cancelled_at``. An authorized payment is a CASH
payment with no movement yet: the cashier executes it from its own open session. No row is rewritten.

Downgrade is LOSSLESS ONLY: it refuses (before any change) while an authorized or cancelled payment exists.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0025'
down_revision: Union[str, Sequence[str], None] = '0024'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('payroll_payments', sa.Column('status', sa.String(length=20), server_default='paid', nullable=False))
    op.add_column('payroll_payments', sa.Column('executed_by', sa.Integer(), nullable=True))
    op.add_column('payroll_payments', sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('payroll_payments', sa.Column('cancelled_by', sa.Integer(), nullable=True))
    op.add_column('payroll_payments', sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(op.f('fk_payroll_payments_executed_by_users'), 'payroll_payments', 'users', ['executed_by'], ['id'])
    op.create_foreign_key(op.f('fk_payroll_payments_cancelled_by_users'), 'payroll_payments', 'users', ['cancelled_by'], ['id'])
    op.create_check_constraint(op.f('ck_payroll_payments_status_valid'), 'payroll_payments',
                               "status IN ('authorized', 'paid', 'cancelled')")
    op.create_check_constraint(op.f('ck_payroll_payments_authorized_is_cash_without_movement'), 'payroll_payments',
                               "status <> 'authorized' OR (source = 'cash' AND cash_movement_id IS NULL)")


def downgrade() -> None:
    bind = op.get_bind()
    n = bind.execute(sa.text("SELECT count(*) FROM payroll_payments WHERE status <> 'paid'")).scalar()
    if n:
        raise RuntimeError(f"cannot downgrade 0025: {n} authorized/cancelled payroll payment(s) exist")
    op.drop_constraint(op.f('ck_payroll_payments_authorized_is_cash_without_movement'), 'payroll_payments', type_='check')
    op.drop_constraint(op.f('ck_payroll_payments_status_valid'), 'payroll_payments', type_='check')
    op.drop_constraint(op.f('fk_payroll_payments_executed_by_users'), 'payroll_payments', type_='foreignkey')
    op.drop_constraint(op.f('fk_payroll_payments_cancelled_by_users'), 'payroll_payments', type_='foreignkey')
    for col in ('cancelled_at', 'cancelled_by', 'executed_at', 'executed_by', 'status'):
        op.drop_column('payroll_payments', col)
