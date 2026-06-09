from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.loan_settings import LoanSettings
from app.models.user import User
from app.schemas.loan_settings import LoanSettingsRead, LoanSettingsUpdate

router = APIRouter()


def get_or_create_settings(db: Session) -> LoanSettings:
    settings = db.get(LoanSettings, 1)
    if settings is None:
        settings = LoanSettings(id=1)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


@router.get("", response_model=LoanSettingsRead)
def read_loan_settings(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> LoanSettings:
    return get_or_create_settings(db)


@router.put("", response_model=LoanSettingsRead)
def update_loan_settings(
    payload: LoanSettingsUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> LoanSettings:
    settings = get_or_create_settings(db)
    for key, value in payload.model_dump().items():
        setattr(settings, key, value)
    db.commit()
    db.refresh(settings)
    return settings
