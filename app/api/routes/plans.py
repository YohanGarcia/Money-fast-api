from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_superadmin
from app.models.company import Company
from app.models.plan import Plan
from app.models.user import User
from app.schemas.plan import PlanCreate, PlanRead
from app.services.plan_limits import seed_default_plans

router = APIRouter()


def _seed_defaults(db: Session) -> None:
    seed_default_plans(db)


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
    _: User = Depends(require_superadmin),
) -> Plan:
    existing = db.scalar(select(Plan).where(Plan.name == payload.name.strip()))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Ya existe un plan con ese nombre.")

    plan = Plan(**payload.model_dump())
    plan.name = payload.name.strip()
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


@router.put("/{plan_id}", response_model=PlanRead)
def update_plan(
    plan_id: int,
    payload: PlanCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
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


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
) -> None:
    """Delete a plan only if no company is using it (otherwise deactivate it)."""
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan no encontrado.")

    in_use = db.scalar(
        select(func.count()).select_from(Company).where(Company.plan_id == plan_id)
    ) or 0
    if in_use > 0:
        raise HTTPException(
            status_code=400,
            detail="El plan está en uso por una o más empresas. Desactívalo en vez de borrarlo.",
        )

    db.delete(plan)
    db.commit()
