"""LEGACY /api/v1/auth. Login/refresh/logout now share the v2 authentication service (throttling, session
revocation, audit). The old code-based password reset (debug_code, verify-reset-code) was removed: use
/api/v2/auth/recovery/*."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.security import get_password_hash
from app.models.company import Company
from app.models.user import User, UserRole
from app.modules.identity import auth as auth_service
from app.modules.identity.audit import record_event
from app.modules.identity.catalog import bootstrap_owner
from app.modules.identity.deps import client_ip
from app.schemas.auth import LoginInput, RefreshInput, RegisterInput, TokenPair
from app.schemas.user import UserRead
from app.services.plan_limits import get_free_plan

router = APIRouter()


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterInput, request: Request, db: Session = Depends(get_db)) -> User:
    """Self-service onboarding: a new company (tenant) and its owner, who receives the tenant admin role."""
    email = payload.email.lower().strip()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=409, detail="Ya existe un usuario con ese correo.")

    free_plan = get_free_plan(db)
    company = Company(name=payload.company_name, plan_id=free_plan.id if free_plan else None)
    db.add(company)
    db.flush()

    user = User(
        full_name=payload.full_name.strip(),
        email=email,
        password_hash=get_password_hash(payload.password),
        role=UserRole.admin,  # LEGACY coarse role; real authorization comes from the assigned tenant role
        company_id=company.id,
        activated_at=datetime.now(UTC),
        password_changed_at=datetime.now(UTC),
    )
    db.add(user)
    db.flush()
    bootstrap_owner(db, user)
    record_event(db, "user.created", tenant_id=company.id, subject_id=user.id, client_ip=client_ip(request),
                 details={"via": "self_registration"})
    db.commit()
    db.refresh(user)
    return user


def _pair(result: auth_service.LoginResult) -> TokenPair:
    return TokenPair(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        user=UserRead.model_validate(result.user),
    )


@router.post("/login", response_model=TokenPair)
def login(payload: LoginInput, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    return _pair(
        auth_service.login(
            db, email=payload.email, password=payload.password, device_name=payload.device_name,
            client_ip=client_ip(request),
        )
    )


@router.post("/refresh", response_model=TokenPair)
def refresh(payload: RefreshInput, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    return _pair(auth_service.refresh(db, refresh_token=payload.refresh_token, client_ip=client_ip(request)))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(payload: RefreshInput, request: Request, db: Session = Depends(get_db)) -> None:
    """Revokes the session named by the refresh token. Idempotent; an invalid token changes nothing."""
    from app.models.session import UserSession  # local import: legacy module graph

    try:
        user_id, session_id = auth_service._decode(payload.refresh_token, "refresh")
    except Exception:
        raise HTTPException(status_code=401, detail="Refresh token invalido.") from None
    session = db.get(UserSession, session_id)
    if session is None or session.user_id != user_id:
        raise HTTPException(status_code=401, detail="Sesion no encontrada.")
    user = db.get(User, user_id)
    auth_service.logout(db, user, session, client_ip(request))


@router.get("/me", response_model=UserRead)
def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user
