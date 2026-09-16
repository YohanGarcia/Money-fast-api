from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.services import cash_service
from app.api.deps import get_company_id, get_current_user, get_db, require_admin_manager
from app.models.customer import Customer
from app.models.route import Route
from app.models.user import User, UserRole
from app.schemas.customer import CustomerCreate, CustomerRead, CustomerUpdate
from app.services.plan_limits import enforce_can_create
from app.services.customer_profile import check_identity, commit_customer, legacy_references, document_key, identity_for_update

router = APIRouter()


def _validate_collector(db: Session, collector_id: int | None, company_id: int) -> int | None:
    """Ensure an assigned collector belongs to the company and has the collector role."""
    if collector_id is None:
        return None
    collector = db.scalar(
        select(User).where(User.id == collector_id, User.company_id == company_id)
    )
    if collector is None:
        raise HTTPException(status_code=404, detail="El cobrador asignado no existe.")
    if collector.role != UserRole.collector:
        raise HTTPException(status_code=400, detail="El usuario asignado no es un cobrador.")
    return collector.id


def _resolve_route_and_collector(
    db: Session, route_id: int | None, collector_id: int | None, company_id: int
) -> tuple[int | None, int | None]:
    """Route → Collector: when a route is set, the collector is derived from it.

    Falls back to the directly-assigned collector only when there is no route.
    """
    if route_id is not None:
        route = db.scalar(select(Route).where(Route.id == route_id, Route.company_id == company_id))
        if route is None:
            raise HTTPException(status_code=404, detail="Ruta no encontrada.")
        return route.id, route.assigned_collector_id
    return None, _validate_collector(db, collector_id, company_id)


@router.get("", response_model=list[CustomerRead])
def list_customers(
    q: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_company_id),
) -> list[Customer]:
    statement = (
        select(Customer)
        .where(Customer.company_id == company_id)
        .options(selectinload(Customer.assigned_collector), selectinload(Customer.route))
        .order_by(Customer.full_name)
    )
    # Collectors only see the customers assigned to them (their portfolio).
    if current_user.role == UserRole.cashier:
        statement = statement.where(Customer.id.in_(cash_service.customer_ids(db, current_user)))
    if current_user.role == UserRole.collector:
        statement = statement.where(Customer.assigned_collector_id == current_user.id)

    customers = db.scalars(statement).all()
    if q:
        term = q.lower()
        customers = [c for c in customers if term in c.full_name.lower() or
                     (document_key(q) and document_key(q) in (document_key(c.document_id) or ""))]
    return list(customers)


@router.post("", response_model=CustomerRead, status_code=status.HTTP_201_CREATED)
def create_customer(
    payload: CustomerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> Customer:
    enforce_can_create(db, company_id, "customer")
    route_id, collector_id = _resolve_route_and_collector(
        db, payload.route_id, payload.assigned_collector_id, company_id
    )
    data = payload.model_dump(exclude={"assigned_collector_id", "route_id"})
    data["document_key"] = check_identity(db, company_id, payload.document_id)
    if "references" not in payload.model_fields_set:
        data["references"] = legacy_references(payload.notes)
    customer = Customer(
        **data,
        created_by_id=current_user.id,
        company_id=company_id,
        route_id=route_id,
        assigned_collector_id=collector_id,
    )
    db.add(customer)
    return commit_customer(db, customer)


def _get_scoped_customer(
    customer_id: int, db: Session, current_user: User, company_id: int
) -> Customer:
    statement = (
        select(Customer)
        .where(Customer.id == customer_id, Customer.company_id == company_id)
        .options(selectinload(Customer.assigned_collector), selectinload(Customer.route))
    )
    if current_user.role == UserRole.cashier:
        statement = statement.where(Customer.id.in_(cash_service.customer_ids(db, current_user)))
    if current_user.role == UserRole.collector:
        statement = statement.where(Customer.assigned_collector_id == current_user.id)
    customer = db.scalar(statement)
    if customer is None:
        raise HTTPException(status_code=404, detail="Cliente no encontrado.")
    return customer


@router.get("/{customer_id}", response_model=CustomerRead)
def get_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_company_id),
) -> Customer:
    return _get_scoped_customer(customer_id, db, current_user, company_id)


@router.put("/{customer_id}", response_model=CustomerRead)
def update_customer(
    customer_id: int,
    payload: CustomerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> Customer:
    customer = db.scalar(
        select(Customer).where(Customer.id == customer_id, Customer.company_id == company_id)
    )
    if customer is None:
        raise HTTPException(status_code=404, detail="Cliente no encontrado.")

    if payload.version != customer.version:
        raise HTTPException(409, "La ficha cambió. Recarga antes de guardar.")
    customer.document_key = identity_for_update(db, company_id,
        payload.document_id if "document_id" in payload.model_fields_set else customer.document_id, customer)
    route_id, collector_id = _resolve_route_and_collector(
        db, payload.route_id if "route_id" in payload.model_fields_set else customer.route_id,
        payload.assigned_collector_id if "assigned_collector_id" in payload.model_fields_set else customer.assigned_collector_id, company_id
    )
    for field, value in payload.model_dump(exclude={"assigned_collector_id", "route_id", "version"}, exclude_unset=True).items():
        setattr(customer, field, value)
    if cash_service.enabled(db, company_id):
        collector_branch = db.get(User, collector_id).branch_id if collector_id else None
        route_branch = db.get(Route, route_id).branch_id if route_id else None
        if collector_id and not collector_branch:
            raise HTTPException(409, 'Asigna una sucursal al cobrador antes de asignarle clientes.')
        if route_branch and collector_branch and route_branch != collector_branch:
            raise HTTPException(409, 'La ruta y el cobrador deben pertenecer a la misma sucursal.')
        # Only the current portfolio changes. Payment custody stays on its recorded branch and collector.
        customer.cash_branch_id = route_branch or collector_branch or customer.cash_branch_id
    customer.route_id = route_id
    customer.assigned_collector_id = collector_id

    return commit_customer(db, customer)
