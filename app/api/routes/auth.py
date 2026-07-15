from datetime import UTC, datetime, timedelta
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_password_reset_token,
    create_refresh_token,
    decode_token,
    get_password_hash,
    hash_token,
    verify_password,
    verify_token_hash,
)
from app.models.company import Company
from app.models.password_reset import PasswordResetCode
from app.models.session import UserSession
from app.models.user import User, UserRole
from app.services.email_service import send_reset_code_email
from app.services.plan_limits import get_free_plan
from app.schemas.auth import (
    LoginInput,
    PasswordResetRequestInput,
    PasswordResetRequestOutput,
    RefreshInput,
    RegisterInput,
    ResetPasswordInput,
    TokenPair,
    VerifyResetCodeInput,
    VerifyResetCodeOutput,
)
from app.schemas.user import UserRead

router = APIRouter()


def _utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterInput, db: Session = Depends(get_db)) -> User:
    """Self-service onboarding.

    Creates a new company and its owner. The owner is always an ``admin`` of the
    new company — the role can never be chosen by the client. Additional users
    (managers, collectors) are created afterwards from the authenticated
    ``POST /users`` endpoint by that admin.
    """
    email = payload.email.lower().strip()
    existing = db.scalar(select(User).where(User.email == email))
    if existing:
        raise HTTPException(status_code=409, detail="Ya existe un usuario con ese correo.")

    company_name = (payload.company_name or payload.full_name).strip()
    free_plan = get_free_plan(db)
    company = Company(name=company_name, plan_id=free_plan.id if free_plan else None)
    db.add(company)
    db.flush()

    user = User(
        full_name=payload.full_name.strip(),
        email=email,
        password_hash=get_password_hash(payload.password),
        role=UserRole.admin,
        company_id=company.id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenPair)
def login(payload: LoginInput, db: Session = Depends(get_db)) -> TokenPair:
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Credenciales invalidas.")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="El usuario esta inactivo.")

    session = UserSession(user_id=user.id, device_name=payload.device_name)
    db.add(session)
    db.flush()

    access_token = create_access_token(user.id, session.id)
    refresh_token = create_refresh_token(user.id, session.id)

    session.refresh_token_hash = hash_token(refresh_token)
    session.last_seen_at = datetime.now(UTC)

    db.commit()

    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserRead.model_validate(user),
    )


