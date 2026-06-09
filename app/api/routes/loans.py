from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user, get_db
from app.models.customer import Customer
from app.models.loan import Loan, LoanStatus
from app.models.user import User
from app.schemas.loan import LoanCreate, LoanRead
from app.services.loan_service import create_loan, refresh_loan_state

router = APIRouter()


@router.get("", response_model=list[LoanRead])
def list_loans(
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[Loan]:
    statement = (
        select(Loan)
        .options(selectinload(Loan.customer), selectinload(Loan.installments))
        .order_by(Loan.created_at.desc())
    )
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
    current_user: User = Depends(get_current_user),
) -> Loan:
    customer = db.get(Customer, payload.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Cliente no encontrado.")

    loan = create_loan(db=db, payload=payload, created_by_id=current_user.id)
    db.commit()
    db.refresh(loan)
    return loan


@router.get("/{loan_id}", response_model=LoanRead)
def get_loan(
    loan_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Loan:
    statement = (
        select(Loan)
        .where(Loan.id == loan_id)
        .options(selectinload(Loan.customer), selectinload(Loan.installments), selectinload(Loan.payments))
    )
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
    _: User = Depends(get_current_user),
) -> Loan:
    statement = (
        select(Loan)
        .where(Loan.id == loan_id)
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
    _: User = Depends(get_current_user),
) -> Loan:
    statement = (
        select(Loan)
        .where(Loan.id == loan_id)
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
