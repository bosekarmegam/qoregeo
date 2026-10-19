"""
qoregeo.utils
=============
Pure utility functions — no side effects, fully testable.

Conventions
-----------
* Public points are ``(lat, lng)`` tuples.
* GeoJSON positions are ``[lng, lat]`` lists (RFC 7946 order).
"""

from __future__ import annotations

import contextlib
import csv
import datetime
import decimal
import errno
import math
import os
import re
import secrets
import shutil
import threading
import warnings
from collections.abc import Iterator, Sequence
from typing import IO, Any

from .exceptions import (
    ColumnNotFoundError,
    InvalidArgumentError,
    InvalidBufferError,
    InvalidCoordinateError,
    InvalidUnitError,
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
EARTH_RADIUS_KM = 6371.0088   # mean Earth radius (IUGG)
EARTH_RADIUS_M = EARTH_RADIUS_KM * 1000.0

# WGS-84 ellipsoid (used by the Vincenty distance)
WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_B = WGS84_A * (1 - WGS84_F)

# ─────────────────────────────────────────────────────────────────────────────
# Units
# ─────────────────────────────────────────────────────────────────────────────

# Kilometres per unit — one table for every distance-taking method.
_KM_PER_UNIT: dict[str, float] = {
    "km": 1.0,
    "m": 0.001,
    "miles": 1.609344,
    "mi": 1.609344,
    "ft": 0.0003048,
    "yd": 0.0009144,
    "nmi": 1.852,
}
_DISTANCE_ALIASES: dict[str, str] = {
    "kilometer": "km", "kilometers": "km", "kilometre": "km", "kilometres": "km",
    "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "mile": "miles",
    "foot": "ft", "feet": "ft",
    "yard": "yd", "yards": "yd",
    "nautical_mile": "nmi", "nautical_miles": "nmi",
}

# Square metres per unit.
_M2_PER_UNIT: dict[str, float] = {
    "m2": 1.0,
    "km2": 1_000_000.0,
    "ha": 10_000.0,
    "acres": 4046.8564224,
    "mi2": 2_589_988.110336,
    "ft2": 0.09290304,
}
_AREA_ALIASES: dict[str, str] = {
    "sqm": "m2", "sq_m": "m2",
    "sqkm": "km2", "sq_km": "km2",
    "hectare": "ha", "hectares": "ha",
    "acre": "acres",
    "sqmi": "mi2", "sq_mi": "mi2",
    "sqft": "ft2", "sq_ft": "ft2",
}


def _resolve_unit(unit: Any, table: dict[str, float], aliases: dict[str, str]) -> str:
    if not isinstance(unit, str):
        raise InvalidUnitError(unit, list(table))
    key = unit.strip().lower()
    key = aliases.get(key, key)
    if key not in table:
        raise InvalidUnitError(unit, list(table))
    return key


def _km_per_unit(unit: Any) -> float:
    """Return how many kilometres one ``unit`` is. Raises InvalidUnitError."""
    return _KM_PER_UNIT[_resolve_unit(unit, _KM_PER_UNIT, _DISTANCE_ALIASES)]


def _m2_per_unit(unit: Any) -> float:
    """Return how many square metres one area ``unit`` is. Raises InvalidUnitError."""
    return _M2_PER_UNIT[_resolve_unit(unit, _M2_PER_UNIT, _AREA_ALIASES)]


# ─────────────────────────────────────────────────────────────────────────────
# Coordinate helpers
# ─────────────────────────────────────────────────────────────────────────────

def _validate_coord(lat: Any, lng: Any, context: str | None = None) -> tuple[float, float]:
    """
    Return ``(lat, lng)`` as floats, or raise InvalidCoordinateError if the
    values are non-numeric, non-finite or out of range.
    """
    if isinstance(lat, bool) or isinstance(lng, bool):
        raise InvalidCoordinateError(lat, lng, context)
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError, OverflowError):
        raise InvalidCoordinateError(lat, lng, context) from None

    # NaN fails every comparison, so it is rejected here too.
    if not (-90.0 <= lat_f <= 90.0) or not (-180.0 <= lng_f <= 180.0):
        raise InvalidCoordinateError(lat_f, lng_f, context)
    return lat_f, lng_f


def _coerce_point(point: Any, context: str | None = None) -> tuple[float, float]:
    """
    Turn a point-like value into a validated ``(lat, lng)`` float tuple.

    Accepts ``(lat, lng)`` / ``(lat, lng, alt)`` sequences, GeoJSON Point
    geometries and Features with a Point geometry.
    """
    if isinstance(point, dict):
        geom = point.get("geometry") if point.get("type") == "Feature" else point
        if isinstance(geom, dict) and geom.get("type") == "Point":
            coords = geom.get("coordinates")
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                return _validate_coord(coords[1], coords[0], context)
        raise InvalidCoordinateError(point, None, context or "expected a (lat, lng) tuple")

    if isinstance(point, (str, bytes)):
        raise InvalidCoordinateError(point, None, context or "expected a (lat, lng) tuple")
    try:
        lat, lng = point[0], point[1]
    except (TypeError, IndexError, KeyError):
        raise InvalidCoordinateError(point, None, context or "expected a (lat, lng) tuple") from None
    return _validate_coord(lat, lng, context)


def _to_radians(deg: float) -> float:
    return math.radians(deg)


def _normalize_lng(lng: float) -> float:
    """Wrap a longitude into [-180, 180]."""
    if -180.0 <= lng <= 180.0:
        return lng
    wrapped = ((lng + 180.0) % 360.0) - 180.0
    return 180.0 if wrapped == -180.0 and lng > 0 else wrapped


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


# ─────────────────────────────────────────────────────────────────────────────
# Distance
# ─────────────────────────────────────────────────────────────────────────────

def _haversine_km(point_a: tuple[float, float], point_b: tuple[float, float]) -> float:
    """Return great-circle distance in km using the Haversine formula."""
    lat1, lng1 = _to_radians(point_a[0]), _to_radians(point_a[1])
    lat2, lng2 = _to_radians(point_b[0]), _to_radians(point_b[1])

    d_lat = lat2 - lat1
    d_lng = lng2 - lng1

    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(d_lng / 2) ** 2
    )
    # Rounding can push `a` a hair above 1 for near-antipodal points.
    c = 2 * math.asin(min(1.0, math.sqrt(a)))
    return EARTH_RADIUS_KM * c