@router.post("/refresh", response_model=TokenPair)
def refresh(payload: RefreshInput, db: Session = Depends(get_db)) -> TokenPair:
    try:
        claims = decode_token(payload.refresh_token)
        user_id = claims.get("sub")
        session_id = claims.get("sid")
        token_type = claims.get("type")
        session_id_int = int(session_id)
        user_id_int = int(user_id)
    except (JWTError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Refresh token invalido.") from exc

    if token_type != "refresh":
        raise HTTPException(status_code=401, detail="Refresh token invalido.")

    session = db.get(UserSession, session_id_int)
    user = db.get(User, user_id_int)
    if (
        session is None
        or user is None
        or not session.is_active
        or session.user_id != user.id
        or not verify_token_hash(payload.refresh_token, session.refresh_token_hash)
    ):
        raise HTTPException(status_code=401, detail="La sesion no es valida.")

    access_token = create_access_token(user.id, session.id)
    refresh_token = create_refresh_token(user.id, session.id)
    session.refresh_token_hash = hash_token(refresh_token)
    session.last_seen_at = datetime.now(UTC)
    db.commit()

    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserRead.model_validate(user),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(payload: RefreshInput, db: Session = Depends(get_db)) -> None:
    try:
        claims = decode_token(payload.refresh_token)
        session_id = claims.get("sid")
        session_id_int = int(session_id)
    except (JWTError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Refresh token invalido.") from exc

    session = db.get(UserSession, session_id_int)
    if session is None:
        raise HTTPException(status_code=401, detail="Sesion no encontrada.")

    session.is_active = False
    db.commit()


@router.get("/me", response_model=UserRead)
def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@router.post("/request-password-reset", response_model=PasswordResetRequestOutput)
def request_password_reset(
    payload: PasswordResetRequestInput,
    db: Session = Depends(get_db),
) -> PasswordResetRequestOutput:
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    generic_message = "Si el correo existe, enviamos un codigo de verificacion."

    if user is None:
        return PasswordResetRequestOutput(message=generic_message, email=payload.email.lower())

    active_codes = db.scalars(
        select(PasswordResetCode).where(
            PasswordResetCode.user_id == user.id,
            PasswordResetCode.is_used.is_(False),
        )
    ).all()
    for item in active_codes:
        item.is_used = True

    raw_code = f"{secrets.randbelow(1000000):06d}"
    reset_code = PasswordResetCode(
        user_id=user.id,
        code_hash=hash_token(raw_code),
        expires_at=_utc_now_naive() + timedelta(minutes=15),
    )
    db.add(reset_code)
    db.commit()

    smtp_configured = bool(settings.smtp_host and settings.smtp_user and settings.smtp_password)
    try:
        send_reset_code_email(user.email, raw_code)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"No se pudo enviar el correo: {exc}",
        ) from exc

    # Expose the code only in development when there is no real email delivery.
    debug_code = raw_code if (not settings.is_production and not smtp_configured) else None
    return PasswordResetRequestOutput(message=generic_message, email=user.email, debug_code=debug_code)


@router.post("/verify-reset-code", response_model=VerifyResetCodeOutput)
def verify_reset_code(
    payload: VerifyResetCodeInput,
    db: Session = Depends(get_db),
) -> VerifyResetCodeOutput:
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if user is None:
        raise HTTPException(status_code=404, detail="No existe una cuenta con ese correo.")

    reset_code = db.scalar(
        select(PasswordResetCode)
        .where(
            PasswordResetCode.user_id == user.id,
            PasswordResetCode.is_used.is_(False),
        )
        .order_by(PasswordResetCode.created_at.desc())
    )
    if reset_code is None:
        raise HTTPException(status_code=400, detail="No hay un codigo activo para este usuario.")
    if reset_code.expires_at < _utc_now_naive():
        raise HTTPException(status_code=400, detail="El codigo ha expirado.")
    if not verify_token_hash(payload.code, reset_code.code_hash):
        raise HTTPException(status_code=400, detail="El codigo es invalido.")

    reset_token = create_password_reset_token(user.id, reset_code.id)
    return VerifyResetCodeOutput(
        reset_token=reset_token,
        message="Codigo verificado correctamente.",
    )


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(
    payload: ResetPasswordInput,
    db: Session = Depends(get_db),
) -> None:
    try:
        claims = decode_token(payload.reset_token)
        user_id = claims.get("sub")
        reset_code_id = claims.get("sid")
        token_type = claims.get("type")
        user_id_int = int(user_id)
        reset_code_id_int = int(reset_code_id)
    except (JWTError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="El token de recuperacion es invalido.") from exc

    if token_type != "password_reset":
        raise HTTPException(status_code=401, detail="El token de recuperacion es invalido.")

    user = db.get(User, user_id_int)
    reset_code = db.get(PasswordResetCode, reset_code_id_int)
    if user is None or reset_code is None or reset_code.user_id != user.id:
        raise HTTPException(status_code=404, detail="No se encontro la solicitud de recuperacion.")
    if reset_code.is_used or reset_code.expires_at < _utc_now_naive():
        raise HTTPException(status_code=400, detail="La solicitud de recuperacion ya no es valida.")

    user.password_hash = get_password_hash(payload.new_password)
    reset_code.is_used = True

    sessions = db.scalars(select(UserSession).where(UserSession.user_id == user.id)).all()
    for session in sessions:
        session.is_active = False

    db.commit()
