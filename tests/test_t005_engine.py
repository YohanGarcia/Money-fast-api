"""T-005 pure engine/validation/hash tests (no DB): schedules, calendar A/B/C, rounding, hash, validation."""

import copy
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.modules.credit import engine
from app.modules.credit.rules import RulesIn, canonical_json, compute_rules_hash
from app.modules.credit.validation import validate_rules
from tests import pg_env  # noqa: F401  (must precede app imports)

CUR = [{"code": "DOP", "min_amount": "100", "max_amount": "500000"}]
ENABLED = {"DOP": 2, "USD": 2}


def rules(**over) -> dict:
    r = {
        "schema_version": 1,
        "method": {"code": "reducing_balance", "rate": {"type": "annual", "value": "12"}, "time_basis": "periodic"},
        "frequency": {"code": "monthly", "monthly_day_rule": "same_day_clamped"},
        "term": {"min_periods": 1, "max_periods": 60},
        "first_due": {"periods_after_start": 1},
        "rounding": {"scale": 2, "mode": "half_up", "moment": "per_installment", "residual": "last_installment"},
        "calendar": {
            "source": "product",
            "timezone": "America/Santo_Domingo",
            "non_working_weekdays": [5, 6],
            "holidays": ["2026-03-20"],
            "adjustment": "keep_original",
            "delinquency_start_basis": "effective_due_date",
            "accrual_basis": "contractual_dates",
        },
        "grace": {"delinquency_grace_days": 3, "principal_grace_periods": 0},
        "delinquency": {
            "enabled": True,
            "fee": {"kind": "percent", "percent": "5"},
            "base": "overdue_installment_total",
            "frequency": "once",
            "cap": {"type": "none"},
            "late_on_late": False,
        },
        "allocation": {
            "order": ["fees", "delinquency", "interest", "principal"],
            "apply_by": "installment_then_component",
        },
        "prepayment": {
            "allowed": True,
            "partial_allowed": True,
            "partial_effect": "reduce_term",
            "future_interest": "recalculate",
        },
        "payoff": {
            "interest_basis": "accrued_to_date",
            "include_fees": True,
            "include_delinquency": True,
            "discount_allowed": False,
        },
        "fees": [],
        "restructure": {"allowed": False},
        "refinance": {"allowed": False},
    }
    for k, v in over.items():
        r[k] = v
    return r


def valid(raw=None, cur=None):
    v = validate_rules(raw or rules(), cur or CUR, ENABLED)
    assert v.issues == [], [i.as_dict() for i in v.issues]
    return v.rules


def sim(r, principal="10000", term=3, start=date(2026, 1, 15), currency="DOP"):
    return engine.simulate(
        valid(r),
        currency=currency,
        exponent=2,
        limits=(Decimal("100"), Decimal("500000")),
        principal=Decimal(principal),
        term_periods=term,
        start_date=start,
    )


def col(res, name):
    return [row[name] for row in res["schedule"]]


# ============ Calculation capability (M01-M05) =====================================================
def test_m01_reducing_balance_matches_hand_computed_annuity():
    res = sim(rules())  # 12 % annual / 12 periods = 1 % per period: payment 10000*.01/(1-1.01^-3) = 3400.22
    assert col(res, "interest") == ["100.00", "67.00", "33.67"]
    assert col(res, "principal") == ["3300.22", "3333.22", "3366.56"]
    assert col(res, "total") == ["3400.22", "3400.22", "3400.23"]  # the residual cent lands on the LAST installment
    assert col(res, "closing_balance") == ["6699.78", "3366.56", "0.00"]
    assert res["totals"]["total_scheduled"] == "10200.67"
    assert col(res, "due_date") == ["2026-02-15", "2026-03-15", "2026-04-15"]


def test_m02_flat_interest_is_an_explicit_method_not_a_default():
    flat = rules(method={"code": "flat", "rate": {"type": "total_over_term", "value": "10"}})
    res = sim(flat)
    assert col(res, "interest") == ["333.33", "333.33", "333.34"] and res["totals"]["interest"] == "1000.00"
    assert col(res, "principal") == ["3333.33", "3333.33", "3333.34"] and res["totals"]["total_scheduled"] == "11000.00"
    per_period = rules(method={"code": "flat", "rate": {"type": "per_period", "value": "2"}})
    assert sim(per_period)["totals"]["interest"] == "600.00"  # 2 % * 3 periods
    annual = rules(method={"code": "flat", "rate": {"type": "annual", "value": "12"}, "time_basis": "periodic"})
    assert sim(annual)["totals"]["interest"] == "300.00"  # 12 % / 12 * 3 periods