def _vincenty_km(
    point_a: tuple[float, float],
    point_b: tuple[float, float],
    max_iter: int = 200,
    tol: float = 1e-12,
) -> float:
    """
    Return the WGS-84 ellipsoidal distance in km (Vincenty inverse formula).

    Accurate to ~0.5 mm. For nearly antipodal points, where the iteration
    does not converge, the spherical (Haversine) distance is returned instead.
    """
    lat1, lng1 = point_a[0], point_a[1]
    lat2, lng2 = point_b[0], point_b[1]
    if lat1 == lat2 and lng1 == lng2:
        return 0.0

    f = WGS84_F
    big_l = math.radians(lng2 - lng1)
    u1 = math.atan((1 - f) * math.tan(math.radians(lat1)))
    u2 = math.atan((1 - f) * math.tan(math.radians(lat2)))
    sin_u1, cos_u1 = math.sin(u1), math.cos(u1)
    sin_u2, cos_u2 = math.sin(u2), math.cos(u2)

    lam = big_l
    for _ in range(max_iter):
        sin_lam, cos_lam = math.sin(lam), math.cos(lam)
        sin_sigma = math.hypot(cos_u2 * sin_lam, cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lam)
        if sin_sigma == 0:
            return 0.0   # coincident points
        cos_sigma = sin_u1 * sin_u2 + cos_u1 * cos_u2 * cos_lam
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cos_u1 * cos_u2 * sin_lam / sin_sigma
        cos2_alpha = 1 - sin_alpha ** 2
        cos_2sm = cos_sigma - 2 * sin_u1 * sin_u2 / cos2_alpha if cos2_alpha != 0 else 0.0
        c = f / 16 * cos2_alpha * (4 + f * (4 - 3 * cos2_alpha))
        lam_prev = lam
        lam = big_l + (1 - c) * f * sin_alpha * (
            sigma + c * sin_sigma * (cos_2sm + c * cos_sigma * (-1 + 2 * cos_2sm ** 2))
        )
        if abs(lam - lam_prev) < tol:
            break
    else:
        warnings.warn(
            "QOREgeo: Vincenty's formula did not converge for these nearly antipodal "
            "points; the spherical (haversine) distance was used instead.",
            stacklevel=4,
        )
        return _haversine_km(point_a, point_b)

    u_sq = cos2_alpha * (WGS84_A ** 2 - WGS84_B ** 2) / WGS84_B ** 2
    big_a = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    big_b = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))
    delta_sigma = big_b * sin_sigma * (
        cos_2sm + big_b / 4 * (
            cos_sigma * (-1 + 2 * cos_2sm ** 2)
            - big_b / 6 * cos_2sm * (-3 + 4 * sin_sigma ** 2) * (-3 + 4 * cos_2sm ** 2)
        )
    )
    return WGS84_B * big_a * (sigma - delta_sigma) / 1000.0


_DISTANCE_METHODS = ("haversine", "vincenty")


def _resolve_method(method: Any) -> str:
    """Validate a distance method name (case-insensitive)."""
    key = method.strip().lower() if isinstance(method, str) else method
    if key not in _DISTANCE_METHODS:
        raise InvalidArgumentError(
            "method", method, "'haversine' (sphere) or 'vincenty' (WGS-84 ellipsoid)",
            "geo.distance(a, b, method='vincenty')",
        )
    return str(key)


def _distance_km(
    point_a: tuple[float, float],
    point_b: tuple[float, float],
    method: str = "haversine",
) -> float:
    if method == "haversine":
        return _haversine_km(point_a, point_b)
    if _resolve_method(method) == "vincenty":
        return _vincenty_km(point_a, point_b)
    return _haversine_km(point_a, point_b)


# ─────────────────────────────────────────────────────────────────────────────
# Bearing, destination, midpoint
# ─────────────────────────────────────────────────────────────────────────────

def _initial_bearing_deg(point_a: tuple[float, float], point_b: tuple[float, float]) -> float:
    """Initial great-circle bearing from a to b, in degrees [0, 360)."""
    lat1 = _to_radians(point_a[0])
    lat2 = _to_radians(point_b[0])
    d_lng = _to_radians(point_b[1] - point_a[1])

    x = math.sin(d_lng) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d_lng)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _destination_point(
    point: tuple[float, float],
    distance_km: float,
    bearing_deg: float,
) -> tuple[float, float]:
    """Point reached by travelling ``distance_km`` from ``point`` on ``bearing_deg``."""
    lat1 = _to_radians(point[0])
    lng1 = _to_radians(point[1])
    d = distance_km / EARTH_RADIUS_KM
    brng = _to_radians(bearing_deg)

    lat2 = math.asin(_clamp(
        math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(brng)
    ))
    lng2 = lng1 + math.atan2(
        math.sin(brng) * math.sin(d) * math.cos(lat1),
        math.cos(d) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), _normalize_lng(math.degrees(lng2))


def _midpoint(point_a: tuple[float, float], point_b: tuple[float, float]) -> tuple[float, float]:
    """Great-circle midpoint of a and b."""
    lat1, lng1 = _to_radians(point_a[0]), _to_radians(point_a[1])
    lat2 = _to_radians(point_b[0])
    d_lng = _to_radians(point_b[1] - point_a[1])

    bx = math.cos(lat2) * math.cos(d_lng)
    by = math.cos(lat2) * math.sin(d_lng)
    lat3 = math.atan2(
        math.sin(lat1) + math.sin(lat2),
        math.hypot(math.cos(lat1) + bx, by),
    )
    lng3 = lng1 + math.atan2(by, math.cos(lat1) + bx)
    return math.degrees(lat3), _normalize_lng(math.degrees(lng3))


# ─────────────────────────────────────────────────────────────────────────────
# Compass
# ─────────────────────────────────────────────────────────────────────────────

_COMPASS_POINTS = [
    "North",
    "North-Northeast",
    "Northeast",
    "East-Northeast",
    "East",
    "East-Southeast",
    "Southeast",
    "South-Southeast",
    "South",
    "South-Southwest",
    "Southwest",
    "West-Southwest",
    "West",
    "West-Northwest",
    "Northwest",
    "North-Northwest",
]


