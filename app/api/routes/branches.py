from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.branch import Branch
from app.models.user import User
from app.schemas.branch import BranchCreate, BranchRead, BranchUpdate

router = APIRouter()


@router.get("", response_model=list[BranchRead])
def list_branches(
    q: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[Branch]:
    branches = db.scalars(select(Branch).order_by(Branch.name)).all()
    if q:
        term = q.lower().strip()
        branches = [
            branch for branch in branches
            if term in branch.name.lower() or term in branch.manager_name.lower()
        ]
    return branches


@router.post("", response_model=BranchRead, status_code=status.HTTP_201_CREATED)
def create_branch(
    payload: BranchCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Branch:
    existing = db.scalar(select(Branch).where(Branch.name == payload.name.strip()))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Ya existe una sucursal con ese nombre.")

    branch = Branch(
        name=payload.name.strip(),
        address=payload.address.strip(),
        manager_name=payload.manager_name.strip(),
        notary_name=payload.notary_name.strip(),
        phone=payload.phone.strip(),
    )
    db.add(branch)
    db.commit()
    db.refresh(branch)
    return branch


@router.put("/{branch_id}", response_model=BranchRead)
def update_branch(
    branch_id: int,
    payload: BranchUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Branch:
    branch = db.get(Branch, branch_id)
    if branch is None:
        raise HTTPException(status_code=404, detail="Sucursal no encontrada.")

    existing = db.scalar(select(Branch).where(Branch.name == payload.name.strip(), Branch.id != branch_id))
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
