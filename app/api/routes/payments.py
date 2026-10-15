from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_company_id, get_current_user, get_db
from app.api.pagination import Page, page_params, paginate_query
from app.models.customer import Customer
from app.models.loan import Loan
from app.models.payment import Payment
from app.models.user import User, UserRole
from app.schemas.payment import PaymentCreate, PaymentRead
from app.services import cash_service
from app.schemas.cash import CashPaymentResult
from app.api.routes.cash import commit

router = APIRouter()


def _day_window(day: date):
    """[start, end) UTC instants of a calendar day in the company time zone."""
    from app.services.payroll_service import _window

    return _window(day, day)


@router.get("", response_model=list[PaymentRead])
def list_payments(
    response: Response,
    loan_id: int | None = Query(default=None),
    q: str | None = Query(default=None, description="Cliente, cédula, cobrador o número de préstamo"),
    start: date | None = None,
    end: date | None = None,
    payment_type: str | None = Query(default=None, max_length=30),
    page: Page = Depends(page_params),
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
    if start and end and start > end:
        raise HTTPException(422, "Rango de fechas inválido.")
    if start:
        statement = statement.where(Payment.paid_at >= _day_window(start)[0])
    if end:
        statement = statement.where(Payment.paid_at < _day_window(end)[1])
    if payment_type:
        statement = statement.where(Payment.payment_type == payment_type)
    if q and q.strip():
        term = f"%{q.strip()}%"
        collectors = select(User.id).where(User.company_id == company_id, User.full_name.ilike(term))
        conditions = [Customer.full_name.ilike(term), Customer.document_id.ilike(term),
                      Payment.collected_by_id.in_(collectors)]
        if q.strip().isdigit():
            conditions.append(Payment.loan_id == int(q.strip()))
        statement = statement.where(or_(*conditions))
    total_amount = db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.id.in_(statement.with_only_columns(Payment.id).order_by(None)))) or 0
    response.headers["X-Total-Amount"] = str(total_amount)
    return paginate_query(db, response, statement, page)


@router.post("", response_model=PaymentRead | CashPaymentResult, status_code=status.HTTP_201_CREATED)
def create_payment(
    payload: PaymentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_company_id),
) -> Payment:
    cash_service.lock_company(db, company_id)
    if not cash_service.enabled(db, company_id):
        # Caja is the only way to register a payment. No role gets a bypass:
        # until the company activates Caja for every active branch, no
        # payment can be recorded at all.
        raise HTTPException(409, "Activa Caja antes de registrar pagos.")
    return commit(db, lambda: cash_service.register_payment(db, current_user, payload))
