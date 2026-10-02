"""Loan HTTP API (v2). GET handlers never write. Amounts are strings."""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.modules.identity.authorization import Principal
from app.modules.identity.deps import client_ip, get_principal
from app.modules.loans import service
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
