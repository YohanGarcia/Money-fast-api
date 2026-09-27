"""Remove obsolete cash opening mode setting."""

from alembic import op
import sqlalchemy as sa

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("company_settings")}
    if "digital_cash_opening_handover" in columns:
        with op.batch_alter_table("company_settings") as batch:
            batch.drop_column("digital_cash_opening_handover")


def downgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("company_settings")}
    if "digital_cash_opening_handover" not in columns:
        with op.batch_alter_table("company_settings") as batch:
            batch.add_column(sa.Column("digital_cash_opening_handover", sa.Boolean(), nullable=False, server_default=sa.true()))
