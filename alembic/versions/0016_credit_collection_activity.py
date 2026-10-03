"""credit collection activity

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-03 18:00:00.000000

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0016'
down_revision: Union[str, Sequence[str], None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_PERMISSIONS = [("collections.actions.create", "Registrar gestiones de cobranza sobre prestamos (no concede acceso de lectura)", True)]
# Functions and triggers (copied from app.modules.loans.models so the migration is self-contained)
ACTIVITY_FUNCTIONS = ["\nCREATE OR REPLACE FUNCTION credit_collection_activities_guard() RETURNS trigger AS $$\nBEGIN\n  RAISE EXCEPTION 'collection activity % is append-only history: it cannot be % ', OLD.id, lower(TG_OP);\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION credit_collection_activities_insert_check() RETURNS trigger AS $$\nBEGIN\n  IF NEW.managing_branch_id IS DISTINCT FROM\n     (SELECT managing_branch_id FROM credit_loans WHERE id = NEW.loan_id AND tenant_id = NEW.tenant_id) THEN\n    RAISE EXCEPTION 'collection activity managing branch must be the loan''s own managing branch';\n  END IF;\n  IF NEW.assignment_id IS DISTINCT FROM\n     (SELECT id FROM credit_collection_assignments\n       WHERE tenant_id = NEW.tenant_id AND loan_id = NEW.loan_id AND ended_at IS NULL) THEN\n    RAISE EXCEPTION 'collection activity assignment snapshot must be the loan''s open assignment (or NULL)';\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n"]
ACTIVITY_TRIGGERS = ['CREATE TRIGGER trg_credit_collection_activities_insert_check BEFORE INSERT ON credit_collection_activities FOR EACH ROW EXECUTE FUNCTION credit_collection_activities_insert_check()', 'CREATE TRIGGER trg_credit_collection_activities_guard BEFORE UPDATE OR DELETE ON credit_collection_activities FOR EACH ROW EXECUTE FUNCTION credit_collection_activities_guard()']
ACTIVITY_TYPES = ['phone_call', 'whatsapp', 'sms', 'email', 'in_person_visit', 'office_visit', 'no_contact', 'other']


def upgrade() -> None:
    """Upgrade schema."""
    # relational support only (no new rule): the target of the tenant-safe FK of an activity's assignment snapshot
    op.create_unique_constraint('uq_credit_collection_assignments_tenant_id_loan', 'credit_collection_assignments', ['tenant_id', 'id', 'loan_id'])
    op.create_table('credit_collection_activities',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('loan_id', sa.Integer(), nullable=False),
    sa.Column('managing_branch_id', sa.Integer(), nullable=True),
    sa.Column('recorded_by', sa.Integer(), nullable=False),
    sa.Column('assignment_id', sa.Integer(), nullable=True),
    sa.Column('activity_type', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('idempotency_key', sa.String(length=120), nullable=False),
    sa.Column('request_digest', sa.String(length=80), nullable=False),
    sa.CheckConstraint("activity_type IN (" + ", ".join("'%s'" % t for t in ACTIVITY_TYPES) + ")", name=op.f('ck_credit_collection_activities_activity_type_valid')),
    sa.ForeignKeyConstraint(['tenant_id', 'loan_id'], ['credit_loans.tenant_id', 'credit_loans.id'], name='fk_credit_collection_activities_loan'),
    sa.ForeignKeyConstraint(['tenant_id', 'recorded_by'], ['users.company_id', 'users.id'], name='fk_credit_collection_activities_actor'),
    sa.ForeignKeyConstraint(['tenant_id', 'managing_branch_id'], ['branches.company_id', 'branches.id'], name='fk_credit_collection_activities_branch'),
    sa.ForeignKeyConstraint(['tenant_id', 'assignment_id', 'loan_id'], ['credit_collection_assignments.tenant_id', 'credit_collection_assignments.id', 'credit_collection_assignments.loan_id'], name='fk_credit_collection_activities_assignment'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'idempotency_key', name='uq_credit_collection_activities_tenant_key')
    )
    op.create_index('ix_credit_collection_activities_loan_id_id', 'credit_collection_activities', ['tenant_id', 'loan_id', 'id'], unique=False)
    for fn_sql in ACTIVITY_FUNCTIONS:
        op.execute(fn_sql)
    for trigger_sql in ACTIVITY_TRIGGERS:
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

    REFUSES to run when any activity exists: dropping the table would silently erase collection history (who recorded which
    management on which loan, and when). It runs BEFORE any DDL; with an empty table the downgrade is clean."""
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM credit_collection_activities)")).scalar():
        raise RuntimeError(
            "Cannot downgrade 0016: credit_collection_activities contains collection history. "
            "Dropping it would erase it; keep revision 0016 or restore from backup."
        )
    codes = ", ".join("'%s'" % c for c, *_ in NEW_PERMISSIONS)
    op.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN (" + codes + "))")
    op.execute("DELETE FROM permissions WHERE code IN (" + codes + ")")
    op.execute("DROP TRIGGER IF EXISTS trg_credit_collection_activities_insert_check ON credit_collection_activities")
    op.execute("DROP TRIGGER IF EXISTS trg_credit_collection_activities_guard ON credit_collection_activities")
    op.drop_index('ix_credit_collection_activities_loan_id_id', table_name='credit_collection_activities')
    op.drop_table('credit_collection_activities')
    op.drop_constraint('uq_credit_collection_assignments_tenant_id_loan', 'credit_collection_assignments', type_='unique')
    for fn_name in ("credit_collection_activities_guard", "credit_collection_activities_insert_check"):
        op.execute("DROP FUNCTION IF EXISTS %s()" % fn_name)