def _bearing_to_compass(degrees: float) -> str:
    """
    Convert a bearing in degrees to a 16-point compass string.

    Sector boundaries (11.25°, 33.75°, …) always round up to the next point,
    unlike Python's ``round()`` which rounds half to even.
    """
    idx = int(((degrees % 360.0) + 11.25) // 22.5) % 16
    return _COMPASS_POINTS[idx]


# ─────────────────────────────────────────────────────────────────────────────
# Circle polygon
# ─────────────────────────────────────────────────────────────────────────────

def _generate_circle_polygon(
    center: tuple[float, float],
    radius_km: float,
    num_points: int = 64,
) -> list[list[float]]:
    """
    Generate a closed ring of [lng, lat] points approximating a circle.

    Uses spherical geometry to ensure geodesic accuracy. The ring runs
    counter-clockwise (RFC 7946 right-hand rule). Longitudes are kept
    continuous around the centre (they may exceed ±180 near the antimeridian)
    so the ring renders and tests correctly as one shape.
    """
    lat, lng = center[0], center[1]
    lat_r = _to_radians(lat)
    lng_r = _to_radians(lng)
    d = radius_km / EARTH_RADIUS_KM   # angular distance in radians

    points: list[list[float]] = []
    if abs(lat) >= 90.0 - 1e-9:
        # At a pole every direction is south/north and the bearing formula
        # degenerates: lay the vertices out along a circle of latitude instead.
        ring_lat = math.degrees(math.copysign(math.pi / 2 - d, lat))
        for i in range(num_points):
            step = 360.0 * i / num_points
            points.append([-180.0 + step if lat > 0 else 180.0 - step, ring_lat])
        points.append(list(points[0]))
        return points

    for i in range(num_points):
        brng = _to_radians((360 - i * 360 / num_points) % 360)   # N, then west: CCW

        p_lat = math.asin(_clamp(
            math.sin(lat_r) * math.cos(d)
            + math.cos(lat_r) * math.sin(d) * math.cos(brng)
        ))
        p_lng = lng_r + math.atan2(
            math.sin(brng) * math.sin(d) * math.cos(lat_r),
            math.cos(d) - math.sin(lat_r) * math.sin(p_lat),
        )

        points.append([math.degrees(p_lng), math.degrees(p_lat)])

    points.append(list(points[0]))   # close the ring
    return points


# ─────────────────────────────────────────────────────────────────────────────
# Point-in-polygon (ray casting)
# ─────────────────────────────────────────────────────────────────────────────

def _point_in_polygon_ray(
    lat: float,
    lng: float,
    ring: Sequence[Sequence[float]],
) -> bool:
    """
    Ray-casting test against a single ring.

    ring is a list of [lng, lat] pairs (GeoJSON convention).
    """
    inside = False
    j = len(ring) - 1

    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]   # lng, lat
        xj, yj = ring[j][0], ring[j][1]

        # (yi > lat) != (yj > lat) guarantees yj != yi, so no division by zero.
        if ((yi > lat) != (yj > lat)) and (lng < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i

    return inside


def _point_in_polygon_rings(lat: float, lng: float, rings: list[list[list[float]]]) -> bool:
    """
    Test a point against one GeoJSON polygon: ``rings[0]`` is the outer ring,
    any further rings are holes.

    Edges are straight lines in lng/lat (the planar reading used by RFC 7946
    §3.1.1, Turf, Shapely and Leaflet's rendering). Rings drawn *continuously*
    across the antimeridian (longitudes beyond ±180, as buffer() produces) are
    supported by also trying the point at lng ± 360.
    """
    if not rings or not rings[0]:
        return False
    outer = rings[0]

    lo = min(p[0] for p in outer)
    hi = max(p[0] for p in outer)
    for cand in (lng, lng + 360.0, lng - 360.0):
        if lo <= cand <= hi:
            lng = cand
            break

    if not _point_in_polygon_ray(lat, lng, outer):
        return False
    return not any(_point_in_polygon_ray(lat, lng, hole) for hole in rings[1:])


def _polygon_parts(polygon: Any) -> list[list[list[list[float]]]]:
    """
    Normalise a polygon-like argument to a list of polygons (each a list of
    rings). Accepts Polygon, MultiPolygon, a Feature wrapping one of them, or a
    FeatureCollection (the union of its polygon features).
    Raises InvalidBufferError for anything else or malformed coordinates.
    """
    if not isinstance(polygon, dict):
        raise InvalidBufferError(f"expected a dict, got {type(polygon).__name__}")

    if polygon.get("type") == "FeatureCollection":
        # Union of every (Multi)Polygon feature; other geometry types are ignored.
        collected: list[list[list[list[float]]]] = []
        for feat in polygon.get("features") or []:
            geom_type = (feat.get("geometry") or {}).get("type") if isinstance(feat, dict) else None
            if geom_type in ("Polygon", "MultiPolygon"):
                collected.extend(_polygon_parts(feat))
        if not collected:
            raise InvalidBufferError("the FeatureCollection contains no Polygon or MultiPolygon features")
        return collected

    geom: Any = polygon
    if polygon.get("type") == "Feature":
        geom = polygon.get("geometry")
        if not isinstance(geom, dict):
            raise InvalidBufferError("the Feature has no geometry")

    gtype = geom.get("type")
    coords = geom.get("coordinates")
    parts: Any
    if gtype == "Polygon":
        parts = [coords]
    elif gtype == "MultiPolygon":
        parts = coords if isinstance(coords, list) else None
    else:
        raise InvalidBufferError(f"geometry type {gtype!r} is not a Polygon or MultiPolygon")

    if not isinstance(parts, list):
        raise InvalidBufferError("'coordinates' is missing or not a list")

    clean: list[list[list[list[float]]]] = []
    for rings in parts:
        if not isinstance(rings, list) or not rings:
            raise InvalidBufferError("a polygon has no rings")
        checked = []
        for ring in rings:
            if not isinstance(ring, list) or len(ring) < 3:
                raise InvalidBufferError("each ring needs at least 3 [lng, lat] positions")
            try:
                checked.append([[float(p[0]), float(p[1])] for p in ring])
            except (TypeError, ValueError, IndexError):
                raise InvalidBufferError("ring positions must be [lng, lat] number pairs") from None
        clean.append(checked)
    return clean


def _buffer_circle(polygon: Any) -> tuple[float, float, float] | None:
    """
    Return ``(lat, lng, radius_km)`` if ``polygon`` is a geo.buffer() result
    (or a Feature wrapping one).
    """
    if isinstance(polygon, dict) and polygon.get("type") == "Feature":
        polygon = polygon.get("geometry")
    if not isinstance(polygon, dict):
        return None
    center = polygon.get("_center")
    radius = polygon.get("_radius_km")
    if center is None or radius is None:
        return None
    try:
        lat, lng = float(center[0]), float(center[1])
        radius_f = float(radius)
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not math.isfinite(radius_f) or radius_f <= 0:
        return None
    return lat, lng, radius_f


def _point_in_geometry(lat: float, lng: float, polygon: Any) -> bool:
    """Point-in-polygon for buffers, Polygons, MultiPolygons and Features."""
    circle = _buffer_circle(polygon)
    if circle is not None:
        # Exact geodesic test — also correct near the poles and antimeridian.
        c_lat, c_lng, radius_km = circle
        return _haversine_km((lat, lng), (c_lat, c_lng)) <= radius_km
    return any(_point_in_polygon_rings(lat, lng, rings) for rings in _polygon_parts(polygon))


# ─────────────────────────────────────────────────────────────────────────────
# Geometry inspection
# ─────────────────────────────────────────────────────────────────────────────

GEOMETRY_TYPES = (
    "Point", "MultiPoint", "LineString", "MultiLineString",
    "Polygon", "MultiPolygon", "GeometryCollection",
)


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _flatten_positions(coords: Any) -> Iterator[Sequence[float]]:
    if isinstance(coords, (list, tuple)):
        if len(coords) >= 2 and _is_number(coords[0]) and _is_number(coords[1]):
            yield coords
            return
        for c in coords:
            yield from _flatten_positions(c)


def _iter_positions(geometry: Any) -> Iterator[Sequence[float]]:
    """Yield every [lng, lat, ...] position of any GeoJSON geometry."""
    if not isinstance(geometry, dict):
        return
    if geometry.get("type") == "GeometryCollection":
        for g in geometry.get("geometries") or []:
            yield from _iter_positions(g)
        return
    yield from _flatten_positions(geometry.get("coordinates"))


def _ring_centroid(ring: list[list[float]]) -> tuple[tuple[float, float] | None, float]:
    """Planar area centroid ``((lat, lng), |area|)`` of a closed [lng, lat] ring."""
    # Work relative to the first vertex: with raw coordinates (e.g. lng ≈ 73)
    # the shoelace cross products cancel and leave float noise in the result.
    ox, oy = ring[0][0], ring[0][1]
    area2 = cx = cy = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = ring[i][0] - ox, ring[i][1] - oy
        x1, y1 = ring[i + 1][0] - ox, ring[i + 1][1] - oy
        cross = x0 * y1 - x1 * y0
        area2 += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area2) < 1e-18:
        return None, 0.0
    return (oy + cy / (3 * area2), ox + cx / (3 * area2)), abs(area2) / 2


