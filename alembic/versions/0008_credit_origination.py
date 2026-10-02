"""credit origination

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-01 20:46:33.074576

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_PERMISSIONS = [
    ('credit.applications.read', 'Consultar solicitudes de credito', False),
    ('credit.applications.create', 'Crear solicitudes de credito', False),
    ('credit.applications.update_draft', 'Editar borradores y reabrir solicitudes enviadas', False),
    ('credit.applications.submit', 'Enviar solicitudes de credito', False),
    ('credit.applications.evaluate', 'Revisar, evaluar y gestionar requisitos', True),
    ('credit.applications.approve', 'Aprobar solicitudes (sujeto a politica y limites)', True),
    ('credit.applications.reject', 'Rechazar solicitudes de credito', True),
    ('credit.applications.cancel', 'Cancelar solicitudes no formalizadas', True),
    ('credit.applications.formalize', 'Formalizar solicitudes aprobadas', True),
    ('credit.approval_policy.manage', 'Configurar la politica de aprobacion de credito', True),
    ('credit.approval_limits.manage', 'Configurar limites de aprobacion', True),
]
# Immutability guards (kept in sync with app.modules.origination.models; copied so the migration is self-contained)
GUARD_FUNCTIONS = ["\nCREATE OR REPLACE FUNCTION origination_append_only() RETURNS trigger AS $$\nBEGIN\n  RAISE EXCEPTION 'origination record % is immutable (append-only): % refused', TG_TABLE_NAME, TG_OP;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION credit_formalizations_guard() RETURNS trigger AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN\n    RAISE EXCEPTION 'credit formalization % is immutable: DELETE refused', OLD.id;\n  END IF;\n  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id\n     OR NEW.application_id IS DISTINCT FROM OLD.application_id OR NEW.approval_id IS DISTINCT FROM OLD.approval_id\n     OR NEW.reference IS DISTINCT FROM OLD.reference OR NEW.approved_amount IS DISTINCT FROM OLD.approved_amount\n     OR NEW.currency_code IS DISTINCT FROM OLD.currency_code OR NEW.term IS DISTINCT FROM OLD.term\n     OR NEW.frequency IS DISTINCT FROM OLD.frequency OR NEW.origin_branch_id IS DISTINCT FROM OLD.origin_branch_id\n     OR NEW.managing_branch_id IS DISTINCT FROM OLD.managing_branch_id OR NEW.product_id IS DISTINCT FROM OLD.product_id\n     OR NEW.product_version_id IS DISTINCT FROM OLD.product_version_id OR NEW.rules_hash IS DISTINCT FROM OLD.rules_hash\n     OR NEW.contract_snapshot IS DISTINCT FROM OLD.contract_snapshot OR NEW.contract_hash IS DISTINCT FROM OLD.contract_hash\n     OR NEW.formalized_by IS DISTINCT FROM OLD.formalized_by OR NEW.formalized_at IS DISTINCT FROM OLD.formalized_at THEN\n    RAISE EXCEPTION 'credit formalization % is immutable: the frozen contract cannot change', OLD.id;\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION credit_application_conditions_guard() RETURNS trigger AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN\n    RAISE EXCEPTION 'credit application condition % cannot be deleted', OLD.id;\n  END IF;\n  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id\n     OR NEW.application_id IS DISTINCT FROM OLD.application_id OR NEW.decision_id IS DISTINCT FROM OLD.decision_id\n     OR NEW.kind IS DISTINCT FROM OLD.kind OR NEW.description IS DISTINCT FROM OLD.description\n     OR NEW.blocks_formalization IS DISTINCT FROM OLD.blocks_formalization THEN\n    RAISE EXCEPTION 'credit application condition % definition is immutable (only its resolution changes)', OLD.id;\n  END IF;\n  IF OLD.status <> 'pending' AND NEW.status IS DISTINCT FROM OLD.status THEN\n    RAISE EXCEPTION 'credit application condition % is already resolved', OLD.id;\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n", "\nCREATE OR REPLACE FUNCTION credit_application_submissions_guard() RETURNS trigger AS $$\nBEGIN\n  IF TG_OP = 'DELETE' THEN\n    RAISE EXCEPTION 'credit application submission % cannot be deleted', OLD.id;\n  END IF;\n  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id\n     OR NEW.application_id IS DISTINCT FROM OLD.application_id OR NEW.submission_number IS DISTINCT FROM OLD.submission_number\n     OR NEW.request IS DISTINCT FROM OLD.request OR NEW.submitted_by IS DISTINCT FROM OLD.submitted_by\n     OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at THEN\n    RAISE EXCEPTION 'credit application submission % is immutable: the submitted request cannot change', OLD.id;\n  END IF;\n  RETURN NEW;\nEND $$ LANGUAGE plpgsql\n"]
GUARDED_TABLES = [('credit_application_evaluations', 'origination_append_only', 'UPDATE OR DELETE'), ('credit_decisions', 'origination_append_only', 'UPDATE OR DELETE'), ('credit_approvals', 'origination_append_only', 'UPDATE OR DELETE'), ('credit_formalizations', 'credit_formalizations_guard', 'UPDATE OR DELETE'), ('credit_application_conditions', 'credit_application_conditions_guard', 'UPDATE OR DELETE'), ('credit_application_submissions', 'credit_application_submissions_guard', 'UPDATE OR DELETE')]  # (table, function, events)


def upgrade() -> None:
    """Upgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    # must exist before the composite FKs that point at it
    op.create_unique_constraint('uq_credit_product_versions_tenant_product_id', 'credit_product_versions', ['tenant_id', 'product_id', 'id'])
    op.create_table('credit_approval_policies',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=True),
    sa.Column('maker_checker_required', sa.Boolean(), nullable=False),
    sa.Column('limits_enforced', sa.Boolean(), nullable=False),
    sa.Column('approved_may_exceed_requested', sa.Boolean(), nullable=False),
    sa.Column('evaluation_required', sa.Boolean(), nullable=False),
    sa.Column('updated_by', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id', 'product_id'], ['credit_products.tenant_id', 'credit_products.id'], name='fk_credit_approval_policies_tenant_product'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_credit_approval_policies_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_approval_policies'))
    )
    op.create_index(op.f('ix_credit_approval_policies_tenant_id'), 'credit_approval_policies', ['tenant_id'], unique=False)
    op.create_index('uq_credit_approval_policies_tenant_default', 'credit_approval_policies', ['tenant_id'], unique=True, postgresql_where=sa.text('product_id IS NULL'))
    op.create_index('uq_credit_approval_policies_tenant_product', 'credit_approval_policies', ['tenant_id', 'product_id'], unique=True, postgresql_where=sa.text('product_id IS NOT NULL'))
    op.create_table('credit_approval_limits',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('role_id', sa.Integer(), nullable=True),
    sa.Column('operation', sa.String(length=10), nullable=False),
    sa.Column('currency_code', sa.String(length=3), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=True),
    sa.Column('branch_id', sa.Integer(), nullable=True),
    sa.Column('max_amount', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_by', sa.Integer(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("operation = 'approve'", name=op.f('ck_credit_approval_limits_operation_valid')),
    sa.CheckConstraint('(user_id IS NULL) <> (role_id IS NULL)', name=op.f('ck_credit_approval_limits_exactly_one_subject')),
    sa.CheckConstraint('max_amount > 0', name=op.f('ck_credit_approval_limits_max_amount_positive')),
    sa.ForeignKeyConstraint(['role_id'], ['roles.id'], name=op.f('fk_credit_approval_limits_role_id_roles')),
    sa.ForeignKeyConstraint(['tenant_id', 'branch_id'], ['branches.company_id', 'branches.id'], name='fk_credit_approval_limits_tenant_branch'),
    sa.ForeignKeyConstraint(['tenant_id', 'currency_code'], ['tenant_currencies.tenant_id', 'tenant_currencies.currency_code'], name='fk_credit_approval_limits_tenant_currency'),
    sa.ForeignKeyConstraint(['tenant_id', 'product_id'], ['credit_products.tenant_id', 'credit_products.id'], name='fk_credit_approval_limits_tenant_product'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_credit_approval_limits_tenant_id_companies')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_credit_approval_limits_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_approval_limits'))
    )
    op.create_index(op.f('ix_credit_approval_limits_role_id'), 'credit_approval_limits', ['role_id'], unique=False)
    op.create_index(op.f('ix_credit_approval_limits_tenant_id'), 'credit_approval_limits', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_credit_approval_limits_user_id'), 'credit_approval_limits', ['user_id'], unique=False)
    op.create_table('credit_applications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('application_number', sa.String(length=20), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('product_version_id', sa.Integer(), nullable=False),
    sa.Column('requested_amount', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('currency_code', sa.String(length=3), nullable=False),
    sa.Column('requested_term', sa.Integer(), nullable=False),
    sa.Column('requested_frequency', sa.String(length=12), nullable=False),
    sa.Column('origin_branch_id', sa.Integer(), nullable=False),
    sa.Column('managing_branch_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('row_version', sa.Integer(), nullable=False),
    sa.Column('submission_count', sa.Integer(), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('submitted_by', sa.Integer(), nullable=True),
    sa.Column('created_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('cancelled_by', sa.Integer(), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancellation_reason', sa.Text(), nullable=True),
    sa.Column('cancelled_from_status', sa.String(length=20), nullable=True),
    sa.CheckConstraint("requested_frequency IN ('daily', 'weekly', 'biweekly', 'monthly')", name=op.f('ck_credit_applications_frequency_valid')),
    sa.CheckConstraint("status IN ('draft', 'submitted', 'under_review', 'approved', 'rejected', 'cancelled', 'formalized')", name=op.f('ck_credit_applications_status_valid')),
    sa.CheckConstraint('requested_amount > 0', name=op.f('ck_credit_applications_requested_amount_positive')),
    sa.CheckConstraint('requested_term >= 1', name=op.f('ck_credit_applications_requested_term_positive')),
    sa.ForeignKeyConstraint(['tenant_id', 'currency_code'], ['tenant_currencies.tenant_id', 'tenant_currencies.currency_code'], name='fk_credit_applications_tenant_currency'),
    sa.ForeignKeyConstraint(['tenant_id', 'customer_id'], ['customer_profiles.tenant_id', 'customer_profiles.id'], name='fk_credit_applications_tenant_customer'),
    sa.ForeignKeyConstraint(['tenant_id', 'managing_branch_id'], ['branches.company_id', 'branches.id'], name='fk_credit_applications_tenant_managing_branch'),
    sa.ForeignKeyConstraint(['tenant_id', 'origin_branch_id'], ['branches.company_id', 'branches.id'], name='fk_credit_applications_tenant_origin_branch'),
    sa.ForeignKeyConstraint(['tenant_id', 'product_id', 'product_version_id'], ['credit_product_versions.tenant_id', 'credit_product_versions.product_id', 'credit_product_versions.id'], name='fk_credit_applications_tenant_product_version'),
    sa.ForeignKeyConstraint(['tenant_id', 'product_id'], ['credit_products.tenant_id', 'credit_products.id'], name='fk_credit_applications_tenant_product'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_credit_applications_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_applications')),
    sa.UniqueConstraint('tenant_id', 'application_number', name='uq_credit_applications_tenant_number'),
    sa.UniqueConstraint('tenant_id', 'id', name='uq_credit_applications_tenant_id')
    )
    op.create_index('ix_credit_applications_customer', 'credit_applications', ['customer_id'], unique=False)
    op.create_index('ix_credit_applications_status', 'credit_applications', ['tenant_id', 'status'], unique=False)
    op.create_index(op.f('ix_credit_applications_tenant_id'), 'credit_applications', ['tenant_id'], unique=False)
    op.create_table('credit_application_document_links',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('application_id', sa.Integer(), nullable=False),
    sa.Column('requirement', sa.String(length=80), nullable=False),
    sa.Column('reference', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('note', sa.String(length=500), nullable=True),
    sa.Column('created_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_by', sa.Integer(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('pending', 'provided', 'verified', 'rejected')", name=op.f('ck_credit_application_document_links_status_valid')),
    sa.ForeignKeyConstraint(['tenant_id', 'application_id'], ['credit_applications.tenant_id', 'credit_applications.id'], name='fk_credit_application_document_links_tenant_application'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_credit_application_document_links_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_application_document_links'))
    )
    op.create_index(op.f('ix_credit_application_document_links_application_id'), 'credit_application_document_links', ['application_id'], unique=False)
    op.create_index(op.f('ix_credit_application_document_links_tenant_id'), 'credit_application_document_links', ['tenant_id'], unique=False)
    op.create_table('credit_application_evaluations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('application_id', sa.Integer(), nullable=False),
    sa.Column('evaluator_id', sa.Integer(), nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id', 'application_id'], ['credit_applications.tenant_id', 'credit_applications.id'], name='fk_credit_application_evaluations_tenant_application'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_credit_application_evaluations_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_application_evaluations'))
    )
    op.create_index(op.f('ix_credit_application_evaluations_application_id'), 'credit_application_evaluations', ['application_id'], unique=False)
    op.create_index(op.f('ix_credit_application_evaluations_tenant_id'), 'credit_application_evaluations', ['tenant_id'], unique=False)
    op.create_table('credit_application_submissions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('application_id', sa.Integer(), nullable=False),
    sa.Column('submission_number', sa.Integer(), nullable=False),
    sa.Column('request', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('submitted_by', sa.Integer(), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('reopened_by', sa.Integer(), nullable=True),
    sa.Column('reopened_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reopen_reason', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['tenant_id', 'application_id'], ['credit_applications.tenant_id', 'credit_applications.id'], name='fk_credit_application_submissions_tenant_application'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_credit_application_submissions_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_application_submissions')),
    sa.UniqueConstraint('application_id', 'submission_number', name='uq_credit_application_submissions_number'),
    sa.UniqueConstraint('tenant_id', 'id', name='uq_credit_application_submissions_tenant_id')
    )
    op.create_index(op.f('ix_credit_application_submissions_application_id'), 'credit_application_submissions', ['application_id'], unique=False)
    op.create_index(op.f('ix_credit_application_submissions_tenant_id'), 'credit_application_submissions', ['tenant_id'], unique=False)
    op.create_table('credit_decisions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('application_id', sa.Integer(), nullable=False),
    sa.Column('outcome', sa.String(length=10), nullable=False),
    sa.Column('decided_by', sa.Integer(), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('application_row_version', sa.Integer(), nullable=False),
    sa.CheckConstraint("outcome <> 'rejected' OR (reason IS NOT NULL AND length(btrim(reason)) > 0)", name=op.f('ck_credit_decisions_reject_has_reason')),
    sa.CheckConstraint("outcome IN ('approved', 'rejected')", name=op.f('ck_credit_decisions_outcome_valid')),
    sa.ForeignKeyConstraint(['tenant_id', 'application_id'], ['credit_applications.tenant_id', 'credit_applications.id'], name='fk_credit_decisions_tenant_application'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_credit_decisions_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_decisions')),
    sa.UniqueConstraint('application_id', name='uq_credit_decisions_application'),
    sa.UniqueConstraint('id', 'outcome', name='uq_credit_decisions_id_outcome'),
    sa.UniqueConstraint('tenant_id', 'id', name='uq_credit_decisions_tenant_id')
    )
    op.create_index(op.f('ix_credit_decisions_tenant_id'), 'credit_decisions', ['tenant_id'], unique=False)
    op.create_table('credit_application_conditions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('application_id', sa.Integer(), nullable=False),
    sa.Column('decision_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=False),
    sa.Column('blocks_formalization', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('resolved_by', sa.Integer(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolution_note', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(status = 'pending') = (resolved_at IS NULL)", name=op.f('ck_credit_application_conditions_resolution_consistent')),
    sa.CheckConstraint("kind IN ('guarantee_required', 'guarantor_required', 'document_pending', 'administrative', 'other')", name=op.f('ck_credit_application_conditions_kind_valid')),
    sa.CheckConstraint("status IN ('pending', 'fulfilled', 'waived')", name=op.f('ck_credit_application_conditions_status_valid')),
    sa.ForeignKeyConstraint(['tenant_id', 'application_id'], ['credit_applications.tenant_id', 'credit_applications.id'], name='fk_credit_application_conditions_tenant_application'),
    sa.ForeignKeyConstraint(['tenant_id', 'decision_id'], ['credit_decisions.tenant_id', 'credit_decisions.id'], name='fk_credit_application_conditions_tenant_decision'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_credit_application_conditions_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_application_conditions'))
    )
    op.create_index(op.f('ix_credit_application_conditions_application_id'), 'credit_application_conditions', ['application_id'], unique=False)
    op.create_index(op.f('ix_credit_application_conditions_tenant_id'), 'credit_application_conditions', ['tenant_id'], unique=False)
    op.create_table('credit_approvals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('decision_id', sa.Integer(), nullable=False),
    sa.Column('outcome', sa.String(length=10), nullable=False),
    sa.Column('application_id', sa.Integer(), nullable=False),
    sa.Column('approved_amount', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('currency_code', sa.String(length=3), nullable=False),
    sa.Column('approved_term', sa.Integer(), nullable=False),
    sa.Column('approved_frequency', sa.String(length=12), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('product_version_id', sa.Integer(), nullable=False),
    sa.Column('rules_hash', sa.String(length=80), nullable=False),
    sa.Column('requested_amount', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('authorization', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.CheckConstraint("outcome = 'approved'", name=op.f('ck_credit_approvals_decision_is_approval')),
    sa.CheckConstraint('approved_amount > 0 AND approved_term >= 1', name=op.f('ck_credit_approvals_terms_positive')),
    sa.ForeignKeyConstraint(['decision_id', 'outcome'], ['credit_decisions.id', 'credit_decisions.outcome'], name='fk_credit_approvals_decision_outcome'),
    sa.ForeignKeyConstraint(['tenant_id', 'application_id'], ['credit_applications.tenant_id', 'credit_applications.id'], name='fk_credit_approvals_tenant_application'),
    sa.ForeignKeyConstraint(['tenant_id', 'product_id', 'product_version_id'], ['credit_product_versions.tenant_id', 'credit_product_versions.product_id', 'credit_product_versions.id'], name='fk_credit_approvals_tenant_product_version'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_credit_approvals_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_approvals')),
    sa.UniqueConstraint('application_id', name='uq_credit_approvals_application'),
    sa.UniqueConstraint('decision_id', name='uq_credit_approvals_decision'),
    sa.UniqueConstraint('tenant_id', 'id', name='uq_credit_approvals_tenant_id')
    )
    op.create_index(op.f('ix_credit_approvals_tenant_id'), 'credit_approvals', ['tenant_id'], unique=False)
    op.create_table('credit_formalizations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('application_id', sa.Integer(), nullable=False),
    sa.Column('approval_id', sa.Integer(), nullable=False),
    sa.Column('reference', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('approved_amount', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('currency_code', sa.String(length=3), nullable=False),
    sa.Column('term', sa.Integer(), nullable=False),
    sa.Column('frequency', sa.String(length=12), nullable=False),
    sa.Column('origin_branch_id', sa.Integer(), nullable=False),
    sa.Column('managing_branch_id', sa.Integer(), nullable=True),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('product_version_id', sa.Integer(), nullable=False),
    sa.Column('rules_hash', sa.String(length=80), nullable=False),
    sa.Column('contract_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('contract_hash', sa.String(length=80), nullable=False),
    sa.Column('formalized_by', sa.Integer(), nullable=False),
    sa.Column('formalized_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('ready_for_disbursement')", name=op.f('ck_credit_formalizations_status_valid')),
    sa.CheckConstraint('approved_amount > 0 AND term >= 1', name=op.f('ck_credit_formalizations_terms_positive')),
    sa.ForeignKeyConstraint(['tenant_id', 'application_id'], ['credit_applications.tenant_id', 'credit_applications.id'], name='fk_credit_formalizations_tenant_application'),
    sa.ForeignKeyConstraint(['tenant_id', 'approval_id'], ['credit_approvals.tenant_id', 'credit_approvals.id'], name='fk_credit_formalizations_tenant_approval'),
    sa.ForeignKeyConstraint(['tenant_id', 'product_id', 'product_version_id'], ['credit_product_versions.tenant_id', 'credit_product_versions.product_id', 'credit_product_versions.id'], name='fk_credit_formalizations_tenant_product_version'),
    sa.ForeignKeyConstraint(['tenant_id'], ['companies.id'], name=op.f('fk_credit_formalizations_tenant_id_companies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_credit_formalizations')),
    sa.UniqueConstraint('application_id', name='uq_credit_formalizations_application'),
    sa.UniqueConstraint('approval_id', name='uq_credit_formalizations_approval'),
    sa.UniqueConstraint('tenant_id', 'reference', name='uq_credit_formalizations_tenant_reference')
    )
    op.create_index(op.f('ix_credit_formalizations_tenant_id'), 'credit_formalizations', ['tenant_id'], unique=False)
    # ### end Alembic commands ###
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
    """Downgrade schema (origination data created with the new structures is dropped)."""
    codes = ", ".join("'%s'" % c for c, *_ in NEW_PERMISSIONS)
    op.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN (" + codes + "))")
    op.execute("DELETE FROM permissions WHERE code IN (" + codes + ")")
    for table, _fn, _events in GUARDED_TABLES:
        op.execute("DROP TRIGGER IF EXISTS trg_%s_guard ON %s" % (table, table))
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f('ix_credit_formalizations_tenant_id'), table_name='credit_formalizations')
    op.drop_table('credit_formalizations')
    op.drop_index(op.f('ix_credit_approvals_tenant_id'), table_name='credit_approvals')
    op.drop_table('credit_approvals')
    op.drop_index(op.f('ix_credit_application_conditions_tenant_id'), table_name='credit_application_conditions')
    op.drop_index(op.f('ix_credit_application_conditions_application_id'), table_name='credit_application_conditions')
    op.drop_table('credit_application_conditions')
    op.drop_index(op.f('ix_credit_decisions_tenant_id'), table_name='credit_decisions')
    op.drop_table('credit_decisions')
    op.drop_index(op.f('ix_credit_application_submissions_tenant_id'), table_name='credit_application_submissions')
    op.drop_index(op.f('ix_credit_application_submissions_application_id'), table_name='credit_application_submissions')
    op.drop_table('credit_application_submissions')
    op.drop_index(op.f('ix_credit_application_evaluations_tenant_id'), table_name='credit_application_evaluations')
    op.drop_index(op.f('ix_credit_application_evaluations_application_id'), table_name='credit_application_evaluations')
    op.drop_table('credit_application_evaluations')
    op.drop_index(op.f('ix_credit_application_document_links_tenant_id'), table_name='credit_application_document_links')
    op.drop_index(op.f('ix_credit_application_document_links_application_id'), table_name='credit_application_document_links')
    op.drop_table('credit_application_document_links')
    op.drop_index(op.f('ix_credit_applications_tenant_id'), table_name='credit_applications')
    op.drop_index('ix_credit_applications_status', table_name='credit_applications')
    op.drop_index('ix_credit_applications_customer', table_name='credit_applications')
    op.drop_table('credit_applications')
    op.drop_index(op.f('ix_credit_approval_limits_user_id'), table_name='credit_approval_limits')
    op.drop_index(op.f('ix_credit_approval_limits_tenant_id'), table_name='credit_approval_limits')
    op.drop_index(op.f('ix_credit_approval_limits_role_id'), table_name='credit_approval_limits')
    op.drop_table('credit_approval_limits')
    op.drop_index('uq_credit_approval_policies_tenant_product', table_name='credit_approval_policies', postgresql_where=sa.text('product_id IS NOT NULL'))
    op.drop_index('uq_credit_approval_policies_tenant_default', table_name='credit_approval_policies', postgresql_where=sa.text('product_id IS NULL'))
    op.drop_index(op.f('ix_credit_approval_policies_tenant_id'), table_name='credit_approval_policies')
    op.drop_table('credit_approval_policies')
    op.drop_constraint('uq_credit_product_versions_tenant_product_id', 'credit_product_versions', type_='unique')
    # ### end Alembic commands ###
    for fn_name in ("origination_append_only", "credit_formalizations_guard", "credit_application_conditions_guard", "credit_application_submissions_guard"):
        op.execute("DROP FUNCTION IF EXISTS %s()" % fn_name)
    # ### end Alembic commands ###
