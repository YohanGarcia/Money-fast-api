"""Compute an employee's suggested pay for a period: fixed salary (reference) plus
configured commissions over the real data they generated."""
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.models.loan import Loan
from app.models.payment import Payment
from app.services import cash_service as svc

ZERO = Decimal("0.00")
BASES = {"collections", "interest", "disbursements"}
BASE_LABELS = {
    "collections": "Comisión sobre lo cobrado",
    "interest": "Comisión sobre interés y mora",
    "disbursements": "Comisión sobre préstamos colocados",
}


def _window(start: date, end: date):
    low = datetime.combine(start, time.min, svc.TZ).astimezone(UTC)
    high = datetime.combine(end + timedelta(days=1), time.min, svc.TZ).astimezone(UTC)
    return low, high


def base_amount(db, user_id: int, base: str, start: date, end: date) -> Decimal:
    low, high = _window(start, end)
    if base == "collections":
        q = select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.collected_by_id == user_id, Payment.paid_at >= low, Payment.paid_at < high)
    elif base == "interest":
        q = select(func.coalesce(func.sum(Payment.interest_applied + Payment.late_fee_applied), 0)).where(
            Payment.collected_by_id == user_id, Payment.paid_at >= low, Payment.paid_at < high)
    elif base == "disbursements":
        q = select(func.coalesce(func.sum(Loan.principal_amount), 0)).where(
            Loan.created_by_id == user_id, Loan.created_at >= low, Loan.created_at < high)
    else:
        return ZERO
    return Decimal(db.scalar(q) or 0)


def compute(db, config, start: date, end: date) -> dict:
    """config: PayrollConfig. Returns salary_part, commissions breakdown and total."""
    salary = Decimal(config.salary_amount or 0)
    lines = []
    commission_total = ZERO
    for rule in (config.commissions or []):
        base = rule.get("base")
        if base not in BASES:
            continue
        percent = Decimal(str(rule.get("percent", "0")))
        amount = (base_amount(db, config.user_id, base, start, end) * percent / 100).quantize(Decimal("0.01"))
        commission_total += amount
        lines.append({"base": base, "label": BASE_LABELS[base], "percent": str(percent),
                      "base_amount": str(base_amount(db, config.user_id, base, start, end).quantize(Decimal("0.01"))),
                      "amount": str(amount)})
    total = (salary + commission_total).quantize(Decimal("0.01"))
    return {"salary_part": str(salary.quantize(Decimal("0.01"))),
            "salary_frequency": config.salary_frequency,
            "commission_part": str(commission_total.quantize(Decimal("0.01"))),
            "commissions": lines,
            "total": str(total)}