def _representative_point(geometry: Any) -> tuple[float, float] | None:
    """
    Return a ``(lat, lng)`` point standing in for ``geometry``.

    Points return themselves; polygons return the area centroid of their
    outer rings; everything else returns the spherical mean of its vertices.
    Returns None for null / empty / malformed geometries.
    """
    if not isinstance(geometry, dict):
        return None
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")

    if gtype == "Point":
        if isinstance(coords, (list, tuple)) and len(coords) >= 2 \
                and _is_number(coords[0]) and _is_number(coords[1]):
            return float(coords[1]), float(coords[0])
        return None

    if gtype in ("Polygon", "MultiPolygon"):
        polys = [coords] if gtype == "Polygon" else (coords or [])
        rings = [p[0] for p in polys if isinstance(p, list) and p and isinstance(p[0], list)]
        try:
            rings = [[[float(q[0]), float(q[1])] for q in r] for r in rings]
        except (TypeError, ValueError, IndexError):
            return None
        total = lat_acc = lng_acc = 0.0
        for ring in rings:
            if len(ring) < 3:
                continue   # empty or degenerate ring
            if ring[0] != ring[-1]:
                ring = ring + [ring[0]]
            c, area = _ring_centroid(ring)
            if c is not None:
                lat_acc += c[0] * area
                lng_acc += c[1] * area
                total += area
        if total > 0:
            return lat_acc / total, _normalize_lng(lng_acc / total)
        # Degenerate polygons fall through to the vertex mean.

    positions = list(_iter_positions(geometry))
    if not positions:
        return None
    return _spherical_mean([(float(p[1]), float(p[0])) for p in positions])


