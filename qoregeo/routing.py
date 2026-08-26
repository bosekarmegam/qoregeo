"""
qoregeo.routing
===============
Stop ordering and route optimisation.

This solves the question every delivery, sales-visit and inspection workflow
runs into: given a depot and a list of stops, what order costs the least
travel? That is the Travelling Salesman Problem — NP-hard, so an exact answer
is out of reach past a handful of stops, and unnecessary in practice.

The approach here is the standard, well-behaved pair: a greedy
nearest-neighbour tour for a fast starting point, then 2-opt local search to
untangle it. Typical results land within a few percent of optimal for the
dozens-of-stops routes real businesses actually plan.

Distances are straight-line great-circle, not road distances — this plans the
*order* of stops, not the turn-by-turn path between them.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .exceptions import EmptyDatasetError
from .geometry import bearing_degrees, convert_length
from .utils import _bearing_to_compass, _haversine_km, _validate_coord

Coord = Tuple[float, float]


def _matrix(points: Sequence[Coord]) -> List[List[float]]:
    """Symmetric distance matrix in km, computed once and reused by every pass."""
    n = len(points)
    m = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = _haversine_km(points[i], points[j])
            m[i][j] = m[j][i] = d
    return m


def _tour_length(order: Sequence[int], m: List[List[float]], round_trip: bool) -> float:
    total = sum(m[order[i]][order[i + 1]] for i in range(len(order) - 1))
    if round_trip and len(order) > 1:
        total += m[order[-1]][order[0]]
    return total


def nearest_neighbour_tour(m: List[List[float]], start: int = 0) -> List[int]:
    """Greedy tour: always hop to the closest stop not yet visited."""
    n = len(m)
    unvisited = set(range(n))
    unvisited.discard(start)
    order = [start]

    current = start
    while unvisited:
        nxt = min(unvisited, key=lambda j: m[current][j])
        unvisited.discard(nxt)
        order.append(nxt)
        current = nxt
    return order


def two_opt(
    order: List[int],
    m: List[List[float]],
    round_trip: bool = False,
    fixed_start: bool = True,
    max_passes: int = 40,
) -> List[int]:
    """
    2-opt improvement: repeatedly reverse a segment when doing so shortens
    the tour.

    Geometrically this removes crossings — a tour that crosses itself is never
    optimal, and 2-opt is the cheapest way to spot and fix that.
    """
    n = len(order)
    if n < 4:
        return order

    best = list(order)
    best_len = _tour_length(best, m, round_trip)
    first = 1 if fixed_start else 0

    for _ in range(max_passes):
        improved = False
        for i in range(first, n - 1):
            for j in range(i + 1, n):
                # Every j > i is a real move. Reversing a two-stop segment
                # swaps adjacent stops, which still exchanges two edges —
                # skipping it (a common off-by-one) strands tours in worse
                # local optima than 2-opt should ever settle for.
                candidate = best[:i] + best[i : j + 1][::-1] + best[j + 1 :]
                cand_len = _tour_length(candidate, m, round_trip)
                if cand_len + 1e-12 < best_len:
                    best, best_len = candidate, cand_len
                    improved = True
        if not improved:
            break

    return best


def optimise_route(
    stops: Sequence[Coord],
    start: Optional[Coord] = None,
    round_trip: bool = False,
    unit: str = "km",
    improve: bool = True,
) -> Dict[str, Any]:
    """
    Order a set of stops into a short route.

    Parameters
    ----------
    stops      : ``(lat, lng)`` coordinates to visit
    start      : depot; defaults to the first stop
    round_trip : return to the start at the end
    unit       : distance unit for the reported totals
    improve    : run 2-opt after the greedy pass (leave on unless benchmarking)

    Returns
    -------
    ``{"order", "points", "legs", "total_distance", "unit", "improvement_pct"}``.

    ``order`` indexes back into your ``stops`` list. ``points`` is the full
    visiting sequence in ``(lat, lng)`` form, including the depot when
    ``start`` was not itself one of the stops. ``legs`` describes each hop
    with its distance and compass heading.

    Examples
    --------
    >>> route = optimise_route([(19.07, 72.87), (28.61, 77.20), (13.08, 80.27)],
    ...                        start=(28.61, 77.20), round_trip=True)
    >>> route["total_distance"]
    4218.9
    """
    points: List[Coord] = [(float(p[0]), float(p[1])) for p in stops]
    for p in points:
        _validate_coord(*p)

    if not points:
        raise EmptyDatasetError("<route with no stops>")

    prepended = False
    if start is not None:
        _validate_coord(*start)
        depot: Coord = (float(start[0]), float(start[1]))
        if depot not in points:
            points = [depot] + points
            prepended = True
        else:
            idx = points.index(depot)
            points = [points[idx]] + points[:idx] + points[idx + 1 :]

    if len(points) == 1:
        return {
            "order": [0],
            "points": points,
            "legs": [],
            "total_distance": 0.0,
            "unit": unit,
            "improvement_pct": 0.0,
        }

    m = _matrix(points)
    greedy = nearest_neighbour_tour(m, 0)
    greedy_len = _tour_length(greedy, m, round_trip)

    order = two_opt(greedy, m, round_trip) if improve else greedy
    final_len = _tour_length(order, m, round_trip)

    legs = []
    sequence = list(order) + ([order[0]] if round_trip and len(order) > 1 else [])
    for i in range(len(sequence) - 1):
        a, b = points[sequence[i]], points[sequence[i + 1]]
        km = m[sequence[i]][sequence[i + 1]]
        deg = bearing_degrees(a, b)
        legs.append(
            {
                "from_index": sequence[i],
                "to_index": sequence[i + 1],
                "from": a,
                "to": b,
                "distance": round(convert_length(km, unit), 4),
                "bearing": round(deg, 2),
                "direction": _bearing_to_compass(deg),
            }
        )

    improvement = (
        round((greedy_len - final_len) / greedy_len * 100, 2) if greedy_len > 0 else 0.0
    )

    # Indices refer to the caller's list, so undo the depot we prepended.
    public_order = [i - 1 for i in order if i > 0] if prepended else list(order)

    return {
        "order": public_order,
        "points": [points[i] for i in order],
        "legs": legs,
        "total_distance": round(convert_length(final_len, unit), 4),
        "unit": unit,
        "improvement_pct": improvement,
        "round_trip": round_trip,
    }


def route_line(route: Dict[str, Any]) -> Dict[str, Any]:
    """Turn an :func:`optimise_route` result into a GeoJSON LineString to draw."""
    points = route.get("points") or []
    coords = [[lng, lat] for lat, lng in points]
    if route.get("round_trip") and len(coords) > 1:
        coords.append(list(coords[0]))
    return {"type": "LineString", "coordinates": coords}


def travel_time(
    distance_km: float,
    speed_kmh: float = 40.0,
    stop_minutes: float = 0.0,
    stops: int = 0,
) -> Dict[str, Any]:
    """
    Rough schedule for a route: driving time plus time spent at each stop.

    ``speed_kmh`` is an average including traffic — 40 km/h is a reasonable
    urban default, 60–80 for intercity.
    """
    if speed_kmh <= 0:
        raise ValueError("speed_kmh must be greater than zero")

    driving = distance_km / speed_kmh * 60.0
    dwell = stop_minutes * max(0, stops)
    total = driving + dwell
    return {
        "driving_minutes": round(driving, 2),
        "stop_minutes": round(dwell, 2),
        "total_minutes": round(total, 2),
        "total_hours": round(total / 60.0, 2),
    }
