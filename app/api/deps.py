from collections.abc import Generator

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.security import decode_token
from app.models.session import UserSession
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No fue posible validar la sesion.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = decode_token(token)
        user_id = payload.get("sub")
        session_id = payload.get("sid")
        token_type = payload.get("type")
    except JWTError as exc:
        raise credentials_exception from exc

    if not user_id or not session_id or token_type != "access":
        raise credentials_exception

    session = db.get(UserSession, session_id)
    user = db.get(User, int(user_id)) if str(user_id).isdigit() else None

    if (
        user is None
        or not user.is_active
        or session is None
        or session.user_id != user.id
        or not session.is_active
    ):
        raise credentials_exception

    return user
