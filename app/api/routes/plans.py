from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.plan import Plan
from app.models.user import User
from app.schemas.plan import PlanCreate, PlanRead

router = APIRouter()


def _seed_defaults(db: Session) -> None:
    existing = db.scalars(select(Plan)).all()
    if existing:
        return

    defaults = [
        Plan(name="Gratis", customer_limit=5, loan_limit=5, user_limit=1, monthly_price_usd=Decimal("0.00")),
        Plan(name="Basico", customer_limit=100, loan_limit=100, user_limit=3, monthly_price_usd=Decimal("17.99")),
        Plan(name="Estandar", customer_limit=200, loan_limit=200, user_limit=5, monthly_price_usd=Decimal("29.99")),
        Plan(name="Pro", customer_limit=500, loan_limit=500, user_limit=10, monthly_price_usd=Decimal("49.99")),
        Plan(name="Empresarial", customer_limit=0, loan_limit=0, user_limit=0, monthly_price_usd=Decimal("79.99")),
    ]
    db.add_all(defaults)
    db.commit()


@router.get("", response_model=list[PlanRead])
def list_plans(
    q: str | None = Query(default=None),
    active_only: bool = Query(default=False),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[Plan]:
    _seed_defaults(db)
    plans = db.scalars(select(Plan).order_by(Plan.monthly_price_usd, Plan.name)).all()
    if active_only:
        plans = [plan for plan in plans if plan.is_active]
    if q:
        term = q.lower().strip()
        plans = [plan for plan in plans if term in plan.name.lower()]
    return plans


@router.post("", response_model=PlanRead, status_code=status.HTTP_201_CREATED)
def create_plan(
    payload: PlanCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Plan:
    existing = db.scalar(select(Plan).where(Plan.name == payload.name.strip()))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Ya existe un plan con ese nombre.")

    plan = Plan(**payload.model_dump(), name=payload.name.strip())
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


@router.put("/{plan_id}", response_model=PlanRead)
def update_plan(
    plan_id: int,
    payload: PlanCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Plan:
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan no encontrado.")

    existing = db.scalar(select(Plan).where(Plan.name == payload.name.strip(), Plan.id != plan_id))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Ya existe un plan con ese nombre.")

    for key, value in payload.model_dump().items():
        setattr(plan, key, value)
    plan.name = payload.name.strip()
    db.commit()
    db.refresh(plan)
    return plan
