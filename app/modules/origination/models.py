"""Credit origination persistence (T-006): application -> evaluation -> decision/approval -> formalization.

``requested_amount`` (application) != ``approved_amount`` (approval) != disbursed amount (T-007, not here).
Nothing in this module creates debt, cash/bank movements, payments or a live loan: a formalization is a frozen,
auditable contract in ``ready_for_disbursement`` and money does not move.

Integrity is also enforced by PostgreSQL: composite tenant-safe FKs, one terminal decision and one formalization per
application (UNIQUE), append-only/immutable rows guarded by triggers (installed by ``create_all`` and by migration 0008).
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


APPLICATION_STATUSES = ("draft", "submitted", "under_review", "approved", "rejected", "cancelled", "formalized")
CONDITION_KINDS = ("guarantee_required", "guarantor_required", "document_pending", "administrative", "other")
CONDITION_STATUSES = ("pending", "fulfilled", "waived")
DOCUMENT_STATUSES = ("pending", "provided", "verified", "rejected")
DECISION_OUTCOMES = ("approved", "rejected")
FORMALIZATION_STATUSES = ("ready_for_disbursement", "disbursed")  # T-007: the only transition is ready -> disbursed


class CreditApplication(Base):
    __tablename__ = "credit_applications"
    __table_args__ = (
        CheckConstraint(_in("status", APPLICATION_STATUSES), name="status_valid"),
        CheckConstraint("requested_amount > 0", name="requested_amount_positive"),
        CheckConstraint("requested_term >= 1", name="requested_term_positive"),
        CheckConstraint(_in("requested_frequency", ("daily", "weekly", "biweekly", "monthly")), name="frequency_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer_profiles.tenant_id", "customer_profiles.id"],
            name="fk_credit_applications_tenant_customer",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["credit_products.tenant_id", "credit_products.id"],
            name="fk_credit_applications_tenant_product",
        ),
        # the pinned version must be a version OF the pinned product, inside the same tenant
        ForeignKeyConstraint(
            ["tenant_id", "product_id", "product_version_id"],
            [
                "credit_product_versions.tenant_id",
                "credit_product_versions.product_id",
                "credit_product_versions.id",
            ],
            name="fk_credit_applications_tenant_product_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "currency_code"],
            ["tenant_currencies.tenant_id", "tenant_currencies.currency_code"],
            name="fk_credit_applications_tenant_currency",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "origin_branch_id"],
            ["branches.company_id", "branches.id"],
            name="fk_credit_applications_tenant_origin_branch",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "managing_branch_id"],
            ["branches.company_id", "branches.id"],
            name="fk_credit_applications_tenant_managing_branch",
        ),
        UniqueConstraint("tenant_id", "application_number", name="uq_credit_applications_tenant_number"),
        UniqueConstraint("tenant_id", "id", name="uq_credit_applications_tenant_id"),
        Index("ix_credit_applications_customer", "customer_id"),
        Index("ix_credit_applications_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    application_number: Mapped[str] = mapped_column(String(20))
    customer_id: Mapped[int] = mapped_column(Integer)
    product_id: Mapped[int] = mapped_column(Integer)
    product_version_id: Mapped[int] = mapped_column(Integer)
    requested_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    currency_code: Mapped[str] = mapped_column(String(3))
    requested_term: Mapped[int] = mapped_column(Integer)  # in periods of the product frequency
    requested_frequency: Mapped[str] = mapped_column(String(12))
    origin_branch_id: Mapped[int] = mapped_column(Integer)
    managing_branch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    submission_count: Mapped[int] = mapped_column(Integer, default=0)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    cancelled_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)


class CreditApplicationSubmission(Base):
    """The request exactly as it was submitted. Append-only: later corrections never rewrite it."""

    __tablename__ = "credit_application_submissions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["credit_applications.tenant_id", "credit_applications.id"],
            name="fk_credit_application_submissions_tenant_application",
        ),
        UniqueConstraint("application_id", "submission_number", name="uq_credit_application_submissions_number"),
        UniqueConstraint("tenant_id", "id", name="uq_credit_application_submissions_tenant_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    application_id: Mapped[int] = mapped_column(Integer, index=True)
    submission_number: Mapped[int] = mapped_column(Integer)
    request: Mapped[dict] = mapped_column(JSONB)
    submitted_by: Mapped[int] = mapped_column(Integer)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    reopened_by: Mapped[int | None] = mapped_column(Integer, nullable=True)  # lifecycle only
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reopen_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class CreditApplicationEvaluation(Base):
    """Structured evaluation entry. Append-only; no scoring formula exists (BLOCKED_BY_SPEC)."""

    __tablename__ = "credit_application_evaluations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["credit_applications.tenant_id", "credit_applications.id"],
            name="fk_credit_application_evaluations_tenant_application",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    application_id: Mapped[int] = mapped_column(Integer, index=True)
    evaluator_id: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CreditDecision(Base):
    """The single terminal decision of an application (UNIQUE): approved XOR rejected. Immutable."""

    __tablename__ = "credit_decisions"
    __table_args__ = (
        CheckConstraint(_in("outcome", DECISION_OUTCOMES), name="outcome_valid"),
        CheckConstraint(
            "outcome <> 'rejected' OR (reason IS NOT NULL AND length(btrim(reason)) > 0)", name="reject_has_reason"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["credit_applications.tenant_id", "credit_applications.id"],
            name="fk_credit_decisions_tenant_application",
        ),
        UniqueConstraint("application_id", name="uq_credit_decisions_application"),
        UniqueConstraint("id", "outcome", name="uq_credit_decisions_id_outcome"),
        UniqueConstraint("tenant_id", "id", name="uq_credit_decisions_tenant_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    application_id: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(10))
    decided_by: Mapped[int] = mapped_column(Integer)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    application_row_version: Mapped[int] = mapped_column(Integer)  # the exact content that was reviewed


class CreditApproval(Base):
    """Explicit approved terms (never copied from the request) + the evidence of how the approval was authorised."""

    __tablename__ = "credit_approvals"
    __table_args__ = (
        CheckConstraint("approved_amount > 0 AND approved_term >= 1", name="terms_positive"),
        CheckConstraint("outcome = 'approved'", name="decision_is_approval"),
        ForeignKeyConstraint(
            ["decision_id", "outcome"],
            ["credit_decisions.id", "credit_decisions.outcome"],
            name="fk_credit_approvals_decision_outcome",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["credit_applications.tenant_id", "credit_applications.id"],
            name="fk_credit_approvals_tenant_application",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "product_id", "product_version_id"],
            [
                "credit_product_versions.tenant_id",
                "credit_product_versions.product_id",
                "credit_product_versions.id",
            ],
            name="fk_credit_approvals_tenant_product_version",
        ),
        UniqueConstraint("decision_id", name="uq_credit_approvals_decision"),
        UniqueConstraint("application_id", name="uq_credit_approvals_application"),
        UniqueConstraint("tenant_id", "id", name="uq_credit_approvals_tenant_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    decision_id: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(10), default="approved")
    application_id: Mapped[int] = mapped_column(Integer)
    approved_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    currency_code: Mapped[str] = mapped_column(String(3))
    approved_term: Mapped[int] = mapped_column(Integer)
    approved_frequency: Mapped[str] = mapped_column(String(12))
    product_id: Mapped[int] = mapped_column(Integer)
    product_version_id: Mapped[int] = mapped_column(Integer)
    rules_hash: Mapped[str] = mapped_column(String(80))
    requested_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))  # kept beside it: the delta stays auditable
    authorization: Mapped[dict] = mapped_column(JSONB)  # policy flags, limit used, maker/checker actors


class CreditApplicationCondition(Base):
    """A condition attached to an approval. ``blocks_formalization`` is chosen explicitly by the approver."""

    __tablename__ = "credit_application_conditions"
    __table_args__ = (
        CheckConstraint(_in("kind", CONDITION_KINDS), name="kind_valid"),
        CheckConstraint(_in("status", CONDITION_STATUSES), name="status_valid"),
        CheckConstraint("(status = 'pending') = (resolved_at IS NULL)", name="resolution_consistent"),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["credit_applications.tenant_id", "credit_applications.id"],
            name="fk_credit_application_conditions_tenant_application",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "decision_id"],
            ["credit_decisions.tenant_id", "credit_decisions.id"],
            name="fk_credit_application_conditions_tenant_decision",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    application_id: Mapped[int] = mapped_column(Integer, index=True)
    decision_id: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(30))
    description: Mapped[str] = mapped_column(String(500))
    blocks_formalization: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(10), default="pending")
    resolved_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CreditApplicationDocumentLink(Base):
    """Reference to a requirement/document. No binary storage here (T-014); no mandatory legal documents invented."""

    __tablename__ = "credit_application_document_links"
    __table_args__ = (
        CheckConstraint(_in("status", DOCUMENT_STATUSES), name="status_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["credit_applications.tenant_id", "credit_applications.id"],
            name="fk_credit_application_document_links_tenant_application",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    application_id: Mapped[int] = mapped_column(Integer, index=True)
    requirement: Mapped[str] = mapped_column(String(80))
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="pending")
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CreditFormalization(Base):
    """The frozen operational contract, ready for a future disbursement. One per application (UNIQUE)."""

    __tablename__ = "credit_formalizations"
    __table_args__ = (
        CheckConstraint(_in("status", FORMALIZATION_STATUSES), name="status_valid"),
        CheckConstraint("approved_amount > 0 AND term >= 1", name="terms_positive"),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["credit_applications.tenant_id", "credit_applications.id"],
            name="fk_credit_formalizations_tenant_application",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "approval_id"],
            ["credit_approvals.tenant_id", "credit_approvals.id"],
            name="fk_credit_formalizations_tenant_approval",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "product_id", "product_version_id"],
            [
                "credit_product_versions.tenant_id",
                "credit_product_versions.product_id",
                "credit_product_versions.id",
            ],
            name="fk_credit_formalizations_tenant_product_version",
        ),
        UniqueConstraint("application_id", name="uq_credit_formalizations_application"),
        UniqueConstraint("approval_id", name="uq_credit_formalizations_approval"),
        UniqueConstraint(
            "tenant_id", "id", name="uq_credit_formalizations_tenant_id"
        ),  # target of the loan FKs (T-007)
        UniqueConstraint("tenant_id", "reference", name="uq_credit_formalizations_tenant_reference"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    application_id: Mapped[int] = mapped_column(Integer)
    approval_id: Mapped[int] = mapped_column(Integer)
    reference: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), default="ready_for_disbursement")
    approved_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    currency_code: Mapped[str] = mapped_column(String(3))
    term: Mapped[int] = mapped_column(Integer)
    frequency: Mapped[str] = mapped_column(String(12))
    origin_branch_id: Mapped[int] = mapped_column(Integer)
    managing_branch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_id: Mapped[int] = mapped_column(Integer)
    product_version_id: Mapped[int] = mapped_column(Integer)
    rules_hash: Mapped[str] = mapped_column(String(80))
    contract_snapshot: Mapped[dict] = mapped_column(JSONB)
    contract_hash: Mapped[str] = mapped_column(String(80))
    formalized_by: Mapped[int] = mapped_column(Integer)
    formalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class CreditApprovalPolicy(Base):
    """Explicit approval policy of a tenant (product NULL = tenant-wide). Every flag is chosen by the tenant admin:
    there are no defaults, and without a policy nothing can be approved (BLOCKED_BY_SPEC: the business rules)."""

    __tablename__ = "credit_approval_policies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["credit_products.tenant_id", "credit_products.id"],
            name="fk_credit_approval_policies_tenant_product",
        ),
        Index(
            "uq_credit_approval_policies_tenant_default",
            "tenant_id",
            unique=True,
            postgresql_where=text("product_id IS NULL"),
        ),
        Index(
            "uq_credit_approval_policies_tenant_product",
            "tenant_id",
            "product_id",
            unique=True,
            postgresql_where=text("product_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    maker_checker_required: Mapped[bool] = mapped_column(Boolean)
    limits_enforced: Mapped[bool] = mapped_column(Boolean)
    approved_may_exceed_requested: Mapped[bool] = mapped_column(Boolean)
    evaluation_required: Mapped[bool] = mapped_column(Boolean)
    updated_by: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CreditApprovalLimit(Base):
    """Maximum approved amount a user (or a role) may authorise, per currency and optionally product/branch.
    Revocation keeps the row. The values are configured by the tenant: none are hard-coded."""

    __tablename__ = "credit_approval_limits"
    __table_args__ = (
        CheckConstraint("max_amount > 0", name="max_amount_positive"),
        CheckConstraint("(user_id IS NULL) <> (role_id IS NULL)", name="exactly_one_subject"),
        CheckConstraint("operation = 'approve'", name="operation_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["credit_products.tenant_id", "credit_products.id"],
            name="fk_credit_approval_limits_tenant_product",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "branch_id"],
            ["branches.company_id", "branches.id"],
            name="fk_credit_approval_limits_tenant_branch",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "currency_code"],
            ["tenant_currencies.tenant_id", "tenant_currencies.currency_code"],
            name="fk_credit_approval_limits_tenant_currency",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    role_id: Mapped[int | None] = mapped_column(ForeignKey("roles.id"), nullable=True, index=True)
    operation: Mapped[str] = mapped_column(String(10), default="approve")
    currency_code: Mapped[str] = mapped_column(String(3))
    product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    branch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    revoked_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --- immutability guards (installed by create_all AND by migration 0008) -----------------------------
IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION origination_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'origination record % is immutable (append-only): % refused', TG_TABLE_NAME, TG_OP;
END $$ LANGUAGE plpgsql
"""
FORMALIZATION_GUARD_FN = """
CREATE OR REPLACE FUNCTION credit_formalizations_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'credit formalization % is immutable: DELETE refused', OLD.id;
  END IF;
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
     OR NEW.application_id IS DISTINCT FROM OLD.application_id OR NEW.approval_id IS DISTINCT FROM OLD.approval_id
     OR NEW.reference IS DISTINCT FROM OLD.reference OR NEW.approved_amount IS DISTINCT FROM OLD.approved_amount
     OR NEW.currency_code IS DISTINCT FROM OLD.currency_code OR NEW.term IS DISTINCT FROM OLD.term
     OR NEW.frequency IS DISTINCT FROM OLD.frequency OR NEW.origin_branch_id IS DISTINCT FROM OLD.origin_branch_id
     OR NEW.managing_branch_id IS DISTINCT FROM OLD.managing_branch_id OR NEW.product_id IS DISTINCT FROM OLD.product_id
     OR NEW.product_version_id IS DISTINCT FROM OLD.product_version_id OR NEW.rules_hash IS DISTINCT FROM OLD.rules_hash
     OR NEW.contract_snapshot IS DISTINCT FROM OLD.contract_snapshot OR NEW.contract_hash IS DISTINCT FROM OLD.contract_hash
     OR NEW.formalized_by IS DISTINCT FROM OLD.formalized_by OR NEW.formalized_at IS DISTINCT FROM OLD.formalized_at THEN
    RAISE EXCEPTION 'credit formalization % is immutable: the frozen contract cannot change', OLD.id;
  END IF;
  IF NEW.status IS DISTINCT FROM OLD.status AND NOT (OLD.status = 'ready_for_disbursement' AND NEW.status = 'disbursed') THEN
    RAISE EXCEPTION 'credit formalization % status transition % -> % is not allowed', OLD.id, OLD.status, NEW.status;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""
CONDITION_GUARD_FN = """
CREATE OR REPLACE FUNCTION credit_application_conditions_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'credit application condition % cannot be deleted', OLD.id;
  END IF;
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
     OR NEW.application_id IS DISTINCT FROM OLD.application_id OR NEW.decision_id IS DISTINCT FROM OLD.decision_id
     OR NEW.kind IS DISTINCT FROM OLD.kind OR NEW.description IS DISTINCT FROM OLD.description
     OR NEW.blocks_formalization IS DISTINCT FROM OLD.blocks_formalization THEN
    RAISE EXCEPTION 'credit application condition % definition is immutable (only its resolution changes)', OLD.id;
  END IF;
  IF OLD.status <> 'pending' AND NEW.status IS DISTINCT FROM OLD.status THEN
    RAISE EXCEPTION 'credit application condition % is already resolved', OLD.id;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""
