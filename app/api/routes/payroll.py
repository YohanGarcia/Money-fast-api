import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import base64

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_company_id, get_current_user, get_db, require_admin, require_admin_manager
from app.models.branch import Branch
from app.models.bank_account import BankAccount, BankAccountMovement
from app.models.cash import CashBox, CashSession
from app.models.payroll import PayrollConfig, PayrollPayment
from app.models.user import User
from app.schemas.payroll import PayrollConfigUpdate, PayrollPaymentCreate
from app.services import capital_service, cash_service as svc, payroll_service
from app.services.cash_live import publish

router = APIRouter()
EMPLOYEE_ROLES = ("admin", "manager", "cashier", "collector")


def _config_for(db, company_id, user_id):
    return db.scalar(select(PayrollConfig).where(PayrollConfig.company_id == company_id, PayrollConfig.user_id == user_id))


def _config_dict(c: PayrollConfig | None):
    if c is None:
        return dict(salary_amount="0.00", salary_frequency="monthly", commissions=[])
    return dict(salary_amount=str(c.salary_amount), salary_frequency=c.salary_frequency, commissions=c.commissions or [])


@router.get("")
def list_payroll(db: Session = Depends(get_db), company_id: int = Depends(get_company_id),
                 _=Depends(require_admin_manager)):
    users = db.scalars(select(User).where(User.company_id == company_id, User.role.in_(EMPLOYEE_ROLES),
                                          User.is_active == True).order_by(User.full_name)).all()
    configs = {c.user_id: c for c in db.scalars(select(PayrollConfig).where(PayrollConfig.company_id == company_id)).all()}
    return dict(employees=[dict(user_id=u.id, name=u.full_name, role=u.role, email=u.email,
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
            company_id: int = Depends(get_company_id), _=Depends(require_admin_manager)):
    if start > end:
        raise HTTPException(422, "Rango de fechas inválido.")
    user = db.get(User, user_id)
    if user is None or user.company_id != company_id:
        raise HTTPException(404, "Empleado no encontrado.")
    c = _config_for(db, company_id, user_id) or PayrollConfig(company_id=company_id, user_id=user_id,
                                                              salary_amount=Decimal("0"), commissions=[])
    return payroll_service.compute(db, c, start, end)


@router.get("/payments")
def list_payments(db: Session = Depends(get_db), company_id: int = Depends(get_company_id),
                  _=Depends(require_admin_manager)):
    users = {u.id: u.full_name for u in db.scalars(select(User).where(User.company_id == company_id)).all()}
    rows = db.scalars(select(PayrollPayment).where(PayrollPayment.company_id == company_id)
                      .order_by(PayrollPayment.id.desc())).all()
    banks = {m.payroll_payment_id: m.account_label for m in db.scalars(
        select(BankAccountMovement).where(BankAccountMovement.company_id == company_id)).all()}
    return dict(payments=[dict(id=p.id, user_id=p.user_id, user_name=users.get(p.user_id, "—"),
                               amount=str(p.amount), source=p.source, salary_part=str(p.salary_part),
                               commission_part=str(p.commission_part), period_start=p.period_start,
                               period_end=p.period_end, notes=p.notes, created_at=p.created_at,
                               actor_name=users.get(p.actor_id, "—"), reference=p.reference or "",
                               has_proof=bool(p.proof), bank_account_id=p.bank_account_id,
                               bank_label=banks.get(p.id), cash_movement_id=p.cash_movement_id,
                               historical_capital=p.source == "capital", status=p.status, branch_id=p.branch_id,
                               executed_at=p.executed_at) for p in rows])


def _digest(payload: PayrollPaymentCreate, employee_branch_id) -> str:
    """Content of the request WITHOUT its idempotency key (the proof counts by its own hash)."""
    body = payload.model_dump(mode="json", exclude={"idempotency_key", "proof"})
    body["proof"] = hashlib.sha256((payload.proof.content_base64 if payload.proof else "").encode()).hexdigest()
    return hashlib.sha256(json.dumps(jsonable_encoder(body), sort_keys=True).encode()).hexdigest()


def _replayed(db, row: PayrollPayment) -> dict:
    out = dict(id=row.id, source=row.source, replayed=True)
    if row.source == "cash":
        out["movement_id"] = row.cash_movement_id
        out["status"] = row.status
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

    # source == cash (FM-FUNCTIONAL-018-R1): the administrator only AUTHORIZES. No session is debited here: the cashier
    # of the branch executes the payment out of ITS OWN open session (``POST /payments/{id}/execute``).
    if payload.session_id is not None:
        raise HTTPException(422, "La jornada la elige el cajero al ejecutar el pago: no se indica al autorizarlo.")
    branch_id = payload.branch_id or employee.branch_id
    if not branch_id:
        raise HTTPException(422, "Indica la sucursal de la caja para pagar en efectivo.")
    box = svc.scope(db, actor, branch_id, lock=True)
    row.status = "authorized"
    row.branch_id = branch_id
    db.add(row)
    try:
        db.flush()
        svc.audit(db, box, actor, "payroll_payment_authorized", payroll_payment_id=row.id, employee_id=employee.id,
                  amount=str(payload.amount), branch_id=branch_id)
        db.commit()
    except IntegrityError:
        db.rollback()
        prior = existing()
        if prior is None:
            raise
        return answer_existing(prior)
    for cid, bid in db.info.pop("cash_notifications", set()):
        publish(cid, bid)
    return dict(id=row.id, source="cash", status="authorized", branch_id=branch_id)


def _authorized_row(db, company_id: int, payment_id: int) -> PayrollPayment:
    row = db.scalar(select(PayrollPayment).where(PayrollPayment.id == payment_id,
                                                 PayrollPayment.company_id == company_id).with_for_update())
    if row is None or row.source != "cash":
        raise HTTPException(404, "Pago de nómina no encontrado.")
    return row


@router.get("/authorizations")
def authorizations(branch_id: int | None = None, db: Session = Depends(get_db),
                   company_id: int = Depends(get_company_id), user: User = Depends(get_current_user)):
    """Cash payroll payments authorized and waiting for the cashier (admin sees them all, a cashier its branch)."""
    svc.require_role(user, "admin", "manager", "cashier")
    if user.role == "cashier":
        branch_id = user.branch_id
    q = select(PayrollPayment).where(PayrollPayment.company_id == company_id, PayrollPayment.status == "authorized")
    if branch_id is not None:
        q = q.where(PayrollPayment.branch_id == branch_id)
    users = {u.id: u.full_name for u in db.scalars(select(User).where(User.company_id == company_id)).all()}
    return dict(authorizations=[dict(id=p.id, user_id=p.user_id, user_name=users.get(p.user_id, "—"),
                                     amount=str(p.amount), branch_id=p.branch_id, notes=p.notes,
                                     authorized_by=users.get(p.actor_id, "—"), created_at=p.created_at)
                                for p in db.scalars(q.order_by(PayrollPayment.id)).all()])


@router.post("/payments/{payment_id}/execute")
def execute(payment_id: int, db: Session = Depends(get_db), company_id: int = Depends(get_company_id),
            actor: User = Depends(get_current_user)):
    """The cashier of the branch pays an AUTHORIZED payroll payment out of its own open session: one cash expense,
    balance checked, audited. Replaying it answers the same movement; nobody else can execute or debit the session."""
    svc.require_role(actor, "cashier")
    svc.lock_company(db, company_id)
    row = _authorized_row(db, company_id, payment_id)
    if row.status == "paid" and row.executed_by == actor.id:
        return dict(id=row.id, source="cash", status="paid", movement_id=row.cash_movement_id, replayed=True)
    if row.status != "authorized":
        raise HTTPException(409, "El pago ya fue ejecutado o cancelado.")
    box = svc.scope(db, actor, row.branch_id, lock=True)
    if actor.branch_id != row.branch_id:
        raise HTTPException(403, "Este pago pertenece a la caja de otra sucursal.")
    session = svc.operational_session(db, box, actor)  # ONLY the cashier's own open session
    session = db.scalar(select(CashSession).where(CashSession.id == session.id).with_for_update())
    if session.state != "open" or session.cashier_id != actor.id:
        raise HTTPException(409, "Debes tener tu jornada abierta para ejecutar el pago.")
    if session.balance < row.amount:
        raise HTTPException(409, "El efectivo en caja no alcanza para este pago.")
    employee = db.get(User, row.user_id)
    note = f"Nómina: {employee.full_name}" + (f" — {row.notes}" if row.notes else "")
    movement = svc.add_movement(db, box, session, actor, "expense", -row.amount, note, reference=f"NOM-{row.id}")
    row.status = "paid"
    row.cash_movement_id = movement.id
    row.executed_by = actor.id
    row.executed_at = datetime.now(UTC)
    svc.audit(db, box, actor, "payroll_payment", payroll_payment_id=row.id, movement_id=movement.id,
              employee_id=row.user_id, amount=str(row.amount), session_id=session.id, authorized_by=row.actor_id)
    db.commit()
    for cid, bid in db.info.pop("cash_notifications", set()):
        publish(cid, bid)
    return dict(id=row.id, source="cash", status="paid", movement_id=movement.id, session_id=session.id)


@router.post("/payments/{payment_id}/cancel")
def cancel(payment_id: int, db: Session = Depends(get_db), company_id: int = Depends(get_company_id),
           actor: User = Depends(require_admin)):
    row = _authorized_row(db, company_id, payment_id)
    if row.status == "cancelled":
        return dict(id=row.id, status="cancelled", replayed=True)
    if row.status != "authorized":
        raise HTTPException(409, "Solo se puede cancelar un pago autorizado que aún no se ejecutó.")
    row.status = "cancelled"
    row.cancelled_by = actor.id
    row.cancelled_at = datetime.now(UTC)
    db.commit()
    return dict(id=row.id, status="cancelled")


@router.get("/payments/{payment_id}/proof")
def payment_proof(payment_id: int, db: Session = Depends(get_db), company_id: int = Depends(get_company_id),
                  _=Depends(require_admin_manager)):
    row = db.get(PayrollPayment, payment_id)
    if row is None or row.company_id != company_id or not row.proof:
        raise HTTPException(404, "Comprobante no encontrado.")
    return Response(base64.b64decode(row.proof["content_base64"]), media_type=row.proof["media_type"],
                    headers={"Content-Disposition": 'attachment; filename="comprobante-nomina"',
                             "X-Content-Type-Options": "nosniff"})
