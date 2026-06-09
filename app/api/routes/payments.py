from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user, get_db
from app.models.loan import Loan
from app.models.payment import Payment
from app.models.user import User
from app.schemas.payment import PaymentCreate, PaymentRead
from app.services.payment_service import apply_payment

router = APIRouter()


@router.get("", response_model=list[PaymentRead])
def list_payments(
    loan_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[Payment]:
    statement = (
        select(Payment)
        .options(selectinload(Payment.loan), selectinload(Payment.collected_by))
        .order_by(Payment.paid_at.desc())
    )
    if loan_id is not None:
        statement = statement.where(Payment.loan_id == loan_id)
    return list(db.scalars(statement).unique().all())


@router.post("", response_model=PaymentRead, status_code=status.HTTP_201_CREATED)
def create_payment(
    payload: PaymentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Payment:
    loan = db.get(Loan, payload.loan_id)
    if loan is None:
        raise HTTPException(status_code=404, detail="Prestamo no encontrado.")

    payment = apply_payment(db=db, payload=payload, collected_by_id=current_user.id)
    db.commit()
    db.refresh(payment)
    return payment
