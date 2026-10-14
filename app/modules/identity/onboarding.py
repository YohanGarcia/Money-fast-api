"""Tenant + owner creation, shared by self-service registration (when open) and the first-administrator CLI."""

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import get_password_hash
from app.models.company import Company
from app.models.user import User, UserRole
from app.modules.identity.audit import record_event
from app.modules.identity.catalog import bootstrap_owner
from app.modules.identity.tenant import is_valid_slug, normalize_slug, unique_slug
from app.services.plan_limits import get_free_plan


def create_tenant_owner(
    db: Session,
    *,
    full_name: str,
    email: str,
    password: str,
    company_name: str,
    tenant_slug: str | None = None,
    client_ip: str | None = None,
    via: str = "self_registration",
) -> User:
    """A new company (tenant) and its owner, who receives the tenant admin role. Commits."""
    email = email.lower().strip()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=409, detail="Ya existe un usuario con ese correo.")
    if tenant_slug is not None:
        slug = normalize_slug(tenant_slug)
        if not is_valid_slug(slug):
            raise HTTPException(status_code=422, detail="El identificador de agencia no es valido.")
        if db.scalar(select(Company.id).where(Company.slug == slug)):
            raise HTTPException(status_code=409, detail="Ese identificador de agencia ya esta en uso.")
    else:
        slug = unique_slug(db, company_name)
    free_plan = get_free_plan(db)
    company = Company(name=company_name, slug=slug, plan_id=free_plan.id if free_plan else None)
    db.add(company)
    db.flush()
    user = User(
        full_name=full_name.strip(),
        email=email,
        password_hash=get_password_hash(password),
        role=UserRole.admin,  # LEGACY coarse role; real authorization comes from the assigned tenant role
        company_id=company.id,
        activated_at=datetime.now(UTC),
        password_changed_at=datetime.now(UTC),
    )
    db.add(user)
    db.flush()
    bootstrap_owner(db, user)
    record_event(
        db, "user.created", tenant_id=company.id, subject_id=user.id, client_ip=client_ip, details={"via": via}
    )
    db.commit()
    db.refresh(user)
    return user