"""Field custody HTTP API (v2, T-019). GET handlers never write. Amounts are strings. No PATCH of a state exists."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.modules.field_custody import service
from app.modules.field_custody.schemas import AcceptIn, CancelIn, DeclareIn, RefundIn, RejectIn
from app.modules.identity.authorization import Principal
from app.modules.identity.deps import client_ip, get_principal

router = APIRouter(prefix="/api/v2", tags=["field-custody"])

Actor = Depends(get_principal)
Db = Depends(get_session)
R = "/cash/field-renditions"


@router.post(R)
def declare(body: DeclareIn, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.declare(db, actor, body, client_ip(request))


@router.post(R + "/{rendition_id}/accept")
def accept(rendition_id: int, body: AcceptIn, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.accept(db, actor, rendition_id, body, client_ip(request))


@router.post(R + "/{rendition_id}/reject")
def reject(rendition_id: int, body: RejectIn, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.reject(db, actor, rendition_id, body, client_ip(request))


@router.post(R + "/{rendition_id}/cancel")
def cancel(rendition_id: int, body: CancelIn, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.cancel(db, actor, rendition_id, body, client_ip(request))


@router.get(R)
def list_renditions(
    receiving_branch_id: int | None = Query(default=None, gt=0),
    state: Literal["declared", "accepted", "rejected", "cancelled"] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=200),
    actor: Principal = Actor,
    db: Session = Db,
):
    return service.list_renditions(
        db, actor, receiving_branch_id=receiving_branch_id, state=state, limit=limit, cursor=cursor
    )


@router.get(R + "/{rendition_id}")
def get_rendition(rendition_id: int, actor: Principal = Actor, db: Session = Db):
    return service.get_rendition(db, actor, rendition_id)


@router.get("/cash/field-custody")
def list_custody(
    receiving_branch_id: int | None = Query(default=None, gt=0),
    custodian_user_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=200),
    actor: Principal = Actor,
    db: Session = Db,
):
    return service.list_custody(
        db,
        actor,
        receiving_branch_id=receiving_branch_id,
        custodian_user_id=custodian_user_id,
        limit=limit,
        cursor=cursor,
    )


@router.get("/cash/field-custody/summary")
def custody_summary(receiving_branch_id: int = Query(gt=0), actor: Principal = Actor, db: Session = Db):
    return service.custody_summary(db, actor, receiving_branch_id=receiving_branch_id)


@router.get("/payments/{payment_id}/field-custody")
def payment_custody(payment_id: int, actor: Principal = Actor, db: Session = Db):
    return service.payment_custody(db, actor, payment_id)


# --- T-020 field refund ---
@router.post("/payment-reversals/{reversal_id}/field-refund")
def field_refund(reversal_id: int, body: RefundIn, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.refund(db, actor, reversal_id, body, client_ip(request))


@router.get("/cash/field-refunds")
def list_field_refunds(
    status: Literal["pending", "refunded"] = "pending",
    receiving_branch_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=200),
    actor: Principal = Actor,
    db: Session = Db,
):
    return service.list_refunds(
        db, actor, status=status, receiving_branch_id=receiving_branch_id, limit=limit, cursor=cursor
    )
