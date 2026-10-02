"""Credit origination HTTP API (v2). GET handlers never write. Responses are plain dicts whose amounts are strings."""

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.modules.identity.authorization import Principal
from app.modules.identity.deps import client_ip, get_principal
from app.modules.origination import service
from app.modules.origination.schemas import (
    ApplicationCreateIn,
    ApplicationPatchIn,
    ApproveIn,
    ConditionResolveIn,
    DocumentLinkIn,
    DocumentStatusIn,
    EvaluationIn,
    LimitIn,
    PolicyIn,
    ReasonIn,
    RejectIn,
)

router = APIRouter(prefix="/api/v2/credit-applications", tags=["credit-origination"])
policy_router = APIRouter(prefix="/api/v2/credit-approval-policies", tags=["credit-origination"])
limit_router = APIRouter(prefix="/api/v2/credit-approval-limits", tags=["credit-origination"])

Actor = Depends(get_principal)
Db = Depends(get_session)


@router.get("")
def list_applications(
    status_filter: str | None = Query(default=None, alias="status"),
    customer_id: int | None = None,
    product_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    actor: Principal = Actor,
    db: Session = Db,
):
    return service.list_applications(
        db, actor, status=status_filter, customer_id=customer_id, product_id=product_id, limit=limit, offset=offset
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_application(body: ApplicationCreateIn, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.create_application(db, actor, body, client_ip(request))


@router.get("/{application_id}")
def get_application(application_id: int, actor: Principal = Actor, db: Session = Db):
    return service.get_application(db, actor, application_id)


@router.patch("/{application_id}")
def update_draft(
    application_id: int, body: ApplicationPatchIn, request: Request, actor: Principal = Actor, db: Session = Db
):
    return service.update_draft(db, actor, application_id, body, client_ip(request))


@router.post("/{application_id}/submit")
def submit(application_id: int, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.submit(db, actor, application_id, client_ip(request))


@router.post("/{application_id}/reopen")
def reopen(application_id: int, body: ReasonIn, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.reopen(db, actor, application_id, body.reason, client_ip(request))


@router.post("/{application_id}/start-review")
def start_review(application_id: int, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.start_review(db, actor, application_id, client_ip(request))


@router.post("/{application_id}/evaluate", status_code=status.HTTP_201_CREATED)
def evaluate(application_id: int, body: EvaluationIn, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.add_evaluation(db, actor, application_id, body, client_ip(request))


@router.get("/{application_id}/evaluations")
def evaluations(application_id: int, actor: Principal = Actor, db: Session = Db):
    return service.list_evaluations(db, actor, application_id)


@router.post("/{application_id}/approve")
def approve(application_id: int, body: ApproveIn, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.approve(db, actor, application_id, body, client_ip(request))


@router.post("/{application_id}/reject")
def reject(application_id: int, body: RejectIn, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.reject(db, actor, application_id, body, client_ip(request))


@router.post("/{application_id}/cancel")
def cancel(application_id: int, body: ReasonIn, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.cancel(db, actor, application_id, body.reason, client_ip(request))


@router.post("/{application_id}/formalize")
def formalize(application_id: int, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.formalize(db, actor, application_id, client_ip(request))


@router.get("/{application_id}/formalization")
def get_formalization(application_id: int, actor: Principal = Actor, db: Session = Db):
    return service.get_formalization(db, actor, application_id)


@router.post("/{application_id}/conditions/{condition_id}/resolve")
def resolve_condition(
    application_id: int,
    condition_id: int,
    body: ConditionResolveIn,
    request: Request,
    actor: Principal = Actor,
    db: Session = Db,
):
    return service.resolve_condition(db, actor, application_id, condition_id, body, client_ip(request))


@router.post("/{application_id}/documents", status_code=status.HTTP_201_CREATED)
def add_document(
    application_id: int, body: DocumentLinkIn, request: Request, actor: Principal = Actor, db: Session = Db
):
    return service.add_document_link(db, actor, application_id, body, client_ip(request))


@router.post("/{application_id}/documents/{link_id}/status")
def document_status(
    application_id: int,
    link_id: int,
    body: DocumentStatusIn,
    request: Request,
    actor: Principal = Actor,
    db: Session = Db,
):
    return service.set_document_status(db, actor, application_id, link_id, body, client_ip(request))


@policy_router.get("")
def list_policies(actor: Principal = Actor, db: Session = Db):
    return service.list_policies(db, actor)


@policy_router.put("")
def set_policy(body: PolicyIn, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.set_policy(db, actor, body, client_ip(request))


@limit_router.get("")
def list_limits(actor: Principal = Actor, db: Session = Db):
    return service.list_limits(db, actor)


@limit_router.post("", status_code=status.HTTP_201_CREATED)
def create_limit(body: LimitIn, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.create_limit(db, actor, body, client_ip(request))


@limit_router.post("/{limit_id}/revoke")
def revoke_limit(limit_id: int, request: Request, actor: Principal = Actor, db: Session = Db):
    return service.revoke_limit(db, actor, limit_id, client_ip(request))
