import hashlib
import json
from datetime import date
from decimal import Decimal
from html import escape as html_escape
from typing import Literal

import base64

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_company_id, get_db, require_admin, require_admin_manager
from app.models.branch import Branch
from app.models.bank_account import BankAccount, BankAccountMovement
from app.models.cash import CashBox, CashSession
from app.models.payroll import PayrollConfig, PayrollPayment
from app.models.user import User
from app.schemas.payroll import PayrollConfigUpdate, PayrollPaymentCreate
from app.services import cash_service as svc, payroll_service
from app.services.cash_live import publish
from app.services.xlsx import build_xlsx

router = APIRouter()
EMPLOYEE_ROLES = ("admin", "manager", "cashier", "collector")


def _own_branch(actor: User) -> int | None:
    """None = the whole company (administrator). A branch manager only sees the staff and the payments of its own
    branch: salaries of other branches (and of the administration) are not theirs to read."""
    if str(getattr(actor.role, "value", actor.role)) == "admin":
        return None
    if actor.branch_id is None:
        raise HTTPException(403, "Asigna una sucursal al gerente para ver la nómina.")
    return actor.branch_id


def _config_for(db, company_id, user_id):
    return db.scalar(select(PayrollConfig).where(PayrollConfig.company_id == company_id, PayrollConfig.user_id == user_id))


def _config_dict(c: PayrollConfig | None):
    if c is None:
        return dict(salary_amount="0.00", salary_frequency="monthly", commissions=[])
    return dict(salary_amount=str(c.salary_amount), salary_frequency=c.salary_frequency, commissions=c.commissions or [])


@router.get("")
def list_payroll(db: Session = Depends(get_db), company_id: int = Depends(get_company_id),
                 actor: User = Depends(require_admin_manager)):
    branch = _own_branch(actor)
    query = select(User).where(User.company_id == company_id, User.role.in_(EMPLOYEE_ROLES), User.is_active == True)
    if branch is not None:
        query = query.where(User.branch_id == branch)
    users = db.scalars(query.order_by(User.full_name)).all()
    configs = {c.user_id: c for c in db.scalars(select(PayrollConfig).where(PayrollConfig.company_id == company_id)).all()}
    # a "Notario" is a legacy collector that never collects: label it by its real role
    return dict(employees=[dict(user_id=u.id, name=u.full_name,
                                role="notary" if "Notario" in u.branch_roles else u.role, email=u.email,
                                config=_config_dict(configs.get(u.id))) for u in users])


@router.put("/config/{user_id}")
def set_config(user_id: int, payload: PayrollConfigUpdate, db: Session = Depends(get_db),
               company_id: int = Depends(get_company_id), _=Depends(require_admin)):
    user = db.get(User, user_id)
    if user is None or user.company_id != company_id or user.role not in EMPLOYEE_ROLES:
        raise HTTPException(404, "Empleado no encontrado.")
    c = _config_for(db, company_id, user_id)
    if c is None:
        c = PayrollConfig(company_id=company_id, user_id=user_id)
        db.add(c)
    c.salary_amount = payload.salary_amount
    c.salary_frequency = payload.salary_frequency
    c.commissions = [r.model_dump(mode="json") for r in payload.commissions]
    db.commit()
    return dict(ok=True)


@router.get("/suggest")
def suggest(user_id: int, start: date, end: date, db: Session = Depends(get_db),
            company_id: int = Depends(get_company_id), actor: User = Depends(require_admin_manager)):
    if start > end:
        raise HTTPException(422, "Rango de fechas inválido.")
    branch = _own_branch(actor)
    user = db.get(User, user_id)
    if user is None or user.company_id != company_id or (branch is not None and user.branch_id != branch):
        raise HTTPException(404, "Empleado no encontrado.")
    c = _config_for(db, company_id, user_id) or PayrollConfig(company_id=company_id, user_id=user_id,
                                                              salary_amount=Decimal("0"), commissions=[])
    return payroll_service.compute(db, c, start, end)


MAX_EXPORT_ROWS = 10_000
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SOURCE_LABEL = {"cash": "Caja", "bank": "Banco", "capital": "Capital (histórico)"}


