from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_company_id, get_db, require_admin
from app.models.loan_settings import LoanSettings
from app.schemas.loan_settings import LoanSettingsRead, LoanSettingsUpdate

router = APIRouter()


def get_or_create(db: Session, company_id: int) -> LoanSettings:
    s = db.scalar(select(LoanSettings).where(LoanSettings.company_id == company_id))
    if s is None:
        s = LoanSettings(company_id=company_id)
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


@router.get("", response_model=LoanSettingsRead)
def read_loan_settings(
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
) -> LoanSettings:
    return get_or_create(db, company_id)


@router.put("", response_model=LoanSettingsRead)
def update_loan_settings(
    payload: LoanSettingsUpdate,
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
    _=Depends(require_admin),
) -> LoanSettings:
    s = get_or_create(db, company_id)
    for key, value in payload.model_dump().items():
        setattr(s, key, value)
    db.commit()
    db.refresh(s)
    return s
