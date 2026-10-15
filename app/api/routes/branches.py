from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_company_id, get_current_user, get_db, require_admin
from app.services.branch_scope import user_branch
from app.models.branch import Branch
from app.services.plan_limits import enforce_feature
from app.models.cash import CashBox, CashSession, CashTransfer, CashDelivery
from app.services import cash_service
from app.models.route import Route
from app.models.user import User
from app.modules.identity.catalog import sync_branch_staff
from app.schemas.branch import BranchCreate, BranchRead, BranchUpdate

router = APIRouter()


def _apply_staff(db: Session, branch: Branch, payload: BranchCreate, actor_id: int | None) -> None:
    """Assign the manager / notary roles (branch scope) from real users of the SAME company and keep the displayed
    names in sync. ``None`` for an id means "leave the free text as is"; an explicit clear is an empty name + no id."""
    for kind, user_id_attr, name_attr in (
        ("manager", "manager_user_id", "manager_name"),
        ("notary", "notary_user_id", "notary_name"),
    ):
        user_id = getattr(payload, user_id_attr)
        if user_id is not None:
            user = db.scalar(select(User).where(User.id == user_id, User.company_id == branch.company_id))
            if user is None or not user.is_active:
                raise HTTPException(status_code=422, detail="El usuario elegido no existe o esta inactivo.")
            setattr(branch, name_attr, user.full_name.strip())
        else:
            setattr(branch, name_attr, getattr(payload, name_attr).strip())
        setattr(branch, user_id_attr, user_id)
        sync_branch_staff(db, branch.company_id, branch.id, kind, user_id, actor_id)


@router.get("", response_model=list[BranchRead])
def list_branches(
    q: str | None = Query(default=None),
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
    current_user: User = Depends(get_current_user),
) -> list[Branch]:
    query = select(Branch).where(Branch.company_id == company_id)
    own = user_branch(current_user) if current_user.branch_id is not None or current_user.role == "admin" else None
    if own is not None:  # a branch user only knows their own branch
        query = query.where(Branch.id == own)
    branches = db.scalars(query.order_by(Branch.name)).all()
    if q:
        term = q.lower().strip()
        branches = [b for b in branches if term in b.name.lower() or term in b.manager_name.lower()]
    return branches


@router.post("", response_model=BranchRead, status_code=status.HTTP_201_CREATED)
def create_branch(
    payload: BranchCreate,
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
    current_user: User = Depends(require_admin),
) -> Branch:
    existing = db.scalar(select(Branch).where(Branch.name == payload.name.strip(), Branch.company_id == company_id))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Ya existe una sucursal con ese nombre.")
    if db.scalar(select(func.count()).select_from(Branch).where(Branch.company_id == company_id)):
        enforce_feature(db, company_id, "branches")  # the first branch is included in every plan

    branch = Branch(company_id=company_id, name=payload.name.strip(), address=payload.address.strip(),
                    phone=payload.phone.strip())
    db.add(branch)
    db.flush()
    _apply_staff(db, branch, payload, current_user.id)
    db.commit()
    db.refresh(branch)
    return branch


@router.put("/{branch_id}", response_model=BranchRead)
def update_branch(
    branch_id: int,
    payload: BranchUpdate,
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
    current_user: User = Depends(require_admin),
) -> Branch:
    branch = db.scalar(select(Branch).where(Branch.id == branch_id, Branch.company_id == company_id))
    if branch is None:
        raise HTTPException(status_code=404, detail="Sucursal no encontrada.")

    existing = db.scalar(select(Branch).where(Branch.name == payload.name.strip(),
                                               Branch.company_id == company_id, Branch.id != branch_id))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Ya existe una sucursal con ese nombre.")

    box = db.scalar(select(CashBox).where(CashBox.branch_id == branch_id))
    if box and not payload.is_active:
        if cash_service.active_session(db, box, False) or cash_service.pending(db, box)>0 or db.scalar(select(CashTransfer.id).where(CashTransfer.box_id==box.id,CashTransfer.state=='pending')) or db.scalar(select(CashDelivery.id).where(CashDelivery.box_id==box.id,CashDelivery.state=='pending')):
            raise HTTPException(409, 'Cierra la jornada y resuelve entregas pendientes antes de desactivar la sucursal.')
    branch.name = payload.name.strip()
    branch.address = payload.address.strip()
    _apply_staff(db, branch, payload, current_user.id)
    branch.phone = payload.phone.strip()
    branch.is_active = payload.is_active
    db.commit()
    db.refresh(branch)
    return branch


@router.delete("/{branch_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_branch(
    branch_id: int,
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
    _=Depends(require_admin),
) -> None:
    """Delete a branch only if no route or user is assigned to it."""
    branch = db.scalar(select(Branch).where(Branch.id == branch_id, Branch.company_id == company_id))
    if branch is None:
        raise HTTPException(status_code=404, detail="Sucursal no encontrada.")

    if db.scalar(select(CashBox.id).where(CashBox.branch_id == branch_id)):
        raise HTTPException(409, 'La sucursal conserva una caja y su historial; no se puede eliminar.')
    route_count = db.scalar(select(func.count()).select_from(Route).where(Route.branch_id == branch_id)) or 0
    user_count = db.scalar(select(func.count()).select_from(User).where(User.branch_id == branch_id)) or 0
    if route_count > 0 or user_count > 0:
        raise HTTPException(
            status_code=400,
            detail="La sucursal tiene rutas o cobradores asignados. Reasígnalos antes de borrarla.",
        )

    db.delete(branch)
    db.commit()
