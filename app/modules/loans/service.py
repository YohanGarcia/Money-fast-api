"""Loan birth use cases (T-007): disbursement of a formalized contract and the read side of the loan.

One transaction does everything or nothing: lock the formalization, verify its frozen contract, move the money through
the Cash port, create the ACTIVE loan, its disbursement record and its contractual obligations, mark the formalization
``disbursed`` and audit. Lock order: formalization -> cash box -> cash session (the legacy cash order).

The frozen contract (T-006) is the only contractual source: the live product (retired, inactive, superseded) is NOT consulted.
Reads never write.
"""

import hashlib
from datetime import date
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import IdempotencyConflict
from app.core.time import now_utc, to_zone
from app.models.branch import Branch
from app.modules.cash import port as cash_port
from app.modules.credit import engine
from app.modules.credit.rules import canonical_json, parse_rules
from app.modules.customers.service import visible_branch_ids
from app.modules.identity.audit import record_event
from app.modules.identity.authorization import Principal, require
from app.modules.identity.errors import PermissionDenied, TenantMismatch
from app.modules.loans import allocation, ledger
from app.modules.loans.errors import (
    AlreadyDisbursed,
    ContractIntegrityFailed,
    DisbursementBlockedBySpec,
    FormalizationNotReady,
    ScheduleGenerationFailed,
)
from app.modules.loans.models import CreditLoan, CreditLoanDisbursement, CreditLoanObligation
from app.modules.loans.schemas import DisburseIn
from app.modules.organization.models import Currency
from app.modules.origination.models import CreditApplication, CreditFormalization
from app.modules.origination.service import _amt, _next_number, verify_formalization_contract

READ, DISBURSE = "loans.read", "loans.disburse"
DISBURSEMENT_KIND = "credit_disbursement"  # a kind the legacy cash reversal does not accept: no side door to undo it


def _gate_tenant(actor: Principal) -> None:
    if actor.tenant_id is None:
        raise TenantMismatch()


def _digest(formalization_id: int, body: DisburseIn) -> str:
    payload = {
        "formalization_id": formalization_id,
        "disbursement_branch_id": body.disbursement_branch_id,
        "funding_source": {"type": body.funding_source.type, "session_id": body.funding_source.session_id},
    }
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _loan_allowed(actor: Principal, permission: str, loan: CreditLoan) -> bool:
    branches = {loan.origin_branch_id, loan.managing_branch_id, loan.disbursement_branch_id} - {None}
    return any(actor.allows(permission, tenant_id=actor.tenant_id, branch_id=b) for b in branches)


def _loan(db: Session, actor: Principal, loan_id: int) -> CreditLoan:
    _gate_tenant(actor)
    loan = db.scalar(select(CreditLoan).where(CreditLoan.id == loan_id, CreditLoan.tenant_id == actor.tenant_id))
    if loan is None:
        raise TenantMismatch()
    if not _loan_allowed(actor, READ, loan):
        raise PermissionDenied()
    return loan


# --- serialisation ----------------------------------------------------------------------------------
def _summary(loan: CreditLoan) -> dict:
    return {
        "id": loan.id,
        "loan_number": loan.loan_number,
        "customer_id": loan.customer_id,
        "formalization_id": loan.formalization_id,
        "application_id": loan.application_id,
        "product_id": loan.product_id,
        "product_version_id": loan.product_version_id,
        "status": loan.status,
        "currency_code": loan.currency_code,
        "original_principal": _amt(loan.original_principal),
        "term_periods": loan.term_periods,
        "frequency": loan.frequency,
        "origin_branch_id": loan.origin_branch_id,
        "managing_branch_id": loan.managing_branch_id,
        "disbursement_branch_id": loan.disbursement_branch_id,
        "disbursed_at": loan.disbursed_at,
        "maturity_date": loan.maturity_date,
    }


