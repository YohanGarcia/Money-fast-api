"""Business finance report for the lending company owner (admin/manager). All amounts RD$."""
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from html import escape

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_company_id, get_db, require_admin_manager
from app.models.branch import Branch
from app.models.cash import CashBox, CashSession
from app.models.customer import Customer
from app.models.loan import Loan, LoanStatus
from app.models.payment import Payment
from app.services import cash_service as svc
from app.services.xlsx import build_xlsx

router = APIRouter()
ZERO = Decimal("0.00")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def compute(db: Session, company_id: int, start: date, end: date) -> dict:
    if start > end:
        svc.fail("Rango de fechas inválido.")
    low = datetime.combine(start, time.min, svc.TZ).astimezone(UTC)
    high = datetime.combine(end + timedelta(days=1), time.min, svc.TZ).astimezone(UTC)

    loans = db.scalars(select(Loan).join(Loan.customer).where(Customer.company_id == company_id)).all()
    active = [l for l in loans if l.status in (LoanStatus.active, LoanStatus.late)]
    cartera_capital = sum((l.principal_balance for l in active), ZERO)
    cartera_interes = sum((l.interest_balance for l in active), ZERO)
    cartera_mora = sum((l.late_fee_balance for l in active), ZERO)
    clientes_activos = len({l.customer_id for l in active})

    loan_ids = [l.id for l in loans]
    pays = db.scalars(select(Payment).where(Payment.loan_id.in_(loan_ids))).all() if loan_ids else []
    period_pays = [p for p in pays if low <= _aware(p.paid_at) < high]
    cobrado = sum((p.amount for p in period_pays), ZERO)
    cap_recuperado = sum((p.principal_applied for p in period_pays), ZERO)
    interes_cobrado = sum((p.interest_applied for p in period_pays), ZERO)
    mora_cobrada = sum((p.late_fee_applied for p in period_pays), ZERO)

    desembolsado = sum((l.principal_amount for l in loans if low <= _aware(l.created_at) < high), ZERO)
    prestamos_nuevos = sum(1 for l in loans if low <= _aware(l.created_at) < high)

    efectivo = None
    if svc.enabled(db, company_id):
        efectivo = ZERO
        for box in db.scalars(select(CashBox).where(CashBox.company_id == company_id)).all():
            last = db.scalar(select(CashSession).where(CashSession.box_id == box.id).order_by(CashSession.id.desc()))
            if last is None:
                efectivo += box.initial_balance
            elif last.state == "open":
                efectivo += last.balance
            else:
                efectivo += last.counted if last.counted is not None else last.balance

    por_cobrar = cartera_capital + cartera_interes + cartera_mora
    ganancia_cobrada = interes_cobrado + mora_cobrada
    ganancia_proyectada = cartera_interes + cartera_mora
    capital_en_negocio = (efectivo or ZERO) + cartera_capital

    def s(v: Decimal) -> str:
        return str(v.quantize(Decimal("0.01")))

    return dict(
        start=str(start), end=str(end),
        caja_enabled=svc.enabled(db, company_id),
        efectivo_caja=None if efectivo is None else s(efectivo),
        cartera_capital=s(cartera_capital),
        cartera_interes=s(cartera_interes),
        cartera_mora=s(cartera_mora),
        por_cobrar=s(por_cobrar),
        capital_en_negocio=s(capital_en_negocio),
        prestamos_activos=len(active),
        clientes_activos=clientes_activos,
        cobrado=s(cobrado),
        capital_recuperado=s(cap_recuperado),
        interes_cobrado=s(interes_cobrado),
        mora_cobrada=s(mora_cobrada),
        ganancia_cobrada=s(ganancia_cobrada),
        ganancia_proyectada=s(ganancia_proyectada),
        desembolsado=s(desembolsado),
        prestamos_nuevos=prestamos_nuevos,
        pagos_periodo=len(period_pays),
    )


def _rows(d: dict) -> list[list]:
    efectivo = d["efectivo_caja"] if d["efectivo_caja"] is not None else "Caja no habilitada"
    return [
        ["Reporte financiero"],
        ["Período", f'{d["start"]} a {d["end"]}'],
        [],
        ["Tu dinero ahora (foto actual)", "RD$"],
        ["Efectivo en caja", efectivo],
        ["En préstamos (capital en la calle)", d["cartera_capital"]],
        ["Interés por cobrar", d["cartera_interes"]],
        ["Mora por cobrar", d["cartera_mora"]],
        ["Total por cobrar (capital + interés + mora)", d["por_cobrar"]],
        ["Capital en el negocio (efectivo + capital en la calle)", d["capital_en_negocio"]],
        ["Préstamos activos", d["prestamos_activos"]],
        ["Clientes con préstamo activo", d["clientes_activos"]],
        [],
        ["Movimiento del período", "RD$"],
        ["Cobrado (total)", d["cobrado"]],
        ["  · Capital recuperado", d["capital_recuperado"]],
        ["  · Interés cobrado", d["interes_cobrado"]],
        ["  · Mora cobrada", d["mora_cobrada"]],
        ["Ganancia cobrada (interés + mora)", d["ganancia_cobrada"]],
        ["Ganancia proyectada (interés + mora pendiente)", d["ganancia_proyectada"]],
        ["Desembolsado (capital colocado)", d["desembolsado"]],
        ["Préstamos nuevos", d["prestamos_nuevos"]],
        ["Pagos registrados", d["pagos_periodo"]],
    ]


@router.get("/finance")
def finance(start: date, end: date, format: str = "json",
            db: Session = Depends(get_db),
            company_id: int = Depends(get_company_id),
            _=Depends(require_admin_manager)):
    data = compute(db, company_id, start, end)
    if format == "json":
        return data
    rows = _rows(data)
    if format == "print":
        html = ('<html><head><meta charset="utf-8"><title>Reporte financiero</title>'
                '<style>@page{size:A4}body{font:13px Arial;color:#17334f;padding:20px}'
                'h1{font-size:20px}table{border-collapse:collapse;width:100%;margin-top:10px}'
                'td{border:1px solid #ccc;padding:7px}tr:first-child td{background:#edf3fa;font-weight:bold}</style>'
                '</head><body><h1>Reporte financiero</h1><table>'
                + ''.join('<tr>' + ''.join('<td>' + escape(str(c)) + '</td>' for c in (row or [''])) + '</tr>' for row in rows)
                + '</table></body></html>')
        return {"html": html}
    if format != "xlsx":
        svc.fail("Formato inválido.")
    payload = build_xlsx("Finanzas", rows)
    return Response(payload, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="reporte-financiero.xlsx"'})
