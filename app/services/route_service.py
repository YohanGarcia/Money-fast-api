from math import asin, cos, radians, sin, sqrt


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in kilometers."""
    r = 6371.0
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def order_stops_nearest_neighbor(stops: list[dict]) -> list[dict]:
    """Order located stops with a nearest-neighbor heuristic (free, no external API).

    Each stop is a dict with numeric ``lat`` and ``lng``. Starts from the first
    stop (deterministic) and repeatedly walks to the closest unvisited stop.
    Returns a new list; input is not mutated.
    """
    remaining = list(stops)
    if len(remaining) <= 1:
        return remaining

    ordered = [remaining.pop(0)]
    while remaining:
        last = ordered[-1]
        nearest_index = min(
            range(len(remaining)),
            key=lambda i: haversine_km(last["lat"], last["lng"], remaining[i]["lat"], remaining[i]["lng"]),
        )
        ordered.append(remaining.pop(nearest_index))
    return ordered

# Priority when a customer has more than one loan: the most "actionable" one
# wins for display purposes (a route/collector view shows one status per stop).
_LOAN_STATUS_PRIORITY = ["late", "active", "pending_approval", "paid"]


def loan_status_by_customer(db, customer_ids: list[int]) -> dict[int, str | None]:
    """Return ``{customer_id: loan_status}`` for the given customers.

    Picks the most relevant loan per customer (see ``_LOAN_STATUS_PRIORITY``);
    cancelled loans are ignored. Customers with no (non-cancelled) loan are
    left out of the returned dict.
    """
    from sqlalchemy import select
    from app.models.loan import Loan, LoanStatus

    if not customer_ids:
        return {}

    rows = db.execute(
        select(Loan.customer_id, Loan.status).where(
            Loan.customer_id.in_(customer_ids), Loan.status != LoanStatus.cancelled
        )
    ).all()

    best: dict[int, str] = {}
    for customer_id, status in rows:
        status_value = status.value if hasattr(status, "value") else status
        current = best.get(customer_id)
        if current is None:
            best[customer_id] = status_value
            continue
        current_rank = _LOAN_STATUS_PRIORITY.index(current) if current in _LOAN_STATUS_PRIORITY else 99
        new_rank = _LOAN_STATUS_PRIORITY.index(status_value) if status_value in _LOAN_STATUS_PRIORITY else 99
        if new_rank < current_rank:
            best[customer_id] = status_value
    return best

