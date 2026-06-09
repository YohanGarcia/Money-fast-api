from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from app.models.loan import Loan, LoanInstallment, LoanStatus, PaymentFrequency
from app.schemas.loan import LoanCreate

TWOPLACES = Decimal("0.01")


def q(value: Decimal) -> Decimal:
    return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def remaining_due(current: Decimal, paid: Decimal) -> Decimal:
    return max(Decimal("0.00"), q(current - paid))


def frequency_delta(start_date: date, frequency: PaymentFrequency, step: int) -> date:
    if frequency == PaymentFrequency.daily:
        return start_date + timedelta(days=step)
    if frequency == PaymentFrequency.weekly:
        return start_date + timedelta(days=7 * step)
    if frequency == PaymentFrequency.biweekly:
        return start_date + timedelta(days=14 * step)
    return start_date + timedelta(days=30 * step)


def refresh_loan_state(loan: Loan, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    changed = False

    if loan.status in {LoanStatus.cancelled, LoanStatus.pending_approval}:
        return changed

    for installment in loan.installments:
        overdue_days = max((now.date() - installment.due_date).days - loan.grace_days, 0)
        if overdue_days > 0 and installment.status != "paid":
            pending_base = remaining_due(
                installment.principal_due + installment.interest_due,
                installment.principal_paid + installment.interest_paid,
            )
            expected_fee = q(pending_base * loan.late_fee_rate / Decimal("100"))
            if expected_fee > installment.fee_due:
                delta = q(expected_fee - installment.fee_due)
                installment.fee_due = expected_fee
                loan.late_fee_balance = q(loan.late_fee_balance + delta)
                changed = True

    for installment in loan.installments:
        pending_fee = remaining_due(installment.fee_due, installment.fee_paid)
        pending_interest = remaining_due(installment.interest_due, installment.interest_paid)
        pending_principal = remaining_due(installment.principal_due, installment.principal_paid)
        previous_status = installment.status

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

        if installment.status != previous_status:
            changed = True

    previous_loan_status = loan.status
    if loan.principal_balance == 0 and loan.interest_balance == 0 and loan.late_fee_balance == 0:
        loan.status = LoanStatus.paid
    elif any(installment.status == "late" for installment in loan.installments):
        loan.status = LoanStatus.late
    else:
        loan.status = LoanStatus.active

    if loan.status != previous_loan_status:
        changed = True

    return changed


def create_loan(db: Session, payload: LoanCreate, created_by_id: int) -> Loan:
    principal = q(payload.principal_amount)
    interest_total = q(principal * payload.interest_rate / Decimal("100"))
    total_amount = q(principal + interest_total)
    installment_amount = q(total_amount / payload.installment_count)
    principal_per_installment = q(principal / payload.installment_count)
    interest_per_installment = q(interest_total / payload.installment_count)

    end_date = frequency_delta(payload.start_date, payload.payment_frequency, payload.installment_count - 1)

    loan = Loan(
        customer_id=payload.customer_id,
        created_by_id=created_by_id,
        principal_amount=principal,
        interest_rate=q(payload.interest_rate),
        late_fee_rate=q(payload.late_fee_rate),
        grace_days=payload.grace_days,
        installment_count=payload.installment_count,
        payment_frequency=payload.payment_frequency,
        start_date=payload.start_date,
        end_date=end_date,
        principal_balance=principal,
        interest_balance=interest_total,
        late_fee_balance=Decimal("0.00"),
        total_amount=total_amount,
        installment_amount=installment_amount,
        status=LoanStatus.active if payload.auto_approve else LoanStatus.pending_approval,
        route_name=payload.route_name,
        requires_promissory_note=payload.requires_promissory_note,
    )
    db.add(loan)
    db.flush()

    principal_allocated = Decimal("0.00")
    interest_allocated = Decimal("0.00")

    for sequence in range(1, payload.installment_count + 1):
        principal_due = principal_per_installment
        interest_due = interest_per_installment

        if sequence == payload.installment_count:
            principal_due = q(principal - principal_allocated)
            interest_due = q(interest_total - interest_allocated)

        principal_allocated += principal_due
        interest_allocated += interest_due

        installment = LoanInstallment(
            loan_id=loan.id,
            sequence_number=sequence,
            due_date=frequency_delta(payload.start_date, payload.payment_frequency, sequence - 1),
            principal_due=principal_due,
            interest_due=interest_due,
            fee_due=Decimal("0.00"),
            status="pending",
        )
        db.add(installment)

    return loan
