"""Business finance report for the company owner (admin) and the branch manager. Amounts in the company currency.

Two kinds of figures, labelled as such in the UI:

* a PHOTO of now (portfolio and cash positions) that ignores the period, and
* the MOVEMENT of the period (collected, disbursed, expenses, payroll, result).

Everything is aggregated by the database (no per-row Python loops over loans or payments). A branch manager only ever
sees its own branch; an administrator may narrow the report to one branch.
"""
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import and_, case, distinct, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.api.deps import get_company_id, get_db, require_admin_manager
from app.models.branch import Branch
from app.models.cash import CashBox, CashMovement, CashSession, CashTransfer
from app.models.company_settings import CompanySettings
from app.models.customer import Customer
from app.models.loan import Loan, LoanStatus
from app.models.payment import Payment
from app.models.payroll import PayrollPayment
from app.models.route import Route
from app.models.user import User
from app.services import cash_service as svc
from app.services.xlsx import build_xlsx

router = APIRouter()
ZERO = Decimal("0.00")
ACTIVE_LOAN = (LoanStatus.active, LoanStatus.late)


def _dec(value) -> Decimal:
    return Decimal(value or 0)


def period_range(period: str, today: date) -> tuple[date, date]:
    """Calendar week (Monday-Sunday), month or year that contains ``today`` (the company's local date)."""
    if period == "week":
        first = today - timedelta(days=today.weekday())
        return first, first + timedelta(days=6)
    if period == "month":
        first = today.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return first, nxt - timedelta(days=1)
    if period == "year":
        return date(today.year, 1, 1), date(today.year, 12, 31)
    if period == "day":
        return today, today
    raise HTTPException(422, "Periodo inválido. Usa week, month, year o day.")


def _loan_scope(company_id: int, branch_id: int | None):
    """Conditions selecting the loans of the company (and of one branch: its disbursement branch, else the branch that
    manages the customer through its own assignment, its route or its collector)."""
    conds = [Customer.company_id == company_id]
    if branch_id is not None:
        conds.append(
            or_(
                Loan.cash_branch_id == branch_id,
                and_(
                    Loan.cash_branch_id.is_(None),
                    or_(
                        Customer.cash_branch_id == branch_id,
                        Customer.route_id.in_(select(Route.id).where(Route.branch_id == branch_id)),
                        Customer.assigned_collector_id.in_(select(User.id).where(User.branch_id == branch_id)),
                    ),
                ),
            )
        )
    return conds