def _spherical_mean(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Mean of (lat, lng) points via 3-D unit vectors (antimeridian-safe)."""
    x = y = z = 0.0
    for lat, lng in points:
        la, lo = _to_radians(lat), _to_radians(lng)
        x += math.cos(la) * math.cos(lo)
        y += math.cos(la) * math.sin(lo)
        z += math.sin(la)
    n = len(points)
    x, y, z = x / n, y / n, z / n
    if math.hypot(x, y, z) < 1e-12:
        # Points cancel out (e.g. two antipodes) — fall back to the plain mean.
        return sum(p[0] for p in points) / n, sum(p[1] for p in points) / n
    return math.degrees(math.atan2(z, math.hypot(x, y))), _normalize_lng(math.degrees(math.atan2(y, x)))


# ─────────────────────────────────────────────────────────────────────────────
# Intersection test
# ─────────────────────────────────────────────────────────────────────────────

_XY = tuple[float, float]   # (lng, lat) — planar GeoJSON order


class _Shape:
    """A geometry decomposed into points, line strings and polygons (lng, lat)."""

    def __init__(self) -> None:
        self.points: list[_XY] = []
        self.lines: list[list[_XY]] = []
        self.polygons: list[list[list[_XY]]] = []
        self.circles: list[tuple[float, float, float]] = []   # (lat, lng, radius_km)

    def segments(self) -> Iterator[tuple[_XY, _XY]]:
        for line in self.lines:
            for i in range(1, len(line)):
                yield line[i - 1], line[i]
        for rings in self.polygons:
            for ring in rings:
                for i in range(1, len(ring)):
                    yield ring[i - 1], ring[i]
                if ring and ring[0] != ring[-1]:
                    yield ring[-1], ring[0]

    def vertices(self) -> Iterator[_XY]:
        yield from self.points
        for line in self.lines:
            yield from line
        for rings in self.polygons:
            for ring in rings:
                yield from ring

    def is_empty(self) -> bool:
        return not (self.points or self.lines or self.polygons or self.circles)


def _decompose(obj: Any, name: str = "geometry") -> _Shape:
    """
    Break a geometry / Feature / FeatureCollection / buffer() result into a
    _Shape. Raises InvalidArgumentError for anything that isn't GeoJSON.
    """
    shape = _Shape()

    def pos(c: Any) -> _XY:
        return float(c[0]), float(c[1])

    def add(g: Any, depth: int = 0) -> None:
        if g is None:
            return
        if not isinstance(g, dict) or depth > 16:
            raise InvalidArgumentError(name, g, "a GeoJSON geometry, Feature or FeatureCollection",
                                       "geo.intersects(zone, {'type': 'Point', 'coordinates': [77.2, 28.6]})")
        gtype = g.get("type")
        if gtype == "Feature":
            add(g.get("geometry"), depth + 1)
            return
        if gtype == "FeatureCollection":
            for f in g.get("features") or []:
                add(f, depth + 1)
            return
        circle = _buffer_circle(g)
        if circle is not None:
            shape.circles.append(circle)
            return
        problem = _geometry_problem(g)
        if problem:
            raise InvalidArgumentError(name, g, f"a valid GeoJSON geometry ({problem})",
                                       "geo.intersects(zone, {'type': 'Point', 'coordinates': [77.2, 28.6]})")
        coords: Any = g.get("coordinates")
        if gtype == "GeometryCollection":
            for sub in g.get("geometries") or []:
                add(sub, depth + 1)
        elif gtype == "Point":
            shape.points.append(pos(coords))
        elif gtype == "MultiPoint":
            shape.points.extend(pos(c) for c in coords)
        elif gtype in ("LineString", "MultiLineString"):
            for line in ([coords] if gtype == "LineString" else coords):
                pts = [pos(c) for c in line]
                if len(pts) == 1:
                    shape.points.append(pts[0])
                elif pts:
                    shape.lines.append(pts)
        elif gtype in ("Polygon", "MultiPolygon"):
            for rings in ([coords] if gtype == "Polygon" else coords):
                clean = [[pos(c) for c in ring] for ring in rings if len(ring) >= 3]
                if clean:
                    shape.polygons.append(clean)

    add(obj)
    return shape


_EPS = 1e-12


def _orient(a: _XY, b: _XY, c: _XY) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: _XY, b: _XY, p: _XY) -> bool:
    if abs(_orient(a, b, p)) > _EPS * max(1.0, abs(b[0] - a[0]) + abs(b[1] - a[1])):
        return False
    return (min(a[0], b[0]) - _EPS <= p[0] <= max(a[0], b[0]) + _EPS
            and min(a[1], b[1]) - _EPS <= p[1] <= max(a[1], b[1]) + _EPS)


def _segments_intersect(p1: _XY, p2: _XY, q1: _XY, q2: _XY) -> bool:
    """True if the closed segments p1–p2 and q1–q2 share at least one point."""
    d1 = _orient(q1, q2, p1)
    d2 = _orient(q1, q2, p2)
    d3 = _orient(p1, p2, q1)
    d4 = _orient(p1, p2, q2)
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True
    return (_on_segment(q1, q2, p1) or _on_segment(q1, q2, p2)
            or _on_segment(p1, p2, q1) or _on_segment(p1, p2, q2))


def _point_in_rings_or_boundary(p: _XY, rings: list[list[_XY]]) -> bool:
    for ring in rings:
        for i in range(len(ring)):
            if _on_segment(ring[i - 1], ring[i], p):
                return True
    return _point_in_polygon_rings(p[1], p[0], [[list(c) for c in r] for r in rings])


def _point_in_shape(p: _XY, shape: _Shape) -> bool:
    """True if point ``p`` touches ``shape`` (inside a polygon or circle, on a line, or equal)."""
    if any(_haversine_km((p[1], p[0]), (c[0], c[1])) <= c[2] for c in shape.circles):
        return True
    if any(abs(p[0] - q[0]) <= _EPS and abs(p[1] - q[1]) <= _EPS for q in shape.points):
        return True
    for line in shape.lines:
        if any(_on_segment(line[i - 1], line[i], p) for i in range(1, len(line))):
            return True
    return any(_point_in_rings_or_boundary(p, rings) for rings in shape.polygons)


def _circle_as_polygon(c: tuple[float, float, float]) -> list[list[_XY]]:
    ring = _generate_circle_polygon((c[0], c[1]), c[2], 128)
    return [[(x, y) for x, y in ring]]


def _planar_intersect(pa: _Shape, pb: _Shape) -> bool:
    """Straight-edge lng/lat intersection of two shapes without circles."""
    for p in pa.points:
        if _point_in_shape(p, pb):
            return True
    for p in pb.points:
        if _point_in_shape(p, pa):
            return True

    seg_b = list(pb.segments())
    for s1 in pa.segments():
        for s2 in seg_b:
            if _segments_intersect(s1[0], s1[1], s2[0], s2[1]):
                return True

    # No crossing edges: one shape may still lie entirely inside the other.
    for inner, outer in ((pa, pb), (pb, pa)):
        for part in inner.lines + [rings[0] for rings in inner.polygons]:
            if part and any(_point_in_rings_or_boundary(part[0], rings) for rings in outer.polygons):
                return True
    return False


def _shifted(shape: _Shape, dx: float) -> _Shape:
    """``shape`` moved ``dx`` degrees in longitude."""
    out = _Shape()
    out.points = [(x + dx, y) for x, y in shape.points]
    out.lines = [[(x + dx, y) for x, y in line] for line in shape.lines]
    out.polygons = [[[(x + dx, y) for x, y in ring] for ring in rings] for rings in shape.polygons]
    return out


def _geometries_intersect(a: Any, b: Any) -> bool:
    """
    True if two geometries share at least one point (touching counts), using
    straight edges in lng/lat like point_in_polygon(). Buffers are tested as
    exact circles where possible. Shapes drawn with continuous longitudes
    across the antimeridian (e.g. 175° → 185°) also meet shapes written in
    the usual -180°…180° range.
    """
    for value, name in ((a, "geometry_a"), (b, "geometry_b")):
        if value is None:
            raise InvalidArgumentError(name, value, "a GeoJSON geometry, Feature or FeatureCollection",
                                       "geo.intersects(zone, {'type': 'Point', 'coordinates': [77.2, 28.6]})")
    sa = _decompose(a, "geometry_a")
    sb = _decompose(b, "geometry_b")
    if sa.is_empty() or sb.is_empty():
        return False

    # Circle–circle and circle–point tests are exact on the sphere.
    for ca in sa.circles:
        for cb in sb.circles:
            if _haversine_km((ca[0], ca[1]), (cb[0], cb[1])) <= ca[2] + cb[2]:
                return True
    for first, second in ((sa, sb), (sb, sa)):
        for c in first.circles:
            if any(_haversine_km((p[1], p[0]), (c[0], c[1])) <= c[2] for p in second.points):
                return True

    # Other combinations with circles use a fine polygon approximation.
    def with_circles_as_polygons(shape: _Shape) -> _Shape:
        out = _Shape()
        out.points, out.lines = shape.points, shape.lines
        out.polygons = shape.polygons + [_circle_as_polygon(c) for c in shape.circles]
        return out

    pa, pb = with_circles_as_polygons(sa), with_circles_as_polygons(sb)
    offsets = [0.0]
    if any(abs(x) > 180.0 for shape in (pa, pb) for x, _ in shape.vertices()):
        offsets += [360.0, -360.0]   # compare on the other world copy too
    return any(_planar_intersect(pa, _shifted(pb, dx) if dx else pb) for dx in offsets)


# ─────────────────────────────────────────────────────────────────────────────
# Area and length
# ─────────────────────────────────────────────────────────────────────────────

def _ring_area_m2(ring: list[list[float]]) -> float:
    """
    Spherical area of a [lng, lat] ring in m² (Chamberlain & Duquette, 2007,
    "Some Algorithms for Polygons on a Sphere", JPL — the method used by
    Turf.js and OpenLayers).
    """
    if len(ring) < 3:
        return 0.0
    if ring[0][0] != ring[-1][0] or ring[0][1] != ring[-1][1]:
        ring = list(ring) + [ring[0]]
    total = 0.0
    for i in range(len(ring) - 1):
        lng1, lat1 = ring[i][0], ring[i][1]
        lng2, lat2 = ring[i + 1][0], ring[i + 1][1]
        total += _to_radians(lng2 - lng1) * (
            2 + math.sin(_to_radians(lat1)) + math.sin(_to_radians(lat2))
        )
    return abs(total * EARTH_RADIUS_M ** 2 / 2.0)


def _polygon_area_m2(polygon: Any) -> float:
    """
    Area of a Polygon / MultiPolygon / Feature in m² (holes subtracted).

    A buffer() result is measured exactly as the spherical cap it represents
    (2πR²(1 − cos(r/R))), consistent with point_in_polygon's exact circle test.
    """
    circle = _buffer_circle(polygon)
    if circle is not None:
        return 2 * math.pi * EARTH_RADIUS_M ** 2 * (1 - math.cos(circle[2] / EARTH_RADIUS_KM))
    total = 0.0
    for rings in _polygon_parts(polygon):
        outer = _ring_area_m2(rings[0])
        holes = sum(_ring_area_m2(r) for r in rings[1:])
        total += max(0.0, outer - holes)
    return total


def _path_length_km(points: Sequence[tuple[float, float]], method: str = "haversine") -> float:
    """Sum of segment lengths along a list of (lat, lng) points."""
    return sum(_distance_km(points[i - 1], points[i], method) for i in range(1, len(points)))


def _point_segment_km(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    """
    Great-circle distance in km from ``p`` to the segment ``a``–``b``
    (cross-track distance when the foot of the perpendicular lies on the
    segment, else the distance to the nearer end; Veness, "Movable Type").
    """
    d_ap = _haversine_km(a, p)
    d_bp = _haversine_km(b, p)
    if a[0] == b[0] and a[1] == b[1]:
        return d_ap
    d13 = d_ap / EARTH_RADIUS_KM
    if d13 == 0.0:
        return 0.0
    theta13 = _to_radians(_initial_bearing_deg(a, p))
    theta12 = _to_radians(_initial_bearing_deg(a, b))
    if math.cos(theta13 - theta12) <= 0:          # p lies "behind" a
        return min(d_ap, d_bp)
    dxt = math.asin(_clamp(math.sin(d13) * math.sin(theta13 - theta12)))
    cos_dxt = math.cos(dxt)
    dat = math.acos(_clamp(math.cos(d13) / cos_dxt)) if cos_dxt else 0.0
    if dat * EARTH_RADIUS_KM > _haversine_km(a, b):   # foot beyond b
        return min(d_ap, d_bp)
    return abs(dxt) * EARTH_RADIUS_KM


def _point_line_km(p: tuple[float, float], parts: list[list[tuple[float, float]]]) -> float:
    """Shortest distance in km from ``p`` to any segment of a (multi)line."""
    best = math.inf
    for pts in parts:
        if len(pts) == 1:
            best = min(best, _haversine_km(p, pts[0]))
        for i in range(1, len(pts)):
            best = min(best, _point_segment_km(p, pts[i - 1], pts[i]))
    return best


def _lng_span(lngs: list[float]) -> tuple[float, float]:
    """
    Smallest ``(west, east)`` longitude interval covering ``lngs``.
    ``west > east`` means the interval crosses the antimeridian (RFC 7946 §5.2).
    """
    xs = sorted({_normalize_lng(x) for x in lngs})
    west, east = xs[0], xs[-1]
    best_gap = xs[0] + 360.0 - xs[-1]          # the gap across the antimeridian
    for i in range(1, len(xs)):
        gap = xs[i] - xs[i - 1]
        if gap > best_gap:
            best_gap = gap
            west, east = xs[i], xs[i - 1]
    return west, east


# ─────────────────────────────────────────────────────────────────────────────
# Geohash
# ─────────────────────────────────────────────────────────────────────────────

_GEOHASH_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_GEOHASH_INDEX = {c: i for i, c in enumerate(_GEOHASH_BASE32)}


def _geohash_encode(lat: float, lng: float, precision: int = 9) -> str:
    """Encode a coordinate as a geohash string (Niemeyer, 2008)."""
    lat_lo, lat_hi = -90.0, 90.0
    lng_lo, lng_hi = -180.0, 180.0
    chars: list[str] = []
    bits = ch = 0
    even = True
    while len(chars) < precision:
        if even:
            mid = (lng_lo + lng_hi) / 2
            if lng >= mid:
                ch = (ch << 1) | 1
                lng_lo = mid
            else:
                ch <<= 1
                lng_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                ch = (ch << 1) | 1
                lat_lo = mid
            else:
                ch <<= 1
                lat_hi = mid
        even = not even
        bits += 1
        if bits == 5:
            chars.append(_GEOHASH_BASE32[ch])
            bits = ch = 0
    return "".join(chars)


def _geohash_bounds(geohash: str) -> tuple[float, float, float, float]:
    """Return ``(min_lat, min_lng, max_lat, max_lng)`` of a geohash cell."""
    lat_lo, lat_hi = -90.0, 90.0
    lng_lo, lng_hi = -180.0, 180.0
    even = True
    for c in geohash.lower():
        if c not in _GEOHASH_INDEX:
            raise InvalidArgumentError(
                "geohash", geohash,
                f"geohash characters from '{_GEOHASH_BASE32}' (found {c!r})",
                "geo.geohash_decode('ttnfv2u')",
            )
        idx = _GEOHASH_INDEX[c]
        for shift in range(4, -1, -1):
            bit = (idx >> shift) & 1
            if even:
                mid = (lng_lo + lng_hi) / 2
                if bit:
                    lng_lo = mid
                else:
                    lng_hi = mid
            else:
                mid = (lat_lo + lat_hi) / 2
                if bit:
                    lat_lo = mid
                else:
                    lat_hi = mid
            even = not even
    return lat_lo, lng_lo, lat_hi, lng_hi


# ─────────────────────────────────────────────────────────────────────────────
# CSV column auto-detection
# ─────────────────────────────────────────────────────────────────────────────

_LAT_CANDIDATES = [
    "latitude", "lat", "y", "ylat", "lat_deg",
    "latitude_deg", "geo_lat", "point_lat",
]
_LNG_CANDIDATES = [
    "longitude", "lng", "lon", "long", "x",
    "xlong", "lng_deg", "longitude_deg", "geo_lng",
    "geo_long", "point_lng",
]


def _find_column(columns: list[str], candidates: list[str]) -> str | None:
    lower_map: dict[str, str] = {}
    for c in columns:
        if c is not None:
            lower_map.setdefault(c.strip().lower(), c)
    return next((lower_map[cand] for cand in candidates if cand in lower_map), None)


def _detect_lat_lng_columns(
    columns: list[str],
    filepath: str,
) -> tuple[str, str]:
    """
    Auto-detect latitude and longitude column names (case- and
    whitespace-insensitive).

    Raises ColumnNotFoundError with clear guidance if detection fails.
    """
    lat_col = _find_column(columns, _LAT_CANDIDATES)
    lng_col = _find_column(columns, _LNG_CANDIDATES)

    hint = (f"geo.load({filepath!r}, lat_col={(lat_col or '<your lat column>')!r}, "
            f"lng_col={(lng_col or '<your lng column>')!r})")
    if lat_col is None:
        raise ColumnNotFoundError("latitude", columns, filepath, hint=hint)
    if lng_col is None:
        raise ColumnNotFoundError("longitude", columns, filepath, hint=hint)

    return lat_col, lng_col


# ─────────────────────────────────────────────────────────────────────────────
# Safe parsing
# ─────────────────────────────────────────────────────────────────────────────

_INT_RE = re.compile(r"^[+-]?(0|[1-9]\d*)$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


def _infer_value(value: Any) -> Any:
    """
    Convert a CSV string to int / float / bool / None where unambiguous.

    Values with leading zeros (ZIP/PIN codes, IDs) or a leading '+' (phone
    numbers) stay strings, as do numbers too large to represent as a float.
    """
    if not isinstance(value, str):
        return value
    s = value.strip()
    if s == "":
        return None
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if s.startswith("+"):
        return value
    if _INT_RE.match(s):
        try:
            return int(s)
        except ValueError:   # beyond Python's int-string conversion limit
            return value
    if _FLOAT_RE.match(s) and not re.match(r"^-?0\d", s):
        digits = re.sub(r"[eE].*$", "", s).lstrip("+-").replace(".", "").lstrip("0")
        if len(digits) > 15:
            return value      # more significant digits than a float keeps
        f = float(s)
        return f if math.isfinite(f) else value
    return value


# ─────────────────────────────────────────────────────────────────────────────
# GeoJSON validation
# ─────────────────────────────────────────────────────────────────────────────

_NESTING = {"Point": 0, "MultiPoint": 1, "LineString": 1,
            "MultiLineString": 2, "Polygon": 2, "MultiPolygon": 3}


def _position_problem(pos: Any, strict_lng: bool) -> str | None:
    if not isinstance(pos, (list, tuple)) or len(pos) < 2:
        return f"position {pos!r} is not a [lng, lat] pair"
    lng, lat = pos[0], pos[1]
    if not (_is_number(lng) and _is_number(lat)):
        return f"position {list(pos[:2])!r} must contain numbers"
    try:
        lng_f, lat_f = float(lng), float(lat)
    except OverflowError:
        return "position contains a number too large for a coordinate"
    if not (math.isfinite(lng_f) and math.isfinite(lat_f)):
        return f"position {list(pos[:2])!r} is not finite"
    if not -90.0 <= lat_f <= 90.0:
        return f"latitude {lat} is outside -90..90 (are lng/lat swapped?)"
    limit = 180.0 if strict_lng else 360.0
    if not -limit <= lng_f <= limit:
        return f"longitude {lng} is outside -{limit:g}..{limit:g}"
    return None


def _coords_problem(coords: Any, depth: int, gtype: str) -> str | None:
    if depth == 0:
        return _position_problem(coords, strict_lng=gtype == "Point")
    if not isinstance(coords, (list, tuple)):
        return f"{gtype} 'coordinates' must be nested {depth + 1} level(s) deep"
    if gtype in ("Polygon", "MultiPolygon") and depth == 1 and 0 < len(coords) < 3:
        return f"a {gtype} ring needs at least 3 positions"
    for c in coords:
        problem = _coords_problem(c, depth - 1, gtype)
        if problem:
            return problem
    return None


def _geometry_problem(geometry: Any, _depth: int = 0) -> str | None:
    """
    Return why ``geometry`` is not a usable GeoJSON geometry, or None if it
    is. Checks the type, the nesting of 'coordinates' and every position
    (numbers, finite, latitude within ±90; Points within ±180 longitude,
    other geometries within ±360 so rings drawn across the antimeridian load).
    """
    if not isinstance(geometry, dict):
        return "geometry must be a JSON object"
    gtype = geometry.get("type")
    if gtype == "GeometryCollection":
        geoms = geometry.get("geometries")
        if not isinstance(geoms, list):
            return "GeometryCollection 'geometries' must be a list"
        if _depth > 16:
            return "GeometryCollection is nested too deeply"
        for g in geoms:
            problem = _geometry_problem(g, _depth + 1)
            if problem:
                return problem
        return None
    if gtype not in _NESTING:
        return f"unsupported geometry type {gtype!r}"
    if "coordinates" not in geometry:
        return f"{gtype} has no 'coordinates'"
    return _coords_problem(geometry["coordinates"], _NESTING[gtype], gtype)


def _copy_geometry(geometry: Any) -> Any:
    """Copy a geometry's nested coordinate lists (cheaper than deepcopy)."""
    if not isinstance(geometry, dict):
        return geometry

    def nested(c: Any) -> Any:
        return [nested(x) for x in c] if isinstance(c, (list, tuple)) else c

    out = dict(geometry)
    if "coordinates" in out:
        out["coordinates"] = nested(out["coordinates"])
    if isinstance(out.get("geometries"), list):
        out["geometries"] = [_copy_geometry(g) for g in out["geometries"]]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Serialisation & files
# ─────────────────────────────────────────────────────────────────────────────

_MAX_SAFE_INT = 2 ** 53 - 1


def _json_key(k: Any) -> str:
    if isinstance(k, str):
        return k
    if isinstance(k, bool):
        return "true" if k else "false"
    if k is None:
        return "null"
    if isinstance(k, float) and not math.isfinite(k):
        return "NaN" if k != k else ("Infinity" if k > 0 else "-Infinity")
    return str(k)


def _json_safe(obj: Any, js: bool = False) -> Any:
    """
    Return a JSON-compatible copy of ``obj``: NaN / ±Infinity (float or
    Decimal) become None, sets and tuples become lists, dates become ISO
    strings, Decimals become floats, dict keys become strings and any other
    object becomes ``str(obj)``. With ``js=True``, integers too large for a
    JavaScript number (> 2**53 − 1) become strings so no digits are lost.
    """
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, int):
        return str(obj) if js and abs(obj) > _MAX_SAFE_INT else obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, decimal.Decimal):
        if not obj.is_finite():
            return None
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, dict):
        return {_json_key(k): _json_safe(v, js) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v, js) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return [_json_safe(v, js) for v in sorted(obj, key=str)]
    if isinstance(obj, (datetime.date, datetime.time)):
        return obj.isoformat()
    return str(obj)


