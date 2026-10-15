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


def _filtered(db, current_user, company_id, *, loan_id, q, start, end, payment_type, method, origin, reversed_only):
    """The payments a user may see, with the filters shared by the list and the export."""
    from app.services.payroll_service import reversed_payment_ids

    statement = (
        select(Payment)
        .join(Payment.loan)
        .join(Loan.customer)
        .where(Customer.company_id == company_id)
        .options(selectinload(Payment.loan).selectinload(Loan.customer), selectinload(Payment.collected_by))
        .order_by(Payment.paid_at.desc(), Payment.id.desc())
    )
    # Historical custody follows the original collector, not reassigned customers.
    if current_user.role == UserRole.collector:
        statement = statement.where(Payment.collected_by_id == current_user.id)
    if current_user.role == UserRole.cashier:
        if not current_user.branch_id:
            raise HTTPException(403, "Asigna una sucursal al cajero.")
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
    if method:
        statement = statement.where(Payment.method == method)
    if origin:
        statement = statement.where(Payment.origin == origin)
    if reversed_only is not None:
        in_reversed = Payment.id.in_(reversed_payment_ids())
        statement = statement.where(in_reversed if reversed_only else ~in_reversed)
    if q and q.strip():
        term = f"%{q.strip()}%"
        collectors = select(User.id).where(User.company_id == company_id, User.full_name.ilike(term))
        conditions = [Customer.full_name.ilike(term), Customer.document_id.ilike(term),
                      Payment.collected_by_id.in_(collectors)]
        if q.strip().isdigit():
            conditions.append(Payment.loan_id == int(q.strip()))
        statement = statement.where(or_(*conditions))
    return statement


def _decorate(db, company_id, rows):
    """Customer, branch and 'reversed' for a batch of payments (two queries, whatever the size)."""
    from app.models.branch import Branch
    from app.services.payroll_service import reversed_payment_ids

    ids = [r.id for r in rows]
    reversed_ids: set[int] = set()
    if ids:
        reversal = reversed_payment_ids().subquery()
        reversed_ids = set(db.scalars(select(reversal.c.payment_id).where(reversal.c.payment_id.in_(ids))).all())
    branches = {b.id: b.name for b in db.scalars(select(Branch).where(Branch.company_id == company_id)).all()}
    for r in rows:
        customer = r.loan.customer if r.loan is not None else None
        r.customer_id = customer.id if customer else None
        r.customer_name = customer.full_name if customer else None
        r.branch_name = branches.get(r.branch_id)
        r.reversed = r.id in reversed_ids
    return rows


@router.get("", response_model=list[PaymentRead])
def list_payments(
    response: Response,
    loan_id: int | None = Query(default=None),
    q: str | None = Query(default=None, description="Cliente, cédula, cobrador o número de préstamo"),
    start: date | None = None,
    end: date | None = None,
    payment_type: str | None = Query(default=None, max_length=30),
    method: str | None = Query(default=None, pattern="^(cash|transfer)$"),
    origin: str | None = Query(default=None, pattern="^(counter|field)$"),
    reversed: bool | None = Query(default=None, description="true = solo revertidos, false = solo vigentes"),
    page: Page = Depends(page_params),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_company_id),
) -> list[Payment]:
    from app.services.payroll_service import reversed_payment_ids

    statement = _filtered(db, current_user, company_id, loan_id=loan_id, q=q, start=start, end=end,
                          payment_type=payment_type, method=method, origin=origin, reversed_only=reversed)
    # the amount total ignores reversed payments: they no longer count as collected
    ids = statement.with_only_columns(Payment.id).order_by(None)
    total_amount = db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.id.in_(ids), Payment.id.not_in(reversed_payment_ids()))) or 0
    response.headers["X-Total-Amount"] = str(total_amount)
    rows = paginate_query(db, response, statement, page)
    return _decorate(db, company_id, rows)


@router.get("/export")
def export_payments(
    format: str = Query(default="xlsx", pattern="^(xlsx|print)$"),
    loan_id: int | None = Query(default=None),
    q: str | None = Query(default=None),
    start: date | None = None,
    end: date | None = None,
    payment_type: str | None = Query(default=None, max_length=30),
    method: str | None = Query(default=None, pattern="^(cash|transfer)$"),
    origin: str | None = Query(default=None, pattern="^(counter|field)$"),
    reversed: bool | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_company_id),
):
    """The WHOLE filtered list (up to 10,000 rows) as an Excel sheet or a printable page."""
    from decimal import Decimal
    from html import escape

    from app.services.xlsx import build_xlsx

    statement = _filtered(db, current_user, company_id, loan_id=loan_id, q=q, start=start, end=end,
                          payment_type=payment_type, method=method, origin=origin, reversed_only=reversed)
    rows = _decorate(db, company_id, list(db.scalars(statement.limit(10_000)).unique().all()))
    types = {"installment": "Cuota", "payoff": "Liquidación", "custom": "Personalizado",
             "principal_only": "Solo capital", "interest_only": "Solo interés"}
    head = ["Recibo", "Fecha", "Cliente", "Préstamo", "Tipo", "Monto (RD$)", "Capital", "Interés", "Mora", "Medio",
            "Origen", "Sucursal", "Cobrador", "Estado", "Referencia", "Nota"]
    table = [head]
    for p in rows:
        when = p.paid_at.astimezone(cash_service.TZ).strftime("%d/%m/%Y %H:%M")
        table.append([f"RC-{p.id:06d}", when, p.customer_name or "", f"#{p.loan_id}",
                      types.get(p.payment_type.value, p.payment_type.value), str(p.amount), str(p.principal_applied),
                      str(p.interest_applied), str(p.late_fee_applied),
                      {"cash": "Efectivo", "transfer": "Transferencia"}.get(p.method or "", ""),
                      {"counter": "Ventanilla", "field": "Calle"}.get(p.origin or "", ""), p.branch_name or "",
                      p.collector_name or "", "Revertido" if p.reversed else "Vigente", p.reference_code or "",
                      p.notes or ""])
    live_total = sum((Decimal(p.amount) for p in rows if not p.reversed), Decimal("0.00"))
    rango = f"{start or 'inicio'} a {end or 'hoy'}"
    if format == "xlsx":
        table += [[], ["Total vigente (sin revertidos)", "", "", "", "", str(live_total)]]
        return Response(build_xlsx("Pagos", table),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": 'attachment; filename="historial-pagos.xlsx"'})
    body = "".join("<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in r) + "</tr>" for r in table[1:])
    html = ('<html><head><meta charset="utf-8"><title>Historial de pagos</title>'
            '<style>@page{size:A4 landscape}body{font:11px Arial;color:#17334f;padding:20px}h1{font-size:19px}'
            'table{border-collapse:collapse;width:100%;margin-top:10px}td,th{border:1px solid #ccc;padding:5px;'
            'text-align:left}th{background:#edf3fa}</style></head><body>'
            f"<h1>Historial de pagos</h1><p>Periodo: {escape(rango)} · Pagos: {len(rows)} · "
            f"Total vigente: RD$ {live_total}</p><table><tr>"
            + "".join(f"<th>{escape(h)}</th>" for h in head) + "</tr>" + body + "</table></body></html>")
    return {"html": html}


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
