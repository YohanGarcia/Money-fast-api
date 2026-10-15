from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.services import cash_service
from app.api.pagination import Page, matches, page_params, paginate
from app.services.branch_scope import scope_customers
from app.api.deps import get_company_id, get_current_user, get_db, require_admin_manager
from app.models.customer import Customer
from app.models.loan import Loan, LoanStatus
from app.models.user import User
from app.schemas.loan import LoanCreate, LoanRead
from app.services.loan_service import create_loan, refresh_loan_state
from app.services.plan_limits import enforce_can_create, enforce_feature

router = APIRouter()


@router.get("", response_model=list[LoanRead])
def list_loans(
    response: Response,
    status_filter: str | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None, description="Cliente, cédula o número de préstamo"),
    page: Page = Depends(page_params),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_company_id),
) -> list[Loan]:
    statement = (
        select(Loan)
        .join(Loan.customer)
        .where(Customer.company_id == company_id)
        .options(selectinload(Loan.customer), selectinload(Loan.installments))
        .order_by(Loan.created_at.desc())
    )
    # Everyone but the administration sees the loans of their branch; a collector, those of their portfolio.
    statement = scope_customers(statement, current_user)
    loans = list(db.scalars(statement).unique().all())
    changed = any([refresh_loan_state(loan) for loan in loans])  # a loan may have just turned late: filter AFTER it
    if changed and not cash_service.enabled(db, company_id):
        db.commit()
    if status_filter is not None:
        loans = [loan for loan in loans if loan.status.value == status_filter]
    if q:
        loans = [ln for ln in loans if matches(q, ln.customer.full_name, ln.customer.document_id, ln.id)]
    return paginate(response, loans, page)


@router.post("", response_model=LoanRead, status_code=status.HTTP_201_CREATED)
def add_loan(
    payload: LoanCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> Loan:
    customer = db.scalar(scope_customers(
        select(Customer).where(Customer.id == payload.customer_id, Customer.company_id == company_id), current_user
    ))
    if customer is None:
        raise HTTPException(status_code=404, detail="Cliente no encontrado.")

    cash_service.lock_company(db, company_id)
    if cash_service.enabled(db, company_id):
        raise HTTPException(409, "Con Caja habilitada, desembolsa una solicitud firmada desde Caja.")
    enforce_can_create(db, company_id, "loan")
    if payload.requires_promissory_note:
        enforce_feature(db, company_id, "promissory_note")
    loan = create_loan(db=db, payload=payload, created_by_id=current_user.id)
    db.commit()
    db.refresh(loan)
    return loan


@router.get("/{loan_id}", response_model=LoanRead)
def get_loan(
    loan_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_company_id),
) -> Loan:
    statement = (
        select(Loan)
        .join(Loan.customer)
        .where(Loan.id == loan_id, Customer.company_id == company_id)
        .options(selectinload(Loan.customer), selectinload(Loan.installments), selectinload(Loan.payments))
    )
    statement = scope_customers(statement, current_user)
    loan = db.scalar(statement)
    if loan is None:
        raise HTTPException(status_code=404, detail="Prestamo no encontrado.")
    if refresh_loan_state(loan) and not cash_service.enabled(db, company_id):
        db.commit()
        db.refresh(loan)
    return loan


@router.post("/{loan_id}/approve", response_model=LoanRead)
def approve_loan(
    loan_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> Loan:
    statement = scope_customers(
        select(Loan)
        .join(Loan.customer)
        .where(Loan.id == loan_id, Customer.company_id == company_id)
        .options(selectinload(Loan.customer), selectinload(Loan.installments), selectinload(Loan.payments)),
        current_user,
    )
    loan = db.scalar(statement)
    if loan is None:
        raise HTTPException(status_code=404, detail="Prestamo no encontrado.")
    if loan.status != LoanStatus.pending_approval:
        raise HTTPException(status_code=400, detail="El prestamo ya no esta pendiente de aprobacion.")

    loan.status = LoanStatus.active
    db.commit()
    db.refresh(loan)
    return loan


@router.post("/{loan_id}/reject", response_model=LoanRead)
def reject_loan(
    loan_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> Loan:
    statement = scope_customers(
        select(Loan)
        .join(Loan.customer)
        .where(Loan.id == loan_id, Customer.company_id == company_id)
        .options(selectinload(Loan.customer), selectinload(Loan.installments), selectinload(Loan.payments)),
        current_user,
    )
    loan = db.scalar(statement)
    if loan is None:
        raise HTTPException(status_code=404, detail="Prestamo no encontrado.")
    if loan.status != LoanStatus.pending_approval:
        raise HTTPException(status_code=400, detail="El prestamo ya no esta pendiente de aprobacion.")

    loan.status = LoanStatus.cancelled
    db.commit()
    db.refresh(loan)
    return loan
