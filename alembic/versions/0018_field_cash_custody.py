"""field cash custody and rendition (T-019)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-07 14:03:26.171533

Physical custody of field-collected cash: one immutable receipt per field payment, renditions with one terminal transition,
items that claim receipts (partial UNIQUE). NO backfill: field payments recorded before this revision stay "pre_custody"
(a payment does not prove its cash is still with the collector).
"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0018'
down_revision: Union[str, Sequence[str], None] = '0017'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_PERMISSIONS = [
    ("cash.field_custody.read", "Consultar la custodia de efectivo de campo y sus rendiciones (solo lectura)", False),
    ("cash.field_custody.render", "Cobrar en campo como custodio del efectivo y declarar o cancelar su rendicion", True),
    ("cash.field_custody.accept", "Aceptar o rechazar rendiciones de efectivo de campo en la propia jornada de caja", True),
]
# Functions and triggers (copied from app.modules.field_custody.models so the migration is self-contained)
CUSTODY_FUNCTIONS = ["\nCREATE OR REPLACE FUNCTION credit_field_custody_receipts_insert_check() RETURNS trigger AS $$\nDECLARE p RECORD;\nBEGIN\n  SELECT tenant_id, loan_id, origin, amount, currency_code, receiving_branch_id, collected_by, received_at\n    INTO p FROM credit_payments WHERE id = NEW.payment_id;\n  IF NOT FOUND THEN\n    RAISE EXCEPTION 'field custody receipt needs an existing payment (%)', NEW.payment_id;\n  END IF;\n  IF p.tenant_id <> NEW.tenant_id OR p.loan_id <> NEW.loan_id THEN\n    RAISE EXCEPTION 'field custody receipt must belong to the payment''s tenant and loan';\n  END IF;\n  IF p.origin <> 'field' THEN\n    RAISE EXCEPTION 'only a field payment creates physical custody (payment % is %)', NEW.payment_id, p.origin;\n  END IF;\n  IF p.amount <> NEW.amount OR p.currency_code <> NEW.currency_code OR p.receiving_branch_id <> NEW.receiving_branch_id\n     OR p.collected_by <> NEW.custodian_user_id OR p.received_at <> NEW.received_at THEN\n    RAISE EXCEPTION 'field custody receipt must snapshot exactly its payment (amount, currency, branch, custodian, time)';\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION credit_field_custody_receipts_guard() RETURNS trigger AS $$\nBEGIN\n  RAISE EXCEPTION 'field custody receipt % is immutable history: it cannot be %', OLD.id, lower(TG_OP);\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION credit_field_renditions_insert_check() RETURNS trigger AS $$\nBEGIN\n  IF NEW.state <> 'declared' OR NEW.counted_amount IS NOT NULL OR NEW.decision_reason IS NOT NULL THEN\n    RAISE EXCEPTION 'a field rendition is born declared, without a decision';\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION credit_field_renditions_guard() RETURNS trigger AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN\n    RAISE EXCEPTION 'field rendition % cannot be deleted: it is history', OLD.id;\n  END IF;\n  IF OLD.state <> 'declared' THEN\n    RAISE EXCEPTION 'field rendition % is % (terminal): it cannot change', OLD.id, OLD.state;\n  END IF;\n  IF NEW.state NOT IN ('accepted', 'rejected', 'cancelled') THEN\n    RAISE EXCEPTION 'field rendition % may only move from declared to accepted, rejected or cancelled', OLD.id;\n  END IF;\n  IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.rendition_number <> OLD.rendition_number\n     OR NEW.receiving_branch_id <> OLD.receiving_branch_id OR NEW.custodian_user_id <> OLD.custodian_user_id\n     OR NEW.currency_code <> OLD.currency_code OR NEW.declared_amount <> OLD.declared_amount\n     OR NEW.declared_by <> OLD.declared_by OR NEW.declared_at <> OLD.declared_at\n     OR NEW.create_idempotency_key <> OLD.create_idempotency_key OR NEW.create_request_digest <> OLD.create_request_digest THEN\n    RAISE EXCEPTION 'field rendition % identity and declaration are immutable', OLD.id;\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION credit_field_rendition_items_insert_check() RETURNS trigger AS $$\nBEGIN\n  IF NEW.released THEN\n    RAISE EXCEPTION 'a field rendition item is born claiming its receipt (released = false)';\n  END IF;\n  IF (SELECT state FROM credit_field_renditions WHERE id = NEW.rendition_id) IS DISTINCT FROM 'declared' THEN\n    RAISE EXCEPTION 'items can only be added to a declared field rendition';\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION credit_field_rendition_items_guard() RETURNS trigger AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN\n    RAISE EXCEPTION 'field rendition item % cannot be deleted: it is history', OLD.id;\n  END IF;\n  IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.rendition_id <> OLD.rendition_id\n     OR NEW.receipt_id <> OLD.receipt_id OR NEW.payment_id <> OLD.payment_id\n     OR NEW.receiving_branch_id <> OLD.receiving_branch_id OR NEW.custodian_user_id <> OLD.custodian_user_id\n     OR NEW.currency_code <> OLD.currency_code OR NEW.amount <> OLD.amount THEN\n    RAISE EXCEPTION 'field rendition item % is immutable: only its release may change', OLD.id;\n  END IF;\n  IF OLD.released OR NOT NEW.released THEN\n    RAISE EXCEPTION 'field rendition item % may only be released once (false -> true)', OLD.id;\n  END IF;\n  IF (SELECT state FROM credit_field_renditions WHERE id = OLD.rendition_id) NOT IN ('rejected', 'cancelled') THEN\n    RAISE EXCEPTION 'only a rejected or cancelled field rendition releases its items';\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION credit_field_rendition_consistency_check() RETURNS trigger AS $$\nDECLARE r RECORD; v_id integer; v_items integer; v_sum numeric; v_released integer; m RECORD;\nBEGIN\n  IF TG_TABLE_NAME = 'credit_field_renditions' THEN v_id := NEW.id; ELSE v_id := NEW.rendition_id; END IF;\n  SELECT * INTO r FROM credit_field_renditions WHERE id = v_id;\n  SELECT count(*), COALESCE(sum(amount), 0), count(*) FILTER (WHERE released)\n    INTO v_items, v_sum, v_released FROM credit_field_rendition_items WHERE rendition_id = r.id;\n  IF v_items = 0 OR v_sum <> r.declared_amount THEN\n    RAISE EXCEPTION 'field rendition % declared amount (%) must equal its items (% in % items)', r.id, r.declared_amount, v_sum, v_items;\n  END IF;\n  IF r.state IN ('declared', 'accepted') AND v_released > 0 THEN\n    RAISE EXCEPTION 'field rendition % is % but has released items', r.id, r.state;\n  END IF;\n  IF r.state IN ('rejected', 'cancelled') AND v_released <> v_items THEN\n    RAISE EXCEPTION 'field rendition % is % but still claims receipts', r.id, r.state;\n  END IF;\n  IF r.state = 'accepted' THEN\n    SELECT kind, amount, session_id INTO m FROM cash_movements WHERE id = r.cash_movement_id;\n    IF m.kind IS DISTINCT FROM 'credit_field_rendition' OR m.amount IS DISTINCT FROM r.declared_amount\n       OR m.session_id IS DISTINCT FROM r.cash_session_id THEN\n      RAISE EXCEPTION 'accepted field rendition % is not backed by its cash movement', r.id;\n    END IF;\n  END IF;\n  RETURN NULL;\nEND $$ LANGUAGE plpgsql\n"]
CUSTODY_TRIGGERS = ['CREATE TRIGGER trg_credit_field_custody_receipts_insert_check BEFORE INSERT ON credit_field_custody_receipts FOR EACH ROW EXECUTE FUNCTION credit_field_custody_receipts_insert_check()', 'CREATE TRIGGER trg_credit_field_custody_receipts_guard BEFORE UPDATE OR DELETE ON credit_field_custody_receipts FOR EACH ROW EXECUTE FUNCTION credit_field_custody_receipts_guard()', 'CREATE TRIGGER trg_credit_field_renditions_insert_check BEFORE INSERT ON credit_field_renditions FOR EACH ROW EXECUTE FUNCTION credit_field_renditions_insert_check()', 'CREATE TRIGGER trg_credit_field_renditions_guard BEFORE UPDATE OR DELETE ON credit_field_renditions FOR EACH ROW EXECUTE FUNCTION credit_field_renditions_guard()', 'CREATE TRIGGER trg_credit_field_rendition_items_insert_check BEFORE INSERT ON credit_field_rendition_items FOR EACH ROW EXECUTE FUNCTION credit_field_rendition_items_insert_check()', 'CREATE TRIGGER trg_credit_field_rendition_items_guard BEFORE UPDATE OR DELETE ON credit_field_rendition_items FOR EACH ROW EXECUTE FUNCTION credit_field_rendition_items_guard()', 'CREATE CONSTRAINT TRIGGER trg_credit_field_renditions_consistency AFTER INSERT OR UPDATE ON credit_field_renditions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION credit_field_rendition_consistency_check()', 'CREATE CONSTRAINT TRIGGER trg_credit_field_rendition_items_consistency AFTER INSERT OR UPDATE ON credit_field_rendition_items DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION credit_field_rendition_consistency_check()']
TABLES = ("credit_field_rendition_items", "credit_field_renditions", "credit_field_custody_receipts")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('credit_field_custody_receipts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('payment_id', sa.Integer(), nullable=False),
    sa.Column('loan_id', sa.Integer(), nullable=False),
    sa.Column('receiving_branch_id', sa.Integer(), nullable=False),
    sa.Column('custodian_user_id', sa.Integer(), nullable=False),
    sa.Column('currency_code', sa.String(length=3), nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("currency_code = 'DOP'", name=op.f('ck_credit_field_custody_receipts_currency_dop')),
    sa.CheckConstraint('amount > 0 AND amount = round(amount, 2)', name=op.f('ck_credit_field_custody_receipts_amount_cent_exact')),
    sa.ForeignKeyConstraint(['tenant_id', 'custodian_user_id'], ['users.company_id', 'users.id'], name='fk_credit_field_custody_receipts_custodian'),
    sa.ForeignKeyConstraint(['tenant_id', 'payment_id', 'loan_id'], ['credit_payments.tenant_id', 'credit_payments.id', 'credit_payments.loan_id'], name='fk_credit_field_custody_receipts_payment'),
    sa.ForeignKeyConstraint(['tenant_id', 'receiving_branch_id'], ['branches.company_id', 'branches.id'], name='fk_credit_field_custody_receipts_branch'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_credit_field_custody_receipts_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_field_custody_receipts')),
    sa.UniqueConstraint('payment_id', name='uq_credit_field_custody_receipts_payment'),
    sa.UniqueConstraint('tenant_id', 'id', 'payment_id', 'receiving_branch_id', 'custodian_user_id', 'currency_code', 'amount', name='uq_credit_field_custody_receipts_item_target')
    )
    op.create_index('ix_credit_field_custody_receipts_custodian', 'credit_field_custody_receipts', ['tenant_id', 'custodian_user_id', 'receiving_branch_id', 'id'], unique=False)
    op.create_table('credit_field_renditions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('rendition_number', sa.String(length=20), nullable=False),
    sa.Column('receiving_branch_id', sa.Integer(), nullable=False),
    sa.Column('custodian_user_id', sa.Integer(), nullable=False),
    sa.Column('currency_code', sa.String(length=3), nullable=False),
    sa.Column('declared_amount', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('state', sa.String(length=12), nullable=False),
    sa.Column('declared_by', sa.Integer(), nullable=False),
    sa.Column('declared_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decided_by', sa.Integer(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_reason', sa.Text(), nullable=True),
    sa.Column('counted_amount', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('cash_session_id', sa.Integer(), nullable=True),
    sa.Column('cash_movement_id', sa.Integer(), nullable=True),
    sa.Column('create_idempotency_key', sa.String(length=120), nullable=False),
    sa.Column('create_request_digest', sa.String(length=80), nullable=False),
    sa.Column('decision_idempotency_key', sa.String(length=120), nullable=True),
    sa.Column('decision_request_digest', sa.String(length=80), nullable=True),
    sa.CheckConstraint("((state = 'accepted') = (cash_session_id IS NOT NULL)) AND ((state = 'accepted') = (cash_movement_id IS NOT NULL))", name=op.f('ck_credit_field_renditions_cash_matches_accepted')),
    sa.CheckConstraint("((state = 'declared') = (decided_at IS NULL)) AND ((state = 'declared') = (decided_by IS NULL)) AND ((state = 'declared') = (decision_idempotency_key IS NULL)) AND ((state = 'declared') = (decision_request_digest IS NULL))", name=op.f('ck_credit_field_renditions_decision_consistent')),
    sa.CheckConstraint("counted_amount IS NULL OR state IN ('accepted', 'rejected')", name=op.f('ck_credit_field_renditions_counted_only_on_decision')),
    sa.CheckConstraint("currency_code = 'DOP'", name=op.f('ck_credit_field_renditions_currency_dop')),
    sa.CheckConstraint("state <> 'accepted' OR counted_amount = declared_amount", name=op.f('ck_credit_field_renditions_accepted_exact')),
    sa.CheckConstraint("state <> 'cancelled' OR decided_by = custodian_user_id", name=op.f('ck_credit_field_renditions_cancel_by_custodian')),
    sa.CheckConstraint("state <> 'rejected' OR length(btrim(coalesce(decision_reason, ''))) >= 3", name=op.f('ck_credit_field_renditions_reject_reason')),
    sa.CheckConstraint("state IN ('declared', 'accepted', 'rejected', 'cancelled')", name=op.f('ck_credit_field_renditions_state_valid')),
    sa.CheckConstraint("state NOT IN ('accepted', 'rejected') OR decided_by <> custodian_user_id", name=op.f('ck_credit_field_renditions_maker_checker')),
    sa.CheckConstraint('counted_amount IS NULL OR (counted_amount >= 0 AND counted_amount = round(counted_amount, 2))', name=op.f('ck_credit_field_renditions_counted_amount_cent_exact')),
    sa.CheckConstraint('declared_amount > 0 AND declared_amount = round(declared_amount, 2)', name=op.f('ck_credit_field_renditions_declared_amount_cent_exact')),
    sa.CheckConstraint('declared_by = custodian_user_id', name=op.f('ck_credit_field_renditions_declared_by_custodian')),
    sa.ForeignKeyConstraint(['cash_movement_id'], ['cash_movements.id'], name=op.f('fk_credit_field_renditions_cash_movement_id_cash_movements')),
    sa.ForeignKeyConstraint(['cash_session_id'], ['cash_sessions.id'], name=op.f('fk_credit_field_renditions_cash_session_id_cash_sessions')),
    sa.ForeignKeyConstraint(['tenant_id', 'custodian_user_id'], ['users.company_id', 'users.id'], name='fk_credit_field_renditions_custodian'),
    sa.ForeignKeyConstraint(['tenant_id', 'decided_by'], ['users.company_id', 'users.id'], name='fk_credit_field_renditions_decider'),
    sa.ForeignKeyConstraint(['tenant_id', 'receiving_branch_id'], ['branches.company_id', 'branches.id'], name='fk_credit_field_renditions_branch'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_credit_field_renditions_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_field_renditions')),
    sa.UniqueConstraint('cash_movement_id', name='uq_credit_field_renditions_cash_movement'),
    sa.UniqueConstraint('tenant_id', 'create_idempotency_key', name='uq_credit_field_renditions_create_key'),
    sa.UniqueConstraint('tenant_id', 'id', 'receiving_branch_id', 'custodian_user_id', 'currency_code', name='uq_credit_field_renditions_item_target'),
    sa.UniqueConstraint('tenant_id', 'rendition_number', name='uq_credit_field_renditions_tenant_number')
    )
    op.create_index('ix_credit_field_renditions_branch_declared', 'credit_field_renditions', ['tenant_id', 'receiving_branch_id', 'id'], unique=False, postgresql_where=sa.text("state = 'declared'"))
    op.create_index('ix_credit_field_renditions_custodian', 'credit_field_renditions', ['tenant_id', 'custodian_user_id', 'id'], unique=False)
    op.create_index('uq_credit_field_renditions_decision_key', 'credit_field_renditions', ['tenant_id', 'decision_idempotency_key'], unique=True, postgresql_where=sa.text('decision_idempotency_key IS NOT NULL'))
    op.create_table('credit_field_rendition_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('rendition_id', sa.Integer(), nullable=False),
    sa.Column('receipt_id', sa.Integer(), nullable=False),
    sa.Column('payment_id', sa.Integer(), nullable=False),
    sa.Column('receiving_branch_id', sa.Integer(), nullable=False),
    sa.Column('custodian_user_id', sa.Integer(), nullable=False),
    sa.Column('currency_code', sa.String(length=3), nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('released', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id', 'receipt_id', 'payment_id', 'receiving_branch_id', 'custodian_user_id', 'currency_code', 'amount'], ['credit_field_custody_receipts.tenant_id', 'credit_field_custody_receipts.id', 'credit_field_custody_receipts.payment_id', 'credit_field_custody_receipts.receiving_branch_id', 'credit_field_custody_receipts.custodian_user_id', 'credit_field_custody_receipts.currency_code', 'credit_field_custody_receipts.amount'], name='fk_credit_field_rendition_items_receipt'),
    sa.ForeignKeyConstraint(['tenant_id', 'rendition_id', 'receiving_branch_id', 'custodian_user_id', 'currency_code'], ['credit_field_renditions.tenant_id', 'credit_field_renditions.id', 'credit_field_renditions.receiving_branch_id', 'credit_field_renditions.custodian_user_id', 'credit_field_renditions.currency_code'], name='fk_credit_field_rendition_items_rendition'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_credit_field_rendition_items_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_field_rendition_items')),
    sa.UniqueConstraint('rendition_id', 'receipt_id', name='uq_credit_field_rendition_items_row')
    )
    op.create_index('ix_credit_field_rendition_items_receipt', 'credit_field_rendition_items', ['receipt_id'], unique=False)
    op.create_index('ix_credit_field_rendition_items_rendition', 'credit_field_rendition_items', ['rendition_id'], unique=False)
    op.create_index('uq_credit_field_rendition_items_live_receipt', 'credit_field_rendition_items', ['receipt_id'], unique=True, postgresql_where=sa.text('NOT released'))
    for fn_sql in CUSTODY_FUNCTIONS:
        op.execute(fn_sql)
    for trigger_sql in CUSTODY_TRIGGERS:
        op.execute(trigger_sql)

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

    REFUSES to run when any custody or rendition exists: dropping the tables would erase who holds which field cash and
    which cash entered which session. It runs BEFORE any DDL; with empty tables the downgrade is clean."""
    bind = op.get_bind()
    for table in TABLES:
        if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM %s)" % table)).scalar():
            raise RuntimeError(
                "Cannot downgrade 0018: %s contains field cash custody history. "
                "Dropping it would erase it; keep revision 0018 or restore from backup." % table
            )
    codes = ", ".join("'%s'" % c for c, *_ in NEW_PERMISSIONS)
    op.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN (" + codes + "))")
    op.execute("DELETE FROM permissions WHERE code IN (" + codes + ")")
    op.drop_index('uq_credit_field_rendition_items_live_receipt', table_name='credit_field_rendition_items', postgresql_where=sa.text('NOT released'))
    op.drop_index('ix_credit_field_rendition_items_rendition', table_name='credit_field_rendition_items')
    op.drop_index('ix_credit_field_rendition_items_receipt', table_name='credit_field_rendition_items')
    op.drop_table('credit_field_rendition_items')
    op.drop_index('ix_credit_field_custody_receipts_custodian', table_name='credit_field_custody_receipts')
    op.drop_table('credit_field_custody_receipts')
    op.drop_index('uq_credit_field_renditions_decision_key', table_name='credit_field_renditions', postgresql_where=sa.text('decision_idempotency_key IS NOT NULL'))
    op.drop_index('ix_credit_field_renditions_custodian', table_name='credit_field_renditions')
    op.drop_index('ix_credit_field_renditions_branch_declared', table_name='credit_field_renditions', postgresql_where=sa.text("state = 'declared'"))
    op.drop_table('credit_field_renditions')
    for fn_name in ("credit_field_custody_receipts_insert_check", "credit_field_custody_receipts_guard",
                    "credit_field_renditions_insert_check", "credit_field_renditions_guard",
                    "credit_field_rendition_items_insert_check", "credit_field_rendition_items_guard",
                    "credit_field_rendition_consistency_check"):
        op.execute("DROP FUNCTION IF EXISTS %s()" % fn_name)