def compute(db: Session, company_id: int, start: date, end: date, branch_id: int | None = None) -> dict:
    if start > end:
        svc.fail("Rango de fechas inválido.")
    low = datetime.combine(start, time.min, svc.TZ).astimezone(UTC)
    high = datetime.combine(end + timedelta(days=1), time.min, svc.TZ).astimezone(UTC)
    scope = _loan_scope(company_id, branch_id)

    # ---- portfolio photo (active / late loans)
    row = db.execute(
        select(
            func.coalesce(func.sum(Loan.principal_balance), 0),
            func.coalesce(func.sum(Loan.interest_balance), 0),
            func.coalesce(func.sum(Loan.late_fee_balance), 0),
            func.count(Loan.id),
            func.count(distinct(Loan.customer_id)),
        )
        .select_from(Loan)
        .join(Customer, Customer.id == Loan.customer_id)
        .where(*scope, Loan.status.in_(ACTIVE_LOAN))
    ).one()
    cartera_capital, cartera_interes, cartera_mora = _dec(row[0]), _dec(row[1]), _dec(row[2])
    prestamos_activos, clientes_activos = int(row[3]), int(row[4])

    # ---- collected in the period. A payment whose cash movement was reversed no longer counts as collected.
    reversal = aliased(CashMovement)
    reversed_payment_ids = (
        select(CashMovement.payment_id)
        .join(reversal, and_(reversal.reverses_id == CashMovement.id, reversal.kind == "reversal"))
        .where(CashMovement.payment_id.is_not(None))
    )
    base_pay = (
        select(
            func.coalesce(func.sum(Payment.amount), 0),
            func.coalesce(func.sum(Payment.principal_applied), 0),
            func.coalesce(func.sum(Payment.interest_applied), 0),
            func.coalesce(func.sum(Payment.late_fee_applied), 0),
            func.count(Payment.id),
        )
        .select_from(Payment)
        .join(Loan, Loan.id == Payment.loan_id)
        .join(Customer, Customer.id == Loan.customer_id)
        .where(*scope, Payment.paid_at >= low, Payment.paid_at < high)
    )
    p = db.execute(base_pay.where(Payment.id.not_in(reversed_payment_ids))).one()
    cobrado, cap_recuperado, interes_cobrado, mora_cobrada, pagos_periodo = (
        _dec(p[0]), _dec(p[1]), _dec(p[2]), _dec(p[3]), int(p[4]),
    )
    pagos_revertidos = int(db.execute(base_pay.where(Payment.id.in_(reversed_payment_ids))).one()[4])

    # ---- disbursed in the period (the loan is created at the moment it is disbursed)
    d = db.execute(
        select(func.coalesce(func.sum(Loan.principal_amount), 0), func.count(Loan.id))
        .select_from(Loan)
        .join(Customer, Customer.id == Loan.customer_id)
        .where(*scope, Loan.created_at >= low, Loan.created_at < high)
    ).one()
    desembolsado, prestamos_nuevos = _dec(d[0]), int(d[1])

    # ---- payroll (an employee belongs to a branch)
    payroll = select(func.coalesce(func.sum(PayrollPayment.amount), 0)).where(
        PayrollPayment.company_id == company_id, PayrollPayment.created_at >= low, PayrollPayment.created_at < high
    )
    if branch_id is not None:
        payroll = payroll.where(PayrollPayment.user_id.in_(select(User.id).where(User.branch_id == branch_id)))
    nomina_pagada = _dec(db.scalar(payroll))

    # ---- cash positions
    enabled = svc.enabled(db, company_id)
    boxes_q = select(CashBox).where(CashBox.company_id == company_id)
    if branch_id is not None:
        boxes_q = boxes_q.where(CashBox.branch_id == branch_id)
    boxes = db.scalars(boxes_q).all()
    box_ids = [b.id for b in boxes] or [0]

    efectivo = efectivo_cerrado = en_cobradores = transferencias = None
    gastos_caja = depositos_banco = retiros_caja = aportes_caja = ZERO
    if enabled:
        # cash held by OPEN (or closing) sessions: the counted figure when the count is already in
        efectivo = _dec(
            db.scalar(
                select(
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    and_(CashSession.state == "closing", CashSession.counted.is_not(None)),
                                    CashSession.counted,
                                ),
                                else_=CashSession.balance,
                            )
                        ),
                        0,
                    )
                ).where(CashSession.box_id.in_(box_ids), CashSession.state.in_(("open", "closing")))
            )
        )
        # cash counted at the LAST close of each cash point that has no active session now (it stays with the cashier
        # until the next opening declares its own initial amount, so it must not vanish from the report)
        last_ids = (
            select(func.max(CashSession.id)).where(CashSession.box_id.in_(box_ids)).group_by(CashSession.cash_point_id)
        )
        efectivo_cerrado = _dec(
            db.scalar(
                select(func.coalesce(func.sum(CashSession.counted), 0)).where(
                    CashSession.id.in_(last_ids), CashSession.state == "closed", CashSession.counted.is_not(None)
                )
            )
        )
        # collected in the field and not delivered yet / transfers waiting for the bank confirmation
        en_cobradores = sum((svc.pending(db, b) for b in boxes), ZERO)
        transferencias = _dec(
            db.scalar(
                select(func.coalesce(func.sum(CashTransfer.amount), 0)).where(
                    CashTransfer.box_id.in_(box_ids), CashTransfer.state == "pending"
                )
            )
        )
        flows = dict(
            db.execute(
                select(CashMovement.kind, func.coalesce(func.sum(CashMovement.amount), 0))
                .where(
                    CashMovement.box_id.in_(box_ids),
                    CashMovement.created_at >= low,
                    CashMovement.created_at < high,
                    CashMovement.kind.in_(("expense", "bank_deposit", "withdrawal", "cash_addition")),
                    # payroll paid from cash is already reported as "nómina"
                    or_(CashMovement.reference.is_(None), ~CashMovement.reference.like("NOM-%")),
                )
                .group_by(CashMovement.kind)
            ).all()
        )
        gastos_caja = abs(_dec(flows.get("expense")))
        depositos_banco = abs(_dec(flows.get("bank_deposit")))
        retiros_caja = abs(_dec(flows.get("withdrawal")))
        aportes_caja = _dec(flows.get("cash_addition"))

    por_cobrar = cartera_capital + cartera_interes + cartera_mora
    ganancia_cobrada = interes_cobrado + mora_cobrada
    ganancia_proyectada = cartera_interes + cartera_mora
    resultado_neto = ganancia_cobrada - gastos_caja - nomina_pagada
    symbol = db.scalar(select(CompanySettings.currency_symbol).where(CompanySettings.company_id == company_id)) or "RD$"
    branch_name = db.scalar(select(Branch.name).where(Branch.id == branch_id)) if branch_id is not None else None

    def s(v: Decimal) -> str:
        return str(v.quantize(Decimal("0.01")))

    def opt(v: Decimal | None) -> str | None:
        return None if v is None else s(v)

    return dict(
        start=str(start), end=str(end),
        branch_id=branch_id, branch_name=branch_name, currency_symbol=symbol,
        caja_enabled=enabled,
        efectivo_caja=opt(efectivo),
        efectivo_cerrado=opt(efectivo_cerrado),
        en_cobradores=opt(en_cobradores),
        transferencias_pendientes=opt(transferencias),
        cartera_capital=s(cartera_capital),
        cartera_interes=s(cartera_interes),
        cartera_mora=s(cartera_mora),
        por_cobrar=s(por_cobrar),
        prestamos_activos=prestamos_activos,
        clientes_activos=clientes_activos,
        cobrado=s(cobrado),
        capital_recuperado=s(cap_recuperado),
        interes_cobrado=s(interes_cobrado),
        mora_cobrada=s(mora_cobrada),
        ganancia_cobrada=s(ganancia_cobrada),
        ganancia_proyectada=s(ganancia_proyectada),
        desembolsado=s(desembolsado),
        prestamos_nuevos=prestamos_nuevos,
        nomina_pagada=s(nomina_pagada),
        gastos_caja=s(gastos_caja),
        depositos_banco=s(depositos_banco),
        retiros_caja=s(retiros_caja),
        aportes_caja=s(aportes_caja),
        resultado_neto=s(resultado_neto),
        pagos_periodo=pagos_periodo,
        pagos_revertidos=pagos_revertidos,
    )


