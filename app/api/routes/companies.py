from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_superadmin
from app.models.company import Company
from app.models.customer import Customer
from app.models.loan import Loan
from app.models.plan import Plan
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import User, UserRole
from app.core.security import get_password_hash
from app.schemas.subscription import PlatformOverview
from app.services.plan_limits import subscription_is_current

router = APIRouter()


class CompanyRead(BaseModel):
    id: int
    name: str
    tax_id: str
    address: str
    phone: str
    is_active: bool
    plan_id: int | None
    subscription_expires_at: datetime | None = None
    user_count: int = 0

    model_config = {"from_attributes": True}


class CompanyCreate(BaseModel):
    name: str
    tax_id: str = ""
    address: str = ""
    phone: str = ""
    admin_full_name: str
    admin_email: str
    admin_password: str


class CompanyUpdate(BaseModel):
    name: str
    tax_id: str = ""
    address: str = ""
    phone: str = ""
    is_active: bool = True
    plan_id: int | None = None
    subscription_expires_at: datetime | None = None


def _company_dict(company: Company, user_count: int) -> dict:
    return {
        "id": company.id, "name": company.name, "tax_id": company.tax_id,
        "address": company.address, "phone": company.phone,
        "is_active": company.is_active, "plan_id": company.plan_id,
        "subscription_expires_at": company.subscription_expires_at,
        "user_count": user_count,
    }


def _user_count(db: Session, company_id: int) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.company_id == company_id)) or 0


@router.get("", response_model=list[CompanyRead])
def list_companies(
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
) -> list[dict]:
    companies = db.scalars(select(Company).order_by(Company.name)).all()
    return [_company_dict(c, _user_count(db, c.id)) for c in companies]


@router.get("/overview", response_model=PlatformOverview)
def platform_overview(
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
) -> PlatformOverview:
    companies = list(db.scalars(select(Company)).all())
    active = [c for c in companies if c.is_active]
    total_users = db.scalar(select(func.count()).select_from(User)) or 0

    # MRR = sum of the monthly price of each active company's plan, but only
    # while its subscription is still current (expired plans don't earn revenue).
    plan_prices = {p.id: p.monthly_price_usd for p in db.scalars(select(Plan)).all()}
    mrr = sum(
        (plan_prices.get(c.plan_id, Decimal("0.00")) for c in active if subscription_is_current(c)),
        Decimal("0.00"),
    )

    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    revenue_month = db.scalar(
        select(func.coalesce(func.sum(Subscription.amount_usd), 0)).where(
            Subscription.status == SubscriptionStatus.active,
            Subscription.created_at >= month_start,
        )
    ) or Decimal("0.00")

    soon_limit = now + timedelta(days=7)

    def _as_aware(dt: datetime) -> datetime:
        # SQLite returns naive datetimes; assume they are UTC so comparisons
        # against timezone-aware ``now`` don't raise.
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

    expiring = [
        {"id": c.id, "name": c.name, "subscription_expires_at": c.subscription_expires_at}
        for c in companies
        if c.subscription_expires_at is not None
        and now <= _as_aware(c.subscription_expires_at) <= soon_limit
    ]

    return PlatformOverview(
        total_companies=len(companies),
        active_companies=len(active),
        total_users=total_users,
        mrr_usd=Decimal(mrr),
        revenue_this_month_usd=Decimal(revenue_month),
        expiring_soon=expiring,
    )


@router.post("", response_model=CompanyRead, status_code=status.HTTP_201_CREATED)
def create_company(
    payload: CompanyCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
) -> dict:
    # Check admin email not taken
    existing = db.scalar(select(User).where(User.email == payload.admin_email.lower()))
    if existing:
        raise HTTPException(status_code=409, detail="Ya existe un usuario con ese correo.")

    company = Company(
        name=payload.name.strip(),
        tax_id=payload.tax_id.strip(),
        address=payload.address.strip(),
        phone=payload.phone.strip(),
    )
    db.add(company)
    db.flush()

    admin = User(
        full_name=payload.admin_full_name.strip(),
        email=payload.admin_email.lower().strip(),
        password_hash=get_password_hash(payload.admin_password),
        role=UserRole.admin,
        company_id=company.id,
    )
    db.add(admin)
    db.commit()
    db.refresh(company)

    return _company_dict(company, user_count=1)


@router.put("/{company_id}", response_model=CompanyRead)
def update_company(
    company_id: int,
    payload: CompanyUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
) -> dict:
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Empresa no encontrada.")

    if payload.plan_id is not None and db.get(Plan, payload.plan_id) is None:
        raise HTTPException(status_code=404, detail="Plan no encontrado.")

    company.name = payload.name.strip()
    company.tax_id = payload.tax_id.strip()
    company.address = payload.address.strip()
    company.phone = payload.phone.strip()
    company.is_active = payload.is_active
    company.plan_id = payload.plan_id
    company.subscription_expires_at = payload.subscription_expires_at
    db.commit()
    db.refresh(company)

    return _company_dict(company, _user_count(db, company.id))


@router.delete("/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_company(
    company_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_superadmin),
) -> None:
    """Permanently delete a company only if it has no customers or loans.

    Companies with operational data must be deactivated (PUT is_active=false)
    instead, to avoid accidental data loss.
    """
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Empresa no encontrada.")

    customer_count = db.scalar(
        select(func.count()).select_from(Customer).where(Customer.company_id == company_id)
    ) or 0
    loan_count = db.scalar(
        select(func.count()).select_from(Loan).join(Loan.customer).where(Customer.company_id == company_id)
    ) or 0
    if customer_count > 0 or loan_count > 0:
        raise HTTPException(
            status_code=400,
            detail="La empresa tiene datos. Desactívala en vez de borrarla.",
        )

    # Safe to remove: drop its users and subscriptions, then the company.
    for user in db.scalars(select(User).where(User.company_id == company_id)).all():
        db.delete(user)
    for sub in db.scalars(select(Subscription).where(Subscription.company_id == company_id)).all():
        db.delete(sub)
    db.delete(company)
    db.commit()