def _obligation_out(o: CreditLoanObligation, view=None, business_date=None) -> dict:
    derived = {}
    if view is not None:  # derived from the applications, never stored
        derived = {
            "paid_amount": _amt(sum((view.applied.get(c, Decimal(0)) for c in view.due), Decimal(0))),
            "outstanding_amount": _amt(view.outstanding_total),
        }
    if (
        view is not None and business_date is not None
    ):  # T-010: overdue facts, DERIVED (effective due date + net ledger), never stored
        derived |= {
            "is_overdue": allocation.is_overdue(view, business_date),
            "days_overdue": allocation.days_overdue(view, business_date),
            "overdue_outstanding": _amt(allocation.overdue_outstanding(view, business_date)),
        }
    return {
        **derived,
        "sequence": o.sequence,
        "contractual_date": o.contractual_date,
        "due_date": o.due_date,
        "delinquency_starts_on": o.delinquency_starts_on,
        "principal_due": _amt(o.principal_due),
        "interest_due": _amt(o.interest_due),
        "fees_due": _amt(o.fees_due),
        "delinquency_due": _amt(o.delinquency_due),
        "total_due": _amt(o.total_due),
        "currency_code": o.currency_code,
        "status": o.status,
    }


def _disbursement_out(d: CreditLoanDisbursement) -> dict:
    return {
        "id": d.id,
        "status": d.status,
        "approved_amount": _amt(d.approved_amount),
        "disbursed_amount": _amt(d.disbursed_amount),
        "currency_code": d.currency_code,
        "disbursement_branch_id": d.disbursement_branch_id,
        "funding_source": {
            "type": d.funding_source_type,
            "session_id": d.cash_session_id,
            "movement_id": d.cash_movement_id,
        },
        "disbursed_by": d.disbursed_by,
        "disbursed_at": d.disbursed_at,
    }


def _detail(db: Session, loan: CreditLoan) -> dict:
    obligations, views = ledger.views(db, loan.id)  # derived: contractual obligations minus payment applications
    disbursement = db.scalar(select(CreditLoanDisbursement).where(CreditLoanDisbursement.loan_id == loan.id))
    return {
        **_summary(loan),
        "tenant_id": loan.tenant_id,
        "rules_hash": loan.rules_hash,
        "contract_hash": loan.contract_hash,
        "disbursement": _disbursement_out(disbursement) if disbursement else None,
        "balances": ledger.balances_out(
            views, ledger.business_date(db, loan, now_utc()), loan.original_principal, loan.currency_code
        ),
        "obligation_count": len(obligations),
    }


# --- disbursement -----------------------------------------------------------------------------------
def _replay(db: Session, f: CreditFormalization) -> dict:
    loan = db.scalar(select(CreditLoan).where(CreditLoan.formalization_id == f.id))
    return {**_detail(db, loan), "replayed": True}


