"""credit collection promise to pay

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-04 10:00:00.000000

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0017'
down_revision: Union[str, Sequence[str], None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_PERMISSIONS = [("collections.promises.create", "Registrar, reemplazar y cancelar promesas de pago de cobranza sobre prestamos (no concede acceso de lectura)", True)]
# Functions and triggers (copied from app.modules.loans.models so the migration is self-contained)
PROMISE_FUNCTIONS = ["\nCREATE OR REPLACE FUNCTION credit_collection_promises_guard() RETURNS trigger AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN\n    RAISE EXCEPTION 'collection promise % cannot be deleted: it is history', OLD.id;\n  END IF;\n  IF OLD.closed_at IS NOT NULL THEN\n    RAISE EXCEPTION 'collection promise % is closed: history is immutable', OLD.id;\n  END IF;\n  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id\n     OR NEW.loan_id IS DISTINCT FROM OLD.loan_id OR NEW.managing_branch_id IS DISTINCT FROM OLD.managing_branch_id\n     OR NEW.assignment_id IS DISTINCT FROM OLD.assignment_id OR NEW.created_by IS DISTINCT FROM OLD.created_by\n     OR NEW.created_at IS DISTINCT FROM OLD.created_at OR NEW.currency_code IS DISTINCT FROM OLD.currency_code\n     OR NEW.promised_amount IS DISTINCT FROM OLD.promised_amount OR NEW.promise_date IS DISTINCT FROM OLD.promise_date\n     OR NEW.supersedes_promise_id IS DISTINCT FROM OLD.supersedes_promise_id\n     OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key OR NEW.request_digest IS DISTINCT FROM OLD.request_digest THEN\n    RAISE EXCEPTION 'collection promise % terms are immutable: only its single closing transition may change', OLD.id;\n  END IF;\n  IF NEW.closed_at IS NULL THEN\n    RAISE EXCEPTION 'collection promise % is open: the only allowed change is to close it', OLD.id;\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION credit_collection_promises_insert_check() RETURNS trigger AS $$\nDECLARE\n  loan_row RECORD;\nBEGIN\n  IF NEW.closed_at IS NOT NULL THEN\n    RAISE EXCEPTION 'a collection promise is born open: it cannot be inserted already closed';\n  END IF;\n  SELECT managing_branch_id, currency_code INTO loan_row\n    FROM credit_loans WHERE id = NEW.loan_id AND tenant_id = NEW.tenant_id;\n  IF NEW.managing_branch_id IS DISTINCT FROM loan_row.managing_branch_id THEN\n    RAISE EXCEPTION 'collection promise managing branch must be the loan''s own managing branch';\n  END IF;\n  IF NEW.currency_code IS DISTINCT FROM loan_row.currency_code THEN\n    RAISE EXCEPTION 'collection promise currency must be the loan''s currency';\n  END IF;\n  IF NEW.assignment_id IS DISTINCT FROM\n     (SELECT id FROM credit_collection_assignments\n       WHERE tenant_id = NEW.tenant_id AND loan_id = NEW.loan_id AND ended_at IS NULL) THEN\n    RAISE EXCEPTION 'collection promise assignment snapshot must be the loan''s open assignment (or NULL)';\n  END IF;\n  IF NEW.supersedes_promise_id IS NOT NULL AND NOT EXISTS (\n       SELECT 1 FROM credit_collection_promises\n        WHERE id = NEW.supersedes_promise_id AND tenant_id = NEW.tenant_id AND loan_id = NEW.loan_id\n          AND closed_kind = 'superseded') THEN\n    RAISE EXCEPTION 'a replacement must supersede a promise of the same loan that is closed as superseded';\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n"]
PROMISE_TRIGGERS = ['CREATE TRIGGER trg_credit_collection_promises_insert_check BEFORE INSERT ON credit_collection_promises FOR EACH ROW EXECUTE FUNCTION credit_collection_promises_insert_check()', 'CREATE TRIGGER trg_credit_collection_promises_guard BEFORE UPDATE OR DELETE ON credit_collection_promises FOR EACH ROW EXECUTE FUNCTION credit_collection_promises_guard()']
PROMISE_CLOSED_KINDS = ['cancelled', 'superseded']


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('credit_collection_promises',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('loan_id', sa.Integer(), nullable=False),
    sa.Column('managing_branch_id', sa.Integer(), nullable=True),
    sa.Column('assignment_id', sa.Integer(), nullable=True),
    sa.Column('created_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('currency_code', sa.String(length=3), nullable=False),
    sa.Column('promised_amount', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('promise_date', sa.Date(), nullable=False),
    sa.Column('supersedes_promise_id', sa.Integer(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=120), nullable=False),
    sa.Column('request_digest', sa.String(length=80), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('closed_by', sa.Integer(), nullable=True),
    sa.Column('closed_kind', sa.String(length=12), nullable=True),
    sa.Column('close_idempotency_key', sa.String(length=120), nullable=True),
    sa.Column('close_request_digest', sa.String(length=80), nullable=True),
    sa.CheckConstraint('promised_amount > 0', name=op.f('ck_credit_collection_promises_amount_positive')),
    sa.CheckConstraint('supersedes_promise_id IS NULL OR supersedes_promise_id <> id', name=op.f('ck_credit_collection_promises_not_self_superseding')),
    sa.CheckConstraint('(closed_at IS NULL) = (closed_by IS NULL) AND (closed_at IS NULL) = (closed_kind IS NULL) AND (closed_at IS NULL) = (close_idempotency_key IS NULL) AND (closed_at IS NULL) = (close_request_digest IS NULL)', name=op.f('ck_credit_collection_promises_closure_fields_together')),
    sa.CheckConstraint("closed_kind IS NULL OR closed_kind IN (" + ", ".join("'%s'" % k for k in PROMISE_CLOSED_KINDS) + ")", name=op.f('ck_credit_collection_promises_closed_kind_valid')),
    sa.CheckConstraint('closed_at IS NULL OR closed_at >= created_at', name=op.f('ck_credit_collection_promises_close_not_before_creation')),
    sa.ForeignKeyConstraint(['tenant_id', 'loan_id'], ['credit_loans.tenant_id', 'credit_loans.id'], name='fk_credit_collection_promises_loan'),
    sa.ForeignKeyConstraint(['tenant_id', 'created_by'], ['users.company_id', 'users.id'], name='fk_credit_collection_promises_creator'),
    sa.ForeignKeyConstraint(['tenant_id', 'closed_by'], ['users.company_id', 'users.id'], name='fk_credit_collection_promises_closer'),
    sa.ForeignKeyConstraint(['tenant_id', 'managing_branch_id'], ['branches.company_id', 'branches.id'], name='fk_credit_collection_promises_branch'),
    sa.ForeignKeyConstraint(['tenant_id', 'assignment_id', 'loan_id'], ['credit_collection_assignments.tenant_id', 'credit_collection_assignments.id', 'credit_collection_assignments.loan_id'], name='fk_credit_collection_promises_assignment'),
    sa.ForeignKeyConstraint(['tenant_id', 'supersedes_promise_id', 'loan_id'], ['credit_collection_promises.tenant_id', 'credit_collection_promises.id', 'credit_collection_promises.loan_id'], name='fk_credit_collection_promises_supersedes'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'id', 'loan_id', name='uq_credit_collection_promises_tenant_id_loan'),
    sa.UniqueConstraint('tenant_id', 'idempotency_key', name='uq_credit_collection_promises_tenant_key')
    )
    op.create_index('uq_credit_collection_promises_current', 'credit_collection_promises', ['tenant_id', 'loan_id'], unique=True, postgresql_where=sa.text('closed_at IS NULL'))
    op.create_index('uq_credit_collection_promises_close_key', 'credit_collection_promises', ['tenant_id', 'close_idempotency_key'], unique=True, postgresql_where=sa.text('close_idempotency_key IS NOT NULL'))
    op.create_index('ix_credit_collection_promises_loan_id_id', 'credit_collection_promises', ['tenant_id', 'loan_id', 'id'], unique=False)
    for fn_sql in PROMISE_FUNCTIONS:
        op.execute(fn_sql)
    for trigger_sql in PROMISE_TRIGGERS:
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

    REFUSES to run when any promise exists: dropping the table would silently erase collection history (who promised what, by
    when, and how it was closed). It runs BEFORE any DDL; with an empty table the downgrade is clean."""
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM credit_collection_promises)")).scalar():
        raise RuntimeError(
            "Cannot downgrade 0017: credit_collection_promises contains collection history. "
            "Dropping it would erase it; keep revision 0017 or restore from backup."
        )
    codes = ", ".join("'%s'" % c for c, *_ in NEW_PERMISSIONS)
    op.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN (" + codes + "))")
    op.execute("DELETE FROM permissions WHERE code IN (" + codes + ")")
    op.execute("DROP TRIGGER IF EXISTS trg_credit_collection_promises_insert_check ON credit_collection_promises")
    op.execute("DROP TRIGGER IF EXISTS trg_credit_collection_promises_guard ON credit_collection_promises")
    op.drop_index('ix_credit_collection_promises_loan_id_id', table_name='credit_collection_promises')
    op.drop_index('uq_credit_collection_promises_close_key', table_name='credit_collection_promises', postgresql_where=sa.text('close_idempotency_key IS NOT NULL'))
    op.drop_index('uq_credit_collection_promises_current', table_name='credit_collection_promises', postgresql_where=sa.text('closed_at IS NULL'))
    op.drop_table('credit_collection_promises')
    for fn_name in ("credit_collection_promises_guard", "credit_collection_promises_insert_check"):
        op.execute("DROP FUNCTION IF EXISTS %s()" % fn_name)
