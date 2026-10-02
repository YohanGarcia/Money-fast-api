"""Loan birth (T-007): CreditLoan, CreditLoanDisbursement, CreditLoanObligation.

``requested_amount != approved_amount != disbursed_amount``: the disbursed amount exists only here, copied from the
confirmed cash movement, never from the request. A loan is born ``active`` in the SAME transaction as the confirmed cash
withdrawal, its disbursement record and its contractual schedule; there is no state in which one exists without the others.
The schedule is a contractual projection (``credit_loan_obligations``) derived from the frozen T-006 contract, never from
the live product. Payments, applications, reversals and adjustments are NOT here (deferred).

Tables carry the ``credit_`` prefix: legacy ``loans`` / ``loan_installments`` / ``payments`` stay untouched.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    DDL,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.modules.origination.models import IMMUTABLE_FN


def _now() -> datetime:
    return datetime.now(UTC)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


LOAN_STATUSES = (
    "pending_disbursement",
    "active",
    "past_due",
    "paid",
    "restructured",
    "refinanced",
    "cancelled_before_disbursement",
)  # T-007 only ever creates 'active'; the rest are reserved for the runtime packages
OBLIGATION_STATUSES = ("pending", "partially_paid", "paid")


class CreditLoan(Base):
    __tablename__ = "credit_loans"
    __table_args__ = (
        CheckConstraint(_in("status", LOAN_STATUSES), name="status_valid"),
        CheckConstraint("original_principal > 0 AND term_periods >= 1", name="terms_positive"),
        CheckConstraint(
            "(status IN ('pending_disbursement', 'cancelled_before_disbursement')) = (disbursed_at IS NULL)",
            name="disbursed_consistent",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer_profiles.tenant_id", "customer_profiles.id"],
            name="fk_credit_loans_tenant_customer",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "formalization_id"],
            ["credit_formalizations.tenant_id", "credit_formalizations.id"],
            name="fk_credit_loans_tenant_formalization",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["credit_applications.tenant_id", "credit_applications.id"],
            name="fk_credit_loans_tenant_application",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "product_id", "product_version_id"],
            [
                "credit_product_versions.tenant_id",
                "credit_product_versions.product_id",
                "credit_product_versions.id",
            ],
            name="fk_credit_loans_tenant_product_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "currency_code"],
            ["tenant_currencies.tenant_id", "tenant_currencies.currency_code"],
            name="fk_credit_loans_tenant_currency",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "origin_branch_id"],
            ["branches.company_id", "branches.id"],
            name="fk_credit_loans_origin_branch",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "managing_branch_id"],
            ["branches.company_id", "branches.id"],
            name="fk_credit_loans_managing_branch",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "disbursement_branch_id"],
            ["branches.company_id", "branches.id"],
            name="fk_credit_loans_disbursement_branch",
        ),
        UniqueConstraint("tenant_id", "loan_number", name="uq_credit_loans_tenant_number"),
        UniqueConstraint("formalization_id", name="uq_credit_loans_formalization"),  # one loan per formalized contract
        UniqueConstraint("tenant_id", "id", name="uq_credit_loans_tenant_id"),
        Index("ix_credit_loans_customer", "customer_id"),
        Index("ix_credit_loans_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    loan_number: Mapped[str] = mapped_column(String(20))
    customer_id: Mapped[int] = mapped_column(Integer)
    formalization_id: Mapped[int] = mapped_column(Integer)
    application_id: Mapped[int] = mapped_column(Integer)
    product_id: Mapped[int] = mapped_column(Integer)
    product_version_id: Mapped[int] = mapped_column(Integer)
    currency_code: Mapped[str] = mapped_column(String(3))
    original_principal: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    term_periods: Mapped[int] = mapped_column(Integer)
    frequency: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(30), default="active")
    origin_branch_id: Mapped[int] = mapped_column(Integer)
    managing_branch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disbursement_branch_id: Mapped[int] = mapped_column(Integer)
    disbursed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    maturity_date: Mapped[date] = mapped_column(Date)  # effective due date of the last obligation
    rules_hash: Mapped[str] = mapped_column(
        String(80)
    )  # the contract (and its T-005 snapshot) stay in the formalization
    contract_hash: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class CreditLoanDisbursement(Base):
    """The confirmed disbursement. One per formalized contract, append-only."""

    __tablename__ = "credit_loan_disbursements"
    __table_args__ = (
        CheckConstraint("approved_amount > 0 AND disbursed_amount > 0", name="amounts_positive"),
        CheckConstraint(_in("status", ("confirmed",)), name="status_valid"),
        CheckConstraint(_in("funding_source_type", ("cash_session",)), name="funding_source_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "loan_id"],
            ["credit_loans.tenant_id", "credit_loans.id"],
            name="fk_credit_loan_disbursements_loan",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "formalization_id"],
            ["credit_formalizations.tenant_id", "credit_formalizations.id"],
            name="fk_credit_loan_disbursements_formalization",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "disbursement_branch_id"],
            ["branches.company_id", "branches.id"],
            name="fk_credit_loan_disbursements_branch",
        ),
        UniqueConstraint("loan_id", name="uq_credit_loan_disbursements_loan"),
        UniqueConstraint("formalization_id", name="uq_credit_loan_disbursements_formalization"),
        UniqueConstraint("cash_movement_id", name="uq_credit_loan_disbursements_cash_movement"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_credit_loan_disbursements_tenant_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    loan_id: Mapped[int] = mapped_column(Integer)
    formalization_id: Mapped[int] = mapped_column(Integer)
    disbursement_branch_id: Mapped[int] = mapped_column(Integer)
    funding_source_type: Mapped[str] = mapped_column(String(20))
    cash_session_id: Mapped[int] = mapped_column(ForeignKey("cash_sessions.id"))
    cash_movement_id: Mapped[int] = mapped_column(ForeignKey("cash_movements.id"))
    approved_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    disbursed_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    currency_code: Mapped[str] = mapped_column(String(3))
    idempotency_key: Mapped[str] = mapped_column(String(120))
    request_digest: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="confirmed")
    disbursed_by: Mapped[int] = mapped_column(Integer)
    disbursed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CreditLoanObligation(Base):
    """One scheduled obligation (installment). The schedule is NOT payment history: its amounts never change."""

    __tablename__ = "credit_loan_obligations"
    __table_args__ = (
        CheckConstraint(_in("status", OBLIGATION_STATUSES), name="status_valid"),
        CheckConstraint(
            "principal_due >= 0 AND interest_due >= 0 AND fees_due >= 0 AND delinquency_due >= 0",
            name="amounts_non_negative",
        ),
        CheckConstraint(
            "total_due = principal_due + interest_due + fees_due + delinquency_due", name="total_consistent"
        ),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        ForeignKeyConstraint(
            ["tenant_id", "loan_id"],
            ["credit_loans.tenant_id", "credit_loans.id"],
            name="fk_credit_loan_obligations_loan",
        ),
        UniqueConstraint("loan_id", "sequence", name="uq_credit_loan_obligations_sequence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    loan_id: Mapped[int] = mapped_column(Integer, index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    contractual_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)  # effective due date after the calendar policy (A/B/C)
    delinquency_starts_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    principal_due: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    interest_due: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    fees_due: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    delinquency_due: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=Decimal(0))
    total_due: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    currency_code: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


# --- guards (installed by create_all AND by migration 0009) -----------------------------------------
LOAN_GUARD_FN = """
CREATE OR REPLACE FUNCTION credit_loans_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'credit loan % cannot be deleted', OLD.id;
  END IF;
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
     OR NEW.loan_number IS DISTINCT FROM OLD.loan_number OR NEW.customer_id IS DISTINCT FROM OLD.customer_id
     OR NEW.formalization_id IS DISTINCT FROM OLD.formalization_id OR NEW.application_id IS DISTINCT FROM OLD.application_id
     OR NEW.product_id IS DISTINCT FROM OLD.product_id OR NEW.product_version_id IS DISTINCT FROM OLD.product_version_id
     OR NEW.currency_code IS DISTINCT FROM OLD.currency_code OR NEW.original_principal IS DISTINCT FROM OLD.original_principal
     OR NEW.term_periods IS DISTINCT FROM OLD.term_periods OR NEW.frequency IS DISTINCT FROM OLD.frequency
     OR NEW.origin_branch_id IS DISTINCT FROM OLD.origin_branch_id
     OR NEW.managing_branch_id IS DISTINCT FROM OLD.managing_branch_id
     OR NEW.disbursement_branch_id IS DISTINCT FROM OLD.disbursement_branch_id
     OR NEW.disbursed_at IS DISTINCT FROM OLD.disbursed_at OR NEW.maturity_date IS DISTINCT FROM OLD.maturity_date
     OR NEW.rules_hash IS DISTINCT FROM OLD.rules_hash OR NEW.contract_hash IS DISTINCT FROM OLD.contract_hash
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'credit loan % is immutable: only its status may change', OLD.id;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""
OBLIGATION_GUARD_FN = """
CREATE OR REPLACE FUNCTION credit_loan_obligations_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'credit loan obligation % cannot be deleted', OLD.id;
  END IF;
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
     OR NEW.loan_id IS DISTINCT FROM OLD.loan_id OR NEW.sequence IS DISTINCT FROM OLD.sequence
     OR NEW.contractual_date IS DISTINCT FROM OLD.contractual_date OR NEW.due_date IS DISTINCT FROM OLD.due_date
     OR NEW.delinquency_starts_on IS DISTINCT FROM OLD.delinquency_starts_on
     OR NEW.principal_due IS DISTINCT FROM OLD.principal_due OR NEW.interest_due IS DISTINCT FROM OLD.interest_due
     OR NEW.fees_due IS DISTINCT FROM OLD.fees_due OR NEW.delinquency_due IS DISTINCT FROM OLD.delinquency_due
     OR NEW.total_due IS DISTINCT FROM OLD.total_due OR NEW.currency_code IS DISTINCT FROM OLD.currency_code THEN
    RAISE EXCEPTION 'credit loan obligation % is immutable: the schedule is not payment history', OLD.id;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""
# (table, function name, function source, trigger events)
GUARDS: tuple[tuple[str, str, str, str], ...] = (
    ("credit_loans", "credit_loans_guard", LOAN_GUARD_FN, "UPDATE OR DELETE"),
    ("credit_loan_disbursements", "origination_append_only", IMMUTABLE_FN, "UPDATE OR DELETE"),
    ("credit_loan_obligations", "credit_loan_obligations_guard", OBLIGATION_GUARD_FN, "UPDATE OR DELETE"),
)


def guard_trigger_sql(table: str, fn_name: str, events: str) -> str:
    return f"CREATE TRIGGER trg_{table}_guard BEFORE {events} ON {table} FOR EACH ROW EXECUTE FUNCTION {fn_name}()"


for _table_name, _fn_name, _fn_sql, _events in GUARDS:
    _t = Base.metadata.tables[_table_name]
    event.listen(_t, "after_create", DDL(_fn_sql.replace("%", "%%")).execute_if(dialect="postgresql"))
    event.listen(
        _t, "after_create", DDL(guard_trigger_sql(_table_name, _fn_name, _events)).execute_if(dialect="postgresql")
    )
