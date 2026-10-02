"""Full payment reversal (T-009): a confirmed payment is compensated, never edited or deleted.

original payment (immutable) + reversal (append-only) + reversal applications mirroring EVERY original application
-> net ledger (applications - reversal applications) -> obligation / loan re-projection (``paid`` can reopen).

Lock order (loan first, always): 1. loan row ``FOR UPDATE``  2. obligations, ascending sequence  3. the payment row
4. cash box  5. cash session. A field reversal stops before Cash. Counter: ONE transaction holds the compensating cash
movement (kind ``credit_payment_reversal``, never the legacy ``reversal``), the reversal row, its applications, the
projection and the audit. The reversal mirrors the original applications exactly: later payments are never re-allocated,
the original ``external_reference`` stays occupied, and the original cash session is never touched or reopened (the cash
leaves the CURRENT open session of the executing cashier, in the payment's receiving branch).
"""

import hashlib
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import IdempotencyConflict
from app.core.time import now_utc
from app.modules.cash import port as cash_port
from app.modules.credit.rules import canonical_json
from app.modules.identity.audit import record_event
from app.modules.identity.authorization import Principal, require
from app.modules.identity.errors import PermissionDenied, TenantMismatch
from app.modules.loans import ledger
from app.modules.loans.errors import (
    LoanNotReversible,
    PaymentAlreadyReversed,
    PaymentInvariantViolation,
    PaymentNotReversed,
    ReversalBranchMismatch,
    ReversalSessionMismatch,
)
from app.modules.loans.models import (
    CreditLoan,
    CreditPayment,
    CreditPaymentApplication,
    CreditPaymentReversal,
    CreditPaymentReversalApplication,
)
from app.modules.loans.payments import READ, _contract, _gate_tenant, _payment_allowed
from app.modules.loans.reversal_schemas import ReversalIn
from app.modules.origination.service import _amt, _next_number

REVERSE = "payments.reverse"
REVERSAL_KIND = "credit_payment_reversal"  # NOT 'reversal' (legacy): the legacy cash reversal must never see it


def _digest(payment_id: int, body: ReversalIn) -> str:
    payload = {
        "payment_id": payment_id,
        "reason": body.reason,
        "reversal_branch_id": body.reversal_branch_id,
        "cash_session_id": body.cash_session_id,
    }
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _out(db: Session, r: CreditPaymentReversal, *, payment_number: str) -> dict:
    rows = db.scalars(
        select(CreditPaymentReversalApplication)
        .where(CreditPaymentReversalApplication.reversal_id == r.id)
        .order_by(CreditPaymentReversalApplication.id)
    ).all()
    return {
        "id": r.id,
        "payment_id": r.payment_id,
        "payment_number": payment_number,
        "loan_id": r.loan_id,
        "reversal_number": r.reversal_number,  # technical reference: NOT a fiscal or legal document
        "amount": _amt(r.amount),
        "currency_code": r.currency_code,
        "origin": r.origin,
        "reason": r.reason,
        "reversed_by": r.reversed_by,
        "reversed_at": r.reversed_at,
        "business_date": r.business_date,
        "reversal_branch_id": r.reversal_branch_id,
        "cash_session_id": r.cash_session_id,
        "cash_movement_id": r.cash_movement_id,
        "applications": [
            {
                "original_application_id": a.original_application_id,
                "obligation_id": a.obligation_id,
                "component": a.component,
                "amount": _amt(a.amount),
            }
            for a in rows
        ],
    }


