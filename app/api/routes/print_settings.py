from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.print_settings import PrintSettings
from app.models.user import User
from app.schemas.print_settings import PrintSettingsRead, PrintSettingsUpdate

router = APIRouter()


def get_or_create_settings(db: Session) -> PrintSettings:
    settings = db.get(PrintSettings, 1)
    if settings is None:
        settings = PrintSettings(id=1)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


@router.get("", response_model=PrintSettingsRead)
def read_print_settings(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> PrintSettings:
    return get_or_create_settings(db)


@router.put("", response_model=PrintSettingsRead)
def update_print_settings(
    payload: PrintSettingsUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> PrintSettings:
    settings = get_or_create_settings(db)
    for key, value in payload.model_dump().items():
        setattr(settings, key, value)
    db.commit()
    db.refresh(settings)
    return settings
