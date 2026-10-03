"""Explicit overdue assessment (T-010): projects the STORED ``credit_loans.status`` (active / past_due / paid) from the
net ledger and the business date. It does NOT calculate any delinquency charge, accrual, fee or money of any kind.

* overdue = ``business_date > effective due_date`` AND net outstanding > 0 (``allocation.is_overdue``); the delinquency
  grace days, ``delinquency_starts_on`` and ``delinquency.enabled`` are irrelevant here.
* Priority (one central rule, ``allocation.loan_status``): fully settled -> ``paid``; any overdue net debt -> ``past_due``;
  otherwise ``active``.
* Lock order (loan first, always): 1. loan row ``FOR UPDATE``  2. obligations, ascending sequence. Cash is not involved.
* Idempotent without a key: it is a deterministic projection. Re-running it on an unchanged state writes nothing and audits
  nothing. Only a real change of the stored status is written and audited.
* Authorization: ``loans.delinquency.assess`` on the loan's MANAGING branch (never origin, disbursement or receiving).
* No batch, no scheduler: a future one can call ``assess`` loan by loan (one transaction per loan, loan lock first).
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import now_utc
from app.modules.identity.audit import record_event
from app.modules.identity.authorization import Principal, require
from app.modules.identity.errors import TenantMismatch
from app.modules.loans import allocation, ledger
from app.modules.loans.errors import LoanNotAssessable
from app.modules.loans.models import CreditLoan
from app.modules.loans.payments import _contract, _gate_tenant
from app.modules.origination.service import _amt

ASSESS = "loans.delinquency.assess"


def assess(db: Session, actor: Principal, loan_id: int, client_ip: str | None) -> dict:
    _gate_tenant(actor)
    # 1. LOAN ROW FIRST (tenant-filtered: a foreign loan is a 404)
    loan = db.scalar(
        select(CreditLoan)
        .where(CreditLoan.id == loan_id, CreditLoan.tenant_id == actor.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if loan is None:
        raise TenantMismatch()
    require(actor, ASSESS, tenant_id=actor.tenant_id, branch_id=loan.managing_branch_id)
    if loan.status not in ledger.PROJECTABLE_STATUSES:
        raise LoanNotAssessable()
    f, _rules = _contract(db, loan)  # the frozen contract (hashes + timezone); the live product is never consulted
    # 2. obligations, ascending sequence (locked); the net ledger is re-read AFTER the lock
    _obligations, views = ledger.views(db, loan.id, lock=True)
    business_date = ledger.business_date(db, loan, now_utc())  # the contract timezone, never the UTC date
    summary = allocation.overdue_summary(views, business_date)
    new_status = allocation.loan_status(views, business_date)
    previous = loan.status
    changed = new_status != previous
    if changed:
        loan.status = new_status
        db.flush()
        record_event(
            db,
            "loan.delinquency_assessed",
            tenant_id=actor.tenant_id,
            actor_id=actor.user_id,
            client_ip=client_ip,
            details={
                "loan_id": loan.id,
                "loan_number": loan.loan_number,
                "previous_status": previous,
                "resulting_status": new_status,
                "business_date": business_date.isoformat(),
                "overdue_obligations": summary["overdue_obligations"],
                "overdue_outstanding": _amt(summary["overdue_outstanding"]),
                "max_days_overdue": summary["max_days_overdue"],
                "managing_branch_id": loan.managing_branch_id,
                "rules_digest": f.rules_hash,
                "contract_digest": f.contract_hash,
            },
        )
    db.commit()  # nothing to write when unchanged: just releases the locks
    return {
        "loan_id": loan.id,
        "previous_status": previous,
        "status": loan.status,
        "changed": changed,
        "business_date": business_date,
        "overdue_obligations": summary["overdue_obligations"],
        "overdue_outstanding": _amt(summary["overdue_outstanding"]),
        "max_days_overdue": summary["max_days_overdue"],
        "currency_code": loan.currency_code,
    }