def _payment_dict(p: PayrollPayment, users: dict, banks: dict, reversed_ids: set) -> dict:
    return dict(id=p.id, user_id=p.user_id, user_name=users.get(p.user_id, "—"),
                amount=str(p.amount), source=p.source, salary_part=str(p.salary_part),
                commission_part=str(p.commission_part), period_start=p.period_start,
                period_end=p.period_end, notes=p.notes, created_at=p.created_at,
                actor_name=users.get(p.actor_id, "—"), reference=p.reference or "",
                has_proof=bool(p.proof), bank_account_id=p.bank_account_id,
                bank_label=banks.get(p.id), cash_movement_id=p.cash_movement_id,
                historical_capital=p.source == "capital", reversed=p.id in reversed_ids)


@router.get("/payments")
def list_payments(start: date | None = None, end: date | None = None, user_id: int | None = None,
                  q: str | None = Query(default=None, max_length=80, description="Empleado, referencia o nota"),
                  page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                  format: Literal["json", "xlsx", "print"] = "json",
                  db: Session = Depends(get_db), company_id: int = Depends(get_company_id),
                  actor: User = Depends(require_admin_manager)):
    """Payment history, newest first. Filter by date range (company calendar day of the payment) and employee; paged
    (``page``/``page_size``) as JSON, or the WHOLE filtered list (up to 10,000 rows) as ``xlsx`` / ``print``."""
    if start and end and start > end:
        raise HTTPException(422, "Rango de fechas inválido.")
    where = [PayrollPayment.company_id == company_id]
    branch = _own_branch(actor)
    if branch is not None:
        where.append(PayrollPayment.user_id.in_(
            select(User.id).where(User.company_id == company_id, User.branch_id == branch)))
    if start:
        where.append(PayrollPayment.created_at >= payroll_service._window(start, start)[0])
    if end:
        where.append(PayrollPayment.created_at < payroll_service._window(end, end)[1])
    if user_id:
        where.append(PayrollPayment.user_id == user_id)
    if q and q.strip():
        term = f"%{q.strip()}%"
        by_name = select(User.id).where(User.company_id == company_id, User.full_name.ilike(term))
        where.append(or_(PayrollPayment.user_id.in_(by_name), PayrollPayment.reference.ilike(term),
                         PayrollPayment.notes.ilike(term)))
    reversed_sub = payroll_service.reversed_payroll_ids()
    total_rows = db.scalar(select(func.count()).select_from(PayrollPayment).where(*where)) or 0
    total_amount = db.scalar(select(func.coalesce(func.sum(PayrollPayment.amount), 0))
                             .where(*where, PayrollPayment.id.not_in(reversed_sub))) or 0
    query = select(PayrollPayment).where(*where).order_by(PayrollPayment.id.desc())
    if format == "json":
        query = query.limit(page_size).offset((page - 1) * page_size)
    else:
        query = query.limit(MAX_EXPORT_ROWS)
    rows = db.scalars(query).all()
    users = {u.id: u.full_name for u in db.scalars(select(User).where(User.company_id == company_id)).all()}
    banks = {m.payroll_payment_id: m.account_label for m in db.scalars(
        select(BankAccountMovement).where(BankAccountMovement.company_id == company_id)).all()}
    reversed_ids = set(db.scalars(select(reversed_sub.subquery().c.id)).all())
    items = [_payment_dict(p, users, banks, reversed_ids) for p in rows]
    if format == "json":
        return dict(payments=items, total=total_rows, page=page, page_size=page_size,
                    pages=max(1, -(-total_rows // page_size)), total_amount=str(total_amount))

    def _row(i: dict) -> list:
        return [i["created_at"].astimezone(svc.TZ).strftime("%d/%m/%Y"), i["user_name"], str(i["amount"]),
                SOURCE_LABEL.get(i["source"], i["source"]), i["bank_label"] or "", i["reference"],
                f"{i['period_start']} a {i['period_end']}" if i["period_start"] and i["period_end"] else "",
                "Revertido" if i["reversed"] else "Vigente", i["actor_name"], i["notes"]]

    head = ["Fecha", "Empleado", "Importe (RD$)", "Origen", "Cuenta", "Referencia", "Período", "Estado", "Registró", "Nota"]
    rango = f"{start or 'inicio'} a {end or 'hoy'}"
    table = [head] + [_row(i) for i in items]
    if format == "xlsx":
        table += [[], ["Total vigente (sin revertidos)", "", str(Decimal(total_amount).quantize(Decimal('0.01')))]]
        return Response(build_xlsx("Nómina", table), media_type=XLSX_TYPE,
                        headers={"Content-Disposition": 'attachment; filename="historial-nomina.xlsx"'})
    cell = lambda c: "<td>" + html_escape(str(c)) + "</td>"  # noqa: E731
    body = "".join("<tr>" + "".join(cell(c) for c in r) + "</tr>" for r in table[1:])
    html = ('<html><head><meta charset="utf-8"><title>Historial de nómina</title>'
            '<style>@page{size:A4 landscape}body{font:12px Arial;color:#17334f;padding:20px}h1{font-size:19px}'
            'table{border-collapse:collapse;width:100%;margin-top:10px}td,th{border:1px solid #ccc;padding:6px;'
            'text-align:left}th{background:#edf3fa}</style></head><body>'
            f"<h1>Historial de nómina</h1><p>Periodo: {html_escape(rango)} · Pagos: {total_rows} · "
            f"Total vigente: RD$ {Decimal(total_amount).quantize(Decimal('0.01'))}</p><table><tr>"
            + "".join(f"<th>{html_escape(h)}</th>" for h in head) + "</tr>" + body + "</table></body></html>")
    return {"html": html}


def _digest(payload: PayrollPaymentCreate, employee_branch_id) -> str:
    """Content of the request WITHOUT its idempotency key (the proof counts by its own hash)."""
    body = payload.model_dump(mode="json", exclude={"idempotency_key", "proof"})
    body["proof"] = hashlib.sha256((payload.proof.content_base64 if payload.proof else "").encode()).hexdigest()
    return hashlib.sha256(json.dumps(jsonable_encoder(body), sort_keys=True).encode()).hexdigest()


def _replayed(db, row: PayrollPayment) -> dict:
    out = dict(id=row.id, source=row.source, replayed=True)
    if row.source == "cash":
        out["movement_id"] = row.cash_movement_id
    if row.source == "bank":
        out["bank_account_id"] = row.bank_account_id
    return out


@router.post("/payments")
def pay(payload: PayrollPaymentCreate, db: Session = Depends(get_db), company_id: int = Depends(get_company_id),
        actor: User = Depends(require_admin)):
    """FM-FINANCIAL-CLOSURE-012: a payroll payment leaves from an OPEN cash session (one cash expense) or from a company
    bank account (one bank ledger outflow). New payments never use Capital. Retrying the same ``idempotency_key`` with
    the same content answers the original payment; other content under the key is a conflict."""
    if payload.source == "capital":
        raise HTTPException(422, "Los pagos de nómina ya no salen de Capital: elige Caja o una cuenta bancaria.")
    if not payload.idempotency_key:
        raise HTTPException(422, "Falta la clave de operación del pago (idempotency_key).")
    employee = db.get(User, payload.user_id)
    if employee is None or employee.company_id != company_id or employee.role not in EMPLOYEE_ROLES:
        raise HTTPException(404, "Empleado no encontrado.")
    digest = _digest(payload, employee.branch_id)

    def existing():
        return db.scalar(select(PayrollPayment).where(PayrollPayment.company_id == company_id,
                                                      PayrollPayment.idempotency_key == payload.idempotency_key))

    def answer_existing(row):
        if row.request_digest != digest or row.actor_id != actor.id:
            raise HTTPException(409, "La clave de operación pertenece a otro pago.")
        return _replayed(db, row)

    prior = existing()
    if prior is not None:
        return answer_existing(prior)
    svc.lock_company(db, company_id)
    prior = existing()
    if prior is not None:
        return answer_existing(prior)
    if payload.period_start and payload.period_end and not payload.allow_duplicate_period:
        overlap = db.scalar(select(PayrollPayment.id).where(
            PayrollPayment.company_id == company_id, PayrollPayment.user_id == employee.id,
            PayrollPayment.period_start <= payload.period_end, PayrollPayment.period_end >= payload.period_start,
            PayrollPayment.id.not_in(payroll_service.reversed_payroll_ids()),
        ).limit(1))
        if overlap is not None:
            raise HTTPException(409, "Ya existe un pago de nómina para este empleado en ese periodo. "
                                     "Confirma si de verdad quieres pagarlo otra vez.")

    note = f"Nómina: {employee.full_name}" + (f" — {payload.notes}" if payload.notes else "")
    row = PayrollPayment(company_id=company_id, user_id=employee.id, amount=payload.amount, source=payload.source,
                         period_start=payload.period_start, period_end=payload.period_end,
                         salary_part=payload.salary_part, commission_part=payload.commission_part,
                         notes=payload.notes, actor_id=actor.id, reference=payload.reference.strip(),
                         proof=payload.proof.model_dump() if payload.proof else None,
                         idempotency_key=payload.idempotency_key, request_digest=digest)

    if payload.source == "bank":
        account = db.get(BankAccount, payload.bank_account_id) if payload.bank_account_id else None
        if account is None or account.company_id != company_id:
            raise HTTPException(404, "Cuenta bancaria no encontrada.")
        if not account.is_active:
            raise HTTPException(409, "La cuenta bancaria está inactiva.")
        if not payload.reference.strip():
            raise HTTPException(422, "Indica el número de operación o referencia del pago bancario.")
        row.bank_account_id = account.id
        db.add(row)
        try:
            db.flush()
            db.add(BankAccountMovement(company_id=company_id, bank_account_id=account.id, kind="payroll_payment",
                                       amount=-payload.amount, reference=row.reference, notes=note,
                                       account_label=account.label, payroll_payment_id=row.id, actor_id=actor.id))
            db.commit()
        except IntegrityError:
            db.rollback()
            prior = existing()
            if prior is None:
                raise
            return answer_existing(prior)
        return dict(id=row.id, source="bank", bank_account_id=account.id, account=account.label)

    # source == cash: exactly one expense against an OPEN session of the chosen branch (admin picks it explicitly when
    # several cashiers have one open, so another cashier's money is never moved by guess).
    branch_id = payload.branch_id or employee.branch_id
    if not branch_id:
        raise HTTPException(422, "Indica la sucursal de la caja para pagar en efectivo.")
    box = svc.scope(db, actor, branch_id, lock=True)
    sessions = db.scalars(select(CashSession).where(CashSession.box_id == box.id, CashSession.state == "open")
                          .order_by(CashSession.id.desc())).all()
    if payload.session_id is not None:
        session = next((s for s in sessions if s.id == payload.session_id), None)
        if session is None:
            raise HTTPException(409, "La jornada seleccionada no está abierta en esa sucursal.")
    elif len(sessions) == 1:
        session = sessions[0]
    elif not sessions:
        raise HTTPException(409, "La sucursal no tiene una caja abierta: abre la caja antes de pagar en efectivo.")
    else:
        raise HTTPException(409, "Hay varias jornadas abiertas. Selecciona la jornada de la que sale el efectivo.")
    session = db.scalar(select(CashSession).where(CashSession.id == session.id).with_for_update())
    if session.state != "open":
        raise HTTPException(409, "La jornada ya no está abierta.")
    if session.balance < payload.amount:
        raise HTTPException(409, "El efectivo en caja no alcanza para este pago.")
    db.add(row)
    try:
        db.flush()
        movement = svc.add_movement(db, box, session, actor, "expense", -payload.amount, note, reference=f"NOM-{row.id}")
        row.branch_id = branch_id
        row.cash_movement_id = movement.id
        svc.audit(db, box, actor, "payroll_payment", payroll_payment_id=row.id, movement_id=movement.id,
                  employee_id=employee.id, amount=str(payload.amount), session_id=session.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        prior = existing()
        if prior is None:
            raise
        return answer_existing(prior)
    for cid, bid in db.info.pop("cash_notifications", set()):
        publish(cid, bid)
    return dict(id=row.id, source="cash", movement_id=movement.id, session_id=session.id)


@router.get("/payments/{payment_id}/proof")
def payment_proof(payment_id: int, db: Session = Depends(get_db), company_id: int = Depends(get_company_id),
                  actor: User = Depends(require_admin_manager)):
    row = db.get(PayrollPayment, payment_id)
    if row is None or row.company_id != company_id or not row.proof:
        raise HTTPException(404, "Comprobante no encontrado.")
    branch = _own_branch(actor)
    if branch is not None:
        employee = db.get(User, row.user_id)
        if employee is None or employee.branch_id != branch:
            raise HTTPException(404, "Comprobante no encontrado.")
    return Response(base64.b64decode(row.proof["content_base64"]), media_type=row.proof["media_type"],
                    headers={"Content-Disposition": 'attachment; filename="comprobante-nomina"',
                             "X-Content-Type-Options": "nosniff"})
