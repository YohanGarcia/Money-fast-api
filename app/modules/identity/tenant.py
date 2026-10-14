"""Tenant slug: the explicit, deterministic context for authenticating tenant-scoped accounts."""

import re
import secrets
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
SLUG_SQL_REGEX = "^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
RESERVED_SLUGS = frozenset({"admin", "api", "app", "www", "platform", "auth", "login", "static", "support", "root"})


def normalize_slug(value: str) -> str:
    return value.strip().lower()


def is_valid_slug(value: str) -> bool:
    return bool(SLUG_PATTERN.fullmatch(value)) and value not in RESERVED_SLUGS


def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:48].strip("-")
    return slug if is_valid_slug(slug) else "agencia"


def default_slug(context) -> str:
    """Fallback for rows inserted without an explicit slug (legacy paths, scripts): name + random suffix."""
    name = context.get_current_parameters().get("name") or "agencia"
    return f"{slugify(name)}-{secrets.token_hex(3)}"


def unique_slug(db: Session, name: str) -> str:
    from app.models.company import Company  # local import: the model imports this module

    base = slugify(name)
    candidate, n = base, 1
    while db.scalar(select(Company.id).where(Company.slug == candidate)) or not is_valid_slug(candidate):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def resolve_tenant(db: Session, slug: str | None):
    """The active Company for ``slug``, or None. Unknown and inactive tenants are indistinguishable."""
    from app.models.company import Company

    if not slug:
        return None
    company = db.scalar(select(Company).where(Company.slug == normalize_slug(slug)))
    return company if company is not None and company.is_active else None