def _rows(d: dict) -> list[list]:
    def cash(key: str):
        return d[key] if d[key] is not None else "Caja no habilitada"

    sym = d["currency_symbol"]
    return [
        ["Reporte financiero"],
        ["Período", f'{d["start"]} a {d["end"]}'],
        ["Sucursal", d["branch_name"] or "Todas"],
        [],
        ["Tu dinero ahora (foto actual)", sym],
        ["Efectivo en jornadas abiertas", cash("efectivo_caja")],
        ["Efectivo contado al último cierre (cajas sin jornada)", cash("efectivo_cerrado")],
        ["Cobrado por cobradores y sin entregar", cash("en_cobradores")],
        ["Transferencias por confirmar", cash("transferencias_pendientes")],
        ["En préstamos (capital en la calle)", d["cartera_capital"]],
        ["Interés por cobrar", d["cartera_interes"]],
        ["Mora por cobrar", d["cartera_mora"]],
        ["Total por cobrar (capital + interés + mora)", d["por_cobrar"]],
        ["Préstamos activos", d["prestamos_activos"]],
        ["Clientes con préstamo activo", d["clientes_activos"]],
        [],
        ["Movimiento del período", sym],
        ["Cobrado (total)", d["cobrado"]],
        ["  · Capital recuperado", d["capital_recuperado"]],
        ["  · Interés cobrado", d["interes_cobrado"]],
        ["  · Mora cobrada", d["mora_cobrada"]],
        ["Ingresos financieros cobrados (interés + mora)", d["ganancia_cobrada"]],
        ["Gastos de caja", d["gastos_caja"]],
        ["Nómina pagada", d["nomina_pagada"]],
        ["Resultado del período (ingresos - gastos - nómina)", d["resultado_neto"]],
        ["Ganancia proyectada (interés + mora pendiente)", d["ganancia_proyectada"]],
        ["Desembolsado (capital colocado)", d["desembolsado"]],
        ["Depósitos al banco", d["depositos_banco"]],
        ["Retiros de caja", d["retiros_caja"]],
        ["Dinero agregado a caja", d["aportes_caja"]],
        ["Préstamos nuevos", d["prestamos_nuevos"]],
        ["Pagos registrados", d["pagos_periodo"]],
        ["Pagos revertidos (no suman a cobrado)", d["pagos_revertidos"]],
    ]


@router.get("/finance")
def finance(
    start: date | None = None,
    end: date | None = None,
    period: str | None = None,
    branch_id: int | None = None,
    format: str = "json",
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
    user: User = Depends(require_admin_manager),
):
    if period:
        # the calendar is always the company's local one (America/Santo_Domingo), never the browser's
        start, end = period_range(period, svc.today())
    if start is None or end is None:
        raise HTTPException(422, "Indica el periodo o el rango de fechas.")
    if user.role == "admin":
        if branch_id is not None and not db.scalar(
            select(Branch.id).where(Branch.id == branch_id, Branch.company_id == company_id)
        ):
            raise HTTPException(404, "Sucursal no encontrada.")
    else:  # a branch manager only sees its own branch
        if user.branch_id is None:
            raise HTTPException(403, "Asigna una sucursal al gerente para ver sus finanzas.")
        branch_id = user.branch_id
    data = compute(db, company_id, start, end, branch_id)
    if format == "json":
        return data
    rows = _rows(data)
    if format == "print":
        html = ('<html><head><meta charset="utf-8"><title>Reporte financiero</title>'
                '<style>@page{size:A4}body{font:13px Arial;color:#17334f;padding:20px}'
                'h1{font-size:20px}table{border-collapse:collapse;width:100%;margin-top:10px}'
                'td{border:1px solid #ccc;padding:7px}tr:first-child td{background:#edf3fa;font-weight:bold}</style>'
                '</head><body><h1>Reporte financiero</h1><table>'
                + ''.join(
                    '<tr>' + ''.join('<td>' + escape(str(c)) + '</td>' for c in (row or [''])) + '</tr>' for row in rows
                )
                + '</table></body></html>')
        return {"html": html}
    if format != "xlsx":
        svc.fail("Formato inválido.")
    payload = build_xlsx("Finanzas", rows)
    xlsx_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return Response(payload, media_type=xlsx_type,
                    headers={"Content-Disposition": 'attachment; filename="reporte-financiero.xlsx"'})
