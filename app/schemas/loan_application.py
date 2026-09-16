"""The two application forms share one definition across web, mobile and validation."""
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def field(key, label, kind="text", required=True, options=None, **extra):
    return dict(key=key, label=label, kind=kind, required=required, options=options or [], **extra)


SECTIONS = [
    dict(title="Información personal", fields=[
        field("full_name", "Nombres y apellidos"), field("document_id", "Cédula de identidad"),
        field("birth_date", "Fecha de nacimiento", "date"),
        field("marital_status", "Estado civil", "select", options=["Soltero/a", "Casado/a", "Unión libre", "Divorciado/a", "Viudo/a"]),
        field("nationality", "Nacionalidad"), field("address", "Dirección actual"),
        field("phone", "Teléfono", "tel"), field("email", "Correo electrónico", "email", False),
        field("home_phone", "Teléfono de casa", "tel", False),
        field("city", "Ciudad / provincia"),
    ]),
    dict(title="Información laboral", fields=[
        field("employer", "Empresa / actividad económica"), field("job_title", "Cargo / puesto"),
        field("job_tenure", "Tiempo en la empresa (años y meses)"),
        field("employment_type", "Tipo de empleo", "select", options=["Empleado/a", "Independiente", "Otro"]),
        field("monthly_income", "Ingreso mensual (RD$)", "money"),
        field("other_income", "Otros ingresos mensuales (RD$)", "money"),
        field("employer_phone", "Teléfono de la empresa", "tel", False),
    ]),
    dict(title="Información financiera", fields=[
        field("total_income", "Ingresos mensuales totales (RD$)", "money"),
        field("monthly_expenses", "Gastos mensuales aproximados (RD$)", "money"),
        field("has_other_loans", "¿Tiene otros créditos actuales?", "select", options=["No", "Sí"]),
        field("other_loan_payment", "Pago mensual de otros créditos (RD$)", "money", when="has_other_loans", equals="Sí"),
        field("other_loan_entity", "Entidad donde posee otros créditos", when="has_other_loans", equals="Sí"),
    ]),
    dict(title="Préstamo solicitado", fields=[
        field("requested_amount", "Monto solicitado (RD$)", "money"),
        field("term_months", "Plazo solicitado (meses)", "integer"),
        field("purpose", "Propósito del préstamo"),
        field("payment_frequency", "Forma de pago preferida", "select", options=["Semanal", "Quincenal", "Mensual"]),
    ]),
    dict(title="Información de la garantía", secured=True, fields=[
        field("collateral_type", "Tipo de garantía", "select", options=["Inmueble", "Vehículo", "Otro"]),
        field("collateral_description", "Descripción de la garantía"),
        field("collateral_value", "Valor estimado (RD$)", "money"),
        field("collateral_owner", "Propietario de la garantía", "select", options=["El solicitante", "Tercero"]),
        field("collateral_owner_name", "Nombre del propietario tercero", when="collateral_owner", equals="Tercero"),
        field("collateral_notes", "Observaciones", "textarea", False),
    ]),
    dict(title="Referencias personales", fields=[
        field("reference_name", "Nombre de la referencia"), field("reference_phone", "Teléfono de la referencia", "tel"),
        field("reference2_name", "Segunda referencia (opcional)", required=False),
        field("reference2_phone", "Teléfono de segunda referencia (opcional)", "tel", False),
    ]),
    dict(title="Declaración y autorización", fields=[
        field("consent", "El solicitante declara que los datos son verdaderos y autoriza su verificación y la consulta de su historial crediticio.", "checkbox"),
        field("declaration_date", "Fecha de la declaración", "date"),
    ]),
]

DOCUMENTS = [
    dict(key="identity", label="Cédula de identidad", required=True),
    dict(key="income_1", label="Comprobante de ingresos · mes 1", required=True),
    dict(key="income_2", label="Comprobante de ingresos · mes 2", required=True),
    dict(key="address", label="Comprobante de domicilio", required=True),
    dict(key="application", label="Solicitud y autorización firmadas", required=True),
    dict(key="property", label="Documento de propiedad / matrícula", required=True, secured=True),
    dict(key="appraisal", label="Tasación del bien", required=True, secured=True),
    dict(key="other_collateral", label="Otros documentos de la garantía (si aplica)", required=False, secured=True),
    dict(key="contract", label="Contrato y pagaré firmados", required=False),
]


