"""Enforcement of plan limits (freemium model).

Every company has an effective plan: its assigned plan when the subscription is
still current, or the free "Gratis" plan otherwise (no plan, or an expired paid
plan). Resource creation (customers, loans, users) is blocked once the company
reaches the plan's limit. A limit of 0 means unlimited.
"""
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.customer import Customer
from app.models.loan import Loan
from app.models.plan import Plan
from app.models.user import User

FREE_PLAN_NAME = "Gratis"

_LABELS = {"customer": "clientes", "loan": "préstamos", "user": "usuarios"}

_DEFAULT_PLANS = [
    ("Gratis", 5, 5, 1, "0.00"),
    ("Basico", 100, 100, 3, "17.99"),
    ("Estandar", 200, 200, 5, "29.99"),
    ("Pro", 500, 500, 10, "49.99"),
    ("Empresarial", 0, 0, 0, "79.99"),
]


def seed_default_plans(db: Session) -> None:
    """Insert the default catalog of plans if none exist yet."""
    if db.scalars(select(Plan)).first() is not None:
        return
    db.add_all([
        Plan(name=n, customer_limit=c, loan_limit=l, user_limit=u, monthly_price_usd=Decimal(p))
        for (n, c, l, u, p) in _DEFAULT_PLANS
    ])
    db.commit()


def get_free_plan(db: Session) -> Plan | None:
    """The free tier, seeding the default catalog if needed."""
    plan = db.scalar(select(Plan).where(Plan.name == FREE_PLAN_NAME))
    if plan is None:
        seed_default_plans(db)
        plan = db.scalar(select(Plan).where(Plan.name == FREE_PLAN_NAME))
    return plan


def subscription_is_current(company: Company) -> bool:
    """True when the company's paid subscription has not expired.

    A missing expiry (free tier, or never subscribed) counts as current — the
    free plan never expires. Naive datetimes from SQLite are treated as UTC.
    """
    exp = company.subscription_expires_at
    if exp is None:
        return True
    exp = exp if exp.tzinfo is not None else exp.replace(tzinfo=UTC)
    return exp >= datetime.now(UTC)


def effective_plan(db: Session, company_id: int) -> Plan | None:
    """The company's plan when its subscription is current, else the free plan.

    An expired paid plan is downgraded to the free tier so its limits apply.
    """
    company = db.get(Company, company_id)
    if (
        company is not None
        and company.plan_id is not None
        and subscription_is_current(company)
    ):
        plan = db.get(Plan, company.plan_id)
        if plan is not None:
            return plan
    return get_free_plan(db)


def _count(db: Session, company_id: int, kind: str) -> int:
    if kind == "customer":
        stmt = select(func.count()).select_from(Customer).where(Customer.company_id == company_id)
    elif kind == "user":
        stmt = select(func.count()).select_from(User).where(User.company_id == company_id)
    else:  # loan — loans belong to a company through their customer
        stmt = select(func.count()).select_from(Loan).join(Loan.customer).where(Customer.company_id == company_id)
    return db.scalar(stmt) or 0


def enforce_can_create(db: Session, company_id: int, kind: str) -> None:
    """Raise HTTP 402 if the company already reached its plan limit for ``kind``.

    ``kind`` is one of ``customer`` | ``loan`` | ``user``. A limit of 0 = unlimited.
    """
    plan = effective_plan(db, company_id)
    if plan is None:
        return  # no plans defined at all — do not block

    limit = {"customer": plan.customer_limit, "loan": plan.loan_limit, "user": plan.user_limit}[kind]
    if limit == 0:
        return  # unlimited

    if _count(db, company_id, kind) >= limit:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Alcanzaste el límite de tu plan {plan.name} "
                f"({limit} {_LABELS[kind]}). Actualiza tu plan para agregar más."
            ),
        )
