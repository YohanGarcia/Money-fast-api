"""cash addition requests: the administration asks, the cashier confirms

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-14 18:00:00.000000

Additive over 0025. ``cash_addition_requests`` holds money the administration sends to (``in``) or takes from
(``out``) a cashier's box. Nothing moves when it is requested; the cash movement is written only when the cashier
confirms it (``movement_id`` is set exactly when ``state = 'confirmed'``). No existing row is touched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0026'
down_revision: Union[str, Sequence[str], None] = '0025'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'cash_addition_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('box_id', sa.Integer(), nullable=False),
        sa.Column('cashier_id', sa.Integer(), nullable=False),
        sa.Column('direction', sa.String(length=3), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('notes', sa.Text(), nullable=False),
        sa.Column('reference', sa.String(length=160), nullable=False),
        sa.Column('state', sa.String(length=12), nullable=False),
        sa.Column('requested_by', sa.Integer(), nullable=False),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('resolved_by', sa.Integer(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolution_note', sa.Text(), nullable=False),
        sa.Column('movement_id', sa.Integer(), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.CheckConstraint("direction IN ('in', 'out')", name=op.f('ck_cash_addition_requests_direction_valid')),
        sa.CheckConstraint("state IN ('pending', 'confirmed', 'rejected', 'cancelled')", name=op.f('ck_cash_addition_requests_state_valid')),
        sa.CheckConstraint('amount > 0', name=op.f('ck_cash_addition_requests_amount_positive')),
        sa.CheckConstraint("(state = 'confirmed') = (movement_id IS NOT NULL)", name=op.f('ck_cash_addition_requests_movement_iff_confirmed')),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], name=op.f('fk_cash_addition_requests_company_id_companies')),
        sa.ForeignKeyConstraint(['box_id'], ['cash_boxes.id'], name=op.f('fk_cash_addition_requests_box_id_cash_boxes')),
        sa.ForeignKeyConstraint(['cashier_id'], ['users.id'], name=op.f('fk_cash_addition_requests_cashier_id_users')),
        sa.ForeignKeyConstraint(['requested_by'], ['users.id'], name=op.f('fk_cash_addition_requests_requested_by_users')),
        sa.ForeignKeyConstraint(['resolved_by'], ['users.id'], name=op.f('fk_cash_addition_requests_resolved_by_users')),
        sa.ForeignKeyConstraint(['movement_id'], ['cash_movements.id'], name=op.f('fk_cash_addition_requests_movement_id_cash_movements')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_cash_addition_requests')),
    )
    op.create_index(op.f('ix_cash_addition_requests_company_id'), 'cash_addition_requests', ['company_id'])
    op.create_index(op.f('ix_cash_addition_requests_box_id'), 'cash_addition_requests', ['box_id'])
    op.create_index(op.f('ix_cash_addition_requests_cashier_id'), 'cash_addition_requests', ['cashier_id'])
    op.create_index('ix_cash_addition_requests_pending', 'cash_addition_requests', ['box_id', 'cashier_id'],
                    postgresql_where=sa.text("state = 'pending'"))


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(sa.text("SELECT count(*) FROM cash_addition_requests")).scalar()
    if count:
        raise RuntimeError(f"cannot downgrade 0026: {count} cash addition request(s) exist. Nothing was changed: "
                           "requests are never deleted.")
    op.drop_table('cash_addition_requests')