def visible_fields(modality, data):
    return [f for s in SECTIONS if not s.get("secured") or modality == "secured"
            for f in s["fields"] if not f.get("when") or data.get(f["when"]) == f["equals"]]


class ApplicationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    modality: Literal["unsecured", "secured"]
    customer_id: int | None = None
    customer_version: int | None = Field(default=None, ge=1)
    create_customer: bool = False
    data: dict[str, str | bool] = Field(default_factory=dict, max_length=60)
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def check_values(self):
        allowed = {f["key"] for s in SECTIONS for f in s["fields"]}
        if set(self.data) - allowed:
            raise ValueError("El formulario contiene campos no reconocidos.")
        cleaned = {}
        for f in visible_fields(self.modality, self.data):
            value = self.data.get(f["key"], False if f["kind"] == "checkbox" else "")
            if f["kind"] == "checkbox":
                if not isinstance(value, bool):
                    raise ValueError(f'{f["label"]}: valor inválido.')
            else:
                limit = {"full_name":160, "document_id":30, "phone":30, "email":255,
                         "reference_phone":30, "reference2_phone":30, "employer_phone":30}.get(f["key"], 2000)
                if not isinstance(value, str) or len(value) > limit:
                    raise ValueError(f'{f["label"]}: texto inválido o demasiado largo.')
                value = value.strip()
                if value and f["kind"] == "select" and value not in f["options"]:
                    raise ValueError(f'{f["label"]}: opción inválida.')
                if value and f["kind"] in ("money", "integer"):
                    try:
                        amount = Decimal(value)
                        if not amount.is_finite() or amount < 0 or amount > Decimal("9999999999.99"):
                            raise InvalidOperation()
                        if amount.as_tuple().exponent < -2:
                            raise InvalidOperation()
                        if f["kind"] == "integer" and (amount != int(amount) or not 1 <= amount <= 120):
                            raise InvalidOperation()
                        if f["key"] in ("requested_amount", "collateral_value") and amount <= 0:
                            raise InvalidOperation()
                    except (InvalidOperation, ValueError):
                        raise ValueError(f'{f["label"]}: importe o plazo inválido.')
                if value and f["kind"] == "date":
                    try:
                        parsed = date.fromisoformat(value)
                        if parsed > date.today():
                            raise ValueError()
                    except ValueError:
                        raise ValueError(f'{f["label"]}: fecha inválida o futura.')
                if value and f["kind"] == "email":
                    from pydantic import TypeAdapter, EmailStr
                    TypeAdapter(EmailStr).validate_python(value)
            cleaned[f["key"]] = value
        self.data = cleaned
        return self


def validate_complete(application):
    validated = ApplicationInput(modality=application.modality, data=application.data)
    missing = [f["label"] for f in visible_fields(validated.modality, validated.data)
               if f["required"] and not validated.data.get(f["key"])]
    if missing:
        raise ValueError("Completa: " + ", ".join(missing))
    d = validated.data
    if Decimal(d["total_income"]) != Decimal(d["monthly_income"]) + Decimal(d["other_income"]):
        raise ValueError("Los ingresos totales deben coincidir con ingreso mensual más otros ingresos.")
    if bool(d.get("reference2_name")) != bool(d.get("reference2_phone")):
        raise ValueError("Completa nombre y teléfono de la segunda referencia, o deja ambos vacíos.")


class LoanTerms(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interest_rate: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)
    installment_count: int = Field(ge=1, le=365)
    late_fee_rate: Decimal = Field(default=Decimal("0"), ge=0, le=100, max_digits=5, decimal_places=2)
    grace_days: int = Field(default=0, ge=0, le=60)


class TransitionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    action: Literal["submit", "evaluate", "return", "approve", "reject", "sign", "disburse"]
    notes: str = Field(default="", max_length=4000)
    terms: LoanTerms | None = None
    first_payment_date: date | None = None
    disbursement_reference: str = Field(default="", max_length=160)


class DocumentInput(BaseModel):
    version: int = Field(ge=1)
    filename: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x1f/\\]+$")
    content_base64: str = Field(max_length=7_000_000)


class ReviewInput(BaseModel):
    version: int = Field(ge=1)
    verified: bool


class CustomerLinkInput(BaseModel):
    version: int = Field(ge=1)
    customer_id: int = Field(ge=1)
    customer_version: int = Field(ge=1)
