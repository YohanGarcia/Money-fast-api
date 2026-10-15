"""Company loan rules (Configuración → Préstamos) applied where loans are created.

The rules only bind once the administrator has saved the settings: a company that never configured them has no
``LoanSettings`` row and nothing here blocks it (no surprise limits from product defaults).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.loan import PaymentFrequency
from app.models.loan_settings import LoanSettings
from app.services.loan_service import frequency_delta

FREQUENCY_DAYS = {"daily": 1, "weekly": 7, "biweekly": 14, "monthly": 30}
UNIT_DAYS = {"Dias": 1, "Semanas": 7, "Meses": 30}
UNIT_LABEL = {"Dias": "días", "Semanas": "semanas", "Meses": "meses"}
APPLICATION_FREQUENCY = {"Semanal": "weekly", "Quincenal": "biweekly", "Mensual": "monthly"}

# What the settings screen shows while the company has not saved anything yet.
DEFAULT_VALUES = {
    "minimum_principal": Decimal("10000.00"),
    "maximum_principal": Decimal("50000.00"),
    "default_interest_rate": Decimal("10.00"),
    "minimum_term_count": 2,
    "minimum_term_unit": "Semanas",
    "default_late_fee_rate": Decimal("3.00"),
    "default_grace_days": 5,
    "require_route_assignment": False,
    "allow_payment_date_change": False,
}


def get_rules(db: Session, company_id: int) -> LoanSettings | None:
    """The saved rules, or None while the company has not configured them."""
    return db.scalar(select(LoanSettings).where(LoanSettings.company_id == company_id, LoanSettings.configured.is_(True)))


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def parse_amount(value: object) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount > 0 else None


def enforce_principal(db: Session, company_id: int, amount: object) -> None:
    rules = get_rules(db, company_id)
    value = parse_amount(amount)
    if rules is None or value is None:
        return
    if value < rules.minimum_principal or value > rules.maximum_principal:
        raise HTTPException(
            422,
            f"El monto del préstamo debe estar entre {_money(rules.minimum_principal)} y {_money(rules.maximum_principal)} "
            "(Configuración → Préstamos).",
        )


def term_days(installment_count: int, frequency: str) -> int:
    return installment_count * FREQUENCY_DAYS[frequency]


def enforce_term(db: Session, company_id: int, installment_count: int, frequency: str) -> None:
    rules = get_rules(db, company_id)
    if rules is None or frequency not in FREQUENCY_DAYS:
        return
    needed = rules.minimum_term_count * UNIT_DAYS[rules.minimum_term_unit]
    if term_days(installment_count, frequency) < needed:
        raise HTTPException(
            422,
            f"El plazo mínimo es de {rules.minimum_term_count} {UNIT_LABEL[rules.minimum_term_unit]}: "
            f"con {installment_count} cuotas no se alcanza (Configuración → Préstamos).",
        )


def enforce_route(db: Session, company_id: int, customer) -> None:
    rules = get_rules(db, company_id)
    if rules is not None and rules.require_route_assignment and customer.route_id is None:
        raise HTTPException(
            409,
            "El cliente no tiene una ruta asignada. Asígnala antes de otorgar el préstamo "
            "(Configuración → Préstamos exige ruta).",
        )


def first_payment_date(db: Session, company_id: int, requested: date | None, frequency: str) -> date | None:
    """The first payment date to use.

    When the company does not allow changing it, the system fixes it one period after today and ignores what was
    sent; otherwise the requested date is used as given.
    """
    rules = get_rules(db, company_id)
    if rules is None or rules.allow_payment_date_change or frequency not in FREQUENCY_DAYS:
        return requested
    return frequency_delta(date.today(), PaymentFrequency(frequency), 1)
