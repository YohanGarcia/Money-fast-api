"""credit loan disbursement

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-02 01:22:29.357956

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_PERMISSIONS = [
    ("loans.read", "Consultar prestamos, desembolsos y cronogramas", False),
    ("loans.disburse", "Desembolsar un contrato formalizado (sale dinero)", True),
]
# Guards (kept in sync with app.modules.loans / origination models; copied so the migration is self-contained)
GUARD_FUNCTIONS = ["\nCREATE OR REPLACE FUNCTION credit_loans_guard() RETURNS trigger AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN\n    RAISE EXCEPTION 'credit loan % cannot be deleted', OLD.id;\n  END IF;\n  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id\n     OR NEW.loan_number IS DISTINCT FROM OLD.loan_number OR NEW.customer_id IS DISTINCT FROM OLD.customer_id\n     OR NEW.formalization_id IS DISTINCT FROM OLD.formalization_id OR NEW.application_id IS DISTINCT FROM OLD.application_id\n     OR NEW.product_id IS DISTINCT FROM OLD.product_id OR NEW.product_version_id IS DISTINCT FROM OLD.product_version_id\n     OR NEW.currency_code IS DISTINCT FROM OLD.currency_code OR NEW.original_principal IS DISTINCT FROM OLD.original_principal\n     OR NEW.term_periods IS DISTINCT FROM OLD.term_periods OR NEW.frequency IS DISTINCT FROM OLD.frequency\n     OR NEW.origin_branch_id IS DISTINCT FROM OLD.origin_branch_id\n     OR NEW.managing_branch_id IS DISTINCT FROM OLD.managing_branch_id\n     OR NEW.disbursement_branch_id IS DISTINCT FROM OLD.disbursement_branch_id\n     OR NEW.disbursed_at IS DISTINCT FROM OLD.disbursed_at OR NEW.maturity_date IS DISTINCT FROM OLD.maturity_date\n     OR NEW.rules_hash IS DISTINCT FROM OLD.rules_hash OR NEW.contract_hash IS DISTINCT FROM OLD.contract_hash\n     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN\n    RAISE EXCEPTION 'credit loan % is immutable: only its status may change', OLD.id;\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION credit_loan_obligations_guard() RETURNS trigger AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN\n    RAISE EXCEPTION 'credit loan obligation % cannot be deleted', OLD.id;\n  END IF;\n  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id\n     OR NEW.loan_id IS DISTINCT FROM OLD.loan_id OR NEW.sequence IS DISTINCT FROM OLD.sequence\n     OR NEW.contractual_date IS DISTINCT FROM OLD.contractual_date OR NEW.due_date IS DISTINCT FROM OLD.due_date\n     OR NEW.delinquency_starts_on IS DISTINCT FROM OLD.delinquency_starts_on\n     OR NEW.principal_due IS DISTINCT FROM OLD.principal_due OR NEW.interest_due IS DISTINCT FROM OLD.interest_due\n     OR NEW.fees_due IS DISTINCT FROM OLD.fees_due OR NEW.delinquency_due IS DISTINCT FROM OLD.delinquency_due\n     OR NEW.total_due IS DISTINCT FROM OLD.total_due OR NEW.currency_code IS DISTINCT FROM OLD.currency_code THEN\n    RAISE EXCEPTION 'credit loan obligation % is immutable: the schedule is not payment history', OLD.id;\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n"]
GUARDED_TABLES = [('credit_loans', 'credit_loans_guard', 'UPDATE OR DELETE'), ('credit_loan_disbursements', 'origination_append_only', 'UPDATE OR DELETE'), ('credit_loan_obligations', 'credit_loan_obligations_guard', 'UPDATE OR DELETE')]  # (table, function, events)
FORMALIZATION_GUARD_NEW = "\nCREATE OR REPLACE FUNCTION credit_formalizations_guard() RETURNS trigger AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN\n    RAISE EXCEPTION 'credit formalization % is immutable: DELETE refused', OLD.id;\n  END IF;\n  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id\n     OR NEW.application_id IS DISTINCT FROM OLD.application_id OR NEW.approval_id IS DISTINCT FROM OLD.approval_id\n     OR NEW.reference IS DISTINCT FROM OLD.reference OR NEW.approved_amount IS DISTINCT FROM OLD.approved_amount\n     OR NEW.currency_code IS DISTINCT FROM OLD.currency_code OR NEW.term IS DISTINCT FROM OLD.term\n     OR NEW.frequency IS DISTINCT FROM OLD.frequency OR NEW.origin_branch_id IS DISTINCT FROM OLD.origin_branch_id\n     OR NEW.managing_branch_id IS DISTINCT FROM OLD.managing_branch_id OR NEW.product_id IS DISTINCT FROM OLD.product_id\n     OR NEW.product_version_id IS DISTINCT FROM OLD.product_version_id OR NEW.rules_hash IS DISTINCT FROM OLD.rules_hash\n     OR NEW.contract_snapshot IS DISTINCT FROM OLD.contract_snapshot OR NEW.contract_hash IS DISTINCT FROM OLD.contract_hash\n     OR NEW.formalized_by IS DISTINCT FROM OLD.formalized_by OR NEW.formalized_at IS DISTINCT FROM OLD.formalized_at THEN\n    RAISE EXCEPTION 'credit formalization % is immutable: the frozen contract cannot change', OLD.id;\n  END IF;\n  IF NEW.status IS DISTINCT FROM OLD.status AND NOT (OLD.status = 'ready_for_disbursement' AND NEW.status = 'disbursed') THEN\n    RAISE EXCEPTION 'credit formalization % status transition % -> % is not allowed', OLD.id, OLD.status, NEW.status;\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n"
FORMALIZATION_GUARD_OLD = "\nCREATE OR REPLACE FUNCTION credit_formalizations_guard() RETURNS trigger AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN\n    RAISE EXCEPTION 'credit formalization % is immutable: DELETE refused', OLD.id;\n  END IF;\n  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id\n     OR NEW.application_id IS DISTINCT FROM OLD.application_id OR NEW.approval_id IS DISTINCT FROM OLD.approval_id\n     OR NEW.reference IS DISTINCT FROM OLD.reference OR NEW.approved_amount IS DISTINCT FROM OLD.approved_amount\n     OR NEW.currency_code IS DISTINCT FROM OLD.currency_code OR NEW.term IS DISTINCT FROM OLD.term\n     OR NEW.frequency IS DISTINCT FROM OLD.frequency OR NEW.origin_branch_id IS DISTINCT FROM OLD.origin_branch_id\n     OR NEW.managing_branch_id IS DISTINCT FROM OLD.managing_branch_id OR NEW.product_id IS DISTINCT FROM OLD.product_id\n     OR NEW.product_version_id IS DISTINCT FROM OLD.product_version_id OR NEW.rules_hash IS DISTINCT FROM OLD.rules_hash\n     OR NEW.contract_snapshot IS DISTINCT FROM OLD.contract_snapshot OR NEW.contract_hash IS DISTINCT FROM OLD.contract_hash\n     OR NEW.formalized_by IS DISTINCT FROM OLD.formalized_by OR NEW.formalized_at IS DISTINCT FROM OLD.formalized_at THEN\n    RAISE EXCEPTION 'credit formalization % is immutable: the frozen contract cannot change', OLD.id;\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n"


def upgrade() -> None:
    """Upgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    # must exist before the composite FKs that point at it
    op.create_unique_constraint('uq_credit_formalizations_tenant_id', 'credit_formalizations', ['tenant_id', 'id'])
    op.create_table('credit_loans',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('loan_number', sa.String(length=20), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('formalization_id', sa.Integer(), nullable=False),
    sa.Column('application_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('product_version_id', sa.Integer(), nullable=False),
    sa.Column('currency_code', sa.String(length=3), nullable=False),
    sa.Column('original_principal', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('term_periods', sa.Integer(), nullable=False),
    sa.Column('frequency', sa.String(length=12), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('origin_branch_id', sa.Integer(), nullable=False),
    sa.Column('managing_branch_id', sa.Integer(), nullable=True),
    sa.Column('disbursement_branch_id', sa.Integer(), nullable=False),
    sa.Column('disbursed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('maturity_date', sa.Date(), nullable=False),
    sa.Column('rules_hash', sa.String(length=80), nullable=False),
    sa.Column('contract_hash', sa.String(length=80), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(status IN ('pending_disbursement', 'cancelled_before_disbursement')) = (disbursed_at IS NULL)", name=op.f('ck_credit_loans_disbursed_consistent')),
    sa.CheckConstraint("status IN ('pending_disbursement', 'active', 'past_due', 'paid', 'restructured', 'refinanced', 'cancelled_before_disbursement')", name=op.f('ck_credit_loans_status_valid')),
    sa.CheckConstraint('original_principal > 0 AND term_periods >= 1', name=op.f('ck_credit_loans_terms_positive')),
    sa.ForeignKeyConstraint(['tenant_id', 'application_id'], ['credit_applications.tenant_id', 'credit_applications.id'], name='fk_credit_loans_tenant_application'),
    sa.ForeignKeyConstraint(['tenant_id', 'currency_code'], ['tenant_currencies.tenant_id', 'tenant_currencies.currency_code'], name='fk_credit_loans_tenant_currency'),
    sa.ForeignKeyConstraint(['tenant_id', 'customer_id'], ['customer_profiles.tenant_id', 'customer_profiles.id'], name='fk_credit_loans_tenant_customer'),
    sa.ForeignKeyConstraint(['tenant_id', 'disbursement_branch_id'], ['branches.company_id', 'branches.id'], name='fk_credit_loans_disbursement_branch'),
    sa.ForeignKeyConstraint(['tenant_id', 'formalization_id'], ['credit_formalizations.tenant_id', 'credit_formalizations.id'], name='fk_credit_loans_tenant_formalization'),
    sa.ForeignKeyConstraint(['tenant_id', 'managing_branch_id'], ['branches.company_id', 'branches.id'], name='fk_credit_loans_managing_branch'),
    sa.ForeignKeyConstraint(['tenant_id', 'origin_branch_id'], ['branches.company_id', 'branches.id'], name='fk_credit_loans_origin_branch'),
    sa.ForeignKeyConstraint(['tenant_id', 'product_id', 'product_version_id'], ['credit_product_versions.tenant_id', 'credit_product_versions.product_id', 'credit_product_versions.id'], name='fk_credit_loans_tenant_product_version'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_credit_loans_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_loans')),
    sa.UniqueConstraint('formalization_id', name='uq_credit_loans_formalization'),
    sa.UniqueConstraint('tenant_id', 'id', name='uq_credit_loans_tenant_id'),
    sa.UniqueConstraint('tenant_id', 'loan_number', name='uq_credit_loans_tenant_number')
    )
    op.create_index('ix_credit_loans_customer', 'credit_loans', ['customer_id'], unique=False)
    op.create_index('ix_credit_loans_status', 'credit_loans', ['tenant_id', 'status'], unique=False)
    op.create_index(op.f('ix_credit_loans_tenant_id'), 'credit_loans', ['tenant_id'], unique=False)
    op.create_table('credit_loan_disbursements',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('loan_id', sa.Integer(), nullable=False),
    sa.Column('formalization_id', sa.Integer(), nullable=False),
    sa.Column('disbursement_branch_id', sa.Integer(), nullable=False),
    sa.Column('funding_source_type', sa.String(length=20), nullable=False),
    sa.Column('cash_session_id', sa.Integer(), nullable=False),
    sa.Column('cash_movement_id', sa.Integer(), nullable=False),
    sa.Column('approved_amount', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('disbursed_amount', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('currency_code', sa.String(length=3), nullable=False),
    sa.Column('idempotency_key', sa.String(length=120), nullable=False),
    sa.Column('request_digest', sa.String(length=80), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('disbursed_by', sa.Integer(), nullable=False),
    sa.Column('disbursed_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("funding_source_type IN ('cash_session')", name=op.f('ck_credit_loan_disbursements_funding_source_valid')),
    sa.CheckConstraint("status IN ('confirmed')", name=op.f('ck_credit_loan_disbursements_status_valid')),
    sa.CheckConstraint('approved_amount > 0 AND disbursed_amount > 0', name=op.f('ck_credit_loan_disbursements_amounts_positive')),
    sa.ForeignKeyConstraint(['cash_movement_id'], ['cash_movements.id'], name=op.f('fk_credit_loan_disbursements_cash_movement_id_cash_movements')),
    sa.ForeignKeyConstraint(['cash_session_id'], ['cash_sessions.id'], name=op.f('fk_credit_loan_disbursements_cash_session_id_cash_sessions')),
    sa.ForeignKeyConstraint(['tenant_id', 'disbursement_branch_id'], ['branches.company_id', 'branches.id'], name='fk_credit_loan_disbursements_branch'),
    sa.ForeignKeyConstraint(['tenant_id', 'formalization_id'], ['credit_formalizations.tenant_id', 'credit_formalizations.id'], name='fk_credit_loan_disbursements_formalization'),
    sa.ForeignKeyConstraint(['tenant_id', 'loan_id'], ['credit_loans.tenant_id', 'credit_loans.id'], name='fk_credit_loan_disbursements_loan'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_credit_loan_disbursements_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_loan_disbursements')),
    sa.UniqueConstraint('cash_movement_id', name='uq_credit_loan_disbursements_cash_movement'),
    sa.UniqueConstraint('formalization_id', name='uq_credit_loan_disbursements_formalization'),
    sa.UniqueConstraint('loan_id', name='uq_credit_loan_disbursements_loan'),
    sa.UniqueConstraint('tenant_id', 'idempotency_key', name='uq_credit_loan_disbursements_tenant_key')
    )
    op.create_index(op.f('ix_credit_loan_disbursements_tenant_id'), 'credit_loan_disbursements', ['tenant_id'], unique=False)
    op.create_table('credit_loan_obligations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('loan_id', sa.Integer(), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('contractual_date', sa.Date(), nullable=False),
    sa.Column('due_date', sa.Date(), nullable=False),
    sa.Column('delinquency_starts_on', sa.Date(), nullable=True),
    sa.Column('principal_due', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('interest_due', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('fees_due', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('delinquency_due', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('total_due', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('currency_code', sa.String(length=3), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'partially_paid', 'paid')", name=op.f('ck_credit_loan_obligations_status_valid')),
    sa.CheckConstraint('principal_due >= 0 AND interest_due >= 0 AND fees_due >= 0 AND delinquency_due >= 0', name=op.f('ck_credit_loan_obligations_amounts_non_negative')),
    sa.CheckConstraint('sequence >= 1', name=op.f('ck_credit_loan_obligations_sequence_positive')),
    sa.CheckConstraint('total_due = principal_due + interest_due + fees_due + delinquency_due', name=op.f('ck_credit_loan_obligations_total_consistent')),
    sa.ForeignKeyConstraint(['tenant_id', 'loan_id'], ['credit_loans.tenant_id', 'credit_loans.id'], name='fk_credit_loan_obligations_loan'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_credit_loan_obligations_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_loan_obligations')),
    sa.UniqueConstraint('loan_id', 'sequence', name='uq_credit_loan_obligations_sequence')
    )
    op.create_index(op.f('ix_credit_loan_obligations_loan_id'), 'credit_loan_obligations', ['loan_id'], unique=False)
    op.create_index(op.f('ix_credit_loan_obligations_tenant_id'), 'credit_loan_obligations', ['tenant_id'], unique=False)
    # ### end Alembic commands ###
    # the formalization lifecycle gains its single transition ready_for_disbursement -> disbursed
    op.drop_constraint(op.f('ck_credit_formalizations_status_valid'), 'credit_formalizations', type_='check')
    op.create_check_constraint(op.f('ck_credit_formalizations_status_valid'), 'credit_formalizations', "status IN ('ready_for_disbursement', 'disbursed')")
    op.execute(FORMALIZATION_GUARD_NEW)
    for fn_sql in GUARD_FUNCTIONS:
        op.execute(fn_sql)
    for table, fn_name, events in GUARDED_TABLES:
        op.execute("CREATE TRIGGER trg_%s_guard BEFORE %s ON %s FOR EACH ROW EXECUTE FUNCTION %s()" % (table, events, table, fn_name))

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
    """Downgrade schema (loans, disbursements and obligations created with the new structures are dropped; the cash
    movements they made stay in the cash ledger, which this package does not own)."""
    codes = ", ".join("'%s'" % c for c, *_ in NEW_PERMISSIONS)
    op.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN (" + codes + "))")
    op.execute("DELETE FROM permissions WHERE code IN (" + codes + ")")
    for table, _fn, _events in GUARDED_TABLES:
        op.execute("DROP TRIGGER IF EXISTS trg_%s_guard ON %s" % (table, table))
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f('ix_credit_loan_obligations_tenant_id'), table_name='credit_loan_obligations')
    op.drop_index(op.f('ix_credit_loan_obligations_loan_id'), table_name='credit_loan_obligations')
    op.drop_table('credit_loan_obligations')
    op.drop_index(op.f('ix_credit_loan_disbursements_tenant_id'), table_name='credit_loan_disbursements')
    op.drop_table('credit_loan_disbursements')
    op.drop_index(op.f('ix_credit_loans_tenant_id'), table_name='credit_loans')
    op.drop_index('ix_credit_loans_status', table_name='credit_loans')
    op.drop_index('ix_credit_loans_customer', table_name='credit_loans')
    op.drop_table('credit_loans')
    op.drop_constraint('uq_credit_formalizations_tenant_id', 'credit_formalizations', type_='unique')
    # ### end Alembic commands ###
    for fn_name in ("credit_loans_guard", "credit_loan_obligations_guard"):
        op.execute("DROP FUNCTION IF EXISTS %s()" % fn_name)
    op.execute(FORMALIZATION_GUARD_OLD)  # the old guard allows any status change
    op.execute("UPDATE credit_formalizations SET status = 'ready_for_disbursement' WHERE status = 'disbursed'")
    op.drop_constraint(op.f('ck_credit_formalizations_status_valid'), 'credit_formalizations', type_='check')
    op.create_check_constraint(op.f('ck_credit_formalizations_status_valid'), 'credit_formalizations', "status IN ('ready_for_disbursement')")
    # ### end Alembic commands ###
