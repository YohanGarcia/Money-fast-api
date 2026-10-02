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
from app.modules.loans.models import CreditLoan, CreditLoanObligation, CreditPaymentApplication
from app.modules.origination.models import CreditFormalization


def _amt(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.0001")), "f")


def applied_by_obligation(db: Session, loan_id: int) -> dict[int, dict[str, Decimal]]:
    rows = db.execute(
        select(
            CreditPaymentApplication.obligation_id,
            CreditPaymentApplication.component,
            func.sum(CreditPaymentApplication.amount),
        )
        .where(CreditPaymentApplication.loan_id == loan_id)
        .group_by(CreditPaymentApplication.obligation_id, CreditPaymentApplication.component)
    )
    out: dict[int, dict[str, Decimal]] = {}
    for oid, component, total in rows:
        out.setdefault(oid, {})[component] = total
    return out


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
    return obligations, [
        allocation.ObligationView(
            id=o.id,
            sequence=o.sequence,
            due_date=o.due_date,
            due={
                "fee": o.fees_due,
                "delinquency": o.delinquency_due,
                "interest": o.interest_due,
                "principal": o.principal_due,
            },
            applied=applied.get(o.id, {}),
        )
        for o in obligations
    ]


def contract_timezone(db: Session, loan: CreditLoan) -> str:
    """The calendar timezone FROZEN in the contract (never the live product)."""
    f = db.get(CreditFormalization, loan.formalization_id)
    return f.contract_snapshot["product"]["snapshot"]["rules"]["calendar"]["timezone"]


def business_date(db: Session, loan: CreditLoan, now: datetime) -> date:
    return to_zone(now, contract_timezone(db, loan)).date()


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
    }
