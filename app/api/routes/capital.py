import hashlib
import json
from decimal import Decimal

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_company_id, get_db, require_admin, require_admin_manager
from app.models.capital import CapitalMovement
from app.models.cash import CashRequest
from app.models.user import User
from app.schemas.capital import CapitalMovementCreate
from app.services import capital_service, cash_service

router = APIRouter()

LABELS = {
    "injection": "Inyección de capital",
    "withdrawal": "Retiro de capital",
    "to_cash": "Enviado a caja",
    "from_cash": "Recibido de caja",
}


@router.get("")
def get_capital(
    db: Session = Depends(get_db), company_id: int = Depends(get_company_id), _=Depends(require_admin_manager)
):
    users = {u.id: u.full_name for u in db.scalars(select(User).where(User.company_id == company_id)).all()}
    rows = db.scalars(
        select(CapitalMovement).where(CapitalMovement.company_id == company_id).order_by(CapitalMovement.id.desc())
    ).all()
    return dict(
        balance=str(capital_service.balance(db, company_id).quantize(Decimal("0.01"))),
        movements=[
            dict(
                id=m.id,
                kind=m.kind,
                label=LABELS.get(m.kind, m.kind),
                amount=str(m.amount),
                notes=m.notes,
                created_at=m.created_at,
                actor_name=users.get(m.actor_id, "—"),
                linked_to_cash=m.cash_movement_id is not None,
            )
            for m in rows
        ],
    )


def _digest(payload: CapitalMovementCreate) -> str:
    canonical = {
        "operation": "capital.movement",
        "kind": payload.kind,
        "amount": str(payload.amount.quantize(Decimal("0.01"))),
        "notes": payload.notes,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def _replayed(db: Session, company_id: int, actor_id: int, scoped_key: str, digest: str, response: Response):
    old = db.scalar(
        select(CashRequest).where(
            CashRequest.company_id == company_id, CashRequest.actor_id == actor_id, CashRequest.key == scoped_key
        )
    )
    if old is None:
        return None
    if old.digest != digest:
        cash_service.fail("La clave de operación pertenece a otro contenido.", 409)
    response.headers["Idempotent-Replayed"] = "true"
    return old.result


@router.post("/movements")
def create_movement(
    payload: CapitalMovementCreate,
    response: Response,
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
    user: User = Depends(require_admin),
):
    # The company lock serialises concurrent writers of the same tenant: the replay lookup below runs under it, so two
    # simultaneous requests with one key create at most one movement. The unique (company, actor, key) constraint is the
    # backstop; the request row is written in the SAME transaction as the movement (atomic, rolled back together).
    cash_service.lock_company(db, company_id)
    scoped = f"capital:{payload.idempotency_key}" if payload.idempotency_key else None
    digest = _digest(payload)
    if scoped:
        old = _replayed(db, company_id, user.id, scoped, digest, response)
        if old is not None:
            return old
    row = capital_service.record(db, company_id, user.id, payload.kind, payload.amount, payload.notes)
    result = dict(id=row.id, balance=str(capital_service.balance(db, company_id).quantize(Decimal("0.01"))))
    if scoped:
        db.add(CashRequest(company_id=company_id, actor_id=user.id, key=scoped, digest=digest, result=result))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if not scoped:
            raise
        old = _replayed(db, company_id, user.id, scoped, digest, response)
        if old is None:
            raise
        return old
    return result
