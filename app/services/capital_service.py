"""Capital reserve = money the owner has put in the business that is neither in a
cash box nor lent out. Balance is the running sum of the ledger."""
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.capital import CapitalMovement

ZERO = Decimal("0.00")
ADD = {"injection", "from_cash"}       # increase the reserve
SUBTRACT = {"withdrawal", "to_cash"}   # decrease the reserve
KINDS = ADD | SUBTRACT


def balance(db, company_id) -> Decimal:
    plus = db.scalar(select(func.coalesce(func.sum(CapitalMovement.amount), 0)).where(
        CapitalMovement.company_id == company_id, CapitalMovement.kind.in_(list(ADD)))) or ZERO
    minus = db.scalar(select(func.coalesce(func.sum(CapitalMovement.amount), 0)).where(
        CapitalMovement.company_id == company_id, CapitalMovement.kind.in_(list(SUBTRACT)))) or ZERO
    return Decimal(plus) - Decimal(minus)


def record(db, company_id, actor_id, kind, amount, notes="", cash_movement_id=None) -> CapitalMovement:
    if kind not in KINDS:
        raise HTTPException(422, "Tipo de movimiento de capital inválido.")
    amount = Decimal(amount)
    if amount <= 0:
        raise HTTPException(422, "El importe debe ser mayor que cero.")
    if kind in SUBTRACT and amount > balance(db, company_id):
        raise HTTPException(409, "El capital disponible no alcanza para esta operación.")
    row = CapitalMovement(company_id=company_id, kind=kind, amount=amount, notes=notes or "",
                          actor_id=actor_id, cash_movement_id=cash_movement_id)
    db.add(row)
    db.flush()
    return row
