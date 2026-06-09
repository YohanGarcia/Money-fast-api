from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.company_settings import CompanySettings
from app.models.user import User
from app.schemas.company_settings import CompanySettingsRead, CompanySettingsUpdate

router = APIRouter()


def get_or_create_settings(db: Session) -> CompanySettings:
    settings = db.get(CompanySettings, 1)
    if settings is None:
        settings = CompanySettings(id=1)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


@router.get("", response_model=CompanySettingsRead)
def read_company_settings(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> CompanySettings:
    return get_or_create_settings(db)


@router.put("", response_model=CompanySettingsRead)
def update_company_settings(
    payload: CompanySettingsUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> CompanySettings:
    settings = get_or_create_settings(db)
    for key, value in payload.model_dump().items():
        setattr(settings, key, value)
    db.commit()
    db.refresh(settings)
    return settings