def test_m03_interest_only_pays_principal_at_the_end():
    res = sim(
        rules(
            method={"code": "interest_only", "rate": {"type": "annual", "value": "12"}, "time_basis": "periodic"},
            prepayment={"allowed": False},
        )
    )
    assert col(res, "interest") == ["100.00"] * 3 and col(res, "principal") == ["0.00", "0.00", "10000.00"]
    assert col(res, "closing_balance") == ["10000.00", "10000.00", "0.00"]


def test_m04_bullet_is_a_single_payment_with_simple_interest():
    res = sim(
        rules(
            method={"code": "bullet", "rate": {"type": "annual", "value": "12"}, "time_basis": "periodic"},
            prepayment={"allowed": False},
        )
    )
    assert len(res["schedule"]) == 1
    row = res["schedule"][0]
    assert (row["principal"], row["interest"], row["total"], row["due_date"]) == (
        "10000.00",
        "300.00",
        "10300.00",
        "2026-04-15",
    )


def test_m05_fixed_total_cost_spreads_a_declared_cost():
    ftc = rules(
        method={"code": "fixed_total_cost", "total_cost": {"type": "percent_of_principal", "percent": "6"}},
        prepayment={"allowed": False},
    )
    res = sim(ftc)
    assert col(res, "interest") == ["200.00"] * 3 and col(res, "total") == ["3533.33", "3533.33", "3533.34"]
    fixed = rules(
        method={"code": "fixed_total_cost", "total_cost": {"type": "fixed_amount", "amounts": {"DOP": "750"}}},
        prepayment={"allowed": False},
    )
    assert sim(fixed)["totals"]["interest"] == "750.00"


def test_actual_365_basis_and_first_period_span_and_principal_grace():
    act = rules(
        method={"code": "interest_only", "rate": {"type": "annual", "value": "36.5"}, "time_basis": "actual_365"},
        frequency={"code": "weekly"},
        prepayment={"allowed": False},
    )
    assert col(sim(act), "interest") == ["70.00"] * 3  # 36.5 % * 7 / 365 = 0.7 %
    span = rules(
        method={"code": "interest_only", "rate": {"type": "per_period", "value": "1"}},
        first_due={"periods_after_start": 2},
        prepayment={"allowed": False},
    )
    assert col(sim(span), "interest") == ["200.00", "100.00", "100.00"]  # first installment covers 2 periods
    grace = rules(
        method={"code": "reducing_balance", "rate": {"type": "per_period", "value": "1"}},
        grace={"delinquency_grace_days": 0, "principal_grace_periods": 1},
        term={"min_periods": 2, "max_periods": 60},
    )
    res = sim(grace)
    assert col(res, "principal")[0] == "0.00" and col(res, "interest")[0] == "100.00"
    assert sum(Decimal(x) for x in col(res, "principal")) == Decimal("10000.00")


@pytest.mark.parametrize(
    "method_cfg",
    [
        {"code": "reducing_balance", "rate": {"type": "annual", "value": "23.99"}, "time_basis": "periodic"},
        {"code": "flat", "rate": {"type": "total_over_term", "value": "7.77"}},
        {"code": "interest_only", "rate": {"type": "per_period", "value": "1.37"}},
        {"code": "bullet", "rate": {"type": "per_period", "value": "2.5"}},
        {"code": "fixed_total_cost", "total_cost": {"type": "percent_of_principal", "percent": "3.333"}},
    ],
)
@pytest.mark.parametrize("principal", ["100", "1234.56", "99999.99"])
@pytest.mark.parametrize("term", [1, 5, 17])
def test_invariants_principal_sums_exactly_and_balance_ends_at_zero(method_cfg, principal, term):
    res = sim(rules(method=method_cfg, prepayment={"allowed": False}), principal=principal, term=term)
    assert (
        res["totals"]["principal"] == f"{Decimal(principal):.2f}" and res["schedule"][-1]["closing_balance"] == "0.00"
    )
    for row in res["schedule"]:  # every row adds up and nothing is negative
        assert Decimal(row["principal"]) + Decimal(row["interest"]) + Decimal(row["fees"]) == Decimal(row["total"])
        assert min(Decimal(row[k]) for k in ("principal", "interest", "closing_balance")) >= 0


