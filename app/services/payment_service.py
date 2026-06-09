from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.loan import Loan, LoanStatus
from app.models.payment import Payment, PaymentType
from app.schemas.payment import PaymentCreate
from app.services.loan_service import refresh_loan_state, remaining_due

TWOPLACES = Decimal("0.01")


def q(value: Decimal) -> Decimal:
    return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def _apply_bucket(amount: Decimal, due: Decimal) -> tuple[Decimal, Decimal]:
    applied = min(amount, due)
    return q(applied), q(amount - applied)


def _apply_interest_only(installments: list, amount: Decimal) -> tuple[Decimal, Decimal]:
    remaining_amount = q(amount)
    interest_applied = Decimal("0.00")
    for installment in installments:
        interest_due = remaining_due(installment.interest_due, installment.interest_paid)
        applied, remaining_amount = _apply_bucket(remaining_amount, interest_due)
        installment.interest_paid = q(installment.interest_paid + applied)
        interest_applied = q(interest_applied + applied)
        if remaining_amount == 0:
            break
    return q(interest_applied), q(remaining_amount)


def _apply_principal_only(installments: list, amount: Decimal) -> tuple[Decimal, Decimal]:
    remaining_amount = q(amount)
    principal_applied = Decimal("0.00")
    for installment in installments:
        principal_due = remaining_due(installment.principal_due, installment.principal_paid)
        applied, remaining_amount = _apply_bucket(remaining_amount, principal_due)
        installment.principal_paid = q(installment.principal_paid + applied)
        principal_applied = q(principal_applied + applied)
        if remaining_amount == 0:
            break
    return q(principal_applied), q(remaining_amount)