def reverse(db: Session, actor: Principal, payment_id: int, body: ReversalIn, client_ip: str | None) -> dict:
    _gate_tenant(actor)
    # the payment row is immutable: reading it first only tells us WHICH loan to lock
    head = db.scalar(
        select(CreditPayment).where(CreditPayment.id == payment_id, CreditPayment.tenant_id == actor.tenant_id)
    )
    if head is None:
        raise TenantMismatch()
    # 1. LOAN ROW FIRST (tenant-filtered)
    loan = db.scalar(
        select(CreditLoan)
        .where(CreditLoan.id == head.loan_id, CreditLoan.tenant_id == actor.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if loan is None:
        raise TenantMismatch()
    require(actor, REVERSE, tenant_id=actor.tenant_id, branch_id=head.receiving_branch_id)
    digest = _digest(head.id, body)

    # idempotency under the loan lock; UNIQUE(tenant, key) is the database backstop
    prior = db.scalar(
        select(CreditPaymentReversal).where(
            CreditPaymentReversal.tenant_id == actor.tenant_id,
            CreditPaymentReversal.idempotency_key == body.idempotency_key,
        )
    )
    if prior is not None:
        if prior.request_digest == digest:
            return {**_out(db, prior, payment_number=head.payment_number), "replayed": True}
        raise IdempotencyConflict()
    if db.scalar(select(CreditPaymentReversal.id).where(CreditPaymentReversal.payment_id == head.id)):
        raise PaymentAlreadyReversed()  # another key on an already reversed payment: never compensate twice

    if body.reversal_branch_id != head.receiving_branch_id:
        raise ReversalBranchMismatch()  # D2: the compensation happens where the money was received
    if (head.origin == "counter") != (body.cash_session_id is not None):
        raise ReversalSessionMismatch()
    if loan.status not in ("active", "paid"):
        raise LoanNotReversible()

    f, _rules = _contract(db, loan)  # contract integrity BEFORE any money moves
    # 2. obligations, ascending sequence (locked)
    obligations, _views = ledger.views(db, loan.id, lock=True)
    # 3. the payment row (and its applications, immutable)
    payment = db.scalar(
        select(CreditPayment)
        .where(CreditPayment.id == head.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    originals = db.scalars(
        select(CreditPaymentApplication)
        .where(CreditPaymentApplication.payment_id == payment.id)
        .order_by(CreditPaymentApplication.id)
    ).all()

    now = now_utc()
    business_date = ledger.business_date(db, loan, now)
    number = _next_number(db, actor.tenant_id, "credit_payment_reversal", "REV-")
    movement = None
    if payment.origin == "counter":
        # 4-5. cash box and session, in the same transaction (the port never commits)
        movement = cash_port.withdraw(
            db,
            tenant_id=actor.tenant_id,
            branch_id=payment.receiving_branch_id,
            session_id=body.cash_session_id,
            amount=payment.amount,
            currency=payment.currency_code,
            actor_user_id=actor.user_id,
            kind=REVERSAL_KIND,
            reference=number,
            notes=f"Reversion {number} del pago {payment.payment_number}",
            reverses_id=payment.cash_movement_id,
            require_cashier_id=actor.user_id,
        )
    reversal = _insert_reversal(db, actor, payment, loan, number, body, digest, now, business_date, movement)
    try:
        _mirror_applications(db, reversal, originals)
        db.flush()
        ledger.project(db, loan, obligations)
        db.flush()
    except IntegrityError:
        db.rollback()
        raise PaymentInvariantViolation() from None

    totals: dict[str, Decimal] = {}
    for a in originals:
        totals[a.component] = totals.get(a.component, Decimal(0)) + a.amount
    record_event(
        db,
        "payment.reversed",
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        client_ip=client_ip,
        details={
            "payment_id": payment.id,
            "payment_number": payment.payment_number,
            "reversal_id": reversal.id,
            "reversal_number": number,
            "loan_id": loan.id,
            "loan_number": loan.loan_number,
            "amount": _amt(payment.amount),
            "currency_code": payment.currency_code,
            "business_date": business_date.isoformat(),
            "receiving_branch_id": payment.receiving_branch_id,
            "reversal_branch_id": reversal.reversal_branch_id,
            "origin": payment.origin,
            "reason": body.reason,
            "cash_session_id": reversal.cash_session_id,
            "original_cash_movement_id": payment.cash_movement_id,
            "reversal_cash_movement_id": reversal.cash_movement_id,
            "component_totals": {k: _amt(v) for k, v in sorted(totals.items())},
            "loan_status": loan.status,
            "rules_digest": f.rules_hash,
            "contract_digest": f.contract_hash,
        },
    )
    db.commit()
    return {**_out(db, reversal, payment_number=payment.payment_number), "replayed": False}


def _insert_reversal(db, actor, payment, loan, number, body, digest, now, business_date, movement):
    reversal = CreditPaymentReversal(
        tenant_id=actor.tenant_id,
        payment_id=payment.id,
        loan_id=loan.id,
        reversal_number=number,
        amount=payment.amount,  # FULL reversal: never taken from the client
        currency_code=payment.currency_code,
        origin=payment.origin,
        reason=body.reason,
        reversed_by=actor.user_id,
        reversed_at=now,
        business_date=business_date,
        reversal_branch_id=body.reversal_branch_id,
        cash_session_id=movement.session_id if movement else None,
        cash_movement_id=movement.movement_id if movement else None,
        idempotency_key=body.idempotency_key,
        request_digest=digest,
    )
    db.add(reversal)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise PaymentInvariantViolation() from None
    return reversal


def _mirror_applications(db: Session, reversal: CreditPaymentReversal, originals: list) -> None:
    """One reversal application per original application, EXACTLY the same obligation / component / amount."""
    for a in originals:
        db.add(
            CreditPaymentReversalApplication(
                tenant_id=reversal.tenant_id,
                reversal_id=reversal.id,
                payment_id=a.payment_id,
                original_application_id=a.id,
                obligation_id=a.obligation_id,
                loan_id=a.loan_id,
                component=a.component,
                amount=a.amount,
            )
        )


# --- reads ------------------------------------------------------------------------------------------
def get_reversal(db: Session, actor: Principal, payment_id: int) -> dict:
    _gate_tenant(actor)
    p = db.scalar(
        select(CreditPayment).where(CreditPayment.id == payment_id, CreditPayment.tenant_id == actor.tenant_id)
    )
    if p is None:
        raise TenantMismatch()
    loan = db.get(CreditLoan, p.loan_id)
    if not _payment_allowed(actor, READ, p, loan):
        raise PermissionDenied()
    r = db.scalar(select(CreditPaymentReversal).where(CreditPaymentReversal.payment_id == p.id))
    if r is None:
        raise PaymentNotReversed()
    return _out(db, r, payment_number=p.payment_number)
