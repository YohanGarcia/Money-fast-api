"""Field cash custody persistence (T-019). PHYSICAL custody only: it never changes debt, payments or loans.

* ``credit_field_custody_receipts``: ONE immutable row per field payment, born in the payment's own transaction. Custodian =
  the payment's ``collected_by`` (the authenticated actor who recorded it). Indivisible: the whole payment amount.
* ``credit_field_renditions``: a custodian hands a set of receipts to a cashier. Born ``declared``; exactly ONE terminal
  transition to ``accepted`` (cash enters a session through the Cash port), ``rejected`` or ``cancelled``.
* ``credit_field_rendition_items``: receipt -> rendition, amount = the receipt's (composite FK). A live item
  (``released = false``) claims its receipt: the partial UNIQUE allows ONE live claim per receipt. Rejected / cancelled
  renditions release their items (false -> true, once); accepted ones never do.

Outstanding custody is DERIVED (a receipt without a live item of an accepted rendition), never stored. Every rule is
enforced by the database (composite FKs, CHECKs, guard triggers, deferred consistency triggers), not only by the service.
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
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

RENDITION_STATES = ("declared", "accepted", "rejected", "cancelled")
TERMINAL_STATES = ("accepted", "rejected", "cancelled")
CUSTODY_CURRENCY = "DOP"
MOVEMENT_KIND = "credit_field_rendition"
CENT_EXACT = "{c} > 0 AND {c} = round({c}, 2)"


def _now() -> datetime:
    return datetime.now(UTC)


class CreditFieldCustodyReceipt(Base):
    """Physical custody born with ONE field payment (same transaction). Immutable; never deleted."""

    __tablename__ = "credit_field_custody_receipts"
    __table_args__ = (
        CheckConstraint(f"currency_code = '{CUSTODY_CURRENCY}'", name="currency_dop"),
        CheckConstraint(CENT_EXACT.format(c="amount"), name="amount_cent_exact"),
        # the exact payment of the same tenant and loan (origin / amount / branch / custodian: insert trigger)
        ForeignKeyConstraint(
            ["tenant_id", "payment_id", "loan_id"],
            ["credit_payments.tenant_id", "credit_payments.id", "credit_payments.loan_id"],
            name="fk_credit_field_custody_receipts_payment",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "receiving_branch_id"],
            ["branches.company_id", "branches.id"],
            name="fk_credit_field_custody_receipts_branch",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "custodian_user_id"],
            ["users.company_id", "users.id"],
            name="fk_credit_field_custody_receipts_custodian",
        ),
        UniqueConstraint("payment_id", name="uq_credit_field_custody_receipts_payment"),
        # target of the item FK: an item repeats the receipt's payment, branch, custodian, currency and FULL amount
        UniqueConstraint(
            "tenant_id",
            "id",
            "payment_id",
            "receiving_branch_id",
            "custodian_user_id",
            "currency_code",
            "amount",
            name="uq_credit_field_custody_receipts_item_target",
        ),
        Index(
            "ix_credit_field_custody_receipts_custodian",
            "tenant_id",
            "custodian_user_id",
            "receiving_branch_id",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    payment_id: Mapped[int] = mapped_column(Integer)
    loan_id: Mapped[int] = mapped_column(Integer)
    receiving_branch_id: Mapped[int] = mapped_column(Integer)
    custodian_user_id: Mapped[int] = mapped_column(Integer)
    currency_code: Mapped[str] = mapped_column(String(3))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CreditFieldRendition(Base):
    """A custodian hands receipts to a cashier. ``declared`` -> ONE of accepted / rejected / cancelled (terminal).
    ``declared_amount`` = SUM(items), server-derived. Accepted = exactly one Cash movement in the acceptor's session."""

    __tablename__ = "credit_field_renditions"
    __table_args__ = (
        CheckConstraint("state IN ('declared', 'accepted', 'rejected', 'cancelled')", name="state_valid"),
        CheckConstraint(f"currency_code = '{CUSTODY_CURRENCY}'", name="currency_dop"),
        CheckConstraint(CENT_EXACT.format(c="declared_amount"), name="declared_amount_cent_exact"),
        CheckConstraint(
            "counted_amount IS NULL OR (counted_amount >= 0 AND counted_amount = round(counted_amount, 2))",
            name="counted_amount_cent_exact",
        ),
        CheckConstraint("declared_by = custodian_user_id", name="declared_by_custodian"),
        CheckConstraint(
            "((state = 'declared') = (decided_at IS NULL)) AND ((state = 'declared') = (decided_by IS NULL)) "
            "AND ((state = 'declared') = (decision_idempotency_key IS NULL)) "
            "AND ((state = 'declared') = (decision_request_digest IS NULL))",
            name="decision_consistent",
        ),
        CheckConstraint(
            "((state = 'accepted') = (cash_session_id IS NOT NULL)) AND ((state = 'accepted') = (cash_movement_id IS NOT NULL))",
            name="cash_matches_accepted",
        ),
        CheckConstraint("state <> 'accepted' OR counted_amount = declared_amount", name="accepted_exact"),
        CheckConstraint(
            "state NOT IN ('accepted', 'rejected') OR decided_by <> custodian_user_id", name="maker_checker"
        ),
        CheckConstraint("state <> 'cancelled' OR decided_by = custodian_user_id", name="cancel_by_custodian"),
        CheckConstraint(
            "state <> 'rejected' OR length(btrim(coalesce(decision_reason, ''))) >= 3", name="reject_reason"
        ),
        CheckConstraint("counted_amount IS NULL OR state IN ('accepted', 'rejected')", name="counted_only_on_decision"),
        ForeignKeyConstraint(
            ["tenant_id", "receiving_branch_id"],
            ["branches.company_id", "branches.id"],
            name="fk_credit_field_renditions_branch",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "custodian_user_id"],
            ["users.company_id", "users.id"],
            name="fk_credit_field_renditions_custodian",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "decided_by"], ["users.company_id", "users.id"], name="fk_credit_field_renditions_decider"
        ),
        UniqueConstraint("tenant_id", "rendition_number", name="uq_credit_field_renditions_tenant_number"),
        UniqueConstraint("tenant_id", "create_idempotency_key", name="uq_credit_field_renditions_create_key"),
        UniqueConstraint("cash_movement_id", name="uq_credit_field_renditions_cash_movement"),
        # target of the item FK: an item belongs to a rendition of the SAME branch, custodian and currency
        UniqueConstraint(
            "tenant_id",
            "id",
            "receiving_branch_id",
            "custodian_user_id",
            "currency_code",
            name="uq_credit_field_renditions_item_target",
        ),
        Index(
            "uq_credit_field_renditions_decision_key",
            "tenant_id",
            "decision_idempotency_key",
            unique=True,
            postgresql_where=text("decision_idempotency_key IS NOT NULL"),
        ),
        Index(
            "ix_credit_field_renditions_branch_declared",
            "tenant_id",
            "receiving_branch_id",
            "id",
            postgresql_where=text("state = 'declared'"),
        ),
        Index("ix_credit_field_renditions_custodian", "tenant_id", "custodian_user_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    rendition_number: Mapped[str] = mapped_column(String(20))
    receiving_branch_id: Mapped[int] = mapped_column(Integer)
    custodian_user_id: Mapped[int] = mapped_column(Integer)
    currency_code: Mapped[str] = mapped_column(String(3))
    declared_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    state: Mapped[str] = mapped_column(String(12), default="declared")
    declared_by: Mapped[int] = mapped_column(Integer)
    declared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    decided_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    counted_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    cash_session_id: Mapped[int | None] = mapped_column(ForeignKey("cash_sessions.id"), nullable=True)
    cash_movement_id: Mapped[int | None] = mapped_column(ForeignKey("cash_movements.id"), nullable=True)
    create_idempotency_key: Mapped[str] = mapped_column(String(120))
    create_request_digest: Mapped[str] = mapped_column(String(80))
    decision_idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decision_request_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)


