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
