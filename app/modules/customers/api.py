"""Customer HTTP API (v2). GET handlers never write. Responses use purpose-built DTOs."""

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.modules.customers import service
from app.modules.customers.schemas import (
    AddressIn,
    AddressOut,
    BranchAssignIn,
    ContactIn,
    ContactOut,
    CustomerCreateIn,
    CustomerDetailOut,
    CustomerPatchIn,
    CustomerSummaryOut,
    DuplicateCheckIn,
    DuplicateResultOut,
    FlagOut,
    FlagReviewIn,
    IdentityCorrectionIn,
    ReferenceIn,
    ReferenceOut,
)
from app.modules.identity.authorization import Principal
from app.modules.identity.deps import client_ip, get_principal

router = APIRouter(prefix="/api/v2/customers", tags=["customers"])


@router.get("", response_model=list[CustomerSummaryOut])
def list_customers(
    q: str | None = Query(default=None, max_length=100),
    status_filter: str | None = Query(default=None, alias="status"),
    branch_id: int | None = None,
    code: str | None = Query(default=None, max_length=20),
    document: str | None = Query(default=None, max_length=40),
    phone: str | None = Query(default=None, max_length=30),
    email: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.list_customers(
        db,
        actor,
        q=q,
        status=status_filter,
        branch_id=branch_id,
        code=code,
        document=document,
        phone=phone,
        email=email,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=CustomerDetailOut, status_code=status.HTTP_201_CREATED)
def create_customer(
    body: CustomerCreateIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    profile = service.create_customer(db, actor, body, client_ip(request))
    return service.get_customer(db, actor, profile.id)


@router.post("/duplicate-check", response_model=DuplicateResultOut)
def duplicate_check(
    body: DuplicateCheckIn, actor: Principal = Depends(get_principal), db: Session = Depends(get_session)
):
    """Classification preview (EXACT_MATCH | POSSIBLE_MATCH | NO_MATCH). Writes nothing."""
    return service.check_duplicates(db, actor, body)


@router.post("/duplicates/{flag_id}/review")
def review_flag(
    flag_id: int,
    body: FlagReviewIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.review_flag(db, actor, flag_id, body.resolution, body.note, client_ip(request))


@router.get("/{customer_id}", response_model=CustomerDetailOut)
def get_customer(customer_id: int, actor: Principal = Depends(get_principal), db: Session = Depends(get_session)):
    return service.get_customer(db, actor, customer_id)


@router.patch("/{customer_id}", response_model=CustomerDetailOut)
def patch_customer(
    customer_id: int,
    body: CustomerPatchIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.update_customer(db, actor, customer_id, body, client_ip(request))


@router.patch("/{customer_id}/identity", response_model=CustomerDetailOut)
def correct_identity(
    customer_id: int,
    body: IdentityCorrectionIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.correct_identity(db, actor, customer_id, body, client_ip(request))


@router.post("/{customer_id}/activate", response_model=CustomerDetailOut)
def activate(
    customer_id: int, request: Request, actor: Principal = Depends(get_principal), db: Session = Depends(get_session)
):
    return service.set_status(db, actor, customer_id, "activate", client_ip(request))


@router.post("/{customer_id}/deactivate", response_model=CustomerDetailOut)
def deactivate(
    customer_id: int, request: Request, actor: Principal = Depends(get_principal), db: Session = Depends(get_session)
):
    return service.set_status(db, actor, customer_id, "deactivate", client_ip(request))


@router.put("/{customer_id}/management-branch", response_model=CustomerDetailOut)
def assign_branch(
    customer_id: int,
    body: BranchAssignIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.assign_management_branch(db, actor, customer_id, body, client_ip(request))


@router.get("/{customer_id}/duplicates", response_model=list[FlagOut])
def duplicates(customer_id: int, actor: Principal = Depends(get_principal), db: Session = Depends(get_session)):
    return service.list_flags(db, actor, customer_id)


# --- contacts ----------------------------------------
@router.get("/{customer_id}/contacts", response_model=list[ContactOut])
def list_contacts(
    customer_id: int,
    include_inactive: bool = False,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.list_contacts(db, actor, customer_id, include_inactive)


@router.post("/{customer_id}/contacts", response_model=ContactOut, status_code=status.HTTP_201_CREATED)
def add_contact(
    customer_id: int,
    body: ContactIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.add_contact(db, actor, customer_id, body, client_ip(request))


@router.post("/{customer_id}/contacts/{contact_id}/primary", response_model=ContactOut)
def primary_contact(
    customer_id: int,
    contact_id: int,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.set_primary_contact(db, actor, customer_id, contact_id, client_ip(request))


@router.post("/{customer_id}/contacts/{contact_id}/deactivate", response_model=ContactOut)
def deactivate_contact(
    customer_id: int,
    contact_id: int,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.deactivate_contact(db, actor, customer_id, contact_id, client_ip(request))


# --- addresses ----------------------------------------
@router.get("/{customer_id}/addresses", response_model=list[AddressOut])
def list_addresses(
    customer_id: int,
    include_inactive: bool = False,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.list_addresses(db, actor, customer_id, include_inactive)


@router.post("/{customer_id}/addresses", response_model=AddressOut, status_code=status.HTTP_201_CREATED)
def add_address(
    customer_id: int,
    body: AddressIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.add_address(db, actor, customer_id, body, client_ip(request))


@router.post("/{customer_id}/addresses/{address_id}/primary", response_model=AddressOut)
def primary_address(
    customer_id: int,
    address_id: int,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.set_primary_address(db, actor, customer_id, address_id, client_ip(request))


@router.post("/{customer_id}/addresses/{address_id}/deactivate", response_model=AddressOut)
def deactivate_address(
    customer_id: int,
    address_id: int,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.deactivate_address(db, actor, customer_id, address_id, client_ip(request))


# --- references ----------------------------------------
@router.get("/{customer_id}/references", response_model=list[ReferenceOut])
def list_references(
    customer_id: int,
    include_inactive: bool = False,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.list_references(db, actor, customer_id, include_inactive)


@router.post("/{customer_id}/references", response_model=ReferenceOut, status_code=status.HTTP_201_CREATED)
def add_reference(
    customer_id: int,
    body: ReferenceIn,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.add_reference(db, actor, customer_id, body, client_ip(request))


@router.post("/{customer_id}/references/{reference_id}/deactivate", response_model=ReferenceOut)
def deactivate_reference(
    customer_id: int,
    reference_id: int,
    request: Request,
    actor: Principal = Depends(get_principal),
    db: Session = Depends(get_session),
):
    return service.deactivate_reference(db, actor, customer_id, reference_id, client_ip(request))
