from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_company_id, get_db, require_admin_manager
from app.models.print_settings import PrintSettings
from app.schemas.print_settings import PrintSettingsRead, PrintSettingsUpdate

router = APIRouter()


def get_or_create(db: Session, company_id: int) -> PrintSettings:
    s = db.scalar(select(PrintSettings).where(PrintSettings.company_id == company_id))
    if s is None:
        s = PrintSettings(company_id=company_id)
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


@router.get("", response_model=PrintSettingsRead)
def read_print_settings(
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
) -> PrintSettings:
    return get_or_create(db, company_id)


@router.put("", response_model=PrintSettingsRead)
def update_print_settings(
    payload: PrintSettingsUpdate,
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
    _=Depends(require_admin_manager),
) -> PrintSettings:
    s = get_or_create(db, company_id)
    for key, value in payload.model_dump().items():
        setattr(s, key, value)
    db.commit()
    db.refresh(s)
    return s
