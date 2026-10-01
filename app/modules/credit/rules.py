"""Typed shape of a credit product's rules, canonical serialisation and the rules hash (T-005 §21-22).

Every field is optional here on purpose: a *draft* may be incomplete. Completeness is the job of
``validation.validate_rules`` and it is what blocks publication. Money and rates travel as decimal STRINGS
(a JSON number is rejected, so a float can never reach a calculation). There are no defaults: an absent
setting stays absent and the validator reports it.
"""

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

HASH_PREFIX = "sha256:"
HASH_SCHEMA = "fastmoney.credit-rules.v1"
RULES_SCHEMA_VERSION = 1

DecStr = Annotated[str, StringConstraints(pattern=r"^\d{1,12}(\.\d{1,8})?$")]  # non-negative, no exponent
Code = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9_-]{0,29}$")]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
AmountMap = dict[CurrencyCode, DecStr]

METHODS = ("reducing_balance", "flat", "interest_only", "bullet", "fixed_total_cost")
FREQUENCIES = ("daily", "weekly", "biweekly", "monthly")
PERIODS_PER_YEAR = {"weekly": 52, "biweekly": 26, "monthly": 12}  # daily has none: needs an actual/N basis
ALLOCATION_COMPONENTS = ("fees", "delinquency", "interest", "principal")
ROUNDING_MODES = ("half_up", "half_even", "down", "up")


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RateIn(_M):
    type: Literal["per_period", "annual", "total_over_term"] | None = None
    value: DecStr | None = None  # percent, e.g. "12.5" = 12.5 %


class TotalCostIn(_M):
    type: Literal["percent_of_principal", "fixed_amount"] | None = None
    percent: DecStr | None = None
    amounts: AmountMap | None = None


class MethodIn(_M):
    code: Literal["reducing_balance", "flat", "interest_only", "bullet", "fixed_total_cost"] | None = None
    rate: RateIn | None = None
    time_basis: Literal["periodic", "actual_360", "actual_365"] | None = None
    total_cost: TotalCostIn | None = None


class FrequencyIn(_M):
    code: Literal["daily", "weekly", "biweekly", "monthly"] | None = None
    monthly_day_rule: Literal["same_day_clamped"] | None = None


class TermIn(_M):
    min_periods: int | None = Field(default=None, ge=1, le=3660)
    max_periods: int | None = Field(default=None, ge=1, le=3660)


class FirstDueIn(_M):
    periods_after_start: int | None = Field(default=None, ge=1, le=60)


class RoundingIn(_M):
    scale: int | None = Field(default=None, ge=0, le=4)
    mode: Literal["half_up", "half_even", "down", "up"] | None = None
    moment: Literal["per_installment"] | None = None
    residual: Literal["last_installment"] | None = None


class CalendarIn(_M):
    source: Literal["product"] | None = None  # tenant/branch calendar catalogue: DEFER (T-016)
    timezone: str | None = Field(default=None, max_length=64)
    non_working_weekdays: list[int] | None = None  # Monday=0 .. Sunday=6
    holidays: list[date] | None = None
    adjustment: Literal["keep_original", "next_business_day", "previous_business_day"] | None = None
    delinquency_start_basis: Literal["effective_due_date", "contractual_due_date"] | None = None
    accrual_basis: Literal["contractual_dates", "effective_dates"] | None = None


class GraceIn(_M):
    delinquency_grace_days: int | None = Field(default=None, ge=0, le=365)
    principal_grace_periods: int | None = Field(default=None, ge=0, le=60)


class LateFeeIn(_M):
    kind: Literal["percent", "fixed"] | None = None
    percent: DecStr | None = None
    amounts: AmountMap | None = None


class CapIn(_M):
    type: Literal["none", "percent_of_base", "fixed_amount"] | None = None
    percent: DecStr | None = None
    amounts: AmountMap | None = None


class DelinquencyIn(_M):
    enabled: bool | None = None
    fee: LateFeeIn | None = None
    base: Literal["overdue_installment_total", "overdue_principal", "overdue_principal_and_interest"] | None = None
    frequency: Literal["once", "per_day", "per_week", "per_month"] | None = None
    cap: CapIn | None = None
    late_on_late: bool | None = None  # mora over mora: never approved (DF-01 §14) -> must be false


class AllocationIn(_M):
    order: list[Literal["fees", "delinquency", "interest", "principal"]] | None = None
    apply_by: Literal["installment_then_component", "component_then_installment"] | None = None


class PrepaymentIn(_M):
    allowed: bool | None = None
    partial_allowed: bool | None = None
    partial_effect: Literal["reduce_installment", "reduce_term", "reduce_outstanding_principal"] | None = None
    future_interest: Literal["recalculate", "keep_scheduled", "waive_unaccrued"] | None = None
    fee_code: Code | None = None