def _apply_standard(installments: list, amount: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    remaining_amount = q(amount)
    principal_applied = Decimal("0.00")
    interest_applied = Decimal("0.00")
    late_fee_applied = Decimal("0.00")

    for installment in installments:
        fee_due = remaining_due(installment.fee_due, installment.fee_paid)
        applied, remaining_amount = _apply_bucket(remaining_amount, fee_due)
        installment.fee_paid = q(installment.fee_paid + applied)
        late_fee_applied = q(late_fee_applied + applied)

        interest_due = remaining_due(installment.interest_due, installment.interest_paid)
        applied, remaining_amount = _apply_bucket(remaining_amount, interest_due)
        installment.interest_paid = q(installment.interest_paid + applied)
        interest_applied = q(interest_applied + applied)

        principal_due = remaining_due(installment.principal_due, installment.principal_paid)
        applied, remaining_amount = _apply_bucket(remaining_amount, principal_due)
        installment.principal_paid = q(installment.principal_paid + applied)
        principal_applied = q(principal_applied + applied)

        if remaining_amount == 0:
            break

    return q(principal_applied), q(interest_applied), q(late_fee_applied), q(remaining_amount)


def apply_payment(db: Session, payload: PaymentCreate, collected_by_id: int) -> Payment:
    loan = db.get(Loan, payload.loan_id)
    if loan is None:
        raise HTTPException(status_code=404, detail="Prestamo no encontrado.")
    refresh_loan_state(loan)
    if loan.status not in {LoanStatus.active, LoanStatus.late}:
        raise HTTPException(status_code=400, detail="El prestamo no admite pagos.")

    now = datetime.now(UTC)
    total_amount = Decimal("0.00")
    principal_applied = Decimal("0.00")
    interest_applied = Decimal("0.00")
    late_fee_applied = Decimal("0.00")

    installments = list(loan.installments)

    if payload.amount is not None:
        total_amount = q(payload.amount)
        if payload.payment_type == "interest_only":
            interest_delta, _ = _apply_interest_only(installments, total_amount)
            interest_applied = q(interest_applied + interest_delta)
        elif payload.payment_type == "principal_only":
            principal_delta, _ = _apply_principal_only(installments, total_amount)
            principal_applied = q(principal_applied + principal_delta)
        else:
            principal_delta, interest_delta, fee_delta, _ = _apply_standard(installments, total_amount)
            principal_applied = q(principal_applied + principal_delta)
            interest_applied = q(interest_applied + interest_delta)
            late_fee_applied = q(late_fee_applied + fee_delta)
        resolved_payment_type = payload.payment_type
    else:
        if payload.installment_amount > 0:
            total_amount = q(total_amount + payload.installment_amount)
            principal_delta, interest_delta, fee_delta, _ = _apply_standard(installments, payload.installment_amount)
            principal_applied = q(principal_applied + principal_delta)
            interest_applied = q(interest_applied + interest_delta)
            late_fee_applied = q(late_fee_applied + fee_delta)

        if payload.principal_amount > 0:
            total_amount = q(total_amount + payload.principal_amount)
            principal_delta, _ = _apply_principal_only(installments, payload.principal_amount)
            principal_applied = q(principal_applied + principal_delta)

        if payload.interest_amount > 0:
            total_amount = q(total_amount + payload.interest_amount)
            interest_delta, _ = _apply_interest_only(installments, payload.interest_amount)
            interest_applied = q(interest_applied + interest_delta)

        if payload.custom_amount > 0:
            total_amount = q(total_amount + payload.custom_amount)
            principal_delta, interest_delta, fee_delta, _ = _apply_standard(installments, payload.custom_amount)
            principal_applied = q(principal_applied + principal_delta)
            interest_applied = q(interest_applied + interest_delta)
            late_fee_applied = q(late_fee_applied + fee_delta)

        component_count = sum(
            amount > 0 for amount in (
                payload.installment_amount,
                payload.principal_amount,
                payload.interest_amount,
                payload.custom_amount,
            )
        )
        if component_count > 1:
            resolved_payment_type = PaymentType.custom
        elif payload.installment_amount > 0:
            resolved_payment_type = PaymentType.installment
        elif payload.principal_amount > 0:
            resolved_payment_type = PaymentType.principal_only
        elif payload.interest_amount > 0:
            resolved_payment_type = PaymentType.interest_only
        else:
            resolved_payment_type = PaymentType.custom

    if principal_applied == 0 and interest_applied == 0 and late_fee_applied == 0:
        raise HTTPException(status_code=400, detail="El monto no pudo aplicarse al prestamo.")

    for installment in installments:
        pending_fee = remaining_due(installment.fee_due, installment.fee_paid)
        pending_interest = remaining_due(installment.interest_due, installment.interest_paid)
        pending_principal = remaining_due(installment.principal_due, installment.principal_paid)
        if pending_fee == 0 and pending_interest == 0 and pending_principal == 0:
            installment.status = "paid"
        elif installment.due_date < now.date():
            installment.status = "late"
        else:
            installment.status = "partial" if any([
                installment.fee_paid > 0,
                installment.interest_paid > 0,
                installment.principal_paid > 0,
            ]) else "pending"

    loan.principal_balance = q(max(Decimal("0.00"), loan.principal_balance - principal_applied))
    loan.interest_balance = q(max(Decimal("0.00"), loan.interest_balance - interest_applied))
    loan.late_fee_balance = q(max(Decimal("0.00"), loan.late_fee_balance - late_fee_applied))

    if loan.principal_balance == 0 and loan.interest_balance == 0 and loan.late_fee_balance == 0:
        loan.status = LoanStatus.paid
    elif any(installment.status == "late" for installment in installments):
        loan.status = LoanStatus.late
    else:
        loan.status = LoanStatus.active

    payment = Payment(
        loan_id=loan.id,
        collected_by_id=collected_by_id,
        payment_type=resolved_payment_type,
        amount=q(total_amount),
        principal_applied=principal_applied,
        interest_applied=interest_applied,
        late_fee_applied=late_fee_applied,
        notes=payload.notes,
        reference_code=payload.reference_code,
    )
    db.add(payment)
    return payment
