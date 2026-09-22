from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_company_id, get_db, require_admin, require_admin_manager
from app.models.branch import Branch
from app.models.cash import CashBox
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
    return dict(payments=[dict(id=p.id, user_id=p.user_id, user_name=users.get(p.user_id, "—"),
                               amount=str(p.amount), source=p.source, salary_part=str(p.salary_part),
                               commission_part=str(p.commission_part), period_start=p.period_start,
                               period_end=p.period_end, notes=p.notes, created_at=p.created_at,
                               actor_name=users.get(p.actor_id, "—")) for p in rows])


@router.post("/payments")
def pay(payload: PayrollPaymentCreate, db: Session = Depends(get_db), company_id: int = Depends(get_company_id),
        actor: User = Depends(require_admin)):
    employee = db.get(User, payload.user_id)
    if employee is None or employee.company_id != company_id or employee.role not in EMPLOYEE_ROLES:
        raise HTTPException(404, "Empleado no encontrado.")
    svc.lock_company(db, company_id)
    row = PayrollPayment(company_id=company_id, user_id=employee.id, amount=payload.amount, source=payload.source,
                         period_start=payload.period_start, period_end=payload.period_end,
                         salary_part=payload.salary_part, commission_part=payload.commission_part,
                         notes=payload.notes, actor_id=actor.id)
    note = f"Nómina: {employee.full_name}" + (f" — {payload.notes}" if payload.notes else "")

    if payload.source == "capital":
        cap = capital_service.record(db, company_id, actor.id, "withdrawal", payload.amount, note)
        row.capital_movement_id = cap.id
        db.add(row)
        db.commit()
        return dict(id=row.id, source="capital", balance_capital=str(capital_service.balance(db, company_id)))

    # source == cash: an expense against the branch's open cash session.
    branch_id = payload.branch_id or employee.branch_id
    if not branch_id:
        raise HTTPException(422, "Indica la sucursal de la caja para pagar en efectivo.")
    box = db.scalar(select(CashBox).where(CashBox.company_id == company_id, CashBox.branch_id == branch_id))
    if box is None:
        raise HTTPException(404, "La sucursal no tiene caja configurada.")
    session = svc.active_session(db, box)  # fails if not open
    if session.balance < payload.amount:
        raise HTTPException(409, "El efectivo en caja no alcanza para este pago.")
    movement = svc.add_movement(db, box, session, actor, "expense", -payload.amount, note, reference="NOMINA")
    row.branch_id = branch_id
    row.cash_movement_id = movement.id
    db.add(row)
    db.info.setdefault("cash_notifications", set()).add((company_id, branch_id))
    db.commit()
    for cid, bid in db.info.pop("cash_notifications", set()):
        publish(cid, bid)
    return dict(id=row.id, source="cash", movement_id=movement.id)
