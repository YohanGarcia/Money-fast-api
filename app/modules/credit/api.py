"""Credit product HTTP API (v2). GET handlers never write; the simulation endpoint is a POST only because it
carries a body, and it performs no write of any kind (not even an audit row)."""

from datetime import date

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.modules.credit import service
from app.modules.credit.schemas import (
    ProductCreateIn,
    ProductOut,
    ProductSummaryOut,
    PublishIn,
    ReasonIn,
    SimulateIn,
    SnapshotOut,
    ValidationOut,
    VersionCreateIn,
    VersionOut,
    VersionUpdateIn,
)
from app.modules.identity.authorization import Principal
from app.modules.identity.deps import client_ip, get_principal

router = APIRouter(prefix="/api/v2/credit-products", tags=["credit-products"])


@router.get("", response_model=list[ProductSummaryOut])
def list_products(
    status_filter: str | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.list_products(db, actor, status=status_filter, q=q, limit=limit, offset=offset)


@router.post("", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
def create_product(
    body: ProductCreateIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.create_product(db, actor, body, client_ip(request))


@router.get("/{product_id}", response_model=ProductOut)
def get_product(product_id: int, actor: Principal = Depends(get_principal), db: Session = Depends(get_session)):
    return service.get_product(db, actor, product_id)


@router.get("/{product_id}/effective", response_model=VersionOut)
def effective_version(
    product_id: int,
    on: date | None = None,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.effective_version(db, actor, product_id, on)


@router.post("/{product_id}/deactivate", response_model=ProductOut)
def deactivate(
    product_id: int,
    body: ReasonIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.set_product_active(
        db, actor, product_id, active=False, reason=body.reason, client_ip=client_ip(request)
    )


@router.post("/{product_id}/activate", response_model=ProductOut)
def activate(
    product_id: int,
    body: ReasonIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.set_product_active(
        db, actor, product_id, active=True, reason=body.reason, client_ip=client_ip(request)
    )


@router.post("/{product_id}/versions", response_model=VersionOut, status_code=status.HTTP_201_CREATED)
def create_version(
    product_id: int,
    body: VersionCreateIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.create_version(db, actor, product_id, body, client_ip(request))


@router.get("/{product_id}/versions/{version_id}", response_model=VersionOut)
def get_version(
    product_id: int, version_id: int, actor: Principal = Depends(get_principal), db: Session = Depends(get_session)
):
    return service.get_version(db, actor, product_id, version_id)


@router.put("/{product_id}/versions/{version_id}", response_model=VersionOut)
def update_version(
    product_id: int,
    version_id: int,
    body: VersionUpdateIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.update_version(db, actor, product_id, version_id, body, client_ip(request))


@router.post("/{product_id}/versions/{version_id}/validate", response_model=ValidationOut)
def validate_version(
    product_id: int,
    version_id: int,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.validate_version(db, actor, product_id, version_id, client_ip(request))


@router.post("/{product_id}/versions/{version_id}/publish", response_model=VersionOut)
def publish_version(
    product_id: int,
    version_id: int,
    body: PublishIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.publish_version(db, actor, product_id, version_id, body, client_ip(request))


@router.post("/{product_id}/versions/{version_id}/retire", response_model=VersionOut)
def retire_version(
    product_id: int,
    version_id: int,
    body: ReasonIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.retire_version(db, actor, product_id, version_id, body.reason, client_ip(request))


@router.get("/{product_id}/versions/{version_id}/snapshot", response_model=SnapshotOut)
def get_snapshot(
    product_id: int, version_id: int, actor: Principal = Depends(get_principal), db: Session = Depends(get_session)
):
    return service.get_snapshot(db, actor, product_id, version_id)


@router.post("/{product_id}/versions/{version_id}/simulate")
def simulate(
    product_id: int,
    version_id: int,
    body: SimulateIn,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.simulate(db, actor, product_id, version_id, body)