def test_fees_origination_deducted_and_per_installment_are_separate_lines():
    fees = [
        {
            "code": "ADM",
            "name": "Gastos de admin",
            "kind": "percent",
            "percent": "2",
            "base": "principal",
            "timing": "at_origination",
            "settlement": "deducted_from_disbursement",
        },
        {"code": "SEG", "name": "Seguro", "kind": "fixed", "amounts": {"DOP": "25"}, "timing": "per_installment"},
    ]
    res = sim(rules(fees=fees))
    assert res["origination_fees"] == [{"code": "ADM", "amount": "200.00", "settlement": "deducted_from_disbursement"}]
    assert res["totals"]["net_disbursement"] == "9800.00" and res["totals"]["installment_fees"] == "75.00"
    assert col(res, "fees") == ["25.00"] * 3 and col(res, "total")[0] == "3425.22"


# ============ Rounding ============================================================================
@pytest.mark.parametrize(
    "mode,expected", [("half_up", "0.13"), ("half_even", "0.12"), ("down", "0.12"), ("up", "0.13")]
)
def test_rounding_modes_are_explicit_and_deterministic(mode, expected):
    r = rules(
        method={"code": "interest_only", "rate": {"type": "per_period", "value": "0.125"}},
        rounding={"scale": 2, "mode": mode, "moment": "per_installment", "residual": "last_installment"},
        prepayment={"allowed": False},
    )
    assert sim(r, principal="100", term=1)["schedule"][0]["interest"] == expected  # 100 * 0.125 % = 0.125 exactly


def test_same_inputs_same_digest_and_no_float_anywhere():
    a, b = sim(rules()), sim(copy.deepcopy(rules()))
    assert a == b and a["result_digest"] == b["result_digest"]
    assert sim(rules(), term=4)["result_digest"] != a["result_digest"]

    def walk(node):
        if isinstance(node, dict):
            [walk(v) for v in node.values()]
        elif isinstance(node, list):
            [walk(v) for v in node]
        else:
            assert not isinstance(node, float)

    walk(a)
    with pytest.raises(ValidationError):  # a JSON number is never accepted for money/rates
        RulesIn.model_validate(rules(method={"code": "flat", "rate": {"type": "per_period", "value": 2.5}}))
    with pytest.raises(TypeError):  # and the canonical serialiser refuses floats outright
        canonical_json({"rules": {"method": {"rate": {"value": 0.1}}}})


def test_engine_rejects_out_of_range_inputs():
    with pytest.raises(engine.EngineError):
        sim(rules(), principal="50")  # below the product minimum
    with pytest.raises(engine.EngineError):
        sim(rules(), term=61)
    with pytest.raises(engine.EngineError):
        sim(rules(), principal="1000.001")  # more decimals than the currency allows


# ============ Calendar (D01-D03) ==================================================================
def weekly(adjustment, **cal):
    c = rules()["calendar"] | {"adjustment": adjustment} | cal
    return rules(
        frequency={"code": "weekly"},
        calendar=c,
        method={"code": "interest_only", "rate": {"type": "per_period", "value": "1"}},
        prepayment={"allowed": False},
    )


def test_d01_d02_d03_calendar_policies_on_a_holiday_and_a_weekend():
    # start Fri 2026-03-06: due Fri 13, Fri 20 (HOLIDAY), Fri 27
    keep, nxt, prev = (
        sim(weekly(a), start=date(2026, 3, 6)) for a in ("keep_original", "next_business_day", "previous_business_day")
    )
    assert col(keep, "due_date") == ["2026-03-13", "2026-03-20", "2026-03-27"]
    assert col(nxt, "due_date") == ["2026-03-13", "2026-03-23", "2026-03-27"]  # Fri holiday -> Mon
    assert col(prev, "due_date") == ["2026-03-13", "2026-03-19", "2026-03-27"]  # Fri holiday -> Thu
    assert col(nxt, "contractual_date") == col(keep, "contractual_date")  # the contractual date is never rewritten
    # start Sat 2026-03-07: due Sat 14 / 21 / 28 (weekend)
    n2 = sim(weekly("next_business_day", holidays=[]), start=date(2026, 3, 7))
    p2 = sim(weekly("previous_business_day", holidays=[]), start=date(2026, 3, 7))
    assert col(n2, "due_date") == ["2026-03-16", "2026-03-23", "2026-03-30"]
    assert col(p2, "due_date") == ["2026-03-13", "2026-03-20", "2026-03-27"]


