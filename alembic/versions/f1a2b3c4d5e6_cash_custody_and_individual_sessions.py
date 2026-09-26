"""Add auditable custody handovers and individual cashier sessions."""

from alembic import op
import sqlalchemy as sa


revision = "f1a2b3c4d5e6"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    session_columns = {c["name"] for c in sa.inspect(bind).get_columns("cash_sessions")}
    with op.batch_alter_table("cash_sessions") as batch:
        if "cashier_id" not in session_columns:
            batch.add_column(sa.Column("cashier_id", sa.Integer(), nullable=True))
            batch.create_foreign_key("fk_cash_sessions_cashier_id", "users", ["cashier_id"], ["id"])
            batch.create_index("ix_cash_sessions_cashier_id", ["cashier_id"], unique=False)

    movement_columns = {c["name"] for c in sa.inspect(bind).get_columns("cash_movements")}
    with op.batch_alter_table("cash_movements") as batch:
        if "custody_transfer_id" not in movement_columns:
            batch.add_column(sa.Column("custody_transfer_id", sa.Integer(), nullable=True))

    if not sa.inspect(bind).has_table("cash_custody_transfers"):
        op.create_table(
            "cash_custody_transfers",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("box_id", sa.Integer(), sa.ForeignKey("cash_boxes.id"), nullable=False),
            sa.Column("session_id", sa.Integer(), sa.ForeignKey("cash_sessions.id"), nullable=False),
            sa.Column("kind", sa.String(length=30), nullable=False),
            sa.Column("from_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("to_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
            sa.Column("state", sa.String(length=30), nullable=False),
            sa.Column("acceptance_id", sa.String(length=80), nullable=True),
            sa.Column("acceptance_method", sa.String(length=40), nullable=True),
            sa.Column("accepted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("cash_movement_id", sa.Integer(), nullable=True),
            sa.Column("capital_movement_id", sa.Integer(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.UniqueConstraint("acceptance_id"),
            sa.UniqueConstraint("cash_movement_id"),
            sa.UniqueConstraint("capital_movement_id"),
        )
        op.create_index("ix_cash_custody_transfers_company_id", "cash_custody_transfers", ["company_id"])
        op.create_index("ix_cash_custody_transfers_box_id", "cash_custody_transfers", ["box_id"])
        op.create_index("ix_cash_custody_transfers_session_id", "cash_custody_transfers", ["session_id"])

    capital_columns = {c["name"] for c in sa.inspect(bind).get_columns("capital_movements")}
    if "cash_movement_id" in capital_columns:
        existing_constraints = {c.get("name") for c in sa.inspect(bind).get_unique_constraints("capital_movements")}
        if "uq_capital_movements_cash_movement_id" not in existing_constraints:
            with op.batch_alter_table("capital_movements") as batch:
                batch.create_unique_constraint("uq_capital_movements_cash_movement_id", ["cash_movement_id"])

    with op.batch_alter_table("cash_movements") as batch:
        existing_fks = {fk.get("name") for fk in sa.inspect(bind).get_foreign_keys("cash_movements")}
        if "fk_cash_movements_custody_transfer_id" not in existing_fks:
            batch.create_foreign_key("fk_cash_movements_custody_transfer_id", "cash_custody_transfers", ["custody_transfer_id"], ["id"])


def downgrade():
    bind = op.get_bind()
    with op.batch_alter_table("cash_movements") as batch:
        columns = {c["name"] for c in sa.inspect(bind).get_columns("cash_movements")}
        if "custody_transfer_id" in columns:
            fks = {fk.get("name") for fk in sa.inspect(bind).get_foreign_keys("cash_movements")}
            if "fk_cash_movements_custody_transfer_id" in fks:
                batch.drop_constraint("fk_cash_movements_custody_transfer_id", type_="foreignkey")
            batch.drop_column("custody_transfer_id")

    if sa.inspect(bind).has_table("cash_custody_transfers"):
        op.drop_table("cash_custody_transfers")

    with op.batch_alter_table("cash_sessions") as batch:
        columns = {c["name"] for c in sa.inspect(bind).get_columns("cash_sessions")}
        if "cashier_id" in columns:
            indexes = {idx.get("name") for idx in sa.inspect(bind).get_indexes("cash_sessions")}
            if "ix_cash_sessions_cashier_id" in indexes:
                batch.drop_index("ix_cash_sessions_cashier_id")
            fks = {fk.get("name") for fk in sa.inspect(bind).get_foreign_keys("cash_sessions")}
            if "fk_cash_sessions_cashier_id" in fks:
                batch.drop_constraint("fk_cash_sessions_cashier_id", type_="foreignkey")
            batch.drop_column("cashier_id")

    with op.batch_alter_table("capital_movements") as batch:
        existing_constraints = {c.get("name") for c in sa.inspect(bind).get_unique_constraints("capital_movements")}
        if "uq_capital_movements_cash_movement_id" in existing_constraints:
            batch.drop_constraint("uq_capital_movements_cash_movement_id", type_="unique")
