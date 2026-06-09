from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.security import get_password_hash
from app.models.user import User, UserRole
from app.schemas.user import UserCreate, UserRead, UserUpdate

router = APIRouter()


@router.get("", response_model=list[UserRead])
def list_users(
    q: str | None = Query(default=None),
    role: UserRole | None = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[User]:
    statement = select(User).order_by(User.full_name)
    users = db.scalars(statement).all()

    if role is not None:
        users = [user for user in users if user.role == role]
    if q:
        term = q.lower().strip()
        users = [
            user for user in users
            if term in user.full_name.lower() or term in user.email.lower()
        ]
    return users


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> User:
    existing = db.scalar(select(User).where(User.email == payload.email.lower()))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Ya existe un usuario con ese correo.")

    user = User(
        full_name=payload.full_name.strip(),
        email=payload.email.lower(),
        password_hash=get_password_hash(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.put("/{user_id}", response_model=UserRead)
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    existing = db.scalar(select(User).where(User.email == payload.email.lower(), User.id != user_id))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Ya existe un usuario con ese correo.")

    user.full_name = payload.full_name.strip()
    user.email = payload.email.lower()
    user.role = payload.role
    user.is_active = payload.is_active
    if payload.password:
        user.password_hash = get_password_hash(payload.password)

    db.commit()
    db.refresh(user)
    return user
