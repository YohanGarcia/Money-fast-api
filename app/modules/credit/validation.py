"""Pre-publication validation (T-005 §20). Nothing is defaulted: an absent setting is an issue, and any issue
blocks publication. ``validate_rules`` is pure: it reads no DB (the tenant's enabled currencies are passed in).
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from app.core.time import get_zone
from app.modules.credit.rules import ALLOCATION_COMPONENTS, RulesIn

REQUIRED_SECTIONS = (
    "method",
    "frequency",
    "term",
    "first_due",
    "rounding",
    "calendar",
    "grace",
    "delinquency",
    "allocation",
    "prepayment",
    "payoff",
    "fees",
    "restructure",
    "refinance",
)
LEGAL_NOTICE = (
    "BLOCKED_BY_SPEC: topes legales de interes/mora, cargos permitidos y tratamiento regulatorio del prepago "
    "no estan validados juridicamente (DF-01 §28); validar antes de produccion."
)


@dataclass(frozen=True)
class Issue:
    path: str
    code: str
    message: str

    def as_dict(self) -> dict:
        return {"path": self.path, "code": self.code, "message": self.message}


@dataclass
class Validation:
    issues: list[Issue]
    warnings: list[str]
    rules: RulesIn | None  # parsed rules (set whenever the shape itself is parseable)

    @property
    def valid(self) -> bool:
        return not self.issues


def _dec(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        return None


def validate_rules(raw: dict, currencies: list[dict], enabled: dict[str, int]) -> Validation:
    """``currencies``: [{code, min_amount, max_amount}] of the version. ``enabled``: tenant's currently enabled
    currency codes -> decimal places (exponent)."""
    issues: list[Issue] = []

    def bad(path: str, code: str, message: str) -> None:
        issues.append(Issue(path, code, message))

    try:
        rules = RulesIn.model_validate(raw)
    except ValidationError as exc:
        for err in exc.errors():
            bad("rules." + ".".join(str(p) for p in err["loc"]), "invalid_value", err["msg"])
        return Validation(issues, [], None)

    for section in REQUIRED_SECTIONS:
        if getattr(rules, section) is None:
            bad(
                f"rules.{section}", "missing", f"Falta la seccion obligatoria '{section}' (no hay valores por defecto)."
            )

    codes = [c["code"] for c in currencies]
    exponents: dict[str, int] = {}
    if not codes:
        bad("currencies", "missing", "El producto debe declarar al menos una moneda.")
    if len(set(codes)) != len(codes):
        bad("currencies", "duplicate", "Moneda repetida.")
    for c in currencies:
        code = c["code"]
        if code not in enabled:
            bad(f"currencies.{code}", "currency_not_enabled", f"La moneda {code} no esta habilitada para la agencia.")
            continue
        exponents[code] = enabled[code]
        lo, hi = _dec(c["min_amount"]), _dec(c["max_amount"])
        if lo is None or hi is None or lo <= 0 or hi < lo:
            bad(f"currencies.{code}", "invalid_range", "Los limites deben cumplir 0 < minimo <= maximo.")
            continue
        for label, v in (("min_amount", lo), ("max_amount", hi)):
            if v != v.quantize(Decimal(1).scaleb(-enabled[code])):
                bad(f"currencies.{code}.{label}", "too_many_decimals", f"{code} admite {enabled[code]} decimales.")

    def check_amounts(path: str, amounts: dict | None) -> None:
        """Per-currency fixed amounts must exist for exactly the product currencies, with valid precision."""
        if not amounts or set(amounts) != set(codes):
            bad(path, "currency_amounts", "Se requiere un importe por cada moneda del producto (y solo esas).")
            return
        for code, value in amounts.items():
            if code in exponents and Decimal(value) != Decimal(value).quantize(Decimal(1).scaleb(-exponents[code])):
                bad(f"{path}.{code}", "too_many_decimals", f"{code} admite {exponents[code]} decimales.")

    m, fq, term, first_due = rules.method, rules.frequency, rules.term, rules.first_due
    method = m.code if m else None

    # --- method ---------------------------------------------------------------------------------
    if m:
        if method is None:
            bad("rules.method.code", "missing", "Falta el metodo de interes.")
        elif method == "fixed_total_cost":
            if m.rate is not None or m.time_basis is not None:
                bad("rules.method", "incompatible", "El costo total fijo no usa tasa ni base temporal.")
            tc = m.total_cost
            if tc is None or tc.type is None:
                bad("rules.method.total_cost", "missing", "Falta el costo total (tipo y valor).")
            elif tc.type == "percent_of_principal":
                if tc.percent is None or tc.amounts is not None:
                    bad("rules.method.total_cost", "incompatible", "Indique solo 'percent' para percent_of_principal.")
            else:
                if tc.percent is not None:
                    bad("rules.method.total_cost", "incompatible", "fixed_amount no admite 'percent'.")
                check_amounts("rules.method.total_cost.amounts", tc.amounts)
        else:
            if m.total_cost is not None:
                bad("rules.method.total_cost", "incompatible", "Solo el costo total fijo declara total_cost.")
            r = m.rate
            if r is None or r.type is None or r.value is None:
                bad("rules.method.rate", "missing", "Falta la tasa (tipo y valor).")
            else:
                if r.type == "total_over_term" and method != "flat":
                    bad("rules.method.rate.type", "incompatible", "total_over_term solo aplica al interes plano.")
                if r.type == "annual":
                    if m.time_basis is None:
                        bad("rules.method.time_basis", "missing", "Una tasa anual requiere base temporal explicita.")
                    elif m.time_basis != "periodic" and method == "flat":
                        bad("rules.method.time_basis", "incompatible", "El interes plano solo admite base 'periodic'.")
                    elif m.time_basis == "periodic" and fq and fq.code == "daily":
                        bad(
                            "rules.method.time_basis",
                            "incompatible",
                            "Tasa anual diaria requiere actual_360/actual_365.",
                        )
                elif m.time_basis is not None:
                    bad("rules.method.time_basis", "incompatible", "La base temporal solo aplica a tasas anuales.")

    # --- frequency / term / first due -----------------------------------------------------------
    if fq:
        if fq.code is None:
            bad("rules.frequency.code", "missing", "Falta la frecuencia.")
        elif (fq.code == "monthly") != (fq.monthly_day_rule is not None):
            bad(
                "rules.frequency.monthly_day_rule",
                "inconsistent",
                "La regla de dia mensual aplica solo (y es obligatoria) a mensual.",
            )
    if term:
        if term.min_periods is None or term.max_periods is None:
            bad("rules.term", "missing", "Falta el plazo minimo y maximo (en periodos).")
        elif term.min_periods > term.max_periods:
            bad("rules.term", "min_greater_than_max", "El plazo minimo no puede superar al maximo.")
    if first_due:
        if first_due.periods_after_start is None:
            bad("rules.first_due.periods_after_start", "missing", "Falta la regla de primera fecha de pago.")
        elif method == "bullet" and first_due.periods_after_start != 1:
            bad("rules.first_due.periods_after_start", "incompatible", "Bullet requiere primera fecha = 1 periodo.")

    # --- rounding -------------------------------------------------------------------------------
    rd = rules.rounding
    if rd:
        for f in ("scale", "mode", "moment", "residual"):
            if getattr(rd, f) is None:
                bad(f"rules.rounding.{f}", "missing", f"Falta el redondeo: {f}.")
        if rd.scale is not None:
            for code, exp in exponents.items():
                if rd.scale != exp:
                    bad("rules.rounding.scale", "scale_mismatch", f"La escala debe ser {exp} para {code}.")

    # --- calendar -------------------------------------------------------------------------------
    cal = rules.calendar
    if cal:
        for f in (
            "source",
            "timezone",
            "non_working_weekdays",
            "holidays",
            "adjustment",
            "delinquency_start_basis",
            "accrual_basis",
        ):
            if getattr(cal, f) is None:
                bad(f"rules.calendar.{f}", "missing", f"Falta la politica de calendario: {f}.")
        if cal.timezone is not None:
            try:
                get_zone(cal.timezone)
            except ValueError:
                bad("rules.calendar.timezone", "invalid_timezone", "Zona horaria IANA invalida.")
        wd = cal.non_working_weekdays
        if wd is not None and (any(d < 0 or d > 6 for d in wd) or len(set(wd)) != len(wd) or len(wd) >= 7):
            bad("rules.calendar.non_working_weekdays", "invalid_value", "Dias 0-6 sin repetir y al menos un dia habil.")
        if cal.holidays is not None and len(set(cal.holidays)) != len(cal.holidays):
            bad("rules.calendar.holidays", "duplicate", "Feriados repetidos.")
        if cal.delinquency_start_basis == "contractual_due_date" and cal.adjustment == "next_business_day":
            bad(
                "rules.calendar.delinquency_start_basis",
                "incompatible",
                "La mora no puede iniciar antes de la fecha efectiva (DR-006).",
            )

    # --- grace ----------------------------------------------------------------------------------
    gr = rules.grace
    if gr:
        if gr.delinquency_grace_days is None or gr.principal_grace_periods is None:
            bad(
                "rules.grace",
                "missing",
                "Declare dias de gracia de mora y periodos de gracia de capital (0 si no hay).",
            )
        elif gr.principal_grace_periods > 0:
            if method != "reducing_balance":
                bad("rules.grace.principal_grace_periods", "incompatible", "Gracia de capital solo en saldo insoluto.")
            elif term and term.min_periods is not None and gr.principal_grace_periods >= term.min_periods:
                bad(
                    "rules.grace.principal_grace_periods",
                    "incoherent_term",
                    "La gracia debe ser menor que el plazo minimo.",
                )

    # --- delinquency ----------------------------------------------------------------------------
    dq = rules.delinquency
    if dq:
        if dq.enabled is None:
            bad("rules.delinquency.enabled", "missing", "Indique si hay mora.")
        elif not dq.enabled:
            if any(v is not None for v in (dq.fee, dq.base, dq.frequency, dq.cap, dq.late_on_late)):
                bad("rules.delinquency", "inconsistent", "Mora deshabilitada no admite configuracion.")
        else:
            if dq.late_on_late is None:
                bad("rules.delinquency.late_on_late", "missing", "Declare explicitamente late_on_late=false.")
            elif dq.late_on_late:
                bad(
                    "rules.delinquency.late_on_late",
                    "blocked_by_spec",
                    "BLOCKED_BY_SPEC: mora sobre mora no aprobada (DF-01 §14).",
                )
            if dq.base is None or dq.frequency is None:
                bad("rules.delinquency", "missing", "La mora requiere base y frecuencia explicitas.")
            fee = dq.fee
            if fee is None or fee.kind is None:
                bad("rules.delinquency.fee", "missing", "La mora requiere el cargo (kind y valor).")
            elif fee.kind == "percent":
                if fee.percent is None or fee.amounts is not None:
                    bad("rules.delinquency.fee", "incompatible", "Cargo percent: indique solo 'percent'.")
            else:
                if fee.percent is not None:
                    bad("rules.delinquency.fee", "incompatible", "Cargo fixed no admite 'percent'.")
                check_amounts("rules.delinquency.fee.amounts", fee.amounts)
            cap = dq.cap
            if cap is None or cap.type is None:
                bad("rules.delinquency.cap", "missing", "Declare el tope de mora (type 'none' si no hay).")
            elif cap.type == "none":
                if cap.percent is not None or cap.amounts is not None:
                    bad("rules.delinquency.cap", "inconsistent", "cap none no admite valores.")
            elif cap.type == "percent_of_base":
                if cap.percent is None or cap.amounts is not None:
                    bad("rules.delinquency.cap", "incompatible", "cap percent_of_base: indique solo 'percent'.")
            else:
                if cap.percent is not None:
                    bad("rules.delinquency.cap", "incompatible", "cap fixed_amount no admite 'percent'.")
                check_amounts("rules.delinquency.cap.amounts", cap.amounts)

    # --- allocation -----------------------------------------------------------------------------
    al = rules.allocation
    if al:
        if al.order is None or sorted(al.order) != sorted(ALLOCATION_COMPONENTS):
            bad(
                "rules.allocation.order",
                "invalid_order",
                "El orden debe contener fees, delinquency, interest y principal, una vez cada uno.",
            )
        if al.apply_by is None:
            bad("rules.allocation.apply_by", "missing", "Falta como se aplica entre cuotas y componentes.")

    # --- fees -----------------------------------------------------------------------------------
    fees = rules.fees or []
    fee_codes = [f.code for f in fees]
    if len(set(fee_codes)) != len(fee_codes):
        bad("rules.fees", "duplicate", "Codigos de cargo repetidos.")
    for idx, f in enumerate(fees):
        p = f"rules.fees[{idx}]"
        if not f.code or not f.name or f.kind is None or f.timing is None:
            bad(p, "missing", "Cada cargo requiere code, name, kind y timing.")
            continue
        if f.kind == "percent":
            if f.percent is None or f.amounts is not None or f.base is None:
                bad(p, "incompatible", "Cargo percent: indique 'percent' y 'base' (sin importes fijos).")
        else:
            if f.percent is not None:
                bad(p, "incompatible", "Cargo fixed no admite 'percent'.")
            check_amounts(p + ".amounts", f.amounts)
        allowed = {
            "at_origination": ("principal",),
            "per_installment": ("installment_amount", "opening_balance"),
            "at_prepayment": ("prepaid_amount",),
            "at_payoff": ("payoff_amount",),
        }[f.timing]
        if f.base is not None and f.base not in allowed:
            bad(p + ".base", "incompatible", f"La base {f.base} no aplica a {f.timing}.")
        if (f.timing == "at_origination") != (f.settlement is not None):
            bad(p + ".settlement", "inconsistent", "settlement es obligatorio (solo) para cargos at_origination.")

    # --- prepayment / payoff --------------------------------------------------------------------
    pp = rules.prepayment
    if pp:
        if pp.allowed is None:
            bad("rules.prepayment.allowed", "missing", "Indique si se permite prepago.")
        elif not pp.allowed:
            if any(v is not None for v in (pp.partial_allowed, pp.partial_effect, pp.future_interest, pp.fee_code)):
                bad("rules.prepayment", "inconsistent", "Prepago no permitido no admite configuracion.")
        else:
            if pp.partial_allowed is None or pp.future_interest is None:
                bad("rules.prepayment", "missing", "Prepago: indique partial_allowed y tratamiento del interes futuro.")
            if pp.partial_allowed and pp.partial_effect is None:
                bad("rules.prepayment.partial_effect", "missing", "Falta el efecto del prepago parcial.")
            if pp.partial_allowed is False and pp.partial_effect is not None:
                bad("rules.prepayment.partial_effect", "inconsistent", "Sin prepago parcial no hay efecto parcial.")
            if pp.partial_allowed and method == "fixed_total_cost":
                bad(
                    "rules.prepayment.partial_allowed",
                    "blocked_by_spec",
                    "BLOCKED_BY_SPEC: prepago parcial de costo total fijo no definido.",
                )
            if pp.partial_effect in ("reduce_installment", "reduce_term") and method not in (
                "reducing_balance",
                "flat",
            ):
                bad("rules.prepayment.partial_effect", "incompatible", "Efecto incompatible con el metodo.")
            if pp.partial_effect == "reduce_outstanding_principal" and method not in ("interest_only", "bullet"):
                bad("rules.prepayment.partial_effect", "incompatible", "Efecto incompatible con el metodo.")
            if pp.future_interest == "recalculate" and method == "fixed_total_cost":
                bad("rules.prepayment.future_interest", "incompatible", "El costo total fijo no recalcula interes.")
            if pp.fee_code is not None and not any(f.code == pp.fee_code and f.timing == "at_prepayment" for f in fees):
                bad("rules.prepayment.fee_code", "unknown_fee", "fee_code debe referir un cargo at_prepayment.")
    po = rules.payoff
    if po and any(
        getattr(po, f) is None for f in ("interest_basis", "include_fees", "include_delinquency", "discount_allowed")
    ):
        bad(
            "rules.payoff",
            "missing",
            "Liquidacion anticipada: declare interest_basis, include_fees, include_delinquency y discount_allowed.",
        )

    # --- restructure / refinance ----------------------------------------------------------------
    rs, rf = rules.restructure, rules.refinance
    if rs:
        if rs.allowed is None:
            bad("rules.restructure.allowed", "missing", "Indique si se permite reestructurar.")
        elif rs.allowed and rs.audit_required is not True:
            bad("rules.restructure.audit_required", "inconsistent", "Reestructurar exige auditoria (DF-01 §23).")
        elif not rs.allowed and rs.audit_required is not None:
            bad("rules.restructure", "inconsistent", "Sin reestructuracion no hay configuracion.")
    if rf:
        if rf.allowed is None:
            bad("rules.refinance.allowed", "missing", "Indique si se permite refinanciar.")
        elif rf.allowed:
            if rf.new_contract_required is None or rf.old_loan_treatment is None or rf.audit_required is not True:
                bad(
                    "rules.refinance",
                    "missing",
                    "Refinanciar: declare new_contract_required, old_loan_treatment y audit_required=true.",
                )
        elif any(v is not None for v in (rf.new_contract_required, rf.old_loan_treatment, rf.audit_required)):
            bad("rules.refinance", "inconsistent", "Sin refinanciamiento no hay configuracion.")

    return Validation(issues, [LEGAL_NOTICE], rules)