class CreditFieldRenditionItem(Base):
    """One receipt handed in one rendition, for its FULL amount. Immutable except ``released`` false -> true (once)."""

    __tablename__ = "credit_field_rendition_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "rendition_id", "receiving_branch_id", "custodian_user_id", "currency_code"],
            [
                "credit_field_renditions.tenant_id",
                "credit_field_renditions.id",
                "credit_field_renditions.receiving_branch_id",
                "credit_field_renditions.custodian_user_id",
                "credit_field_renditions.currency_code",
            ],
            name="fk_credit_field_rendition_items_rendition",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "receipt_id",
                "payment_id",
                "receiving_branch_id",
                "custodian_user_id",
                "currency_code",
                "amount",
            ],
            [
                "credit_field_custody_receipts.tenant_id",
                "credit_field_custody_receipts.id",
                "credit_field_custody_receipts.payment_id",
                "credit_field_custody_receipts.receiving_branch_id",
                "credit_field_custody_receipts.custodian_user_id",
                "credit_field_custody_receipts.currency_code",
                "credit_field_custody_receipts.amount",
            ],
            name="fk_credit_field_rendition_items_receipt",
        ),
        UniqueConstraint("rendition_id", "receipt_id", name="uq_credit_field_rendition_items_row"),
        # ONE live claim per receipt: a receipt is in at most one declared or accepted rendition
        Index(
            "uq_credit_field_rendition_items_live_receipt",
            "receipt_id",
            unique=True,
            postgresql_where=text("NOT released"),
        ),
        Index("ix_credit_field_rendition_items_receipt", "receipt_id"),
        Index("ix_credit_field_rendition_items_rendition", "rendition_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    rendition_id: Mapped[int] = mapped_column(Integer)
    receipt_id: Mapped[int] = mapped_column(Integer)
    payment_id: Mapped[int] = mapped_column(Integer)
    receiving_branch_id: Mapped[int] = mapped_column(Integer)
    custodian_user_id: Mapped[int] = mapped_column(Integer)
    currency_code: Mapped[str] = mapped_column(String(3))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    released: Mapped[bool] = mapped_column(Boolean, default=False)


# =============================== database guards ===============================
RECEIPT_INSERT_FN = """
CREATE OR REPLACE FUNCTION credit_field_custody_receipts_insert_check() RETURNS trigger AS $$
DECLARE p RECORD;
BEGIN
  SELECT tenant_id, loan_id, origin, amount, currency_code, receiving_branch_id, collected_by, received_at
    INTO p FROM credit_payments WHERE id = NEW.payment_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'field custody receipt needs an existing payment (%)', NEW.payment_id;
  END IF;
  IF p.tenant_id <> NEW.tenant_id OR p.loan_id <> NEW.loan_id THEN
    RAISE EXCEPTION 'field custody receipt must belong to the payment''s tenant and loan';
  END IF;
  IF p.origin <> 'field' THEN
    RAISE EXCEPTION 'only a field payment creates physical custody (payment % is %)', NEW.payment_id, p.origin;
  END IF;
  IF p.amount <> NEW.amount OR p.currency_code <> NEW.currency_code OR p.receiving_branch_id <> NEW.receiving_branch_id
     OR p.collected_by <> NEW.custodian_user_id OR p.received_at <> NEW.received_at THEN
    RAISE EXCEPTION 'field custody receipt must snapshot exactly its payment (amount, currency, branch, custodian, time)';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""
RECEIPT_GUARD_FN = """
CREATE OR REPLACE FUNCTION credit_field_custody_receipts_guard() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'field custody receipt % is immutable history: it cannot be %', OLD.id, lower(TG_OP);
END $$ LANGUAGE plpgsql
"""
RENDITION_INSERT_FN = """
CREATE OR REPLACE FUNCTION credit_field_renditions_insert_check() RETURNS trigger AS $$
BEGIN
  IF NEW.state <> 'declared' OR NEW.counted_amount IS NOT NULL OR NEW.decision_reason IS NOT NULL THEN
    RAISE EXCEPTION 'a field rendition is born declared, without a decision';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""
RENDITION_GUARD_FN = """
CREATE OR REPLACE FUNCTION credit_field_renditions_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'field rendition % cannot be deleted: it is history', OLD.id;
  END IF;
  IF OLD.state <> 'declared' THEN
    RAISE EXCEPTION 'field rendition % is % (terminal): it cannot change', OLD.id, OLD.state;
  END IF;
  IF NEW.state NOT IN ('accepted', 'rejected', 'cancelled') THEN
    RAISE EXCEPTION 'field rendition % may only move from declared to accepted, rejected or cancelled', OLD.id;
  END IF;
  IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.rendition_number <> OLD.rendition_number
     OR NEW.receiving_branch_id <> OLD.receiving_branch_id OR NEW.custodian_user_id <> OLD.custodian_user_id
     OR NEW.currency_code <> OLD.currency_code OR NEW.declared_amount <> OLD.declared_amount
     OR NEW.declared_by <> OLD.declared_by OR NEW.declared_at <> OLD.declared_at
     OR NEW.create_idempotency_key <> OLD.create_idempotency_key OR NEW.create_request_digest <> OLD.create_request_digest THEN
    RAISE EXCEPTION 'field rendition % identity and declaration are immutable', OLD.id;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""
ITEM_INSERT_FN = """
CREATE OR REPLACE FUNCTION credit_field_rendition_items_insert_check() RETURNS trigger AS $$
BEGIN
  IF NEW.released THEN
    RAISE EXCEPTION 'a field rendition item is born claiming its receipt (released = false)';
  END IF;
  IF (SELECT state FROM credit_field_renditions WHERE id = NEW.rendition_id) IS DISTINCT FROM 'declared' THEN
    RAISE EXCEPTION 'items can only be added to a declared field rendition';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""
ITEM_GUARD_FN = """
CREATE OR REPLACE FUNCTION credit_field_rendition_items_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'field rendition item % cannot be deleted: it is history', OLD.id;
  END IF;
  IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.rendition_id <> OLD.rendition_id
     OR NEW.receipt_id <> OLD.receipt_id OR NEW.payment_id <> OLD.payment_id
     OR NEW.receiving_branch_id <> OLD.receiving_branch_id OR NEW.custodian_user_id <> OLD.custodian_user_id
     OR NEW.currency_code <> OLD.currency_code OR NEW.amount <> OLD.amount THEN
    RAISE EXCEPTION 'field rendition item % is immutable: only its release may change', OLD.id;
  END IF;
  IF OLD.released OR NOT NEW.released THEN
    RAISE EXCEPTION 'field rendition item % may only be released once (false -> true)', OLD.id;
  END IF;
  IF (SELECT state FROM credit_field_renditions WHERE id = OLD.rendition_id) NOT IN ('rejected', 'cancelled') THEN
    RAISE EXCEPTION 'only a rejected or cancelled field rendition releases its items';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql
"""
# Checked at COMMIT (deferred): header and items can never be committed in an inconsistent state, and an accepted
# rendition is backed by exactly its cash movement.
RENDITION_CONSISTENCY_FN = """
CREATE OR REPLACE FUNCTION credit_field_rendition_consistency_check() RETURNS trigger AS $$
DECLARE r RECORD; v_id integer; v_items integer; v_sum numeric; v_released integer; m RECORD;
BEGIN
  IF TG_TABLE_NAME = 'credit_field_renditions' THEN v_id := NEW.id; ELSE v_id := NEW.rendition_id; END IF;
  SELECT * INTO r FROM credit_field_renditions WHERE id = v_id;
  SELECT count(*), COALESCE(sum(amount), 0), count(*) FILTER (WHERE released)
    INTO v_items, v_sum, v_released FROM credit_field_rendition_items WHERE rendition_id = r.id;
  IF v_items = 0 OR v_sum <> r.declared_amount THEN
    RAISE EXCEPTION 'field rendition % declared amount (%) must equal its items (% in % items)', r.id, r.declared_amount, v_sum, v_items;
  END IF;
  IF r.state IN ('declared', 'accepted') AND v_released > 0 THEN
    RAISE EXCEPTION 'field rendition % is % but has released items', r.id, r.state;
  END IF;
  IF r.state IN ('rejected', 'cancelled') AND v_released <> v_items THEN
    RAISE EXCEPTION 'field rendition % is % but still claims receipts', r.id, r.state;
  END IF;
  IF r.state = 'accepted' THEN
    SELECT kind, amount, session_id INTO m FROM cash_movements WHERE id = r.cash_movement_id;
    IF m.kind IS DISTINCT FROM 'credit_field_rendition' OR m.amount IS DISTINCT FROM r.declared_amount
       OR m.session_id IS DISTINCT FROM r.cash_session_id THEN
      RAISE EXCEPTION 'accepted field rendition % is not backed by its cash movement', r.id;
    END IF;
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql
"""
TRIGGERS = (
    "CREATE TRIGGER trg_credit_field_custody_receipts_insert_check BEFORE INSERT ON credit_field_custody_receipts "
    "FOR EACH ROW EXECUTE FUNCTION credit_field_custody_receipts_insert_check()",
    "CREATE TRIGGER trg_credit_field_custody_receipts_guard BEFORE UPDATE OR DELETE ON credit_field_custody_receipts "
    "FOR EACH ROW EXECUTE FUNCTION credit_field_custody_receipts_guard()",
    "CREATE TRIGGER trg_credit_field_renditions_insert_check BEFORE INSERT ON credit_field_renditions "
    "FOR EACH ROW EXECUTE FUNCTION credit_field_renditions_insert_check()",
    "CREATE TRIGGER trg_credit_field_renditions_guard BEFORE UPDATE OR DELETE ON credit_field_renditions "
    "FOR EACH ROW EXECUTE FUNCTION credit_field_renditions_guard()",
    "CREATE TRIGGER trg_credit_field_rendition_items_insert_check BEFORE INSERT ON credit_field_rendition_items "
    "FOR EACH ROW EXECUTE FUNCTION credit_field_rendition_items_insert_check()",
    "CREATE TRIGGER trg_credit_field_rendition_items_guard BEFORE UPDATE OR DELETE ON credit_field_rendition_items "
    "FOR EACH ROW EXECUTE FUNCTION credit_field_rendition_items_guard()",
    "CREATE CONSTRAINT TRIGGER trg_credit_field_renditions_consistency AFTER INSERT OR UPDATE ON credit_field_renditions "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION credit_field_rendition_consistency_check()",
    "CREATE CONSTRAINT TRIGGER trg_credit_field_rendition_items_consistency AFTER INSERT OR UPDATE ON credit_field_rendition_items "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION credit_field_rendition_consistency_check()",
)
FUNCTIONS = (
    RECEIPT_INSERT_FN,
    RECEIPT_GUARD_FN,
    RENDITION_INSERT_FN,
    RENDITION_GUARD_FN,
    ITEM_INSERT_FN,
    ITEM_GUARD_FN,
    RENDITION_CONSISTENCY_FN,
)
# each trigger goes with its own table
for _name in ("credit_field_custody_receipts", "credit_field_renditions", "credit_field_rendition_items"):
    # CREATE OR REPLACE: whichever of the three tables is created first brings the functions its triggers need
    for _fn in FUNCTIONS:
        event.listen(
            Base.metadata.tables[_name], "after_create", DDL(_fn.replace("%", "%%")).execute_if(dialect="postgresql")
        )
for _sql in TRIGGERS:
    _table = _sql.split(" ON ")[1].split(" ")[0]
    event.listen(Base.metadata.tables[_table], "after_create", DDL(_sql).execute_if(dialect="postgresql"))
