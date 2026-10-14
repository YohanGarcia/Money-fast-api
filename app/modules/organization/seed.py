"""Test/demo organization seed (T-003 §23). Data are test data only; the seed is idempotent."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.branch import Branch
from app.models.company import Company
from app.modules.organization.catalog import sync_currency_catalog
from app.modules.organization.models import CashPoint, CashPointCurrency, TenantCurrency

# (slug, name, base currency, timezone, enabled currencies, branches[(code, name, [(cash code, name, [currencies])])])
SEED = (
    (
        "dominicana",
        "Agencia Dominicana (prueba)",
        "DOP",
        "America/Santo_Domingo",
        ("DOP", "USD"),
        (
            (
                "CENTRO",
                "Sucursal Centro",
                (("CAJA-1", "Caja principal", ("DOP", "USD")), ("CAJA-2", "Caja secundaria", ("DOP",))),
            ),
            ("ESTE", "Sucursal Este", (("CAJA-1", "Caja principal", ("DOP",)),)),
        ),
    ),
    (
        "nueva-york",
        "Agencia Nueva York (prueba)",
        "USD",
        "America/New_York",
        ("USD", "DOP"),
        (
            ("MANHATTAN", "Sucursal Manhattan", (("CAJA-1", "Caja principal", ("USD",)),)),
            ("BRONX", "Sucursal Bronx", (("CAJA-1", "Caja principal", ("USD", "DOP")),)),
        ),
    ),
)


def seed_test_organization(db: Session) -> dict[str, int]:
    """Create (or complete) the two demo tenants. Returns counts. Does not commit."""
    sync_currency_catalog(db)
    for slug, name, base, tz, currencies, branches in SEED:
        tenant = db.scalar(select(Company).where(Company.slug == slug))
        if tenant is None:
            tenant = Company(name=name, slug=slug, base_currency_code=base, default_timezone=tz)
            db.add(tenant)
            db.flush()
        for code in currencies:
            if db.get(TenantCurrency, (tenant.id, code)) is None:
                db.add(TenantCurrency(tenant_id=tenant.id, currency_code=code))
        db.flush()
        for b_code, b_name, cash_points in branches:
            branch = db.scalar(select(Branch).where(Branch.company_id == tenant.id, Branch.code == b_code))
            if branch is None:
                branch = Branch(company_id=tenant.id, code=b_code, name=b_name)
                db.add(branch)
                db.flush()
            for c_code, c_name, allowed in cash_points:
                cp = db.scalar(
                    select(CashPoint).where(CashPoint.tenant_id == tenant.id, CashPoint.code == f"{b_code}-{c_code}")
                )
                if cp is None:
                    cp = CashPoint(tenant_id=tenant.id, branch_id=branch.id, code=f"{b_code}-{c_code}", name=c_name)
                    db.add(cp)
                    db.flush()
                    for cur in allowed:
                        db.add(CashPointCurrency(cash_point_id=cp.id, currency_code=cur, tenant_id=tenant.id))
    db.flush()
    return {
        "tenants": db.query(Company).filter(Company.slug.in_([s[0] for s in SEED])).count(),
        "branches": db.query(Branch).count(),
        "cash_points": db.query(CashPoint).count(),
    }
