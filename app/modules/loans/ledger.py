"""Read side of the loan ledger (T-008): obligations + payment applications -> derived figures. Never writes.

Used by the loan reads (T-007 endpoints) and by the payment command, so every balance in the API comes from the same
derivation: contractual obligations minus payment applications.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.time import to_zone
from app.modules.loans import allocation
from app.modules.loans.models import (
    CreditLoan,
    CreditLoanObligation,
    CreditPaymentApplication,
    CreditPaymentReversalApplication,
)
from app.modules.origination.models import CreditFormalization


def _amt(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.0001")), "f")


def applied_by_obligation_many(db: Session, loan_ids: list[int]) -> dict[int, dict[str, Decimal]]:
    """NET applied per obligation and component for SEVERAL loans in two queries: applications - reversal applications
    (T-009). THE only place where the paid amount is computed: nothing else may read the gross applications as "paid"."""
    if not loan_ids:
        return {}
    gross = db.execute(
        select(
            CreditPaymentApplication.obligation_id,
            CreditPaymentApplication.component,
            func.sum(CreditPaymentApplication.amount),
        )
        .where(CreditPaymentApplication.loan_id.in_(loan_ids))
        .group_by(CreditPaymentApplication.obligation_id, CreditPaymentApplication.component)
    )
    reversed_ = {
        (oid, component): total
        for oid, component, total in db.execute(
            select(
                CreditPaymentReversalApplication.obligation_id,
                CreditPaymentReversalApplication.component,
                func.sum(CreditPaymentReversalApplication.amount),
            )
            .where(CreditPaymentReversalApplication.loan_id.in_(loan_ids))
            .group_by(CreditPaymentReversalApplication.obligation_id, CreditPaymentReversalApplication.component)
        )
    }
    out: dict[int, dict[str, Decimal]] = {}
    for oid, component, total in gross:
        out.setdefault(oid, {})[component] = total - reversed_.get((oid, component), Decimal(0))
    return out


def applied_by_obligation(db: Session, loan_id: int) -> dict[int, dict[str, Decimal]]:
    return applied_by_obligation_many(db, [loan_id])


def _view(o: CreditLoanObligation, applied: dict[str, Decimal]) -> allocation.ObligationView:
    return allocation.ObligationView(
        id=o.id,
        sequence=o.sequence,
        due_date=o.due_date,
        due={
            "fee": o.fees_due,
            "delinquency": o.delinquency_due,
            "interest": o.interest_due,
            "principal": o.principal_due,
        },
        applied=applied,
    )


def views_many(db: Session, loan_ids: list[int]) -> dict[int, list[allocation.ObligationView]]:
    """Obligation views of SEVERAL loans (no N+1): the worklist reads the same net ledger as every single-loan read."""
    out: dict[int, list[allocation.ObligationView]] = {lid: [] for lid in loan_ids}
    if not loan_ids:
        return out
    applied = applied_by_obligation_many(db, loan_ids)
    rows = db.scalars(
        select(CreditLoanObligation)
        .where(CreditLoanObligation.loan_id.in_(loan_ids))
        .order_by(CreditLoanObligation.loan_id, CreditLoanObligation.sequence)
    )
    for o in rows:
        out[o.loan_id].append(_view(o, applied.get(o.id, {})))
    return out


PROJECTABLE_STATUSES = ("active", "past_due", "paid")  # the only stored statuses this projection may move between


def project(db: Session, loan: CreditLoan, obligations: list[CreditLoanObligation], business_date: date) -> None:
    """Re-derive every obligation's status from its amounts and the NET applications ONLY, then the loan status through
    the ONE central rule (``allocation.loan_status``: paid > past_due > active) at ``business_date``.
    Used by payments, payment reversals and the explicit delinquency assessment; never by a read."""
    _o, views_ = views(db, loan.id)  # re-read: includes the rows just flushed
    by_id = {v.id: v for v in views_}
    for ob in obligations:
        ob.status = allocation.obligation_status(by_id[ob.id])
    if loan.status in PROJECTABLE_STATUSES:  # restructured / refinanced / cancelled ... are never touched here
        loan.status = allocation.loan_status(views_, business_date)


def views(
    db: Session, loan_id: int, *, lock: bool = False
) -> tuple[list[CreditLoanObligation], list[allocation.ObligationView]]:
    stmt = (
        select(CreditLoanObligation)
        .where(CreditLoanObligation.loan_id == loan_id)
        .order_by(CreditLoanObligation.sequence)
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)  # ascending sequence: one lock order
    obligations = list(db.scalars(stmt))
    applied = applied_by_obligation(db, loan_id)
    return obligations, [_view(o, applied.get(o.id, {})) for o in obligations]


def contract_timezone(db: Session, loan: CreditLoan) -> str:
    """The calendar timezone FROZEN in the contract (never the live product)."""
    f = db.get(CreditFormalization, loan.formalization_id)
    return f.contract_snapshot["product"]["snapshot"]["rules"]["calendar"]["timezone"]


def business_date(db: Session, loan: CreditLoan, now: datetime) -> date:
    return business_date_in(contract_timezone(db, loan), now)


def business_date_in(timezone: str, now: datetime) -> date:
    """The business date of an instant in a contract timezone (never the UTC date)."""
    return to_zone(now, timezone).date()


def balances_out(
    views_: list[allocation.ObligationView], business_date_: date, original_principal: Decimal, currency: str
) -> dict:
    b = allocation.balances(views_, business_date_)
    return {
        "original_principal": _amt(original_principal),
        "outstanding_principal": _amt(b["outstanding_principal"]),
        "outstanding_interest": _amt(b["outstanding_interest"]),
        "outstanding_fees": _amt(b["outstanding_fee"]),
        "outstanding_delinquency": _amt(b["outstanding_delinquency"]),
        "total_outstanding": _amt(b["total_outstanding"]),
        "due_to_date_outstanding": _amt(b["due_to_date_outstanding"]),
        "total_paid": _amt(b["total_paid"]),
        "total_debt": _amt(b["total_outstanding"]),
        "currency_code": currency,
        "business_date": business_date_.isoformat(),
        **{
            k: (_amt(v) if k == "overdue_outstanding" else v)
            for k, v in allocation.overdue_summary(views_, business_date_).items()
        },
        "projected_status": allocation.loan_status(
            views_, business_date_
        ),  # DERIVED from the net ledger; stored status may lag
    }
