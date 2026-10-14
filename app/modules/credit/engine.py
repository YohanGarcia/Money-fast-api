"""Deterministic, side-effect-free schedule engine (T-005 §28). Pure functions: no DB, no clock, no float.

Everything is computed under one explicit ``decimal`` context (precision 40) and every monetary result is
rounded by ``money()`` with the version's own mode/scale; intermediate values are never rounded. The same
(rules, currency, principal, term, start date) always yields the same schedule and ``result_digest``.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)

from app.modules.credit.rules import PERIODS_PER_YEAR, RulesIn

CTX = Context(
    prec=40, rounding=ROUND_HALF_EVEN, Emin=-999, Emax=999, traps=[InvalidOperation, DivisionByZero, Overflow]
)
_MODES = {"half_up": ROUND_HALF_UP, "half_even": ROUND_HALF_EVEN, "down": ROUND_DOWN, "up": ROUND_UP}
_STEP_DAYS = {"daily": 1, "weekly": 7, "biweekly": 14}
_SEARCH_LIMIT = 400  # days scanned for a business day; beyond that the calendar is unusable
HUNDRED = Decimal(100)


class EngineError(Exception):
    """Calculation cannot be completed with the given rules/inputs (the caller maps it to a 422)."""


def money(value: Decimal, scale: int, mode: str) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-scale), rounding=_MODES[mode], context=CTX)


def fmt(value: Decimal, scale: int) -> str:
    return format(value.quantize(Decimal(1).scaleb(-scale), context=CTX), "f")


# --- calendar ---------------------------------------------------------------------------------------
def add_periods(start: date, frequency: str, steps: int) -> date:
    """Contractual date ``steps`` periods after ``start``; monthly dates are anchored to ``start.day``."""
    if frequency in _STEP_DAYS:
        return start + timedelta(days=_STEP_DAYS[frequency] * steps)
    year, month0 = divmod(start.year * 12 + start.month - 1 + steps, 12)
    month = month0 + 1
    first_of_next = date(year + (month == 12), month % 12 + 1, 1)
    return date(year, month, min(start.day, (first_of_next - timedelta(days=1)).day))


def is_business_day(day: date, non_working: frozenset[int], holidays: frozenset[date]) -> bool:
    return day.weekday() not in non_working and day not in holidays


def effective_due(day: date, adjustment: str, non_working: frozenset[int], holidays: frozenset[date]) -> date:
    """Policy A keeps the date; B moves forward to the next business day; C moves back to the previous one."""
    if adjustment == "keep_original" or is_business_day(day, non_working, holidays):
        return day
    step = 1 if adjustment == "next_business_day" else -1
    for i in range(1, _SEARCH_LIMIT):
        candidate = day + timedelta(days=step * i)
        if is_business_day(candidate, non_working, holidays):
            return candidate
    raise EngineError("El calendario no tiene dias habiles utilizables.")


# --- helpers ----------------------------------------------------------------------------------------
def _dec(value: str) -> Decimal:
    return Decimal(value)


def _rates(rules: RulesIn, start: date, contractual: list[date], effective: list[date]) -> list[Decimal]:
    """Interest rate (a fraction, not a percent) applicable to each period."""
    rate = rules.method.rate
    pct = _dec(rate.value) / HUNDRED
    basis = rules.method.time_basis
    if rate.type == "annual" and basis in ("actual_360", "actual_365"):
        year_days = Decimal(360 if basis == "actual_360" else 365)
        dates = effective if rules.calendar.accrual_basis == "effective_dates" else contractual
        out, prev = [], start
        for d in dates:
            out.append(CTX.divide(CTX.multiply(pct, Decimal((d - prev).days)), year_days))
            prev = d
        return out
    per_period = pct if rate.type == "per_period" else CTX.divide(pct, Decimal(PERIODS_PER_YEAR[rules.frequency.code]))
    first_span = rules.first_due.periods_after_start  # the first installment covers that many periods
    return [CTX.multiply(per_period, Decimal(first_span if k == 0 else 1)) for k in range(len(contractual))]


def _even_split(principal: Decimal, interest_total: Decimal, n: int, scale: int, mode: str):
    """Equal principal and interest parts; the rounding residual lands on the LAST installment."""
    p_i = money(CTX.divide(principal, Decimal(n)), scale, mode)
    i_i = money(CTX.divide(interest_total, Decimal(n)), scale, mode)
    parts, p_left, i_left = [], principal, interest_total
    for k in range(n):
        p, i = (p_left, i_left) if k == n - 1 else (p_i, i_i)
        parts.append((p, i))
        p_left, i_left = p_left - p, i_left - i
    return parts


def _fee_amount(fee, base: Decimal, currency: str, scale: int, mode: str) -> Decimal:
    if fee.kind == "fixed":
        return _dec(fee.amounts[currency])
    return money(CTX.multiply(base, _dec(fee.percent)) / HUNDRED, scale, mode)


# --- calculation methods ----------------------------------------------------------------------------
@dataclass(frozen=True)
class _Plan:
    rules: RulesIn
    currency: str
    start: date
    n: int
    contractual: list[date]
    effective: list[date]
    scale: int
    mode: str


def _reducing_balance(plan: _Plan, principal: Decimal) -> list[tuple[Decimal, Decimal]]:
    n, grace = plan.n, plan.rules.grace.principal_grace_periods
    rates = _rates(plan.rules, plan.start, plan.contractual, plan.effective)
    discount, total = Decimal(1), Decimal(0)
    for j in range(grace, n):  # level payment = principal / sum of the discount factors of the amortising periods
        discount = CTX.divide(discount, 1 + rates[j])
        total += discount
    level = money(CTX.divide(principal, total), plan.scale, plan.mode)
    rows, balance = [], principal
    for j in range(n):
        interest = money(CTX.multiply(balance, rates[j]), plan.scale, plan.mode)
        if j < grace:
            part = Decimal(0)
        elif j == n - 1:
            part = balance  # every rounding residual lands on the last installment
        else:
            part = min(max(level - interest, Decimal(0)), balance)
        rows.append((part, interest))
        balance -= part
    return rows


def _interest_only(plan: _Plan, principal: Decimal) -> list[tuple[Decimal, Decimal]]:
    rates = _rates(plan.rules, plan.start, plan.contractual, plan.effective)
    return [
        (principal if j == plan.n - 1 else Decimal(0), money(CTX.multiply(principal, rates[j]), plan.scale, plan.mode))
        for j in range(plan.n)
    ]


def _bullet(plan: _Plan, principal: Decimal) -> list[tuple[Decimal, Decimal]]:
    """One payment n periods after the start; simple interest accrues over those n periods (first_due is 1)."""
    freq, cal = plan.rules.frequency.code, plan.rules.calendar
    grid = [add_periods(plan.start, freq, k) for k in range(1, plan.n + 1)]
    nw, hol = frozenset(cal.non_working_weekdays), frozenset(cal.holidays)
    grid_eff = [effective_due(d, cal.adjustment, nw, hol) for d in grid]
    total = sum(_rates(plan.rules, plan.start, grid, grid_eff), Decimal(0))
    return [(principal, money(CTX.multiply(principal, total), plan.scale, plan.mode))]


def _flat(plan: _Plan, principal: Decimal) -> list[tuple[Decimal, Decimal]]:
    rate, rules = plan.rules.method.rate, plan.rules
    pct = _dec(rate.value) / HUNDRED
    if rate.type == "total_over_term":
        total_rate = pct
    else:
        per = pct if rate.type == "per_period" else CTX.divide(pct, Decimal(PERIODS_PER_YEAR[rules.frequency.code]))
        total_rate = CTX.multiply(per, Decimal(rules.first_due.periods_after_start + plan.n - 1))
    interest_total = money(CTX.multiply(principal, total_rate), plan.scale, plan.mode)
    return _even_split(principal, interest_total, plan.n, plan.scale, plan.mode)


def _fixed_total_cost(plan: _Plan, principal: Decimal) -> list[tuple[Decimal, Decimal]]:
    cost = plan.rules.method.total_cost
    if cost.type == "fixed_amount":
        total = _dec(cost.amounts[plan.currency])
    else:
        total = money(CTX.multiply(principal, _dec(cost.percent)) / HUNDRED, plan.scale, plan.mode)
    return _even_split(principal, total, plan.n, plan.scale, plan.mode)


_METHODS = {
    "reducing_balance": _reducing_balance,
    "interest_only": _interest_only,
    "bullet": _bullet,
    "flat": _flat,
    "fixed_total_cost": _fixed_total_cost,
}


def _simulate(
    rules: RulesIn,
    *,
    currency: str,
    exponent: int,
    limits: tuple[Decimal, Decimal],
    principal: Decimal,
    term_periods: int,
    start_date: date,
) -> dict:
    """Return the full projected schedule. ``rules`` must already have passed ``validate_rules``."""
    scale, mode, method = rules.rounding.scale, rules.rounding.mode, rules.method.code
    if not (rules.term.min_periods <= term_periods <= rules.term.max_periods):
        raise EngineError(f"El plazo debe estar entre {rules.term.min_periods} y {rules.term.max_periods} periodos.")
    if not (limits[0] <= principal <= limits[1]):
        raise EngineError("El monto esta fuera de los limites del producto para esa moneda.")
    if principal != money(principal, exponent, "down"):
        raise EngineError("El monto tiene mas decimales de los que admite la moneda.")

    n, f, freq, cal = term_periods, rules.first_due.periods_after_start, rules.frequency.code, rules.calendar
    non_working, holidays = frozenset(cal.non_working_weekdays), frozenset(cal.holidays)
    if method == "bullet":
        contractual = [add_periods(start_date, freq, n)]
    else:
        contractual = [add_periods(start_date, freq, f + i) for i in range(n)]
    effective = [effective_due(d, cal.adjustment, non_working, holidays) for d in contractual]
    if effective[0] <= start_date or any(b <= a for a, b in zip(effective, effective[1:], strict=False)):
        raise EngineError("El ajuste de calendario produce fechas de vencimiento no crecientes o anteriores al inicio.")

    plan = _Plan(rules, currency, start_date, n, contractual, effective, scale, mode)
    core = _METHODS[method](plan, principal)

    origination, per_installment = [], []
    for fee in rules.fees or []:
        if fee.timing == "at_origination":
            origination.append((fee, _fee_amount(fee, principal, currency, scale, mode)))
        elif fee.timing == "per_installment":
            per_installment.append(fee)

    grace_days = rules.grace.delinquency_grace_days
    delinquency_on = bool(rules.delinquency and rules.delinquency.enabled)
    rows, balance, sum_fees = [], principal, Decimal(0)
    for idx, (p, i) in enumerate(core):
        opening = balance
        balance = opening - p
        fee_total = Decimal(0)
        for fee in per_installment:
            base = (p + i) if fee.base == "installment_amount" else opening
            fee_total += _fee_amount(fee, base, currency, scale, mode)
        sum_fees += fee_total
        mora_from = effective[idx] if cal.delinquency_start_basis == "effective_due_date" else contractual[idx]
        rows.append(
            {
                "period": idx + 1,
                "contractual_date": contractual[idx].isoformat(),
                "due_date": effective[idx].isoformat(),
                "delinquency_starts_on": (mora_from + timedelta(days=1 + grace_days)).isoformat()
                if delinquency_on
                else None,
                "opening_balance": fmt(opening, scale),
                "principal": fmt(p, scale),
                "interest": fmt(i, scale),
                "fees": fmt(fee_total, scale),
                "total": fmt(p + i + fee_total, scale),
                "closing_balance": fmt(balance, scale),
            }
        )
    sum_p = sum((p for p, _ in core), Decimal(0))
    sum_i = sum((i for _, i in core), Decimal(0))
    if sum_p != principal or balance != 0:
        raise EngineError("Invariante violado: el capital del calendario no suma el principal.")  # never silent
    orig_total = sum((a for _, a in origination), Decimal(0))
    deducted = sum((a for fee, a in origination if fee.settlement == "deducted_from_disbursement"), Decimal(0))
    out = {
        "currency": currency,
        "principal": fmt(principal, scale),
        "term_periods": n,
        "start_date": start_date.isoformat(),
        "schedule": rows,
        "origination_fees": [
            {"code": fee.code, "amount": fmt(a, scale), "settlement": fee.settlement} for fee, a in origination
        ],
        "totals": {
            "principal": fmt(sum_p, scale),
            "interest": fmt(sum_i, scale),
            "installment_fees": fmt(sum_fees, scale),
            "origination_fees": fmt(orig_total, scale),
            "total_scheduled": fmt(sum_p + sum_i + sum_fees, scale),
            "net_disbursement": fmt(principal - deducted, scale),
        },
    }
    out["result_digest"] = hashlib.sha256(
        json.dumps(out, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return out


def simulate(rules: RulesIn, **kwargs) -> dict:
    """Run under the module's explicit decimal context so no thread/global setting can change a result."""
    with localcontext(CTX):
        return _simulate(rules, **kwargs)