SUBMISSION_GUARD_FN = """
CREATE OR REPLACE FUNCTION credit_application_submissions_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'credit application submission % cannot be deleted', OLD.id;
  END IF;
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
     OR NEW.application_id IS DISTINCT FROM OLD.application_id OR NEW.submission_number IS DISTINCT FROM OLD.submission_number
     OR NEW.request IS DISTINCT FROM OLD.request OR NEW.submitted_by IS DISTINCT FROM OLD.submitted_by
     OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at THEN
    RAISE EXCEPTION 'credit application submission % is immutable: the submitted request cannot change', OLD.id;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""
# (table, function name, function source, trigger events)
GUARDS: tuple[tuple[str, str, str, str], ...] = (
    ("credit_application_evaluations", "origination_append_only", IMMUTABLE_FN, "UPDATE OR DELETE"),
    ("credit_decisions", "origination_append_only", IMMUTABLE_FN, "UPDATE OR DELETE"),
    ("credit_approvals", "origination_append_only", IMMUTABLE_FN, "UPDATE OR DELETE"),
    ("credit_formalizations", "credit_formalizations_guard", FORMALIZATION_GUARD_FN, "UPDATE OR DELETE"),
    ("credit_application_conditions", "credit_application_conditions_guard", CONDITION_GUARD_FN, "UPDATE OR DELETE"),
    ("credit_application_submissions", "credit_application_submissions_guard", SUBMISSION_GUARD_FN, "UPDATE OR DELETE"),
)


def guard_trigger_sql(table: str, fn_name: str, events: str) -> str:
    return f"CREATE TRIGGER trg_{table}_guard BEFORE {events} ON {table} FOR EACH ROW EXECUTE FUNCTION {fn_name}()"


_TABLES = Base.metadata.tables
for _table_name, _fn_name, _fn_sql, _events in GUARDS:
    _t = _TABLES[_table_name]
    event.listen(_t, "after_create", DDL(_fn_sql.replace("%", "%%")).execute_if(dialect="postgresql"))
    event.listen(
        _t, "after_create", DDL(guard_trigger_sql(_table_name, _fn_name, _events)).execute_if(dialect="postgresql")
    )
