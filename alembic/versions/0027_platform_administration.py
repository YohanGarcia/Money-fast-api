"""platform administration: settings, payment methods, coupons, staff, annual prices and payment details

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-15 06:00:00.000000

Additive over 0026. The platform owner gets: a global settings document, the payment methods offered to the
companies (PayPal / bank transfer), coupons and a staff list; plans get an annual price; subscriptions record the
cycle, method, coupon and (for transfers) the payer's reference and proof awaiting confirmation; companies remember
their billing cycle and the expiry a renewal reminder was sent for. Existing rows are untouched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0027'
down_revision: Union[str, Sequence[str], None] = '0026'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMPTY_JSON = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        'platform_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('values', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=EMPTY_JSON),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_platform_settings_updated_by_users')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_platform_settings')),
    )
    op.create_table(
        'platform_payment_methods',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('label', sa.String(length=80), nullable=False),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=EMPTY_JSON),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('paypal', 'bank_transfer')", name=op.f('ck_platform_payment_methods_kind_valid')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_platform_payment_methods')),
    )
    op.create_table(
        'coupons',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=40), nullable=False),
        sa.Column('description', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('value', sa.Numeric(8, 2), nullable=False, server_default='0'),
        sa.Column('plan_id', sa.Integer(), nullable=True),
        sa.Column('billing_cycle', sa.String(length=10), nullable=True),
        sa.Column('max_redemptions', sa.Integer(), nullable=True),
        sa.Column('redeemed_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('valid_from', sa.Date(), nullable=True),
        sa.Column('valid_until', sa.Date(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('percent', 'fixed_usd', 'free_months')", name=op.f('ck_coupons_kind_valid')),
        sa.ForeignKeyConstraint(['plan_id'], ['plans.id'], name=op.f('fk_coupons_plan_id_plans')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_coupons')),
    )
    op.create_index(op.f('ix_coupons_code'), 'coupons', ['code'], unique=True)
    op.create_table(
        'platform_staff',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('level', sa.String(length=10), nullable=False, server_default='support'),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('notes', sa.Text(), nullable=False, server_default=''),
        sa.CheckConstraint("level IN ('owner', 'support')", name=op.f('ck_platform_staff_level_valid')),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_platform_staff_user_id_users')),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_platform_staff_created_by_users')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_platform_staff')),
        sa.UniqueConstraint('user_id', name=op.f('uq_platform_staff_user_id')),
    )
    op.add_column('plans', sa.Column('annual_price_usd', sa.Numeric(8, 2), nullable=True))
    op.add_column('plans', sa.Column('paypal_annual_plan_id', sa.String(length=64), nullable=True))
    op.add_column('subscriptions', sa.Column('billing_cycle', sa.String(length=10), nullable=False, server_default='monthly'))
    op.add_column('subscriptions', sa.Column('payment_method', sa.String(length=24), nullable=True))
    op.add_column('subscriptions', sa.Column('coupon_code', sa.String(length=40), nullable=True))
    op.add_column('subscriptions', sa.Column('discount_usd', sa.Numeric(8, 2), nullable=False, server_default='0'))
    op.add_column('subscriptions', sa.Column('reference', sa.String(length=160), nullable=False, server_default=''))
    op.add_column('subscriptions', sa.Column('payer_note', sa.Text(), nullable=False, server_default=''))
    op.add_column('subscriptions', sa.Column('proof', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('subscriptions', sa.Column('reviewed_by', sa.Integer(), nullable=True))
    op.add_column('subscriptions', sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('subscriptions', sa.Column('review_note', sa.Text(), nullable=False, server_default=''))
    op.create_foreign_key(op.f('fk_subscriptions_reviewed_by_users'), 'subscriptions', 'users', ['reviewed_by'], ['id'])
    op.add_column('companies', sa.Column('billing_cycle', sa.String(length=10), nullable=True))
    op.add_column('companies', sa.Column('renewal_notice_for', sa.DateTime(timezone=True), nullable=True))
    # Every platform account that exists today is an owner of the platform.
    op.execute(
        "INSERT INTO platform_staff (user_id, level, notes) "
        "SELECT id, 'owner', 'Creado con la migracion 0027' FROM users WHERE role = 'superadmin'"
    )
    # Default methods: PayPal as it works today, and the bank transfer waiting for the owner's account data.
    op.execute(
        "INSERT INTO platform_payment_methods (kind, label, is_enabled, sort_order, details) VALUES "
        "('paypal', 'PayPal (cuenta o tarjeta)', true, 1, "
        "'{\"accept_account\": true, \"accept_card\": true, \"recurring\": true}'::jsonb), "
        "('bank_transfer', 'Transferencia bancaria', false, 2, '{}'::jsonb)"
    )
    op.execute("INSERT INTO platform_settings (id, values) VALUES (1, '{}'::jsonb)")


def downgrade() -> None:
    op.drop_column('companies', 'renewal_notice_for')
    op.drop_column('companies', 'billing_cycle')
    op.drop_constraint(op.f('fk_subscriptions_reviewed_by_users'), 'subscriptions', type_='foreignkey')
    for name in ('review_note', 'reviewed_at', 'reviewed_by', 'proof', 'payer_note', 'reference', 'discount_usd',
                 'coupon_code', 'payment_method', 'billing_cycle'):
        op.drop_column('subscriptions', name)
    op.drop_column('plans', 'paypal_annual_plan_id')
    op.drop_column('plans', 'annual_price_usd')
    op.drop_table('platform_staff')
    op.drop_index(op.f('ix_coupons_code'), table_name='coupons')
    op.drop_table('coupons')
    op.drop_table('platform_payment_methods')
    op.drop_table('platform_settings')
