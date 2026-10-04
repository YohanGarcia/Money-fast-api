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
    text,
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
        UniqueConstraint(
            "tenant_id", "id", "currency_code", name="uq_credit_loans_tenant_id_currency"
        ),  # T-008 FK target
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
        UniqueConstraint(
            "tenant_id", "id", "loan_id", name="uq_credit_loan_obligations_tenant_id_loan"
        ),  # T-008 FK target
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


# =============================== T-008: payments and their applications ===============================
PAYMENT_STATUSES = (
    "confirmed",
    "pending",
    "failed",
    "reversed",
)  # T-008 only ever creates 'confirmed' and never updates
PAYMENT_METHODS = ("cash",)
PAYMENT_ORIGINS = ("counter", "field")
APPLICATION_COMPONENTS = ("fee", "delinquency", "interest", "principal")


class CreditPayment(Base):
    """A confirmed receipt of money on a loan. Immutable: an economic record is never edited (reversal is a later package).
    ``payment_number`` is a TECHNICAL reference only: it is not a fiscal or legal receipt."""

    __tablename__ = "credit_payments"
    __table_args__ = (
        CheckConstraint(_in("status", PAYMENT_STATUSES), name="status_valid"),
        CheckConstraint(_in("method", PAYMENT_METHODS), name="method_valid"),
        CheckConstraint(_in("origin", PAYMENT_ORIGINS), name="origin_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            "((origin = 'counter') = (cash_session_id IS NOT NULL)) AND ((origin = 'counter') = (cash_movement_id IS NOT NULL))",
            name="cash_matches_origin",  # counter -> exactly one cash movement; field -> none
        ),
        # the payment currency IS the loan's currency (composite FK on (tenant, loan, currency))
        ForeignKeyConstraint(
            ["tenant_id", "loan_id", "currency_code"],
            ["credit_loans.tenant_id", "credit_loans.id", "credit_loans.currency_code"],
            name="fk_credit_payments_loan_currency",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "receiving_branch_id"],
            ["branches.company_id", "branches.id"],
            name="fk_credit_payments_receiving_branch",
        ),
        UniqueConstraint("tenant_id", "payment_number", name="uq_credit_payments_tenant_number"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_credit_payments_tenant_key"),
        UniqueConstraint("cash_movement_id", name="uq_credit_payments_cash_movement"),
        UniqueConstraint("tenant_id", "id", "loan_id", name="uq_credit_payments_tenant_id_loan"),
        # target of the T-009 reversal FK: a reversal can only exist for the SAME amount / origin / currency / branch
        UniqueConstraint(
            "tenant_id",
            "id",
            "loan_id",
            "amount",
            "origin",
            "currency_code",
            "receiving_branch_id",
            name="uq_credit_payments_reversal_target",
        ),
        Index(
            "uq_credit_payments_external_reference",
            "tenant_id",
            "method",
            "external_reference",
            unique=True,
            postgresql_where=text("external_reference IS NOT NULL"),
        ),
        Index("ix_credit_payments_loan", "loan_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    loan_id: Mapped[int] = mapped_column(Integer)
    payment_number: Mapped[str] = mapped_column(String(20))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    currency_code: Mapped[str] = mapped_column(String(3))
    method: Mapped[str] = mapped_column(String(10), default="cash")
    origin: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(10), default="confirmed")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    business_date: Mapped[date] = mapped_column(Date)  # in the contract's timezone, never the UTC date
    receiving_branch_id: Mapped[int] = mapped_column(Integer)
    cash_session_id: Mapped[int | None] = mapped_column(ForeignKey("cash_sessions.id"), nullable=True)
    cash_movement_id: Mapped[int | None] = mapped_column(ForeignKey("cash_movements.id"), nullable=True)
    collected_by: Mapped[int] = mapped_column(Integer)  # the user who received the money (cashier or collector)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    request_digest: Mapped[str] = mapped_column(String(80))
    external_reference: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CreditPaymentApplication(Base):
    """How a payment was used: payment -> obligation -> component -> amount. Append-only. Together with the contractual
    obligations these rows are the ONLY source of truth for what is paid and what is outstanding."""

    __tablename__ = "credit_payment_applications"
    __table_args__ = (
        CheckConstraint(_in("component", APPLICATION_COMPONENTS), name="component_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
        # payment and obligation must belong to the SAME loan of the same tenant (two composite FKs share loan_id)
        ForeignKeyConstraint(
            ["tenant_id", "payment_id", "loan_id"],
            ["credit_payments.tenant_id", "credit_payments.id", "credit_payments.loan_id"],
            name="fk_credit_payment_applications_payment",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "obligation_id", "loan_id"],
            ["credit_loan_obligations.tenant_id", "credit_loan_obligations.id", "credit_loan_obligations.loan_id"],
            name="fk_credit_payment_applications_obligation",
        ),
        UniqueConstraint("payment_id", "obligation_id", "component", name="uq_credit_payment_applications_row"),
        # target of the T-009 mirror FK: a reversal application repeats obligation / component / amount EXACTLY
        UniqueConstraint(
            "tenant_id",
            "id",
            "payment_id",
            "obligation_id",
            "loan_id",
            "component",
            "amount",
            name="uq_credit_payment_applications_mirror",
        ),
        Index("ix_credit_payment_applications_obligation", "obligation_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    payment_id: Mapped[int] = mapped_column(Integer, index=True)
    obligation_id: Mapped[int] = mapped_column(Integer)
    loan_id: Mapped[int] = mapped_column(Integer)
    component: Mapped[str] = mapped_column(String(12))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


PAYMENT_SUM_FN = """
CREATE OR REPLACE FUNCTION credit_payment_sum_check() RETURNS trigger AS $$
DECLARE v_id integer; v_amount numeric; v_sum numeric;
BEGIN
  IF TG_TABLE_NAME = 'credit_payments' THEN v_id := NEW.id; ELSE v_id := NEW.payment_id; END IF;
  SELECT amount INTO v_amount FROM credit_payments WHERE id = v_id;
  SELECT COALESCE(SUM(amount), 0) INTO v_sum FROM credit_payment_applications WHERE payment_id = v_id;
  IF v_amount IS NULL OR v_sum <> v_amount THEN
    RAISE EXCEPTION 'credit payment % applications (%) must equal its amount (%)', v_id, v_sum, v_amount;
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql
"""
# T-009: the sum is NET (applications - reversal applications). Runs at COMMIT. It locks the obligation row first, so two transactions applying to the same obligation serialise here
# and the second one re-reads the first one's committed rows (READ COMMITTED): no over-application can commit.
PAYMENT_COMPONENT_FN = """
CREATE OR REPLACE FUNCTION credit_payment_component_check() RETURNS trigger AS $$
DECLARE v_due numeric; v_sum numeric;
BEGIN
  SELECT CASE NEW.component WHEN 'fee' THEN fees_due WHEN 'delinquency' THEN delinquency_due
                            WHEN 'interest' THEN interest_due ELSE principal_due END
    INTO v_due FROM credit_loan_obligations WHERE id = NEW.obligation_id FOR UPDATE;
  SELECT COALESCE(SUM(amount), 0) INTO v_sum FROM credit_payment_applications
    WHERE obligation_id = NEW.obligation_id AND component = NEW.component;
  SELECT v_sum - COALESCE(SUM(amount), 0) INTO v_sum FROM credit_payment_reversal_applications
    WHERE obligation_id = NEW.obligation_id AND component = NEW.component;
  IF v_sum > v_due THEN
    RAISE EXCEPTION 'credit obligation % component % over-applied (% > %)', NEW.obligation_id, NEW.component, v_sum, v_due;
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql
"""
# (table, trigger name, function)
PAYMENT_CONSTRAINT_TRIGGERS = (
    ("credit_payment_applications", "trg_credit_payment_applications_sum", "credit_payment_sum_check"),
    ("credit_payments", "trg_credit_payments_sum", "credit_payment_sum_check"),
    ("credit_payment_applications", "trg_credit_payment_applications_component", "credit_payment_component_check"),
)


def payment_constraint_trigger_sql(table: str, name: str, fn: str) -> str:
    return (
        f"CREATE CONSTRAINT TRIGGER {name} AFTER INSERT ON {table} DEFERRABLE INITIALLY DEFERRED "
        f"FOR EACH ROW EXECUTE FUNCTION {fn}()"
    )


PAYMENT_GUARDS = (
    ("credit_payments", "origination_append_only", IMMUTABLE_FN, "UPDATE OR DELETE"),
    ("credit_payment_applications", "origination_append_only", IMMUTABLE_FN, "UPDATE OR DELETE"),
)
for _table_name, _fn_name, _fn_sql, _events in PAYMENT_GUARDS:
    _t = Base.metadata.tables[_table_name]
    event.listen(_t, "after_create", DDL(_fn_sql.replace("%", "%%")).execute_if(dialect="postgresql"))
    event.listen(
        _t, "after_create", DDL(guard_trigger_sql(_table_name, _fn_name, _events)).execute_if(dialect="postgresql")
    )
# credit_payments is created before credit_payment_applications: the functions (late-bound plpgsql) go with the FIRST table
_payments_table = Base.metadata.tables["credit_payments"]
for _fn_sql in (PAYMENT_SUM_FN, PAYMENT_COMPONENT_FN):
    event.listen(_payments_table, "after_create", DDL(_fn_sql.replace("%", "%%")).execute_if(dialect="postgresql"))
for _table_name, _trigger, _fn in PAYMENT_CONSTRAINT_TRIGGERS:
    event.listen(
        Base.metadata.tables[_table_name],
        "after_create",
        DDL(payment_constraint_trigger_sql(_table_name, _trigger, _fn)).execute_if(dialect="postgresql"),
    )


# =============================== T-009: full payment reversal ===============================
REVERSAL_ORIGINS = PAYMENT_ORIGINS


class CreditPaymentReversal(Base):
    """The FULL reversal of one confirmed payment. Append-only; at most one per payment (``UNIQUE(payment_id)``).
    The original payment is never touched: "reversed" is DERIVED from the existence of this row. The composite FK ties
    amount, origin, currency and ``reversal_branch_id`` to the payment's own (amount, origin, currency, receiving branch):
    a partial reversal or a reversal at another branch cannot exist."""

    __tablename__ = "credit_payment_reversals"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(_in("origin", REVERSAL_ORIGINS), name="origin_valid"),
        CheckConstraint("char_length(btrim(reason)) BETWEEN 3 AND 500", name="reason_length"),
        CheckConstraint(
            "((origin = 'counter') = (cash_session_id IS NOT NULL)) AND ((origin = 'counter') = (cash_movement_id IS NOT NULL))",
            name="cash_matches_origin",  # counter -> exactly one cash movement; field -> none
        ),
        ForeignKeyConstraint(
            ["tenant_id", "payment_id", "loan_id", "amount", "origin", "currency_code", "reversal_branch_id"],
            [
                "credit_payments.tenant_id",
                "credit_payments.id",
                "credit_payments.loan_id",
                "credit_payments.amount",
                "credit_payments.origin",
                "credit_payments.currency_code",
                "credit_payments.receiving_branch_id",
            ],
            name="fk_credit_payment_reversals_payment",
        ),
        UniqueConstraint("payment_id", name="uq_credit_payment_reversals_payment"),
        UniqueConstraint("tenant_id", "reversal_number", name="uq_credit_payment_reversals_tenant_number"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_credit_payment_reversals_tenant_key"),
        UniqueConstraint("cash_movement_id", name="uq_credit_payment_reversals_cash_movement"),
        UniqueConstraint(
            "tenant_id", "id", "payment_id", "loan_id", name="uq_credit_payment_reversals_tenant_id_payment"
        ),
        Index("ix_credit_payment_reversals_loan", "loan_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    payment_id: Mapped[int] = mapped_column(Integer)
    loan_id: Mapped[int] = mapped_column(Integer)
    reversal_number: Mapped[str] = mapped_column(String(20))  # REV-000001: technical reference, not a fiscal document
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))  # always the payment's amount: full reversal only
    currency_code: Mapped[str] = mapped_column(String(3))
    origin: Mapped[str] = mapped_column(String(10))
    reason: Mapped[str] = mapped_column(String(500))
    reversed_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reversed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    business_date: Mapped[date] = mapped_column(Date)  # in the contract's timezone
    reversal_branch_id: Mapped[int] = mapped_column(Integer)  # == the payment's receiving branch (database-enforced)
    cash_session_id: Mapped[int | None] = mapped_column(ForeignKey("cash_sessions.id"), nullable=True)
    cash_movement_id: Mapped[int | None] = mapped_column(ForeignKey("cash_movements.id"), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    request_digest: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CreditPaymentReversalApplication(Base):
    """One row per original application, mirroring it EXACTLY (obligation, component, amount: composite FK). Net paid =
    applications - reversal applications."""

    __tablename__ = "credit_payment_reversal_applications"
    __table_args__ = (
        CheckConstraint(_in("component", APPLICATION_COMPONENTS), name="component_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "original_application_id",
                "payment_id",
                "obligation_id",
                "loan_id",
                "component",
                "amount",
            ],
            [
                "credit_payment_applications.tenant_id",
                "credit_payment_applications.id",
                "credit_payment_applications.payment_id",
                "credit_payment_applications.obligation_id",
                "credit_payment_applications.loan_id",
                "credit_payment_applications.component",
                "credit_payment_applications.amount",
            ],
            name="fk_credit_payment_reversal_applications_original",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "reversal_id", "payment_id", "loan_id"],
            [
                "credit_payment_reversals.tenant_id",
                "credit_payment_reversals.id",
                "credit_payment_reversals.payment_id",
                "credit_payment_reversals.loan_id",
            ],
            name="fk_credit_payment_reversal_applications_reversal",
        ),
        UniqueConstraint("original_application_id", name="uq_credit_payment_reversal_applications_original"),
        Index("ix_credit_payment_reversal_applications_obligation", "obligation_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    reversal_id: Mapped[int] = mapped_column(Integer, index=True)
    payment_id: Mapped[int] = mapped_column(Integer)
    original_application_id: Mapped[int] = mapped_column(Integer)
    obligation_id: Mapped[int] = mapped_column(Integer)
    loan_id: Mapped[int] = mapped_column(Integer)
    component: Mapped[str] = mapped_column(String(12))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


REVERSAL_SUM_FN = """
CREATE OR REPLACE FUNCTION credit_reversal_sum_check() RETURNS trigger AS $$
DECLARE v_id integer; v_amount numeric; v_sum numeric;
BEGIN
  IF TG_TABLE_NAME = 'credit_payment_reversals' THEN v_id := NEW.id; ELSE v_id := NEW.reversal_id; END IF;
  SELECT amount INTO v_amount FROM credit_payment_reversals WHERE id = v_id;
  SELECT COALESCE(SUM(amount), 0) INTO v_sum FROM credit_payment_reversal_applications WHERE reversal_id = v_id;
  IF v_amount IS NULL OR v_sum <> v_amount THEN
    RAISE EXCEPTION 'credit payment reversal % applications (%) must equal its amount (%)', v_id, v_sum, v_amount;
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql
"""
# A counter reversal MUST be backed by exactly the compensating cash movement: right kind, negative amount, same session,
# and ``reverses_id`` = the original receipt. Checked at COMMIT: debt and cash cannot diverge.
REVERSAL_CASH_FN = """
CREATE OR REPLACE FUNCTION credit_reversal_cash_check() RETURNS trigger AS $$
DECLARE v_receipt integer; m_kind text; m_amount numeric; m_session integer; m_reverses integer;
BEGIN
  IF NEW.origin = 'counter' THEN
    SELECT cash_movement_id INTO v_receipt FROM credit_payments WHERE id = NEW.payment_id;
    SELECT kind, amount, session_id, reverses_id INTO m_kind, m_amount, m_session, m_reverses
      FROM cash_movements WHERE id = NEW.cash_movement_id;
    IF m_kind IS DISTINCT FROM 'credit_payment_reversal' OR m_amount IS DISTINCT FROM -NEW.amount
       OR m_session IS DISTINCT FROM NEW.cash_session_id OR m_reverses IS DISTINCT FROM v_receipt THEN
      RAISE EXCEPTION 'credit payment reversal % is not backed by its compensating cash movement', NEW.id;
    END IF;
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql
"""
REVERSAL_CONSTRAINT_TRIGGERS = (
    (
        "credit_payment_reversal_applications",
        "trg_credit_payment_reversal_applications_sum",
        "credit_reversal_sum_check",
    ),
    ("credit_payment_reversals", "trg_credit_payment_reversals_sum", "credit_reversal_sum_check"),
    ("credit_payment_reversals", "trg_credit_payment_reversals_cash", "credit_reversal_cash_check"),
)
REVERSAL_GUARDS = (
    ("credit_payment_reversals", "origination_append_only", IMMUTABLE_FN, "UPDATE OR DELETE"),
    ("credit_payment_reversal_applications", "origination_append_only", IMMUTABLE_FN, "UPDATE OR DELETE"),
)
for _table_name, _fn_name, _fn_sql, _events in REVERSAL_GUARDS:
    _t = Base.metadata.tables[_table_name]
    event.listen(_t, "after_create", DDL(_fn_sql.replace("%", "%%")).execute_if(dialect="postgresql"))
    event.listen(
        _t, "after_create", DDL(guard_trigger_sql(_table_name, _fn_name, _events)).execute_if(dialect="postgresql")
    )
# the functions (late-bound plpgsql) go with the FIRST created table, before any trigger that uses them
_reversals_table = Base.metadata.tables["credit_payment_reversals"]
for _fn_sql in (REVERSAL_SUM_FN, REVERSAL_CASH_FN):
    event.listen(_reversals_table, "after_create", DDL(_fn_sql.replace("%", "%%")).execute_if(dialect="postgresql"))
for _table_name, _trigger, _fn in REVERSAL_CONSTRAINT_TRIGGERS:
    event.listen(
        Base.metadata.tables[_table_name],
        "after_create",
        DDL(payment_constraint_trigger_sql(_table_name, _trigger, _fn)).execute_if(dialect="postgresql"),
    )


# =============================== T-012: collection assignment (history) ===============================
class CreditCollectionAssignment(Base):
    """Who is responsible for collecting ONE loan, as effective-dated history. Open row (``ended_at IS NULL``) = current; at
    most one per loan (partial UNIQUE). Rows are never deleted nor rewritten: a reassignment closes the open row and inserts
    a new one. Assignment is operational metadata: it grants no access and moves no money, status or debt."""

    __tablename__ = "credit_collection_assignments"
    __table_args__ = (
        # tenant-safe references: loan, assignee, who assigned, who ended, the managing-branch snapshot
        ForeignKeyConstraint(
            ["tenant_id", "loan_id"],
            ["credit_loans.tenant_id", "credit_loans.id"],
            name="fk_credit_collection_assignments_loan",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "assignee_user_id"],
            ["users.company_id", "users.id"],
            name="fk_credit_collection_assignments_assignee",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "assigned_by"],
            ["users.company_id", "users.id"],
            name="fk_credit_collection_assignments_assigned_by",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "ended_by"],
            ["users.company_id", "users.id"],
            name="fk_credit_collection_assignments_ended_by",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "managing_branch_id"],
            ["branches.company_id", "branches.id"],
            name="fk_credit_collection_assignments_branch",
        ),
        # the end transition is all-or-nothing: ended_at, ended_by and the end idempotency pair appear together
        CheckConstraint(
            "(ended_at IS NULL) = (ended_by IS NULL) AND (ended_at IS NULL) = (end_idempotency_key IS NULL) "
            "AND (ended_at IS NULL) = (end_request_digest IS NULL)",
            name="end_fields_together",
        ),
        CheckConstraint("ended_at IS NULL OR ended_at >= assigned_at", name="end_not_before_start"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_credit_collection_assignments_tenant_key"),
        # at most ONE open assignment per loan (the database backstop of the loan lock)
        Index(
            "uq_credit_collection_assignments_open",
            "tenant_id",
            "loan_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        # T-013: serves the worklist filter by current assignee (EXPLAIN evidence in docs/T-013-*.md)
        Index(
            "ix_credit_collection_assignments_open_assignee",
            "tenant_id",
            "assignee_user_id",
            "loan_id",
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "uq_credit_collection_assignments_end_key",
            "tenant_id",
            "end_idempotency_key",
            unique=True,
            postgresql_where=text("end_idempotency_key IS NOT NULL"),
        ),
        Index("ix_credit_collection_assignments_loan", "loan_id"),
        # T-014: target of the tenant-safe FK of an activity's assignment snapshot (relational support, no new rule)
        UniqueConstraint("tenant_id", "id", "loan_id", name="uq_credit_collection_assignments_tenant_id_loan"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    loan_id: Mapped[int] = mapped_column(Integer)
    assignee_user_id: Mapped[int] = mapped_column(Integer)
    managing_branch_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # snapshot of the loan's, may be NULL
    assigned_by: Mapped[int] = mapped_column(Integer)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    request_digest: Mapped[str] = mapped_column(String(80))
    ended_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    end_request_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)


ASSIGNMENT_GUARD_FN = """
CREATE OR REPLACE FUNCTION credit_collection_assignments_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'collection assignment % cannot be deleted: it is history', OLD.id;
  END IF;
  IF OLD.ended_at IS NOT NULL THEN
    RAISE EXCEPTION 'collection assignment % is closed: history is immutable', OLD.id;
  END IF;
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
     OR NEW.loan_id IS DISTINCT FROM OLD.loan_id OR NEW.assignee_user_id IS DISTINCT FROM OLD.assignee_user_id
     OR NEW.managing_branch_id IS DISTINCT FROM OLD.managing_branch_id OR NEW.assigned_by IS DISTINCT FROM OLD.assigned_by
     OR NEW.assigned_at IS DISTINCT FROM OLD.assigned_at OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
     OR NEW.request_digest IS DISTINCT FROM OLD.request_digest THEN
    RAISE EXCEPTION 'collection assignment % is immutable: only its single end transition may change', OLD.id;
  END IF;
  IF NEW.ended_at IS NULL THEN
    RAISE EXCEPTION 'collection assignment % is open: the only allowed change is to close it', OLD.id;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""
# An assignment is born open, under the loan's own managing branch (the snapshot cannot differ from the loan's).
ASSIGNMENT_INSERT_FN = """
CREATE OR REPLACE FUNCTION credit_collection_assignments_insert_check() RETURNS trigger AS $$
BEGIN
  IF NEW.ended_at IS NOT NULL THEN
    RAISE EXCEPTION 'a collection assignment is born open: it cannot be inserted already closed';
  END IF;
  IF NEW.managing_branch_id IS DISTINCT FROM
     (SELECT managing_branch_id FROM credit_loans WHERE id = NEW.loan_id AND tenant_id = NEW.tenant_id) THEN
    RAISE EXCEPTION 'collection assignment managing branch must be the loan''s own managing branch';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""
ASSIGNMENT_TRIGGERS = (
    "CREATE TRIGGER trg_credit_collection_assignments_insert_check BEFORE INSERT ON credit_collection_assignments "
    "FOR EACH ROW EXECUTE FUNCTION credit_collection_assignments_insert_check()",
    guard_trigger_sql("credit_collection_assignments", "credit_collection_assignments_guard", "UPDATE OR DELETE"),
)
_assignments_table = Base.metadata.tables["credit_collection_assignments"]
for _sql in (ASSIGNMENT_GUARD_FN, ASSIGNMENT_INSERT_FN, *ASSIGNMENT_TRIGGERS):
    event.listen(_assignments_table, "after_create", DDL(_sql.replace("%", "%%")).execute_if(dialect="postgresql"))


# =============================== T-014: collection activity (append-only history) ===============================
ACTIVITY_TYPES = (
    "phone_call",
    "whatsapp",
    "sms",
    "email",
    "in_person_visit",
    "office_visit",
    "no_contact",
    "other",
)


class CreditCollectionActivity(Base):
    """One collection management (call, visit, ...) recorded on ONE loan. Append-only business history: no outcome, no free
    text, no contact data, no client time. ``managing_branch_id`` and ``assignment_id`` are server-side snapshots (the open
    T-012 assignment at that instant, or NULL) checked by an INSERT trigger. Moves no money and changes no status."""

    __tablename__ = "credit_collection_activities"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "loan_id"],
            ["credit_loans.tenant_id", "credit_loans.id"],
            name="fk_credit_collection_activities_loan",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "recorded_by"], ["users.company_id", "users.id"], name="fk_credit_collection_activities_actor"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "managing_branch_id"],
            ["branches.company_id", "branches.id"],
            name="fk_credit_collection_activities_branch",
        ),
        # the snapshot of the assignment must belong to the SAME tenant and the SAME loan (NULL = no open assignment)
        ForeignKeyConstraint(
            ["tenant_id", "assignment_id", "loan_id"],
            [
                "credit_collection_assignments.tenant_id",
                "credit_collection_assignments.id",
                "credit_collection_assignments.loan_id",
            ],
            name="fk_credit_collection_activities_assignment",
        ),
        CheckConstraint(
            "activity_type IN (" + ", ".join(f"'{t}'" for t in ACTIVITY_TYPES) + ")", name="activity_type_valid"
        ),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_credit_collection_activities_tenant_key"),
        Index("ix_credit_collection_activities_loan_id_id", "tenant_id", "loan_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    loan_id: Mapped[int] = mapped_column(Integer)
    managing_branch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recorded_by: Mapped[int] = mapped_column(Integer)
    assignment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    activity_type: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    request_digest: Mapped[str] = mapped_column(String(80))


ACTIVITY_GUARD_FN = """
CREATE OR REPLACE FUNCTION credit_collection_activities_guard() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'collection activity % is append-only history: it cannot be % ', OLD.id, lower(TG_OP);
END $$ LANGUAGE plpgsql
"""
# The snapshots cannot differ from the loan's managing branch and the loan's open assignment at INSERT time.
ACTIVITY_INSERT_FN = """
CREATE OR REPLACE FUNCTION credit_collection_activities_insert_check() RETURNS trigger AS $$
BEGIN
  IF NEW.managing_branch_id IS DISTINCT FROM
     (SELECT managing_branch_id FROM credit_loans WHERE id = NEW.loan_id AND tenant_id = NEW.tenant_id) THEN
    RAISE EXCEPTION 'collection activity managing branch must be the loan''s own managing branch';
  END IF;
  IF NEW.assignment_id IS DISTINCT FROM
     (SELECT id FROM credit_collection_assignments
       WHERE tenant_id = NEW.tenant_id AND loan_id = NEW.loan_id AND ended_at IS NULL) THEN
    RAISE EXCEPTION 'collection activity assignment snapshot must be the loan''s open assignment (or NULL)';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""
ACTIVITY_TRIGGERS = (
    "CREATE TRIGGER trg_credit_collection_activities_insert_check BEFORE INSERT ON credit_collection_activities "
    "FOR EACH ROW EXECUTE FUNCTION credit_collection_activities_insert_check()",
    guard_trigger_sql("credit_collection_activities", "credit_collection_activities_guard", "UPDATE OR DELETE"),
)
_activities_table = Base.metadata.tables["credit_collection_activities"]
for _sql in (ACTIVITY_GUARD_FN, ACTIVITY_INSERT_FN, *ACTIVITY_TRIGGERS):
    event.listen(_activities_table, "after_create", DDL(_sql.replace("%", "%%")).execute_if(dialect="postgresql"))


# =============================== T-015: collection promise-to-pay (history, derived outcome) ===============================
PROMISE_CLOSED_KINDS = ("cancelled", "superseded")


class CreditCollectionPromise(Base):
    """A customer's declared commitment to pay ``promised_amount`` on one loan by ``promise_date`` (contract timezone). The
    commercial terms are immutable; the only change a row ever gets is ONE closing transition (cancelled / superseded).
    Fulfilled / broken are NEVER stored: they are derived on read from the net payment ledger. At most one row per loan is
    not closed. A promise moves no money, reduces no debt and changes no status."""

    __tablename__ = "credit_collection_promises"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "loan_id"],
            ["credit_loans.tenant_id", "credit_loans.id"],
            name="fk_credit_collection_promises_loan",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "created_by"], ["users.company_id", "users.id"], name="fk_credit_collection_promises_creator"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "closed_by"], ["users.company_id", "users.id"], name="fk_credit_collection_promises_closer"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "managing_branch_id"],
            ["branches.company_id", "branches.id"],
            name="fk_credit_collection_promises_branch",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "assignment_id", "loan_id"],
            [
                "credit_collection_assignments.tenant_id",
                "credit_collection_assignments.id",
                "credit_collection_assignments.loan_id",
            ],
            name="fk_credit_collection_promises_assignment",
        ),
        # a replacement points to the promise it superseded: same tenant AND same loan (target below)
        ForeignKeyConstraint(
            ["tenant_id", "supersedes_promise_id", "loan_id"],
            [
                "credit_collection_promises.tenant_id",
                "credit_collection_promises.id",
                "credit_collection_promises.loan_id",
            ],
            name="fk_credit_collection_promises_supersedes",
        ),
        UniqueConstraint("tenant_id", "id", "loan_id", name="uq_credit_collection_promises_tenant_id_loan"),
        CheckConstraint("promised_amount > 0", name="amount_positive"),
        CheckConstraint("supersedes_promise_id IS NULL OR supersedes_promise_id <> id", name="not_self_superseding"),
        # the closing transition is all-or-nothing, and only these two kinds exist (fulfilled / broken are derived)
        CheckConstraint(
            "(closed_at IS NULL) = (closed_by IS NULL) AND (closed_at IS NULL) = (closed_kind IS NULL) "
            "AND (closed_at IS NULL) = (close_idempotency_key IS NULL) "
            "AND (closed_at IS NULL) = (close_request_digest IS NULL)",
            name="closure_fields_together",
        ),
        CheckConstraint(
            "closed_kind IS NULL OR closed_kind IN (" + ", ".join(f"'{k}'" for k in PROMISE_CLOSED_KINDS) + ")",
            name="closed_kind_valid",
        ),
        CheckConstraint("closed_at IS NULL OR closed_at >= created_at", name="close_not_before_creation"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_credit_collection_promises_tenant_key"),
        # at most ONE promise per loan that is not closed (the database backstop of the loan lock)
        Index(
            "uq_credit_collection_promises_current",
            "tenant_id",
            "loan_id",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
        Index(
            "uq_credit_collection_promises_close_key",
            "tenant_id",
            "close_idempotency_key",
            unique=True,
            postgresql_where=text("close_idempotency_key IS NOT NULL"),
        ),
        Index("ix_credit_collection_promises_loan_id_id", "tenant_id", "loan_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    loan_id: Mapped[int] = mapped_column(Integer)
    managing_branch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assignment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    currency_code: Mapped[str] = mapped_column(String(3))
    promised_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    promise_date: Mapped[date] = mapped_column(Date)
    supersedes_promise_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    request_digest: Mapped[str] = mapped_column(String(80))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    closed_kind: Mapped[str | None] = mapped_column(String(12), nullable=True)
    close_idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    close_request_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)


PROMISE_GUARD_FN = """
CREATE OR REPLACE FUNCTION credit_collection_promises_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'collection promise % cannot be deleted: it is history', OLD.id;
  END IF;
  IF OLD.closed_at IS NOT NULL THEN
    RAISE EXCEPTION 'collection promise % is closed: history is immutable', OLD.id;
  END IF;
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
     OR NEW.loan_id IS DISTINCT FROM OLD.loan_id OR NEW.managing_branch_id IS DISTINCT FROM OLD.managing_branch_id
     OR NEW.assignment_id IS DISTINCT FROM OLD.assignment_id OR NEW.created_by IS DISTINCT FROM OLD.created_by
     OR NEW.created_at IS DISTINCT FROM OLD.created_at OR NEW.currency_code IS DISTINCT FROM OLD.currency_code
     OR NEW.promised_amount IS DISTINCT FROM OLD.promised_amount OR NEW.promise_date IS DISTINCT FROM OLD.promise_date
     OR NEW.supersedes_promise_id IS DISTINCT FROM OLD.supersedes_promise_id
     OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key OR NEW.request_digest IS DISTINCT FROM OLD.request_digest THEN
    RAISE EXCEPTION 'collection promise % terms are immutable: only its single closing transition may change', OLD.id;
  END IF;
  IF NEW.closed_at IS NULL THEN
    RAISE EXCEPTION 'collection promise % is open: the only allowed change is to close it', OLD.id;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""
# Born open, with the loan's own snapshots and currency, and a valid supersede relation.
PROMISE_INSERT_FN = """
CREATE OR REPLACE FUNCTION credit_collection_promises_insert_check() RETURNS trigger AS $$
DECLARE
  loan_row RECORD;
BEGIN
  IF NEW.closed_at IS NOT NULL THEN
    RAISE EXCEPTION 'a collection promise is born open: it cannot be inserted already closed';
  END IF;
  SELECT managing_branch_id, currency_code INTO loan_row
    FROM credit_loans WHERE id = NEW.loan_id AND tenant_id = NEW.tenant_id;
  IF NEW.managing_branch_id IS DISTINCT FROM loan_row.managing_branch_id THEN
    RAISE EXCEPTION 'collection promise managing branch must be the loan''s own managing branch';
  END IF;
  IF NEW.currency_code IS DISTINCT FROM loan_row.currency_code THEN
    RAISE EXCEPTION 'collection promise currency must be the loan''s currency';
  END IF;
  IF NEW.assignment_id IS DISTINCT FROM
     (SELECT id FROM credit_collection_assignments
       WHERE tenant_id = NEW.tenant_id AND loan_id = NEW.loan_id AND ended_at IS NULL) THEN
    RAISE EXCEPTION 'collection promise assignment snapshot must be the loan''s open assignment (or NULL)';
  END IF;
  IF NEW.supersedes_promise_id IS NOT NULL AND NOT EXISTS (
       SELECT 1 FROM credit_collection_promises
        WHERE id = NEW.supersedes_promise_id AND tenant_id = NEW.tenant_id AND loan_id = NEW.loan_id
          AND closed_kind = 'superseded') THEN
    RAISE EXCEPTION 'a replacement must supersede a promise of the same loan that is closed as superseded';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""
PROMISE_TRIGGERS = (
    "CREATE TRIGGER trg_credit_collection_promises_insert_check BEFORE INSERT ON credit_collection_promises "
    "FOR EACH ROW EXECUTE FUNCTION credit_collection_promises_insert_check()",
    guard_trigger_sql("credit_collection_promises", "credit_collection_promises_guard", "UPDATE OR DELETE"),
)
_promises_table = Base.metadata.tables["credit_collection_promises"]
for _sql in (PROMISE_GUARD_FN, PROMISE_INSERT_FN, *PROMISE_TRIGGERS):
    event.listen(_promises_table, "after_create", DDL(_sql.replace("%", "%%")).execute_if(dialect="postgresql"))
