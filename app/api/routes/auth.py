"""LEGACY /api/v1/auth. Login/refresh/logout share the v2 authentication service (throttling, session revocation,
audit). Password recovery here is the 6-digit-code contract used by the Web and mobile clients; it is backed by the
same hardened recovery service as /api/v2/auth/recovery/*."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.config import settings
from app.core.time import now_utc
from app.models.user import User
from app.modules.identity import auth as auth_service
from app.modules.identity import recovery, throttle
from app.modules.identity.deps import client_ip
from app.modules.identity.errors import RateLimited
from app.modules.identity.notifications import SecretNotifier, get_notifier
from app.modules.identity.onboarding import create_tenant_owner
from app.schemas.auth import LoginInput, RefreshInput, RegisterInput, TokenPair
from app.schemas.user import UserRead

router = APIRouter()
S_REGISTER_IP = "register_ip"


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterInput, request: Request, db: Session = Depends(get_db)) -> User:
    """Self-service registration of a NEW company: the caller becomes the administrator of that company. It never
    joins an existing company and never creates employees (those are created by an administrator in /users).
    PUBLIC_REGISTRATION=false is a kill-switch; per-IP throttling limits abuse."""
    if not settings.registration_open:
        raise HTTPException(status_code=403, detail="El registro de nuevas empresas esta temporalmente deshabilitado.")
    ip = client_ip(request) or "unknown"
    now = now_utc()
    wait = throttle.retry_after(db, S_REGISTER_IP, ip, now)
    if wait:
        raise RateLimited(wait)
    throttle.register_failure(db, S_REGISTER_IP, ip, settings.register_max_per_ip, now)
    db.commit()
    return create_tenant_owner(
        db,
        full_name=payload.full_name,
        email=payload.email,
        password=payload.password,
        company_name=payload.company_name,
        tenant_slug=payload.tenant_slug,
        client_ip=ip,
    )


class PasswordResetRequest(BaseModel):
    email: EmailStr


class VerifyResetCode(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=12)


class ResetPassword(BaseModel):
    reset_token: str = Field(min_length=20, max_length=200)
    new_password: str = Field(min_length=8, max_length=128)


GENERIC_RESET_MESSAGE = "Si el correo esta registrado, te enviaremos un codigo de verificacion."


@router.post("/request-password-reset")
def request_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    notifier: SecretNotifier = Depends(get_notifier),
) -> dict:
    """Always the same answer for known and unknown e-mails; the code travels only by e-mail (never in the response)."""
    pending = recovery.request_code(db, email=payload.email, client_ip=client_ip(request))
    if pending is not None:
        background.add_task(recovery.deliver, pending, notifier)
    return {"message": GENERIC_RESET_MESSAGE, "email": payload.email.lower().strip()}


@router.post("/verify-reset-code")
def verify_reset_code(payload: VerifyResetCode, request: Request, db: Session = Depends(get_db)) -> dict:
    token = recovery.verify_code(db, email=payload.email, code=payload.code, client_ip=client_ip(request))
    return {"reset_token": token, "message": "Codigo verificado."}


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(payload: ResetPassword, request: Request, db: Session = Depends(get_db)) -> None:
    recovery.complete_recovery(
        db, token=payload.reset_token, new_password=payload.new_password, client_ip=client_ip(request)
    )

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
            client_ip=client_ip(request), unscoped=True,
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