def test_delinquency_start_and_non_monotonic_dates():
    res = sim(weekly("next_business_day"), start=date(2026, 3, 6))
    assert col(res, "delinquency_starts_on")[1] == "2026-03-27"  # effective 2026-03-23 + 1 day + 3 grace days
    with pytest.raises(engine.EngineError):  # daily + "next business day": Sat/Sun/Mon collapse onto one date
        sim(
            rules(
                frequency={"code": "daily"},
                method={"code": "interest_only", "rate": {"type": "per_period", "value": "1"}},
                calendar=rules()["calendar"] | {"adjustment": "next_business_day"},
                prepayment={"allowed": False},
            ),
            start=date(2026, 3, 5),
            term=4,
        )


def test_monthly_dates_are_anchored_to_the_start_day_and_clamped():
    res = sim(rules(), start=date(2026, 1, 31), term=3)
    assert col(res, "contractual_date") == ["2026-02-28", "2026-03-31", "2026-04-30"]  # no drift after February


# ============ Hash (V07) ==========================================================================
def test_v07_rules_hash_is_stable_for_the_same_logical_rules_and_changes_otherwise():
    a = valid(rules())
    base = compute_rules_hash(a, CUR)
    shuffled = rules(
        method={"time_basis": "periodic", "rate": {"value": "12.0000", "type": "annual"}, "code": "reducing_balance"}
    )
    shuffled["calendar"] = shuffled["calendar"] | {"holidays": ["2026-03-20", "2026-01-01"]}
    reordered = copy.deepcopy(shuffled)
    reordered["calendar"]["holidays"] = ["2026-01-01", "2026-03-20"]
    assert compute_rules_hash(valid(shuffled), CUR) == compute_rules_hash(valid(reordered), CUR)
    assert (
        compute_rules_hash(valid(rules(method=rules()["method"] | {"rate": {"type": "annual", "value": "12.00"}})), CUR)
        == base
    )
    assert compute_rules_hash(a, [{"code": "DOP", "min_amount": "100.00", "max_amount": "500000.0000"}]) == base
    assert base.startswith("sha256:") and len(base) == 71
    changed = rules(rounding=rules()["rounding"] | {"mode": "half_even"})
    assert compute_rules_hash(valid(changed), CUR) != base
    assert compute_rules_hash(a, [{"code": "DOP", "min_amount": "100", "max_amount": "500001"}]) != base
    reorder_alloc = rules(
        allocation={"order": ["interest", "fees", "delinquency", "principal"], "apply_by": "installment_then_component"}
    )
    assert compute_rules_hash(valid(reorder_alloc), CUR) != base  # allocation order IS part of the rule
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


# ============ Validation (C01-C06 + more) =========================================================
def codes(raw, cur=None, enabled=None):
    return {(i.path, i.code) for i in validate_rules(raw, CUR if cur is None else cur, enabled or ENABLED).issues}


def test_c01_missing_calendar_policy_blocks():
    raw = rules()
    del raw["calendar"]
    assert ("rules.calendar", "missing") in codes(raw)
    partial = rules(
        calendar={"source": "product", "timezone": "America/Santo_Domingo", "non_working_weekdays": [], "holidays": []}
    )
    assert {("rules.calendar.adjustment", "missing"), ("rules.calendar.accrual_basis", "missing")} <= codes(partial)


def test_c02_currency_not_enabled_or_empty():
    assert ("currencies.EUR", "currency_not_enabled") in codes(
        rules(), [{"code": "EUR", "min_amount": "1", "max_amount": "2"}]
    )
    assert ("currencies", "missing") in codes(rules(), [])


def test_c03_invalid_timezone():
    assert ("rules.calendar.timezone", "invalid_timezone") in codes(
        rules(calendar=rules()["calendar"] | {"timezone": "Mars/Olympus"})
    )


def test_c04_min_greater_than_max():
    assert ("rules.term", "min_greater_than_max") in codes(rules(term={"min_periods": 12, "max_periods": 6}))
    assert ("currencies.DOP", "invalid_range") in codes(
        rules(), [{"code": "DOP", "min_amount": "500", "max_amount": "100"}]
    )


