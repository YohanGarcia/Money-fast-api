from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_company_id, get_db, require_admin
from app.models.branch import Branch
from app.models.route import Route
from app.models.user import User
from app.schemas.branch import BranchCreate, BranchRead, BranchUpdate

router = APIRouter()


@router.get("", response_model=list[BranchRead])
def list_branches(
    q: str | None = Query(default=None),
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
) -> list[Branch]:
    branches = db.scalars(select(Branch).where(Branch.company_id == company_id).order_by(Branch.name)).all()
    if q:
        term = q.lower().strip()
        branches = [b for b in branches if term in b.name.lower() or term in b.manager_name.lower()]
    return branches


@router.post("", response_model=BranchRead, status_code=status.HTTP_201_CREATED)
def create_branch(
    payload: BranchCreate,
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
    _=Depends(require_admin),
) -> Branch:
    existing = db.scalar(select(Branch).where(Branch.name == payload.name.strip(), Branch.company_id == company_id))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Ya existe una sucursal con ese nombre.")

    branch = Branch(company_id=company_id, name=payload.name.strip(), address=payload.address.strip(),
                    manager_name=payload.manager_name.strip(), notary_name=payload.notary_name.strip(),
                    phone=payload.phone.strip())
    db.add(branch)
    db.commit()
    db.refresh(branch)
    return branch


@router.put("/{branch_id}", response_model=BranchRead)
def update_branch(
    branch_id: int,
    payload: BranchUpdate,
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
    _=Depends(require_admin),
) -> Branch:
    branch = db.scalar(select(Branch).where(Branch.id == branch_id, Branch.company_id == company_id))
    if branch is None:
        raise HTTPException(status_code=404, detail="Sucursal no encontrada.")

    existing = db.scalar(select(Branch).where(Branch.name == payload.name.strip(),
                                               Branch.company_id == company_id, Branch.id != branch_id))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Ya existe una sucursal con ese nombre.")

    branch.name = payload.name.strip()
    branch.address = payload.address.strip()
    branch.manager_name = payload.manager_name.strip()
    branch.notary_name = payload.notary_name.strip()
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

    route_count = db.scalar(select(func.count()).select_from(Route).where(Route.branch_id == branch_id)) or 0
    user_count = db.scalar(select(func.count()).select_from(User).where(User.branch_id == branch_id)) or 0
    if route_count > 0 or user_count > 0:
        raise HTTPException(
            status_code=400,
            detail="La sucursal tiene rutas o cobradores asignados. Reasígnalos antes de borrarla.",
        )

    db.delete(branch)
    db.commit()
