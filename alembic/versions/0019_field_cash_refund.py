"""field cash refund (T-020)

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-08 01:00:00.000000

Physical refund of a REVERSED FIELD payment (1:1 with its reversal, full amount): from the collector (no Cash) or from
branch cash (one negative movement credit_field_refund). Adds the reversal unique target for the composite FK, the refund
guards, extends the T-019 item insert guard (no rendition after a collector refund) and the refund permission.
"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0019'
down_revision: Union[str, Sequence[str], None] = '0018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_PERMISSIONS = [
    ("cash.field_custody.refund", "Devolver al cliente el efectivo de un pago de campo revertido (custodio o jornada propia)", True),
]
# Functions and triggers (copied from app.modules.field_custody.models so the migration is self-contained)
REFUND_FUNCTIONS = ["\nCREATE OR REPLACE FUNCTION credit_field_refunds_insert_check() RETURNS trigger AS $$\nDECLARE v_state text;\nBEGIN\n  SELECT f.state INTO v_state FROM credit_field_rendition_items i JOIN credit_field_renditions f ON f.id = i.rendition_id\n   WHERE i.receipt_id = NEW.receipt_id AND NOT i.released;\n  IF v_state = 'declared' THEN\n    RAISE EXCEPTION 'receipt % is in a declared rendition: cancel or reject it before refunding', NEW.receipt_id;\n  END IF;\n  IF NEW.source_kind = 'collector' AND v_state IS NOT NULL THEN\n    RAISE EXCEPTION 'receipt % already left collector custody through an accepted rendition', NEW.receipt_id;\n  END IF;\n  IF NEW.source_kind = 'branch_cash' AND v_state IS DISTINCT FROM 'accepted' THEN\n    RAISE EXCEPTION 'a branch cash refund needs the receipt accepted into branch cash (receipt %)', NEW.receipt_id;\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION credit_field_refunds_guard() RETURNS trigger AS $$\nBEGIN\n  RAISE EXCEPTION 'field refund % is immutable history: it cannot be %', OLD.id, lower(TG_OP);\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION credit_field_refund_cash_check() RETURNS trigger AS $$\nDECLARE m RECORD;\nBEGIN\n  IF NEW.source_kind = 'branch_cash' THEN\n    SELECT kind, amount, session_id INTO m FROM cash_movements WHERE id = NEW.cash_movement_id;\n    IF m.kind IS DISTINCT FROM 'credit_field_refund' OR m.amount IS DISTINCT FROM -NEW.amount\n       OR m.session_id IS DISTINCT FROM NEW.cash_session_id THEN\n      RAISE EXCEPTION 'branch cash field refund % is not backed by its cash movement', NEW.id;\n    END IF;\n  END IF;\n  RETURN NULL;\nEND $$ LANGUAGE plpgsql\n"]
REFUND_TRIGGERS = ['CREATE TRIGGER trg_credit_field_refunds_insert_check BEFORE INSERT ON credit_field_refunds FOR EACH ROW EXECUTE FUNCTION credit_field_refunds_insert_check()', 'CREATE TRIGGER trg_credit_field_refunds_guard BEFORE UPDATE OR DELETE ON credit_field_refunds FOR EACH ROW EXECUTE FUNCTION credit_field_refunds_guard()', 'CREATE CONSTRAINT TRIGGER trg_credit_field_refunds_cash AFTER INSERT ON credit_field_refunds DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION credit_field_refund_cash_check()']
ITEM_INSERT_FN_T020 = "\nCREATE OR REPLACE FUNCTION credit_field_rendition_items_insert_check() RETURNS trigger AS $$\nBEGIN\n  IF NEW.released THEN\n    RAISE EXCEPTION 'a field rendition item is born claiming its receipt (released = false)';\n  END IF;\n  IF (SELECT state FROM credit_field_renditions WHERE id = NEW.rendition_id) IS DISTINCT FROM 'declared' THEN\n    RAISE EXCEPTION 'items can only be added to a declared field rendition';\n  END IF;\n  IF EXISTS (SELECT 1 FROM credit_field_refunds WHERE receipt_id = NEW.receipt_id AND source_kind = 'collector') THEN\n    RAISE EXCEPTION 'receipt % was refunded by its collector: it left custody and can never be rendered', NEW.receipt_id;\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n"
ITEM_INSERT_FN_T019 = "\nCREATE OR REPLACE FUNCTION credit_field_rendition_items_insert_check() RETURNS trigger AS $$\nBEGIN\n  IF NEW.released THEN\n    RAISE EXCEPTION 'a field rendition item is born claiming its receipt (released = false)';\n  END IF;\n  IF (SELECT state FROM credit_field_renditions WHERE id = NEW.rendition_id) IS DISTINCT FROM 'declared' THEN\n    RAISE EXCEPTION 'items can only be added to a declared field rendition';\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_unique_constraint('uq_credit_payment_reversals_refund_target', 'credit_payment_reversals', ['tenant_id', 'id', 'payment_id', 'loan_id', 'amount', 'origin', 'currency_code', 'reversal_branch_id'])
    op.create_table('credit_field_refunds',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('refund_number', sa.String(length=20), nullable=False),
    sa.Column('payment_id', sa.Integer(), nullable=False),
    sa.Column('reversal_id', sa.Integer(), nullable=False),
    sa.Column('receipt_id', sa.Integer(), nullable=False),
    sa.Column('loan_id', sa.Integer(), nullable=False),
    sa.Column('receiving_branch_id', sa.Integer(), nullable=False),
    sa.Column('origin', sa.String(length=10), nullable=False),
    sa.Column('currency_code', sa.String(length=3), nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('source_kind', sa.String(length=12), nullable=False),
    sa.Column('custodian_user_id', sa.Integer(), nullable=False),
    sa.Column('refunded_by', sa.Integer(), nullable=False),
    sa.Column('cash_session_id', sa.Integer(), nullable=True),
    sa.Column('cash_movement_id', sa.Integer(), nullable=True),
    sa.Column('reason', sa.String(length=500), nullable=False),
    sa.Column('refunded_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('idempotency_key', sa.String(length=120), nullable=False),
    sa.Column('request_digest', sa.String(length=80), nullable=False),
    sa.CheckConstraint("((source_kind = 'branch_cash') = (cash_session_id IS NOT NULL)) AND ((source_kind = 'branch_cash') = (cash_movement_id IS NOT NULL))", name=op.f('ck_credit_field_refunds_cash_matches_source')),
    sa.CheckConstraint("currency_code = 'DOP'", name=op.f('ck_credit_field_refunds_currency_dop')),
    sa.CheckConstraint("origin = 'field'", name=op.f('ck_credit_field_refunds_origin_field')),
    sa.CheckConstraint("source_kind <> 'collector' OR refunded_by = custodian_user_id", name=op.f('ck_credit_field_refunds_collector_refunds_own')),
    sa.CheckConstraint("source_kind IN ('collector', 'branch_cash')", name=op.f('ck_credit_field_refunds_source_kind_valid')),
    sa.CheckConstraint('amount > 0 AND amount = round(amount, 2)', name=op.f('ck_credit_field_refunds_amount_cent_exact')),
    sa.CheckConstraint('char_length(btrim(reason)) BETWEEN 3 AND 500', name=op.f('ck_credit_field_refunds_reason_length')),
    sa.ForeignKeyConstraint(['cash_movement_id'], ['cash_movements.id'], name=op.f('fk_credit_field_refunds_cash_movement_id_cash_movements')),
    sa.ForeignKeyConstraint(['cash_session_id'], ['cash_sessions.id'], name=op.f('fk_credit_field_refunds_cash_session_id_cash_sessions')),
    sa.ForeignKeyConstraint(['tenant_id', 'receipt_id', 'payment_id', 'receiving_branch_id', 'custodian_user_id', 'currency_code', 'amount'], ['credit_field_custody_receipts.tenant_id', 'credit_field_custody_receipts.id', 'credit_field_custody_receipts.payment_id', 'credit_field_custody_receipts.receiving_branch_id', 'credit_field_custody_receipts.custodian_user_id', 'credit_field_custody_receipts.currency_code', 'credit_field_custody_receipts.amount'], name='fk_credit_field_refunds_receipt'),
    sa.ForeignKeyConstraint(['tenant_id', 'refunded_by'], ['users.company_id', 'users.id'], name='fk_credit_field_refunds_actor'),
    sa.ForeignKeyConstraint(['tenant_id', 'reversal_id', 'payment_id', 'loan_id', 'amount', 'origin', 'currency_code', 'receiving_branch_id'], ['credit_payment_reversals.tenant_id', 'credit_payment_reversals.id', 'credit_payment_reversals.payment_id', 'credit_payment_reversals.loan_id', 'credit_payment_reversals.amount', 'credit_payment_reversals.origin', 'credit_payment_reversals.currency_code', 'credit_payment_reversals.reversal_branch_id'], name='fk_credit_field_refunds_reversal'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_credit_field_refunds_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_field_refunds')),
    sa.UniqueConstraint('cash_movement_id', name='uq_credit_field_refunds_cash_movement'),
    sa.UniqueConstraint('payment_id', name='uq_credit_field_refunds_payment'),
    sa.UniqueConstraint('receipt_id', name='uq_credit_field_refunds_receipt'),
    sa.UniqueConstraint('reversal_id', name='uq_credit_field_refunds_reversal'),
    sa.UniqueConstraint('tenant_id', 'idempotency_key', name='uq_credit_field_refunds_tenant_key'),
    sa.UniqueConstraint('tenant_id', 'refund_number', name='uq_credit_field_refunds_tenant_number')
    )
    op.create_index('ix_credit_field_refunds_branch', 'credit_field_refunds', ['tenant_id', 'receiving_branch_id', 'id'], unique=False)
    for fn_sql in REFUND_FUNCTIONS:
        op.execute(fn_sql)
    for trigger_sql in REFUND_TRIGGERS:
        op.execute(trigger_sql)
    op.execute(ITEM_INSERT_FN_T020)  # a receipt refunded by its collector can never be rendered

    permissions = sa.table("permissions", sa.column("code", sa.String), sa.column("description", sa.String),
                           sa.column("scope_kind", sa.String), sa.column("is_sensitive", sa.Boolean),
                           sa.column("created_at", sa.DateTime(timezone=True)))
    op.bulk_insert(permissions, [
        {"code": c, "description": d, "scope_kind": "tenant", "is_sensitive": x, "created_at": datetime.now(timezone.utc)}
        for c, d, x in NEW_PERMISSIONS
    ])
    codes = ", ".join("'%s'" % c for c, *_ in NEW_PERMISSIONS)
    op.execute("INSERT INTO role_permissions (role_id, permission_id, granted_at) "
               "SELECT r.id, p.id, now() FROM roles r JOIN permissions p ON p.scope_kind = 'tenant' "
               "WHERE r.system_defined AND r.tenant_id IS NOT NULL AND p.code IN (" + codes + ") "
               "ON CONFLICT DO NOTHING")


def downgrade() -> None:
    """Downgrade schema.

    REFUSES to run when any refund exists: dropping the table would erase which reversed payments were physically returned
    (and the link of their cash movements). It runs BEFORE any DDL; with an empty table the downgrade is clean."""
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM credit_field_refunds)")).scalar():
        raise RuntimeError(
            "Cannot downgrade 0019: credit_field_refunds contains physical refund history. "
            "Dropping it would erase it; keep revision 0019 or restore from backup."
        )
    codes = ", ".join("'%s'" % c for c, *_ in NEW_PERMISSIONS)
    op.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN (" + codes + "))")
    op.execute("DELETE FROM permissions WHERE code IN (" + codes + ")")
    op.execute(ITEM_INSERT_FN_T019)
    op.drop_index('ix_credit_field_refunds_branch', table_name='credit_field_refunds')
    op.drop_table('credit_field_refunds')
    op.drop_constraint('uq_credit_payment_reversals_refund_target', 'credit_payment_reversals', type_='unique')
    for fn_name in ("credit_field_refunds_insert_check", "credit_field_refunds_guard", "credit_field_refund_cash_check"):
        op.execute("DROP FUNCTION IF EXISTS %s()" % fn_name)
