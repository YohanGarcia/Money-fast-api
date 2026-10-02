"""Payment Runtime v1 (T-008): confirmed CASH payments (counter and field) -> contractual allocation -> applications.

money received -> confirmed payment -> contractual allocation -> payment applications -> derived balances
-> obligation projection -> loan ``paid`` when fully settled, ALL in one transaction.

Lock order (every later package that changes debt must follow it: LOAN FIRST):
  1. the loan row ``FOR UPDATE``   2. its obligations, ascending sequence   3. cash box   4. cash session.
A payment never exceeds what is due today (v1: no advance / prepayment / payoff). The frozen contract (T-005 snapshot inside
the T-006 formalization) is the only source of the allocation order; the live product and the legacy loan tables are never read.
Reads never write.
"""

import hashlib
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import IdempotencyConflict
from app.core.time import now_utc
from app.models.branch import Branch
from app.modules.cash import port as cash_port
from app.modules.credit.rules import canonical_json, parse_rules
from app.modules.customers.service import visible_branch_ids
from app.modules.identity.audit import record_event
from app.modules.identity.authorization import Principal, require
from app.modules.identity.errors import PermissionDenied, TenantMismatch
from app.modules.loans import allocation, ledger
from app.modules.loans.errors import (
    AllocationPolicyBlocked,
    ContractIntegrityFailed,
    CurrencyMismatch,
    DuplicateExternalReference,
    LoanNotPayable,
    PaymentExceedsDueAmount,
    PaymentInvariantViolation,
)
from app.modules.loans.models import CreditLoan, CreditPayment, CreditPaymentApplication, CreditPaymentReversal
from app.modules.loans.payment_schemas import PaymentIn
from app.modules.origination.models import CreditFormalization
from app.modules.origination.service import _amt, _next_number, verify_formalization_contract

CREATE, READ = "payments.create", "payments.read"
RECEIPT_KIND = "credit_payment_receipt"  # NOT 'counter_payment': the legacy cash reversal must never see it


def _gate_tenant(actor: Principal) -> None:
    if actor.tenant_id is None:
        raise TenantMismatch()


def _digest(loan_id: int, body: PaymentIn, amount: Decimal) -> str:
    payload = {
        "loan_id": loan_id,
        "amount": format(amount, "f"),
        "currency_code": body.currency_code,
        "method": body.method,
        "origin": body.origin,
        "receiving_branch_id": body.receiving_branch_id,
        "cash_session_id": body.cash_session_id,
        "external_reference": body.external_reference,
    }
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# --- serialisation ----------------------------------------------------------------------------------
def _payment_out(
    db: Session, p: CreditPayment, *, with_applications: bool = True, reversal: tuple[int, str] | None | bool = False
) -> dict:
    """``reversal``: (id, number) of the payment's reversal, None if it has none, False = look it up."""
    if reversal is False:
        row = db.execute(
            select(CreditPaymentReversal.id, CreditPaymentReversal.reversal_number).where(
                CreditPaymentReversal.payment_id == p.id
            )
        ).first()
        reversal = (row[0], row[1]) if row else None
    out = {
        "id": p.id,
        "loan_id": p.loan_id,
        "payment_number": p.payment_number,  # technical reference: NOT a fiscal or legal receipt
        "amount": _amt(p.amount),
        "currency_code": p.currency_code,
        "method": p.method,
        "origin": p.origin,
        "status": p.status,
        "received_at": p.received_at,
        "business_date": p.business_date,
        "receiving_branch_id": p.receiving_branch_id,
        "cash_session_id": p.cash_session_id,
        "cash_movement_id": p.cash_movement_id,
        "collected_by": p.collected_by,
        "external_reference": p.external_reference,
        # DERIVED from the existence of the reversal row: the payment itself is immutable and its status never changes
        "reversed": reversal is not None,
        "reversal_id": reversal[0] if reversal else None,
        "reversal_number": reversal[1] if reversal else None,
    }
    if with_applications:
        rows = db.scalars(
            select(CreditPaymentApplication)
            .where(CreditPaymentApplication.payment_id == p.id)
            .order_by(CreditPaymentApplication.id)
        ).all()
        out["applications"] = [
            {"obligation_id": a.obligation_id, "component": a.component, "amount": _amt(a.amount)} for a in rows
        ]
    return out


