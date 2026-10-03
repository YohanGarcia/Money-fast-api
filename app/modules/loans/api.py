"""Loan HTTP API (v2). GET handlers never write. Amounts are strings."""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.modules.identity.authorization import Principal
from app.modules.identity.deps import client_ip, get_principal
from app.modules.loans import overdue, payments, reversals, service, worklist
from app.modules.loans.payment_schemas import PaymentIn
from app.modules.loans.reversal_schemas import ReversalIn
from app.modules.loans.schemas import DisburseIn

router = APIRouter(prefix="/api/v2", tags=["loans"])

Actor = Depends(get_principal)
Db = Depends(get_session)


@router.post("/credit-formalizations/{formalization_id}/disburse")
def disburse(formalization_id: int, body: DisburseIn, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.disburse(db, actor, formalization_id, body, client_ip(request))


@router.get("/loans")
def list_loans(
    status_filter: str | None = Query(default=None, alias="status"),
    customer_id: int | None = None,
    formalization_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    actor: Principal = Actor,
    db: Session = Db,
):
    return service.list_loans(
        db,
        actor,
        status=status_filter,
        customer_id=customer_id,
        formalization_id=formalization_id,
        limit=limit,
        offset=offset,
    )


@router.get("/loans/{loan_id}")
def get_loan(loan_id: int, actor: Principal = Actor, db: Session = Db):
    return service.get_loan(db, actor, loan_id)


@router.get("/loans/{loan_id}/schedule")
def get_schedule(loan_id: int, actor: Principal = Actor, db: Session = Db):
    return service.get_schedule(db, actor, loan_id)


@router.get("/loans/{loan_id}/balances")
def get_balances(loan_id: int, actor: Principal = Actor, db: Session = Db):
    return service.get_balances(db, actor, loan_id)


@router.post("/loans/{loan_id}/payments")
def create_payment(loan_id: int, body: PaymentIn, request: Request, actor: Principal = Actor, db: Session = Db):
    return payments.pay(db, actor, loan_id, body, client_ip(request))


@router.get("/loans/{loan_id}/payments")
def list_payments(
    loan_id: int,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    actor: Principal = Actor,
    db: Session = Db,
):
    return payments.list_payments(db, actor, loan_id, limit=limit, offset=offset)


@router.get("/payments/{payment_id}")
def get_payment(payment_id: int, actor: Principal = Actor, db: Session = Db):
    return payments.get_payment(db, actor, payment_id)


@router.post("/payments/{payment_id}/reversals")
def reverse_payment(payment_id: int, body: ReversalIn, request: Request, actor: Principal = Actor, db: Session = Db):
    return reversals.reverse(db, actor, payment_id, body, client_ip(request))


@router.get("/payments/{payment_id}/reversal")
def get_reversal(payment_id: int, actor: Principal = Actor, db: Session = Db):
    return reversals.get_reversal(db, actor, payment_id)


@router.post("/loans/{loan_id}/delinquency/assess")
def assess_loan(loan_id: int, request: Request, actor: Principal = Actor, db: Session = Db):
    """Projects the stored loan status from the net ledger. Calculates NO delinquency charge (T-010)."""
    return overdue.assess(db, actor, loan_id, client_ip(request))


@router.get("/collections/overdue-loans")
def overdue_loans(
    branch_id: int | None = Query(default=None, gt=0),
    min_days_overdue: int | None = Query(default=None, ge=0),
    currency: str | None = Query(default=None, min_length=3, max_length=3),
    sort: worklist.Sort = "days_overdue",
    order: worklist.Order = "desc",
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=300),
    actor: Principal = Actor,
    db: Session = Db,
):
    """READ-ONLY collection worklist (T-011): loans with overdue net debt. Zero writes, no PII, no score, no bucket."""
    return worklist.overdue_loans(
        db,
        actor,
        branch_id=branch_id,
        min_days_overdue=min_days_overdue,
        currency=currency,
        sort=sort,
        order=order,
        limit=limit,
        cursor=cursor,
    )