class PayoffIn(_M):
    interest_basis: Literal["accrued_to_date", "full_scheduled"] | None = None
    include_fees: bool | None = None
    include_delinquency: bool | None = None
    discount_allowed: bool | None = None


class FeeRuleIn(_M):
    code: Code | None = None
    name: str | None = Field(default=None, max_length=80)
    kind: Literal["fixed", "percent"] | None = None
    percent: DecStr | None = None
    amounts: AmountMap | None = None
    base: Literal["principal", "installment_amount", "opening_balance", "prepaid_amount", "payoff_amount"] | None = None
    timing: Literal["at_origination", "per_installment", "at_prepayment", "at_payoff"] | None = None
    settlement: Literal["deducted_from_disbursement", "paid_separately"] | None = None


class RestructureIn(_M):
    allowed: bool | None = None
    audit_required: bool | None = None


class RefinanceIn(_M):
    allowed: bool | None = None
    new_contract_required: bool | None = None
    old_loan_treatment: Literal["settle", "reclassify"] | None = None
    audit_required: bool | None = None


class RulesIn(_M):
    schema_version: Literal[1] = RULES_SCHEMA_VERSION
    method: MethodIn | None = None
    frequency: FrequencyIn | None = None
    term: TermIn | None = None
    first_due: FirstDueIn | None = None
    rounding: RoundingIn | None = None
    calendar: CalendarIn | None = None
    grace: GraceIn | None = None
    delinquency: DelinquencyIn | None = None
    allocation: AllocationIn | None = None
    prepayment: PrepaymentIn | None = None
    payoff: PayoffIn | None = None
    fees: list[FeeRuleIn] | None = None
    restructure: RestructureIn | None = None
    refinance: RefinanceIn | None = None


class CurrencyLimitIn(_M):
    code: CurrencyCode
    min_amount: DecStr
    max_amount: DecStr


# --- canonical form & hash --------------------------------------------------------------------------
def canonical_decimal(value: str | Decimal) -> str:
    """Positional, exponent-free, trailing zeros trimmed: "10", "10.0", "10.00" are one logical value."""
    d = Decimal(value)
    if d == d.to_integral_value():
        return format(d.quantize(Decimal(1)), "f")
    return format(d.normalize(), "f")


def _canon(node, key: str | None = None):
    if isinstance(node, BaseModel):
        return _canon(node.model_dump(exclude_none=True))
    if isinstance(node, dict):
        return {k: _canon(v, k) for k, v in sorted(node.items()) if v is not None}
    if isinstance(node, list):
        items = [_canon(v) for v in node]
        # set-like lists: order carries no meaning, so it must not change the hash
        if key in ("holidays", "non_working_weekdays"):
            return sorted(set(items))
        if key == "fees":
            return sorted(items, key=lambda f: f.get("code", ""))
        return items  # e.g. allocation.order: order IS the rule
    if isinstance(node, bool) or node is None:
        return node
    if isinstance(node, date):
        return node.isoformat()
    if isinstance(node, (int, str)):
        if isinstance(node, str) and key in _DECIMAL_KEYS:
            return canonical_decimal(node)
        return node
    raise TypeError(f"Valor no serializable de forma canonica: {type(node).__name__}")  # floats land here


_DECIMAL_KEYS = frozenset({"value", "percent", "min_amount", "max_amount"})


def canonical_json(content) -> str:
    """Deterministic UTF-8-safe JSON: sorted keys, no whitespace, ASCII only. Amount maps are canonicalised too."""
    return json.dumps(_canon_amounts(_canon(content)), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _canon_amounts(node, key: str | None = None):
    if isinstance(node, dict):
        if key == "amounts":
            return {k: canonical_decimal(v) for k, v in node.items()}
        return {k: _canon_amounts(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_canon_amounts(v, key) for v in node]
    return node


def compute_rules_hash(rules: dict | RulesIn, currencies: list[dict]) -> str:
    """Hash of the logical rules + currency limits. Independent of ids, version number and key order."""
    content = {
        "rules": rules if isinstance(rules, dict) else rules.model_dump(exclude_none=True),
        "currencies": sorted(
            ({"code": c["code"], "min_amount": c["min_amount"], "max_amount": c["max_amount"]} for c in currencies),
            key=lambda c: c["code"],
        ),
    }
    payload = (HASH_SCHEMA + "\n" + canonical_json(content)).encode("utf-8")
    return HASH_PREFIX + hashlib.sha256(payload).hexdigest()


def parse_rules(raw: dict) -> RulesIn:
    return RulesIn.model_validate(raw)
