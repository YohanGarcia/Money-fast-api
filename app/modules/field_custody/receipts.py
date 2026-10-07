"""Custody birth (T-019): ONE receipt per field payment, written by ``payments.pay`` inside the payment's own transaction.
Kept apart from ``service`` so the payment command imports nothing else of the custody module."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.field_custody.errors import CustodianInactive
from app.modules.field_custody.models import CreditFieldCustodyReceipt as Receipt
from app.modules.loans.models import CreditPayment


def require_active_user(db: Session, user_id: int) -> None:
    """The custodian must be active. ``FOR SHARE`` serialises with a concurrent disable (which locks the user row first)."""
    status = db.execute(text("SELECT status FROM users WHERE id = :u FOR SHARE"), {"u": user_id}).scalar()
    if status != "active":
        raise CustodianInactive()


def create_receipt(db: Session, payment: CreditPayment) -> Receipt:
    """ONE immutable receipt for a field payment, same transaction; any failure rolls the payment back."""
    require_active_user(db, payment.collected_by)
    receipt = Receipt(
        tenant_id=payment.tenant_id,
        payment_id=payment.id,
        loan_id=payment.loan_id,
        receiving_branch_id=payment.receiving_branch_id,
        custodian_user_id=payment.collected_by,  # the authenticated actor: no proxy collection exists
        currency_code=payment.currency_code,
        amount=payment.amount,
        received_at=payment.received_at,
    )
    db.add(receipt)
    db.flush()
    return receipt