_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _escape_surrogates(text: str) -> str:
    """Replace lone UTF-16 surrogates (unencodable in UTF-8) with JSON \\uXXXX escapes."""
    return _SURROGATE_RE.sub(lambda m: f"\\u{ord(m.group(0)):04x}", text)


def _atomic_write(path: str, text: str | bytes, encoding: str = "utf-8", errors: str = "strict") -> None:
    """
    Write ``text`` (str, or bytes for binary files) to ``path`` via a
    temporary file in the same directory and an atomic rename, so a failure
    never leaves a truncated file behind.

    Symlinks are followed (the file they point to is updated), a read-only
    target raises PermissionError like a plain ``open(path, "w")`` would, and
    hard-linked targets or folders where a temporary file can't be created
    are written in place instead.
    """
    real = os.path.realpath(path)
    parent = os.path.dirname(real)
    os.makedirs(parent, exist_ok=True)

    def open_for(name: str, mode: str) -> IO[Any]:
        if isinstance(text, bytes):
            return open(name, mode + "b")
        return open(name, mode, encoding=encoding, errors=errors, newline="")

    def write_in_place() -> None:
        with open_for(real, "w") as f:
            f.write(text)

    if os.path.exists(real):
        if not os.access(real, os.W_OK):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), path)
        if os.stat(real).st_nlink > 1:
            write_in_place()   # keep every hard-linked name pointing at the new content
            return

    tmp = os.path.join(parent, f".qg-{secrets.token_hex(6)}.tmp")
    try:
        f = open_for(tmp, "x")
    except OSError:
        write_in_place()       # e.g. a writable file in a read-only folder
        return
    try:
        with f:
            f.write(text)
        if os.path.exists(real):
            with contextlib.suppress(OSError):
                shutil.copymode(real, tmp)
        os.replace(tmp, real)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# csv.field_size_limit is process-wide; raise it only while QOREgeo is reading
