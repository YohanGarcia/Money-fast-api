from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_company_id, get_db, require_admin, require_admin_manager
from app.models.capital import CapitalMovement
from app.models.user import User
from app.schemas.capital import CapitalMovementCreate
from app.services import capital_service, cash_service

router = APIRouter()

LABELS = {"injection": "Inyección de capital", "withdrawal": "Retiro de capital",
          "to_cash": "Enviado a caja", "from_cash": "Recibido de caja"}


@router.get("")
def get_capital(db: Session = Depends(get_db), company_id: int = Depends(get_company_id),
                _=Depends(require_admin_manager)):
    users = {u.id: u.full_name for u in db.scalars(select(User).where(User.company_id == company_id)).all()}
    rows = db.scalars(select(CapitalMovement).where(CapitalMovement.company_id == company_id)
                      .order_by(CapitalMovement.id.desc())).all()
    return dict(
        balance=str(capital_service.balance(db, company_id).quantize(Decimal("0.01"))),
        movements=[dict(id=m.id, kind=m.kind, label=LABELS.get(m.kind, m.kind), amount=str(m.amount),
                        notes=m.notes, created_at=m.created_at, actor_name=users.get(m.actor_id, "—"),
                        linked_to_cash=m.cash_movement_id is not None) for m in rows],
    )


@router.post("/movements")
def create_movement(payload: CapitalMovementCreate, db: Session = Depends(get_db),
                    company_id: int = Depends(get_company_id), user: User = Depends(require_admin)):
    cash_service.lock_company(db, company_id)
    row = capital_service.record(db, company_id, user.id, payload.kind, payload.amount, payload.notes)
    db.commit()
    return dict(id=row.id, balance=str(capital_service.balance(db, company_id).quantize(Decimal("0.01"))))
