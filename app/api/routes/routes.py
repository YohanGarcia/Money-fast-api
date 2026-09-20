from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_company_id, get_current_user, get_db, require_admin_manager
from app.models.branch import Branch
from app.models.customer import Customer
from app.models.route import Route, RouteArea
from app.models.user import User, UserRole
from app.schemas.route import (
    RouteCreate,
    RouteRead,
    RouteStops,
    RouteSuggestion,
    RouteUpdate,
)
from app.services import cash_service
from app.services.route_assignment import suggest_routes
from app.services.route_service import loan_status_by_customer, order_stops_nearest_neighbor

router = APIRouter()


def _validate_collector(db: Session, collector_id: int | None, company_id: int) -> int | None:
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


def _validate_branch(db: Session, branch_id: int | None, company_id: int) -> int | None:
    if branch_id is None:
        return None
    branch = db.scalar(select(Branch).where(Branch.id == branch_id, Branch.company_id == company_id))
    if branch is None:
        raise HTTPException(status_code=404, detail="La sucursal no existe.")
    return branch.id


def _customer_count(db: Session, route_id: int) -> int:
    return db.scalar(
        select(func.count()).select_from(Customer).where(Customer.route_id == route_id)
    ) or 0


def _replace_areas(db: Session, route: Route, areas: list) -> None:
    """Replace-all: clear the route's areas and insert the given ones."""
    from app.services.route_assignment import normalize

    db.execute(RouteArea.__table__.delete().where(RouteArea.route_id == route.id))
    for area in areas:
        name = area.name.strip()
        if not name:
            continue
        db.add(
            RouteArea(
                route_id=route.id,
                area_type=area.area_type.value,
                name=name,
                normalized_name=normalize(name),
            )
        )


def _serialize(db: Session, route: Route) -> dict:
    return {
        "id": route.id,
        "name": route.name,
        "zone": route.zone,
        "description": route.description,
        "assigned_collector_id": route.assigned_collector_id,
        "branch_id": route.branch_id,
        "is_active": route.is_active,
        "company_id": route.company_id,
        "collector_name": route.collector_name,
        "branch_name": route.branch_name,
        "customer_count": _customer_count(db, route.id),
        "areas": [
            {"id": a.id, "area_type": a.area_type, "name": a.name}
            for a in sorted(route.areas, key=lambda a: a.name)
        ],
        "boundary": route.boundary or [],
        "created_at": route.created_at,
    }


@router.get("", response_model=list[RouteRead])
def list_routes(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_company_id),
) -> list[dict]:
    statement = (
        select(Route)
        .where(Route.company_id == company_id)
        .options(selectinload(Route.assigned_collector), selectinload(Route.branch))
        .order_by(Route.name)
    )
    # Collectors only see the routes assigned to them.
    if current_user.role == UserRole.collector:
        statement = statement.where(Route.assigned_collector_id == current_user.id)
    return [_serialize(db, r) for r in db.scalars(statement).all()]


@router.get("/{route_id}/stops", response_model=RouteStops)
def route_stops(
    route_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_company_id),
) -> dict:
    """Customers of a route as map stops, ordered by nearest-neighbor.

    Stops without GPS coordinates are returned separately as ``unlocated``.
    """
    statement = select(Route).where(Route.id == route_id, Route.company_id == company_id)
    if current_user.role == UserRole.collector:
        statement = statement.where(Route.assigned_collector_id == current_user.id)
    route = db.scalar(statement)
    if route is None:
        raise HTTPException(status_code=404, detail="Ruta no encontrada.")

    customers = db.scalars(select(Customer).where(Customer.route_id == route_id)).all()
    loan_status = loan_status_by_customer(db, [c.id for c in customers])

    located: list[dict] = []
    unlocated: list[dict] = []
    for c in customers:
        if c.latitude is not None and c.longitude is not None:
            located.append({
                "id": c.id, "full_name": c.full_name, "address": c.address,
                "lat": float(c.latitude), "lng": float(c.longitude),
            })
        else:
            unlocated.append({
                "id": c.id, "full_name": c.full_name, "address": c.address,
                "loan_status": loan_status.get(c.id),
            })

    ordered = order_stops_nearest_neighbor(located)
    stops = [
        {
            "id": s["id"], "full_name": s["full_name"], "address": s["address"],
            "latitude": s["lat"], "longitude": s["lng"], "sequence": i + 1,
            "loan_status": loan_status.get(s["id"]),
        }
        for i, s in enumerate(ordered)
    ]

    return {
        "route": {
            "id": route.id, "name": route.name, "zone": route.zone,
            "collector_name": route.collector_name,
        },
        "stops": stops,
        "unlocated": unlocated,
    }