# and restore it once the last concurrent reader is done.
_FIELD_LIMIT_LOCK = threading.Lock()
_field_limit_state = {"users": 0, "saved": 0}


@contextlib.contextmanager
def _large_csv_fields(limit: int) -> Any:
    with _FIELD_LIMIT_LOCK:
        if _field_limit_state["users"] == 0:
            _field_limit_state["saved"] = csv.field_size_limit()
            csv.field_size_limit(limit)
        _field_limit_state["users"] += 1
    try:
        yield
    finally:
        with _FIELD_LIMIT_LOCK:
            _field_limit_state["users"] -= 1
            if _field_limit_state["users"] == 0:
                csv.field_size_limit(_field_limit_state["saved"])


# ─────────────────────────────────────────────────────────────────────────────
# Delimiter detection
# ─────────────────────────────────────────────────────────────────────────────

_DELIMITERS = (",", ";", "\t", "|")


def _detect_delimiter(lines: list[str], prefer_tab: bool = False) -> str:
    """
    Pick the delimiter that splits the header into the most fields while
    keeping the data rows consistent with it. Quoting is honoured, so
    delimiters inside quoted header names are not counted.
    """
    order = ("\t", ",", ";", "|") if prefer_tab else _DELIMITERS
    best = order[0] if prefer_tab else ","
    best_score: tuple[int, int, int] | None = None
    for rank, delim in enumerate(order):
        rows = [r for r in csv.reader(lines, delimiter=delim) if r]
        if not rows or len(rows[0]) < 2:
            continue
        width = len(rows[0])
        consistent = sum(1 for r in rows[1:] if len(r) == width)
        score = (consistent, width, -rank)
        if best_score is None or score > best_score:
            best, best_score = delim, score
    return best