def disburse(db: Session, actor: Principal, formalization_id: int, body: DisburseIn, client_ip: str | None) -> dict:
    _gate_tenant(actor)
    f = db.scalar(
        select(CreditFormalization)
        .where(CreditFormalization.id == formalization_id, CreditFormalization.tenant_id == actor.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if f is None:
        raise TenantMismatch()
    branch = db.get(Branch, body.disbursement_branch_id)
    if branch is None or branch.company_id != actor.tenant_id:
        raise TenantMismatch()  # another tenant's branch is a 404
    require(actor, DISBURSE, tenant_id=actor.tenant_id, branch_id=body.disbursement_branch_id)
    digest = _digest(f.id, body)

    # idempotency, decided under the formalization lock; the UNIQUE constraints are the database backstop
    by_key = db.scalar(
        select(CreditLoanDisbursement).where(
            CreditLoanDisbursement.tenant_id == actor.tenant_id,
            CreditLoanDisbursement.idempotency_key == body.idempotency_key,
        )
    )
    if by_key is not None:
        if by_key.formalization_id == f.id and by_key.request_digest == digest:
            return _replay(db, f)
        raise IdempotencyConflict()  # same key, different content
    existing = db.scalar(select(CreditLoanDisbursement).where(CreditLoanDisbursement.formalization_id == f.id))
    if existing is not None:
        if existing.request_digest == digest:
            return _replay(db, f)  # an equivalent command, whatever its key: a replay, nothing new is written
        raise AlreadyDisbursed()
    if f.status != "ready_for_disbursement":
        raise FormalizationNotReady()
    app = db.get(CreditApplication, f.application_id)
    if app is None or app.status != "formalized":
        raise FormalizationNotReady()
    if not branch.is_active:
        raise FormalizationNotReady("La sucursal de desembolso esta inactiva.")
    if not verify_formalization_contract(db, f):
        raise ContractIntegrityFailed()

    # the schedule comes from the FROZEN contract only (never the live product)
    snapshot = f.contract_snapshot["product"]["snapshot"]
    rules = parse_rules(snapshot["rules"])
    if any(fee.timing == "at_origination" for fee in rules.fees or []):
        raise DisbursementBlockedBySpec(
            "BLOCKED_BY_SPEC: el contrato cobra cargos al desembolso y su tratamiento (neto desembolsado, cobro) no esta definido."
        )
    if f.frequency != rules.frequency.code:
        raise ContractIntegrityFailed()
    limits = next((c for c in snapshot["currencies"] if c["code"] == f.currency_code), None)
    if limits is None:
        raise ContractIntegrityFailed()
    now = now_utc()
    start = to_zone(now, rules.calendar.timezone).date()  # business date in the contract's timezone, never UTC
    exponent = db.scalar(select(Currency.exponent).where(Currency.code == f.currency_code))
    try:
        schedule = engine.simulate(
            rules,
            currency=f.currency_code,
            exponent=exponent,
            limits=(Decimal(limits["min_amount"]), Decimal(limits["max_amount"])),
            principal=f.approved_amount,
            term_periods=f.term,
            start_date=start,
        )
    except engine.EngineError as exc:
        raise ScheduleGenerationFailed(str(exc)) from None

    loan_number = _next_number(db, actor.tenant_id, "credit_loan", "PRE-")
    withdrawal = cash_port.withdraw(  # the money leaves HERE; it is rolled back with everything else on any failure
        db,
        tenant_id=actor.tenant_id,
        branch_id=body.disbursement_branch_id,
        session_id=body.funding_source.session_id,
        amount=f.approved_amount,
        currency=f.currency_code,
        actor_user_id=actor.user_id,
        kind=DISBURSEMENT_KIND,
        reference=loan_number,
        notes=f"Desembolso {f.reference} / {loan_number}",
    )
    rows = schedule["schedule"]
    loan = CreditLoan(
        tenant_id=actor.tenant_id,
        loan_number=loan_number,
        customer_id=app.customer_id,
        formalization_id=f.id,
        application_id=f.application_id,
        product_id=f.product_id,
        product_version_id=f.product_version_id,
        currency_code=f.currency_code,
        original_principal=f.approved_amount,
        term_periods=f.term,
        frequency=f.frequency,
        status="active",
        origin_branch_id=f.origin_branch_id,
        managing_branch_id=f.managing_branch_id,
        disbursement_branch_id=body.disbursement_branch_id,
        disbursed_at=now,
        maturity_date=_iso(rows[-1]["due_date"]),
        rules_hash=f.rules_hash,
        contract_hash=f.contract_hash,
    )
    db.add(loan)
    try:
        db.flush()
        db.add(
            CreditLoanDisbursement(
                tenant_id=actor.tenant_id,
                loan_id=loan.id,
                formalization_id=f.id,
                disbursement_branch_id=body.disbursement_branch_id,
                funding_source_type=body.funding_source.type,
                cash_session_id=withdrawal.session_id,
                cash_movement_id=withdrawal.movement_id,
                approved_amount=f.approved_amount,
                disbursed_amount=withdrawal.amount,  # the confirmed movement, not the request
                currency_code=f.currency_code,
                idempotency_key=body.idempotency_key,
                request_digest=digest,
                disbursed_by=actor.user_id,
                disbursed_at=now,
            )
        )
        _insert_obligations(db, actor.tenant_id, loan, rows)
        f.status = "disbursed"
        db.flush()
    except IntegrityError:
        db.rollback()
        raise AlreadyDisbursed("Otra operacion concurrente desembolso este contrato.") from None
    for event_name in ("loan.disbursed", "loan.activated"):
        record_event(
            db,
            event_name,
            tenant_id=actor.tenant_id,
            actor_id=actor.user_id,
            client_ip=client_ip,
            details={
                "loan_id": loan.id,
                "loan_number": loan_number,
                "formalization_id": f.id,
                "application_id": f.application_id,
                "disbursement_branch_id": body.disbursement_branch_id,
                "amount": _amt(withdrawal.amount),
                "currency_code": f.currency_code,
                "funding_source_type": body.funding_source.type,
                "cash_session_id": withdrawal.session_id,
                "cash_movement_id": withdrawal.movement_id,
                "rules_digest": f.rules_hash,
                "contract_digest": f.contract_hash,
                "before": {"formalization_status": "ready_for_disbursement"},
                "after": {"formalization_status": "disbursed", "loan_status": "active"},
            },
        )
    db.commit()
    return {**_detail(db, loan), "replayed": False}


def _iso(value):
    return value if isinstance(value, date) else date.fromisoformat(value)


def _insert_obligations(db: Session, tenant_id: int, loan: CreditLoan, rows: list[dict]) -> None:
    for row in rows:
        principal, interest, fees = Decimal(row["principal"]), Decimal(row["interest"]), Decimal(row["fees"])
        db.add(
            CreditLoanObligation(
                tenant_id=tenant_id,
                loan_id=loan.id,
                sequence=row["period"],
                contractual_date=_iso(row["contractual_date"]),
                due_date=_iso(row["due_date"]),
                delinquency_starts_on=_iso(row["delinquency_starts_on"]) if row["delinquency_starts_on"] else None,
                principal_due=principal,
                interest_due=interest,
                fees_due=fees,
                delinquency_due=Decimal(0),
                total_due=principal + interest + fees,
                currency_code=loan.currency_code,
            )
        )


# --- reads ------------------------------------------------------------------------------------------
def list_loans(
    db: Session,
    actor: Principal,
    *,
    status: str | None,
    customer_id: int | None,
    formalization_id: int | None,
    limit: int,
    offset: int,
) -> list[dict]:
    _gate_tenant(actor)
    branches = visible_branch_ids(actor, READ)
    stmt = select(CreditLoan).where(CreditLoan.tenant_id == actor.tenant_id)
    if branches is not None:
        stmt = stmt.where(
            or_(
                CreditLoan.origin_branch_id.in_(branches),
                CreditLoan.managing_branch_id.in_(branches),
                CreditLoan.disbursement_branch_id.in_(branches),
            )
        )
    if status:
        stmt = stmt.where(CreditLoan.status == status)
    if customer_id:
        stmt = stmt.where(CreditLoan.customer_id == customer_id)
    if formalization_id:
        stmt = stmt.where(CreditLoan.formalization_id == formalization_id)
    return [_summary(x) for x in db.scalars(stmt.order_by(CreditLoan.id.desc()).limit(limit).offset(offset)).all()]


def get_loan(db: Session, actor: Principal, loan_id: int) -> dict:
    return _detail(db, _loan(db, actor, loan_id))


def get_schedule(db: Session, actor: Principal, loan_id: int) -> dict:
    loan = _loan(db, actor, loan_id)
    rows, views = ledger.views(db, loan.id)
    by_id = {v.id: v for v in views}
    today = ledger.business_date(db, loan, now_utc())  # the contract timezone, never the UTC date
    return {
        "loan_id": loan.id,
        "currency_code": loan.currency_code,
        "original_principal": _amt(loan.original_principal),
        "business_date": today.isoformat(),
        "obligations": [_obligation_out(o, by_id[o.id], today) for o in rows],
        "totals": {
            "principal": _amt(sum((o.principal_due for o in rows), Decimal(0))),
            "interest": _amt(sum((o.interest_due for o in rows), Decimal(0))),
            "fees": _amt(sum((o.fees_due for o in rows), Decimal(0))),
            "total": _amt(sum((o.total_due for o in rows), Decimal(0))),
        },
    }


def get_balances(db: Session, actor: Principal, loan_id: int) -> dict:
    loan = _loan(db, actor, loan_id)
    _rows, views = ledger.views(db, loan.id)
    return ledger.balances_out(
        views, ledger.business_date(db, loan, now_utc()), loan.original_principal, loan.currency_code
    ) | {"loan_id": loan.id, "loan_status": loan.status}