@router.post("", response_model=RouteRead, status_code=status.HTTP_201_CREATED)
def create_route(
    payload: RouteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> dict:
    collector_id = _validate_collector(db, payload.assigned_collector_id, company_id)
    branch_id = _validate_branch(db, payload.branch_id, company_id)
    if cash_service.enabled(db,company_id) and collector_id and (not branch_id or db.get(User,collector_id).branch_id!=branch_id):
        raise HTTPException(409,'Selecciona la sucursal del cobrador para esta ruta.')
    route = Route(
        name=payload.name.strip(),
        zone=payload.zone.strip(),
        description=payload.description,
        assigned_collector_id=collector_id,
        branch_id=branch_id,
        is_active=payload.is_active,
        boundary=payload.boundary,
        company_id=company_id,
    )
    db.add(route)
    db.commit()
    db.refresh(route)
    _replace_areas(db, route, payload.areas)
    db.commit()
    db.refresh(route)
    return _serialize(db, route)


@router.put("/{route_id}", response_model=RouteRead)
def update_route(
    route_id: int,
    payload: RouteUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> dict:
    route = db.scalar(select(Route).where(Route.id == route_id, Route.company_id == company_id))
    if route is None:
        raise HTTPException(status_code=404, detail="Ruta no encontrada.")

    new_collector_id = _validate_collector(db, payload.assigned_collector_id, company_id)
    if cash_service.enabled(db,company_id) and new_collector_id and (not payload.branch_id or db.get(User,new_collector_id).branch_id!=payload.branch_id):
        raise HTTPException(409,'La ruta y el cobrador deben pertenecer a la misma sucursal.')
    if cash_service.enabled(db,company_id) and payload.branch_id:
        db.execute(update(Customer).where(Customer.route_id==route.id).values(cash_branch_id=payload.branch_id))
    collector_changed = new_collector_id != route.assigned_collector_id

    route.name = payload.name.strip()
    route.zone = payload.zone.strip()
    route.description = payload.description
    route.assigned_collector_id = new_collector_id
    route.branch_id = _validate_branch(db, payload.branch_id, company_id)
    route.is_active = payload.is_active
    route.boundary = payload.boundary

    # Reassigning the route's collector reassigns the whole portfolio at once.
    if collector_changed:
        db.execute(
            update(Customer)
            .where(Customer.route_id == route.id)
            .values(assigned_collector_id=new_collector_id)
        )

    _replace_areas(db, route, payload.areas)
    db.commit()
    db.refresh(route)
    return _serialize(db, route)


@router.delete("/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_route(
    route_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> None:
    route = db.scalar(select(Route).where(Route.id == route_id, Route.company_id == company_id))
    if route is None:
        raise HTTPException(status_code=404, detail="Ruta no encontrada.")

    if _customer_count(db, route.id) > 0:
        raise HTTPException(
            status_code=400,
            detail="La ruta tiene clientes asignados. Reasígnalos antes de borrarla.",
        )

    db.delete(route)
    db.commit()


@router.get("/suggest", response_model=list[RouteSuggestion])
def suggest_route_for_address(
    sector: str | None = None,
    calle: str | None = None,
    barrio: str | None = None,
    provincia: str | None = None,
    municipio: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_manager),
    company_id: int = Depends(get_company_id),
) -> list[dict]:
    """Suggest routes matching a sector/calle/barrio/provincia/municipio,
    and/or whose drawn zone contains (lat, lng), for the client form to
    offer as "¿usar esta ruta?" -- never assigns anything by itself."""
    results = suggest_routes(
        db,
        company_id,
        sector=sector,
        calle=calle,
        barrio=barrio,
        provincia=provincia,
        municipio=municipio,
        lat=lat,
        lng=lng,
    )
    return [
        {
            "route_id": r.route_id,
            "route_name": r.route_name,
            "zone": r.zone,
            "matched_on": r.matched_on,
            "score": r.score,
        }
        for r in results
    ]