def test_c05_incompatible_method_settings():
    r1 = codes(rules(method={"code": "reducing_balance", "rate": {"type": "total_over_term", "value": "5"}}))
    assert ("rules.method.rate.type", "incompatible") in r1
    assert ("rules.method.time_basis", "missing") in codes(
        rules(method={"code": "reducing_balance", "rate": {"type": "annual", "value": "5"}})
    )
    daily_annual = codes(rules(frequency={"code": "daily"}))
    assert ("rules.method.time_basis", "incompatible") in daily_annual  # annual + daily needs actual/N
    bullet = codes(
        rules(
            method={"code": "bullet", "rate": {"type": "per_period", "value": "1"}},
            first_due={"periods_after_start": 2},
            prepayment={"allowed": False},
        )
    )
    assert ("rules.first_due.periods_after_start", "incompatible") in bullet
    ftc = codes(
        rules(
            method={
                "code": "fixed_total_cost",
                "rate": {"type": "per_period", "value": "1"},
                "total_cost": {"type": "percent_of_principal", "percent": "5"},
            },
            prepayment={"allowed": False},
        )
    )
    assert ("rules.method", "incompatible") in ftc
    assert ("rules.grace.principal_grace_periods", "incompatible") in codes(
        rules(
            method={"code": "flat", "rate": {"type": "per_period", "value": "1"}},
            grace={"delinquency_grace_days": 0, "principal_grace_periods": 1},
            prepayment={"allowed": False},
        )
    )
    assert ("rules.method.time_basis", "incompatible") in codes(
        rules(
            method={"code": "flat", "rate": {"type": "annual", "value": "12"}, "time_basis": "actual_365"},
            prepayment={"allowed": False},
        )
    )
    assert ("rules.frequency.monthly_day_rule", "inconsistent") in codes(
        rules(frequency={"code": "weekly", "monthly_day_rule": "same_day_clamped"})
    )
    assert ("rules.prepayment.partial_effect", "incompatible") in codes(
        rules(method={"code": "bullet", "rate": {"type": "per_period", "value": "1"}})
    )


def test_c06_rounding_config_required_and_scale_must_match_currency():
    raw = rules()
    raw["rounding"] = {"mode": "half_up"}
    assert {
        ("rules.rounding.scale", "missing"),
        ("rules.rounding.moment", "missing"),
        ("rules.rounding.residual", "missing"),
    } <= codes(raw)
    assert ("rules.rounding.scale", "scale_mismatch") in codes(rules(rounding=rules()["rounding"] | {"scale": 0}))
    none = rules()
    del none["rounding"]
    assert ("rules.rounding", "missing") in codes(none)


def test_negative_values_unknown_methods_and_unknown_fields_are_rejected():
    assert any(
        c == "invalid_value"
        for _, c in codes(
            rules(
                method={"code": "reducing_balance", "rate": {"type": "annual", "value": "-1"}, "time_basis": "periodic"}
            )
        )
    )
    assert any(
        c == "invalid_value"
        for _, c in codes(rules(method={"code": "magic", "rate": {"type": "annual", "value": "1"}}))
    )
    assert any(c == "invalid_value" for _, c in codes(rules() | {"surprise": 1}))


def test_allocation_delinquency_prepayment_fee_and_refinance_consistency():
    dup = codes(
        rules(allocation={"order": ["fees", "fees", "interest", "principal"], "apply_by": "installment_then_component"})
    )
    assert ("rules.allocation.order", "invalid_order") in dup
    mora = rules(delinquency=rules()["delinquency"] | {"late_on_late": True})
    assert ("rules.delinquency.late_on_late", "blocked_by_spec") in codes(mora)
    nofee = rules(
        delinquency={"enabled": True, "base": "overdue_principal", "frequency": "once", "late_on_late": False}
    )
    assert {("rules.delinquency.fee", "missing"), ("rules.delinquency.cap", "missing")} <= codes(nofee)
    assert ("rules.prepayment", "inconsistent") in codes(
        rules(prepayment={"allowed": False, "partial_effect": "reduce_term"})
    )
    assert ("rules.prepayment.fee_code", "unknown_fee") in codes(
        rules(prepayment=rules()["prepayment"] | {"fee_code": "NOPE"})
    )
    badfee = [
        {"code": "X", "name": "x", "kind": "percent", "percent": "1", "base": "principal", "timing": "per_installment"}
    ]
    assert ("rules.fees[0].base", "incompatible") in codes(rules(fees=badfee))
    assert ("rules.refinance", "missing") in codes(rules(refinance={"allowed": True}))
    assert ("rules.restructure.audit_required", "inconsistent") in codes(rules(restructure={"allowed": True}))
    assert ("rules.delinquency.fee.amounts", "currency_amounts") in codes(
        rules(delinquency=rules()["delinquency"] | {"fee": {"kind": "fixed", "amounts": {"USD": "10"}}})
    )
    assert any("BLOCKED_BY_SPEC" in w for w in validate_rules(rules(), CUR, ENABLED).warnings)
