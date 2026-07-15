"""add paypal recurring fields

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
Create Date: 2026-07-14

Adds PayPal recurring-subscription identifiers:
- plans.paypal_plan_id (the P-XXXX billing plan id)
- companies.paypal_subscription_id (the I-XXXX active subscription id)
"""
from alembic import op
import sqlalchemy as sa

revision = "b7c8d9e0f1a2"
down_revision = "a6b7c8d9e0f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("plans") as batch:
        batch.add_column(sa.Column("paypal_plan_id", sa.String(length=64), nullable=True))
    with op.batch_alter_table("companies") as batch:
        batch.add_column(sa.Column("paypal_subscription_id", sa.String(length=64), nullable=True))
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=80), primary_key=True),
        sa.Column("value", sa.String(length=255), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    with op.batch_alter_table("companies") as batch:
        batch.drop_column("paypal_subscription_id")
    with op.batch_alter_table("plans") as batch:
        batch.drop_column("paypal_plan_id")
