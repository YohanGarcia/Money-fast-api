"""Mini "data crédito": lets a company check whether a person already has a
loan with another company on the same system, before approving a new one.

Only cross-tenant, minimal data is exposed (which company, since when, and
whether it's still being paid or already settled) — never the other
company's internal customer id, contact info, or exact balances beyond what
is needed to judge the person's current exposure.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_manager
from app.models.company import Company
from app.models.customer import Customer
from app.models.loan import Loan, LoanStatus
from app.models.user import User
from app.schemas.credit_bureau import CreditBureauLoan, CreditBureauReport
from app.services.customer_profile import document_key

router = APIRouter()

_STATUS_LABELS = {
    LoanStatus.active: "Pagando",
    LoanStatus.late: "Pagando (atrasado)",
    LoanStatus.paid: "Saldado",
    LoanStatus.cancelled: "Cancelado",
    LoanStatus.pending_approval: "Pendiente de aprobación",
}


@router.get("/{document_id}", response_model=CreditBureauReport)
def lookup(
    document_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin_manager),
) -> CreditBureauReport:
    key = document_key(document_id)
    if not key:
        return CreditBureauReport(document_id=document_id, found=False, entries=[])

    # Cross-tenant on purpose: exclude the requester's own company (they already
    # see their own client's loans directly) and look at every other company.
    customers = db.scalars(
        select(Customer).where(
            Customer.document_key == key,
            Customer.company_id != user.company_id,
        )
    ).all()

    entries: list[CreditBureauLoan] = []
    for customer in customers:
        company = db.get(Company, customer.company_id)
        loans = db.scalars(
            select(Loan)
            .where(Loan.customer_id == customer.id)
            .order_by(Loan.start_date)
        ).all()
        for loan in loans:
            entries.append(
                CreditBureauLoan(
                    company_name=company.name if company else "Empresa desconocida",
                    status=_STATUS_LABELS.get(loan.status, str(loan.status)),
                    start_date=loan.start_date,
                    total_amount=str(loan.total_amount),
                    balance=str(loan.principal_balance + loan.interest_balance + loan.late_fee_balance),
                )
            )

    return CreditBureauReport(document_id=document_id, found=bool(entries), entries=entries)
