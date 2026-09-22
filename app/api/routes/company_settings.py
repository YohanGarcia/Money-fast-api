from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_company_id, get_db, require_admin_manager
from app.models.company import Company
from app.models.company_settings import CompanySettings
from app.schemas.company_settings import CompanySettingsRead, CompanySettingsUpdate

router = APIRouter()


def get_or_create(db: Session, company_id: int) -> CompanySettings:
    s = db.scalar(select(CompanySettings).where(CompanySettings.company_id == company_id))
    if s is None:
        # Seed the settings name from the company name chosen at registration.
        company = db.get(Company, company_id)
        s = CompanySettings(company_id=company_id, name=company.name if company and company.name else "Mi Empresa")
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


@router.get("", response_model=CompanySettingsRead)
def read_company_settings(
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
) -> CompanySettings:
    return get_or_create(db, company_id)


@router.put("", response_model=CompanySettingsRead)
def update_company_settings(
    payload: CompanySettingsUpdate,
    db: Session = Depends(get_db),
    company_id: int = Depends(get_company_id),
    _=Depends(require_admin_manager),
) -> CompanySettings:
    s = get_or_create(db, company_id)
    for key, value in payload.model_dump().items():
        setattr(s, key, value)
    # Compose the one-line address (used on receipts/reports) from the structured parts.
    street = f"{payload.street} No. {payload.house_number}".strip() if payload.house_number else payload.street
    s.address = ", ".join(p for p in [street, payload.sector, payload.municipality, payload.province] if p)
    # Keep the company name in sync so registration and settings never diverge.
    company = db.get(Company, company_id)
    if company is not None:
        company.name = payload.name
    db.commit()
    db.refresh(s)
    return s
