"""Suggests a collection route for a customer by matching their structured
address (sector/calle/barrio/provincia/municipio) against the areas each
route declares it covers (``RouteArea``), and/or by checking whether the
customer's GPS point falls inside a zone an admin drew on the map for a
route (``Route.boundary``).

This is suggestion-only by design: nothing here assigns a route to a
customer. The caller (API layer) always leaves that choice to a human.
"""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.route import Route, RouteArea


def normalize(value: str | None) -> str:
    """Lowercase + collapse internal whitespace, for tolerant exact matching."""
    if not value:
        return ""
    return " ".join(value.strip().lower().split())


def _point_in_polygon(lat: float, lng: float, polygon: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon test. ``polygon`` is a list of [lat, lng]
    points; treated as an implicitly closed ring (last point connects back
    to the first)."""
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = polygon[i][0], polygon[i][1]
        yj, xj = polygon[j][0], polygon[j][1]
        intersects = ((yi > lat) != (yj > lat)) and (
            lng < (xj - xi) * (lat - yi) / (yj - yi) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


@dataclass
class RouteSuggestionResult:
    route_id: int
    route_name: str
    zone: str
    matched_on: list[str]
    score: int


def suggest_routes(
    db: Session,
    company_id: int,
    *,
    sector: str | None = None,
    calle: str | None = None,
    barrio: str | None = None,
    provincia: str | None = None,
    municipio: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
) -> list[RouteSuggestionResult]:
    """Return routes matching any of the given address fields and/or whose
    drawn zone contains the given GPS point, ranked by how many signals
    matched (best first).

    Only active routes in the same company are considered. Returns an empty
    list when no address field and no GPS point are provided, or nothing
    matches.
    """
    candidates = {
        "sector": normalize(sector),
        "calle": normalize(calle),
        "barrio": normalize(barrio),
        "provincia": normalize(provincia),
        "municipio": normalize(municipio),
    }
    candidates = {k: v for k, v in candidates.items() if v}

    has_point = lat is not None and lng is not None
    if not candidates and not has_point:
        return []

    by_route: dict[int, RouteSuggestionResult] = {}

    def _add_match(route: Route, label: str) -> None:
        if route.id not in by_route:
            by_route[route.id] = RouteSuggestionResult(
                route_id=route.id,
                route_name=route.name,
                zone=route.zone,
                matched_on=[label],
                score=1,
            )
        else:
            existing = by_route[route.id]
            if label not in existing.matched_on:
                existing.matched_on.append(label)
                existing.score += 1

    if candidates:
        values = list(candidates.values())
        rows = db.execute(
            select(RouteArea, Route)
            .join(Route, RouteArea.route_id == Route.id)
            .where(
                Route.company_id == company_id,
                Route.is_active.is_(True),
                RouteArea.normalized_name.in_(values),
            )
        ).all()
        for area, route in rows:
            matched_field = next(
                (field for field, val in candidates.items() if val == area.normalized_name),
                None,
            )
            if matched_field is None:
                continue
            _add_match(route, f"{matched_field}: {area.name}")

    if has_point:
        routes = db.scalars(
            select(Route).where(Route.company_id == company_id, Route.is_active.is_(True))
        ).all()
        for route in routes:
            if route.boundary and _point_in_polygon(lat, lng, route.boundary):
                _add_match(route, "ubicación dentro de la zona dibujada")

    return sorted(by_route.values(), key=lambda r: (-r.score, r.route_name))
