"""Cash core session HTTP API (v2, T-021). GET handlers never write. Amounts are strings."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.modules.cash import sessions as service
from app.modules.cash.schemas import AcceptHandoverIn, CloseIn, OpenIn
from app.modules.identity.authorization import Principal
from app.modules.identity.deps import client_ip, get_principal

router = APIRouter(prefix="/api/v2/cash", tags=["cash-sessions"])

Actor = Depends(get_principal)
Db = Depends(get_session)


@router.post("/sessions/open")
def open_session(body: OpenIn, request: Request, actor: Principal = Actor, db: Session = Db):
    out = service.open_session(
        db,
        actor,
        cash_point_id=body.cash_point_id,
        source=body.source,
        amount=body.amount,
        denominations=body.denominations,
        observation_note=body.observation_note,
        idempotency_key=body.idempotency_key,
        client_ip=client_ip(request),
    )
    db.commit()
    return out


@router.post("/sessions/{session_id}/close")
def close_session(session_id: int, body: CloseIn, request: Request, actor: Principal = Actor, db: Session = Db):
    out = service.close_session(
        db,
        actor,
        session_id,
        denominations=body.denominations,
        observation_note=body.observation_note,
        receiver_user_id=body.receiver_user_id,
        idempotency_key=body.idempotency_key,
        client_ip=client_ip(request),
    )
    db.commit()
    return out


@router.post("/handovers/{handover_id}/accept")
def accept_handover(
    handover_id: int, body: AcceptHandoverIn, request: Request, actor: Principal = Actor, db: Session = Db
):
    out = service.accept_handover(
        db, actor, handover_id, idempotency_key=body.idempotency_key, notes=body.notes, client_ip=client_ip(request)
    )
    db.commit()
    return out


@router.get("/sessions/current")
def current_session(cash_point_id: int = Query(gt=0), actor: Principal = Actor, db: Session = Db):
    return {"session": service.current_session(db, actor, cash_point_id)}


@router.get("/sessions/{session_id}")
def get_session_(session_id: int, actor: Principal = Actor, db: Session = Db):
    return service.get_session(db, actor, session_id)


@router.get("/handovers")
def list_handovers(
    state: Literal["pending", "confirmed"] = "pending",
    branch_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=100),
    before_id: int | None = Query(default=None, gt=0),
    actor: Principal = Actor,
    db: Session = Db,
):
    return service.list_handovers(db, actor, state=state, branch_id=branch_id, limit=limit, before_id=before_id)


@router.get("/differences")
def list_differences(
    status: Literal["pending_review", "under_review", "resolved", "dismissed"] = "pending_review",
    branch_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=100),
    before_id: int | None = Query(default=None, gt=0),
    actor: Principal = Actor,
    db: Session = Db,
):
    return service.list_differences(db, actor, status=status, branch_id=branch_id, limit=limit, before_id=before_id)
