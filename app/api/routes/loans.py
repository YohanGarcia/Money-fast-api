from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_company_id, get_current_user, get_db, require_admin_manager
from app.models.customer import Customer
from app.models.loan import Loan, LoanStatus
from app.models.user import User, UserRole
from app.schemas.loan import LoanCreate, LoanRead
from app.services.loan_service import create_loan, refresh_loan_state
from app.services.plan_limits import enforce_can_create

router = APIRouter()


@router.get("", response_model=list[LoanRead])
def list_loans(
    status_filter: str | None = Query(default=None, alias="status"),
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
    # Collectors only see loans of customers assigned to them.
    if current_user.role == UserRole.collector:
        statement = statement.where(Customer.assigned_collector_id == current_user.id)
    loans = list(db.scalars(statement).unique().all())
    if status_filter is not None:
        loans = [loan for loan in loans if loan.status.value == status_filter]
    changed = any(refresh_loan_state(loan) for loan in loans)
    if changed:
        db.commit()
    return loans


@router.post("", response_model=LoanRead, status_code=status.HTTP_201_CREATED)
def add_loan(
    payload: LoanCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> Loan:
    customer = db.scalar(
        select(Customer).where(Customer.id == payload.customer_id, Customer.company_id == company_id)
    )
    if customer is None:
        raise HTTPException(status_code=404, detail="Cliente no encontrado.")

    enforce_can_create(db, company_id, "loan")
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
    if current_user.role == UserRole.collector:
        statement = statement.where(Customer.assigned_collector_id == current_user.id)
    loan = db.scalar(statement)
    if loan is None:
        raise HTTPException(status_code=404, detail="Prestamo no encontrado.")
    if refresh_loan_state(loan):
        db.commit()
        db.refresh(loan)
    return loan


@router.post("/{loan_id}/approve", response_model=LoanRead)
def approve_loan(
    loan_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> Loan:
    statement = (
        select(Loan)
        .join(Loan.customer)
        .where(Loan.id == loan_id, Customer.company_id == company_id)
        .options(selectinload(Loan.customer), selectinload(Loan.installments), selectinload(Loan.payments))
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
    _: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> Loan:
    statement = (
        select(Loan)
        .join(Loan.customer)
        .where(Loan.id == loan_id, Customer.company_id == company_id)
        .options(selectinload(Loan.customer), selectinload(Loan.installments), selectinload(Loan.payments))
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
