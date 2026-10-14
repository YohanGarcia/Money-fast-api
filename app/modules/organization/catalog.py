"""Currency catalogue (code-defined reference data, seeded by migration 0005). No FX rates here."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.organization.models import Currency

# (ISO 4217 code, name, symbol, decimal places)
CURRENCY_CATALOG: tuple[tuple[str, str, str | None, int], ...] = (
    ("DOP", "Peso dominicano", "RD$", 2),
    ("USD", "Dolar estadounidense", "US$", 2),
    ("EUR", "Euro", "EUR", 2),
)
CURRENCY_CODES = frozenset(c[0] for c in CURRENCY_CATALOG)


def sync_currency_catalog(db: Session) -> None:
    existing = set(db.scalars(select(Currency.code)))
    for code, name, symbol, exponent in CURRENCY_CATALOG:
        if code not in existing:
            db.add(Currency(code=code, name=name, symbol=symbol, exponent=exponent))
    db.flush()
