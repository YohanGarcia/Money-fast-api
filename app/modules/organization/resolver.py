"""Single place that resolves the effective organizational configuration (T-003 §10).

Read-only: resolving configuration never writes. Timezone inheritance: ``branch.timezone_override`` when set,
otherwise ``tenant.default_timezone`` (ADR-006); both are IANA identifiers.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.branch import Branch
from app.models.company import Company
from app.modules.identity.errors import TenantMismatch
from app.modules.organization.models import TenantCurrency


@dataclass(frozen=True)
class EffectiveConfig:
    tenant_id: int
    tenant_code: str
    branch_id: int | None
    timezone: str
    timezone_source: str  # "branch" | "tenant"
    base_currency: str
    enabled_currencies: tuple[str, ...]


def resolve_effective(db: Session, tenant_id: int, branch_id: int | None = None) -> EffectiveConfig:
    """Effective config for a tenant (and optionally one of ITS branches; another tenant's branch is a 404)."""
    tenant = db.get(Company, tenant_id)
    if tenant is None:
        raise TenantMismatch()
    timezone, source = tenant.default_timezone, "tenant"
    if branch_id is not None:
        branch = db.get(Branch, branch_id)
        if branch is None or branch.company_id != tenant_id:
            raise TenantMismatch()
        if branch.timezone_override:
            timezone, source = branch.timezone_override, "branch"
    enabled = tuple(
        db.scalars(
            select(TenantCurrency.currency_code)
            .where(TenantCurrency.tenant_id == tenant_id, TenantCurrency.disabled_at.is_(None))
            .order_by(TenantCurrency.currency_code)
        )
    )
    return EffectiveConfig(tenant.id, tenant.slug, branch_id, timezone, source, tenant.base_currency_code, enabled)


def context_values(db: Session, tenant_id: int, branch_id: int | None) -> tuple[str, str]:
    """(effective timezone, base currency) for binding into the request context; cheap, two small reads."""
    tenant = db.get(Company, tenant_id)
    timezone = tenant.default_timezone
    if branch_id is not None:
        branch = db.get(Branch, branch_id)
        if branch is not None and branch.company_id == tenant_id and branch.timezone_override:
            timezone = branch.timezone_override
    return timezone, tenant.base_currency_code
