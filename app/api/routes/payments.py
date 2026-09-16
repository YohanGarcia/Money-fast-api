from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_company_id, get_current_user, get_db
from app.models.customer import Customer
from app.models.loan import Loan
from app.models.payment import Payment
from app.models.user import User, UserRole
from app.schemas.payment import PaymentCreate, PaymentRead
from app.services.payment_service import apply_payment
from app.services import cash_service
from app.schemas.cash import CashPaymentResult
from app.api.routes.cash import commit

router = APIRouter()


@router.get("", response_model=list[PaymentRead])
def list_payments(
    loan_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_company_id),
) -> list[Payment]:
    statement = (
        select(Payment)
        .join(Payment.loan)
        .join(Loan.customer)
        .where(Customer.company_id == company_id)
        .options(selectinload(Payment.loan), selectinload(Payment.collected_by))
        .order_by(Payment.paid_at.desc())
    )
    # Historical custody follows the original collector, not reassigned customers.
    if current_user.role == UserRole.collector:
        statement = statement.where(Payment.collected_by_id == current_user.id)
    if current_user.role == UserRole.cashier:
        if not current_user.branch_id: raise HTTPException(403, "Asigna una sucursal al cajero.")
        statement = statement.where(Payment.branch_id == current_user.branch_id)
    if loan_id is not None:
        statement = statement.where(Payment.loan_id == loan_id)
    return list(db.scalars(statement).unique().all())


@router.post("", response_model=PaymentRead | CashPaymentResult, status_code=status.HTTP_201_CREATED)
def create_payment(
    payload: PaymentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_company_id),
) -> Payment:
    cash_service.lock_company(db, company_id)
    if cash_service.enabled(db, company_id):
        return commit(db, lambda: cash_service.register_payment(db, current_user, payload))
    if current_user.role == UserRole.cashier:
        raise HTTPException(409, "Activa Caja antes de operar como cajero.")
    statement = (
        select(Loan)
        .join(Loan.customer)
        .where(Loan.id == payload.loan_id, Customer.company_id == company_id)
    )
    # A collector can only register payments for customers assigned to them.
    if current_user.role == UserRole.collector:
        statement = statement.where(Customer.assigned_collector_id == current_user.id)
    loan = db.scalar(statement)
    if loan is None:
        raise HTTPException(status_code=404, detail="Prestamo no encontrado.")

    payment = apply_payment(db=db, payload=payload, collected_by_id=current_user.id)
    db.commit()
    db.refresh(payment)
    return payment