def _payment_allowed(actor: Principal, permission: str, p: CreditPayment, loan: CreditLoan) -> bool:
    """A payment is visible from its RECEIVING branch and from the loan's origin, managing and disbursement branches."""
    branches = {p.receiving_branch_id, loan.origin_branch_id, loan.managing_branch_id, loan.disbursement_branch_id} - {
        None
    }
    return any(actor.allows(permission, tenant_id=actor.tenant_id, branch_id=b) for b in branches)


# --- the command ------------------------------------------------------------------------------------
def _contract(db: Session, loan: CreditLoan):
    f = db.get(CreditFormalization, loan.formalization_id)
    if (
        f is None
        or f.contract_hash != loan.contract_hash
        or f.rules_hash != loan.rules_hash
        or not verify_formalization_contract(db, f)
    ):
        raise ContractIntegrityFailed()
    return f, parse_rules(f.contract_snapshot["product"]["snapshot"]["rules"])


def pay(db: Session, actor: Principal, loan_id: int, body: PaymentIn, client_ip: str | None) -> dict:
    _gate_tenant(actor)
    # 1. LOAN ROW FIRST (tenant-filtered: a foreign loan is a 404)
    loan = db.scalar(
        select(CreditLoan)
        .where(CreditLoan.id == loan_id, CreditLoan.tenant_id == actor.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if loan is None:
        raise TenantMismatch()
    branch = db.get(Branch, body.receiving_branch_id)
    if branch is None or branch.company_id != actor.tenant_id:
        raise TenantMismatch()  # another tenant's branch is a 404
    require(actor, CREATE, tenant_id=actor.tenant_id, branch_id=body.receiving_branch_id)
    try:
        amount = Decimal(body.amount)
    except InvalidOperation:
        raise PaymentExceedsDueAmount("Monto invalido.") from None
    digest = _digest(loan.id, body, amount)

    # idempotency under the loan lock; UNIQUE(tenant, key) is the database backstop
    prior = db.scalar(
        select(CreditPayment).where(
            CreditPayment.tenant_id == actor.tenant_id, CreditPayment.idempotency_key == body.idempotency_key
        )
    )
    if prior is not None:
        if prior.request_digest == digest:
            return {**_payment_out(db, prior), "replayed": True}
        raise IdempotencyConflict()  # same key, different content
    if body.external_reference and db.scalar(
        select(CreditPayment.id).where(
            CreditPayment.tenant_id == actor.tenant_id,
            CreditPayment.method == body.method,
            CreditPayment.external_reference == body.external_reference,
        )
    ):
        raise DuplicateExternalReference()

    if loan.status != "active":
        raise LoanNotPayable()
    if body.currency_code != loan.currency_code:
        raise CurrencyMismatch()
    if branch.status != "active":
        raise LoanNotPayable("La sucursal receptora esta inactiva.")
    if body.currency_code != cash_port.CASH_CURRENCY:  # cash (counter or field) is RD$ only: no cash multi-currency yet
        raise cash_port.CashCurrencyUnsupported()
    if amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        raise PaymentExceedsDueAmount("El monto debe ser positivo y expresarse en centavos.")

    # contract integrity BEFORE any money moves; the allocation policy comes from the frozen contract only
    f, rules = _contract(db, loan)
    if rules.allocation.apply_by != "installment_then_component":
        raise AllocationPolicyBlocked()
    order = list(rules.allocation.order)

    # 2. obligations, ascending sequence (locked), and the applications already recorded
    obligations, views = ledger.views(db, loan.id, lock=True)
    now = now_utc()
    business_date = ledger.business_date(db, loan, now)
    try:
        rows = allocation.allocate(amount, views, business_date, order)
    except ValueError:
        raise PaymentExceedsDueAmount() from None

    number = _next_number(db, actor.tenant_id, "credit_payment", "PAG-")
    movement = None
    if body.origin == "counter":
        # 3-4. cash box and session (inside the same transaction; the port never commits)
        movement = cash_port.deposit(
            db,
            tenant_id=actor.tenant_id,
            branch_id=body.receiving_branch_id,
            session_id=body.cash_session_id,
            amount=amount,
            currency=body.currency_code,
            actor_user_id=actor.user_id,
            kind=RECEIPT_KIND,
            reference=number,
            notes=f"Cobro {number} / {loan.loan_number}",
        )
    payment = CreditPayment(
        tenant_id=actor.tenant_id,
        loan_id=loan.id,
        payment_number=number,
        amount=amount,
        currency_code=loan.currency_code,
        method=body.method,
        origin=body.origin,
        status="confirmed",
        received_at=now,
        business_date=business_date,
        receiving_branch_id=body.receiving_branch_id,
        cash_session_id=movement.session_id if movement else None,
        cash_movement_id=movement.movement_id if movement else None,
        collected_by=actor.user_id,
        idempotency_key=body.idempotency_key,
        request_digest=digest,
        external_reference=body.external_reference,
    )
    db.add(payment)
    try:
        db.flush()
        for obligation_id, component, part in rows:
            db.add(
                CreditPaymentApplication(
                    tenant_id=actor.tenant_id,
                    payment_id=payment.id,
                    obligation_id=obligation_id,
                    loan_id=loan.id,
                    component=component,
                    amount=part,
                )
            )
        db.flush()
        _project(db, loan, obligations)
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        text_ = str(exc.orig)
        if "external_reference" in text_:
            raise DuplicateExternalReference() from None
        if "idempotency" in text_:
            raise IdempotencyConflict() from None
        raise PaymentInvariantViolation() from None

    totals: dict[str, Decimal] = {}
    for _o, component, part in rows:
        totals[component] = totals.get(component, Decimal(0)) + part
    record_event(
        db,
        "payment.confirmed",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={
            "payment_id": payment.id,
            "payment_number": number,
            "loan_id": loan.id,
            "loan_number": loan.loan_number,
            "amount": _amt(amount),
            "currency_code": loan.currency_code,
            "business_date": business_date.isoformat(),
            "receiving_branch_id": body.receiving_branch_id,
            "origin": body.origin,
            "method": body.method,
            "cash_session_id": payment.cash_session_id,
            "cash_movement_id": payment.cash_movement_id,
            "component_totals": {k: _amt(v) for k, v in sorted(totals.items())},
            "loan_status": loan.status,
            "rules_digest": f.rules_hash,
            "contract_digest": f.contract_hash,
        },
    )
    db.commit()
    return {**_payment_out(db, payment), "replayed": False}


def _project(db: Session, loan: CreditLoan, obligations: list) -> None:
    """Re-derive statuses from the NET applications (see ``ledger.project``); past_due belongs to the delinquency package."""
    ledger.project(db, loan, obligations)


# --- reads ------------------------------------------------------------------------------------------
def _loan_read(db: Session, actor: Principal, loan_id: int) -> CreditLoan:
    _gate_tenant(actor)
    loan = db.scalar(select(CreditLoan).where(CreditLoan.id == loan_id, CreditLoan.tenant_id == actor.tenant_id))
    if loan is None:
        raise TenantMismatch()
    return loan


def get_payment(db: Session, actor: Principal, payment_id: int) -> dict:
    _gate_tenant(actor)
    p = db.scalar(
        select(CreditPayment).where(CreditPayment.id == payment_id, CreditPayment.tenant_id == actor.tenant_id)
    )
    if p is None:
        raise TenantMismatch()
    loan = db.get(CreditLoan, p.loan_id)
    if not _payment_allowed(actor, READ, p, loan):
        raise PermissionDenied()
    return _payment_out(db, p)


def list_payments(db: Session, actor: Principal, loan_id: int, *, limit: int, offset: int) -> list[dict]:
    loan = _loan_read(db, actor, loan_id)
    visible = visible_branch_ids(actor, READ)  # None = tenant-wide
    stmt = select(CreditPayment).where(CreditPayment.tenant_id == actor.tenant_id, CreditPayment.loan_id == loan.id)
    if visible is not None:
        loan_branches = {loan.origin_branch_id, loan.managing_branch_id, loan.disbursement_branch_id} - {None}
        if visible & loan_branches:
            pass  # a branch of the loan: sees every payment of it
        else:
            stmt = stmt.where(CreditPayment.receiving_branch_id.in_(visible))
    rows = db.scalars(stmt.order_by(CreditPayment.id.desc()).limit(limit).offset(offset)).all()
    reversals = {
        pid: (rid, num)
        for pid, rid, num in db.execute(
            select(
                CreditPaymentReversal.payment_id, CreditPaymentReversal.id, CreditPaymentReversal.reversal_number
            ).where(CreditPaymentReversal.payment_id.in_([p.id for p in rows]))
        )
    }
    return [_payment_out(db, p, with_applications=False, reversal=reversals.get(p.id)) for p in rows]
