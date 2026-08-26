"""
qoregeo.geometry
================
Pure-Python spherical and planar geometry primitives.

Everything here is dependency-free and works on any Python 3.8+ runtime.

Two coordinate conventions are used, and the distinction matters:

* **User-facing tuples** are ``(lat, lng)``, the order people say out loud.
* **GeoJSON rings/coordinates** are ``[lng, lat]``, the order the spec mandates.

Functions that take a ``ring``/``coords`` argument expect GeoJSON order.
Functions that take a ``point``/``center`` argument expect ``(lat, lng)``.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .exceptions import (
    InvalidBufferError,
    InvalidGeometryError,
    InvalidRadiusError,
    InvalidUnitError,
)
from .utils import (
    EARTH_RADIUS_KM,
    _haversine_km,
    _point_in_polygon_ray,
    _to_radians,
    _validate_coord,
)

Coord = Tuple[float, float]          # (lat, lng)
Ring = List[List[float]]             # [[lng, lat], ...]
Geometry = Dict[str, Any]

# WGS84 ellipsoid: used by the Vincenty solver.
WGS84_A = 6378137.0                  # semi-major axis, metres
WGS84_F = 1 / 298.257223563          # flattening
WGS84_B = (1 - WGS84_F) * WGS84_A    # semi-minor axis, metres

# ─────────────────────────────────────────────────────────────────────────────
# Unit conversion
# ─────────────────────────────────────────────────────────────────────────────

#: Multiply a value in km by these to convert.
LENGTH_UNITS = {
    "km": 1.0,
    "miles": 0.621371,
    "mi": 0.621371,
    "m": 1000.0,
    "metres": 1000.0,
    "meters": 1000.0,
    "ft": 3280.84,
    "feet": 3280.84,
    "nm": 0.539957,          # nautical miles
    "nmi": 0.539957,
}

#: Multiply a value in square km by these to convert.
AREA_UNITS = {
    "km2": 1.0,
    "sqkm": 1.0,
    "m2": 1_000_000.0,
    "sqm": 1_000_000.0,
    "ha": 100.0,
    "hectares": 100.0,
    "acres": 247.10538,
    "mi2": 0.386102,
    "sqmi": 0.386102,
}


def convert_length(km: float, unit: str = "km") -> float:
    """Convert a distance expressed in km into ``unit``."""
    key = str(unit).lower()
    if key not in LENGTH_UNITS:
        raise InvalidUnitError(unit, sorted(set(LENGTH_UNITS)))
    return km * LENGTH_UNITS[key]


def to_km(value: float, unit: str = "km") -> float:
    """Convert a distance expressed in ``unit`` back into km."""
    key = str(unit).lower()
    if key not in LENGTH_UNITS:
        raise InvalidUnitError(unit, sorted(set(LENGTH_UNITS)))
    return value / LENGTH_UNITS[key]


def convert_area(km2: float, unit: str = "km2") -> float:
    """Convert an area expressed in square km into ``unit``."""
    key = str(unit).lower().replace("²", "2").replace(" ", "")
    if key not in AREA_UNITS:
        raise InvalidUnitError(unit, sorted(set(AREA_UNITS)))
    return km2 * AREA_UNITS[key]


# ─────────────────────────────────────────────────────────────────────────────
# Great-circle navigation
# ─────────────────────────────────────────────────────────────────────────────

def bearing_degrees(point_a: Coord, point_b: Coord) -> float:
    """Initial great-circle bearing from A to B, in degrees clockwise from north."""
    lat1 = _to_radians(point_a[0])
    lat2 = _to_radians(point_b[0])
    d_lng = _to_radians(point_b[1] - point_a[1])

    x = math.sin(d_lng) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d_lng)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def destination(origin: Coord, bearing: float, distance_km: float) -> Coord:
    """
    Solve the *direct* geodesic problem: where do you end up travelling
    ``distance_km`` from ``origin`` on a constant initial ``bearing``?

    >>> lat, lng = destination((28.6139, 77.2090), 90, 100)
    """
    _validate_coord(*origin)
    lat1 = _to_radians(origin[0])
    lng1 = _to_radians(origin[1])
    brng = _to_radians(bearing)
    d = distance_km / EARTH_RADIUS_KM

    lat2 = math.asin(
        math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(brng)
    )
    lng2 = lng1 + math.atan2(
        math.sin(brng) * math.sin(d) * math.cos(lat1),
        math.cos(d) - math.sin(lat1) * math.sin(lat2),
    )
    return (math.degrees(lat2), _normalise_lng(math.degrees(lng2)))


def interpolate(point_a: Coord, point_b: Coord, fraction: float) -> Coord:
    """
    Point at ``fraction`` (0..1) of the great-circle path from A to B.

    Uses spherical linear interpolation, so the result stays on the arc
    rather than cutting through the Earth.
    """
    _validate_coord(*point_a)
    _validate_coord(*point_b)
    fraction = max(0.0, min(1.0, float(fraction)))

    lat1, lng1 = _to_radians(point_a[0]), _to_radians(point_a[1])
    lat2, lng2 = _to_radians(point_b[0]), _to_radians(point_b[1])

    d = _haversine_km(point_a, point_b) / EARTH_RADIUS_KM
    if d == 0:
        return (point_a[0], point_a[1])

    a = math.sin((1 - fraction) * d) / math.sin(d)
    b = math.sin(fraction * d) / math.sin(d)

    x = a * math.cos(lat1) * math.cos(lng1) + b * math.cos(lat2) * math.cos(lng2)
    y = a * math.cos(lat1) * math.sin(lng1) + b * math.cos(lat2) * math.sin(lng2)
    z = a * math.sin(lat1) + b * math.sin(lat2)

    lat = math.atan2(z, math.sqrt(x * x + y * y))
    lng = math.atan2(y, x)
    return (math.degrees(lat), _normalise_lng(math.degrees(lng)))


def midpoint(point_a: Coord, point_b: Coord) -> Coord:
    """Great-circle midpoint between two coordinates."""
    return interpolate(point_a, point_b, 0.5)


def vincenty_km(point_a: Coord, point_b: Coord, max_iterations: int = 200) -> float:
    """
    Ellipsoidal (WGS84) distance via Vincenty's inverse formula.

    Accurate to ~0.5 mm, versus Haversine's ~0.5% error from treating the
    Earth as a sphere. Falls back to Haversine for near-antipodal pairs,
    where Vincenty is known not to converge.
    """
    _validate_coord(*point_a)
    _validate_coord(*point_b)

    lat1, lng1 = _to_radians(point_a[0]), _to_radians(point_a[1])
    lat2, lng2 = _to_radians(point_b[0]), _to_radians(point_b[1])

    if abs(lat1 - lat2) < 1e-12 and abs(lng1 - lng2) < 1e-12:
        return 0.0

    u1 = math.atan((1 - WGS84_F) * math.tan(lat1))
    u2 = math.atan((1 - WGS84_F) * math.tan(lat2))
    sin_u1, cos_u1 = math.sin(u1), math.cos(u1)
    sin_u2, cos_u2 = math.sin(u2), math.cos(u2)

    ll = lng2 - lng1
    lam = ll
    sin_sigma = cos_sigma = sigma = cos_sq_alpha = cos2_sigma_m = 0.0

    for _ in range(max_iterations):
        sin_lam, cos_lam = math.sin(lam), math.cos(lam)
        sin_sigma = math.sqrt(
            (cos_u2 * sin_lam) ** 2
            + (cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lam) ** 2
        )
        if sin_sigma == 0:
            return 0.0
        cos_sigma = sin_u1 * sin_u2 + cos_u1 * cos_u2 * cos_lam
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cos_u1 * cos_u2 * sin_lam / sin_sigma
        cos_sq_alpha = 1 - sin_alpha ** 2
        cos2_sigma_m = (
            cos_sigma - 2 * sin_u1 * sin_u2 / cos_sq_alpha if cos_sq_alpha != 0 else 0.0
        )
        c = WGS84_F / 16 * cos_sq_alpha * (4 + WGS84_F * (4 - 3 * cos_sq_alpha))
        lam_prev = lam
        lam = ll + (1 - c) * WGS84_F * sin_alpha * (
            sigma
            + c
            * sin_sigma
            * (cos2_sigma_m + c * cos_sigma * (-1 + 2 * cos2_sigma_m ** 2))
        )
        if abs(lam - lam_prev) < 1e-12:
            break
    else:
        # Near-antipodal: Vincenty does not converge. Haversine is the sane answer.
        return _haversine_km(point_a, point_b)

    u_sq = cos_sq_alpha * (WGS84_A ** 2 - WGS84_B ** 2) / (WGS84_B ** 2)
    a_coef = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    b_coef = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))
    d_sigma = (
        b_coef
        * sin_sigma
        * (
            cos2_sigma_m
            + b_coef
            / 4
            * (
                cos_sigma * (-1 + 2 * cos2_sigma_m ** 2)
                - b_coef
                / 6
                * cos2_sigma_m
                * (-3 + 4 * sin_sigma ** 2)
                * (-3 + 4 * cos2_sigma_m ** 2)
            )
        )
    )
    return WGS84_B * a_coef * (sigma - d_sigma) / 1000.0


def cross_track_km(point: Coord, line_start: Coord, line_end: Coord) -> float:
    """
    Signed perpendicular distance (km) from ``point`` to the great circle
    through ``line_start`` → ``line_end``.

    Positive means the point lies to the left of the path; negative, right.
    """
    d13 = _haversine_km(line_start, point) / EARTH_RADIUS_KM
    theta13 = _to_radians(bearing_degrees(line_start, point))
    theta12 = _to_radians(bearing_degrees(line_start, line_end))
    return math.asin(math.sin(d13) * math.sin(theta13 - theta12)) * EARTH_RADIUS_KM


def along_track_km(point: Coord, line_start: Coord, line_end: Coord) -> float:
    """How far along the ``line_start`` → ``line_end`` path the point projects."""
    d13 = _haversine_km(line_start, point) / EARTH_RADIUS_KM
    xtd = cross_track_km(point, line_start, line_end) / EARTH_RADIUS_KM
    cos_ratio = math.cos(d13) / math.cos(xtd)
    cos_ratio = max(-1.0, min(1.0, cos_ratio))
    return math.acos(cos_ratio) * EARTH_RADIUS_KM


def nearest_point_on_line(point: Coord, line: Sequence[Coord]) -> Dict[str, Any]:
    """
    Snap a point onto a polyline.

    Returns ``{"point": (lat, lng), "distance": km, "segment": index,
    "fraction": 0..1}``. Everything you need to place a marker on a route.
    """
    if len(line) < 2:
        raise InvalidGeometryError("LineString", "a line needs at least two points")

    best: Dict[str, Any] = {
        "point": tuple(line[0]),
        "distance": _haversine_km(point, line[0]),
        "segment": 0,
        "fraction": 0.0,
    }

    for i in range(len(line) - 1):
        a, b = line[i], line[i + 1]
        seg_len = _haversine_km(a, b)
        if seg_len == 0:
            candidate, fraction = (a[0], a[1]), 0.0
        else:
            along = along_track_km(point, a, b)
            fraction = max(0.0, min(1.0, along / seg_len))
            candidate = interpolate(a, b, fraction)

        d = _haversine_km(point, candidate)
        if d < best["distance"]:
            best = {
                "point": candidate,
                "distance": d,
                "segment": i,
                "fraction": round(fraction, 6),
            }

    best["distance"] = round(best["distance"], 6)
    return best


def _normalise_lng(lng: float) -> float:
    """Wrap a longitude into [-180, 180]."""
    return (lng + 540.0) % 360.0 - 180.0


# ─────────────────────────────────────────────────────────────────────────────
# Length and area
# ─────────────────────────────────────────────────────────────────────────────

def line_length_km(coords: Sequence[Sequence[float]]) -> float:
    """Great-circle length of a GeoJSON ``[[lng, lat], ...]`` line, in km."""
    total = 0.0
    for i in range(len(coords) - 1):
        a = (coords[i][1], coords[i][0])
        b = (coords[i + 1][1], coords[i + 1][0])
        total += _haversine_km(a, b)
    return total


def ring_area_km2(ring: Sequence[Sequence[float]]) -> float:
    """
    Spherical area of a closed ``[[lng, lat], ...]`` ring, in square km.

    Uses the spherical-excess formula, so it is accurate for rings of any
    size, unlike the planar shoelace approximation, which degrades badly
    away from the equator.
    """
    if len(ring) < 3:
        return 0.0

    pts = list(ring)
    if pts[0] != pts[-1]:
        pts.append(pts[0])

    total = 0.0
    for i in range(len(pts) - 1):
        lng1, lat1 = _to_radians(pts[i][0]), _to_radians(pts[i][1])
        lng2, lat2 = _to_radians(pts[i + 1][0]), _to_radians(pts[i + 1][1])
        total += (lng2 - lng1) * (2 + math.sin(lat1) + math.sin(lat2))

    return abs(total * EARTH_RADIUS_KM ** 2 / 2.0)


def ring_is_clockwise(ring: Sequence[Sequence[float]]) -> bool:
    """True if a ring winds clockwise (planar shoelace sign)."""
    total = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        total += (x2 - x1) * (y2 + y1)
    return total > 0


def geometry_area_km2(geometry: Geometry) -> float:
    """
    Area of any GeoJSON geometry, in square km.

    Interior rings (holes) are subtracted. Points and lines have zero area.
    """
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")

    if gtype == "Polygon":
        if not coords:
            return 0.0
        outer = ring_area_km2(coords[0])
        holes = sum(ring_area_km2(r) for r in coords[1:])
        return max(0.0, outer - holes)

    if gtype == "MultiPolygon":
        return sum(
            geometry_area_km2({"type": "Polygon", "coordinates": poly})
            for poly in (coords or [])
        )

    if gtype == "GeometryCollection":
        return sum(geometry_area_km2(g) for g in geometry.get("geometries", []))

    return 0.0


def geometry_length_km(geometry: Geometry) -> float:
    """
    Length of any GeoJSON geometry, in km.

    Lines return their length; polygons return their perimeter (outer ring
    plus every hole); points return zero.
    """
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")

    if gtype == "LineString":
        return line_length_km(coords or [])
    if gtype == "MultiLineString":
        return sum(line_length_km(line) for line in (coords or []))
    if gtype == "Polygon":
        return sum(line_length_km(_closed(ring)) for ring in (coords or []))
    if gtype == "MultiPolygon":
        return sum(
            geometry_length_km({"type": "Polygon", "coordinates": poly})
            for poly in (coords or [])
        )
    if gtype == "GeometryCollection":
        return sum(geometry_length_km(g) for g in geometry.get("geometries", []))
    return 0.0


def _closed(ring: Sequence[Sequence[float]]) -> List[List[float]]:
    """Return a ring with its first point repeated at the end."""
    pts = [list(p) for p in ring]
    if pts and pts[0] != pts[-1]:
        pts.append(list(pts[0]))
    return pts


# ─────────────────────────────────────────────────────────────────────────────
# Coordinate extraction, bounds and centroids
# ─────────────────────────────────────────────────────────────────────────────

def coords_of(geometry: Optional[Geometry]) -> List[List[float]]:
    """Flatten any GeoJSON geometry into a flat list of ``[lng, lat]`` points."""
    if not geometry:
        return []

    gtype = geometry.get("type")
    coords = geometry.get("coordinates")

    if gtype == "Point":
        return [list(coords[:2])] if coords else []
    if gtype in ("MultiPoint", "LineString"):
        return [list(c[:2]) for c in (coords or [])]
    if gtype in ("MultiLineString", "Polygon"):
        return [list(c[:2]) for part in (coords or []) for c in part]
    if gtype == "MultiPolygon":
        return [
            list(c[:2])
            for poly in (coords or [])
            for ring in poly
            for c in ring
        ]
    if gtype == "GeometryCollection":
        out: List[List[float]] = []
        for g in geometry.get("geometries", []):
            out.extend(coords_of(g))
        return out
    return []


def bbox_of(geometry: Geometry) -> List[float]:
    """GeoJSON-order bounding box ``[min_lng, min_lat, max_lng, max_lat]``."""
    pts = coords_of(geometry)
    if not pts:
        raise InvalidGeometryError(str(geometry.get("type")), "geometry has no coordinates")
    lngs = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return [min(lngs), min(lats), max(lngs), max(lats)]


def bbox_polygon(bbox: Sequence[float]) -> Geometry:
    """Turn ``[min_lng, min_lat, max_lng, max_lat]`` into a GeoJSON Polygon."""
    min_lng, min_lat, max_lng, max_lat = bbox
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [min_lng, min_lat],
                [max_lng, min_lat],
                [max_lng, max_lat],
                [min_lng, max_lat],
                [min_lng, min_lat],
            ]
        ],
    }


def centroid_of(geometry: Geometry) -> Coord:
    """
    Centroid of a geometry as ``(lat, lng)``.

    Polygons use the area-weighted centroid (which stays inside a convex
    shape); everything else averages its vertices.
    """
    gtype = geometry.get("type")

    if gtype in ("Polygon", "MultiPolygon"):
        rings = (
            geometry["coordinates"][:1]
            if gtype == "Polygon"
            else [poly[0] for poly in geometry["coordinates"]]
        )
        cx = cy = area_sum = 0.0
        for ring in rings:
            pts = _closed(ring)
            a = 0.0
            rx = ry = 0.0
            for i in range(len(pts) - 1):
                x1, y1 = pts[i]
                x2, y2 = pts[i + 1]
                cross = x1 * y2 - x2 * y1
                a += cross
                rx += (x1 + x2) * cross
                ry += (y1 + y2) * cross
            if a != 0:
                a *= 0.5
                cx += rx / 6.0
                cy += ry / 6.0
                area_sum += a
        if area_sum != 0:
            return (cy / area_sum, cx / area_sum)

    pts = coords_of(geometry)
    if not pts:
        raise InvalidGeometryError(str(gtype), "geometry has no coordinates")
    return (
        sum(p[1] for p in pts) / len(pts),
        sum(p[0] for p in pts) / len(pts),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Point-in-geometry (holes and multi-parts included)
# ─────────────────────────────────────────────────────────────────────────────

def point_in_geometry(point: Coord, geometry: Geometry) -> bool:
    """
    True if ``(lat, lng)`` falls inside a Polygon or MultiPolygon.

    Interior rings are treated as holes: a point inside a hole is *outside*
    the polygon. Anything without an interior (points, lines) returns False.
    """
    if not isinstance(geometry, dict):
        raise InvalidBufferError()

    gtype = geometry.get("type")

    if gtype == "Feature":
        return point_in_geometry(point, geometry.get("geometry") or {})

    if gtype == "FeatureCollection":
        return any(
            point_in_geometry(point, f.get("geometry") or {})
            for f in geometry.get("features", [])
        )

    if gtype == "GeometryCollection":
        return any(point_in_geometry(point, g) for g in geometry.get("geometries", []))

    lat, lng = point

    if gtype == "Polygon":
        rings = geometry.get("coordinates") or []
        if not rings:
            return False
        if not _point_in_polygon_ray(lat, lng, rings[0]):
            return False
        return not any(_point_in_polygon_ray(lat, lng, hole) for hole in rings[1:])

    if gtype == "MultiPolygon":
        return any(
            point_in_geometry(point, {"type": "Polygon", "coordinates": poly})
            for poly in (geometry.get("coordinates") or [])
        )

    if gtype in ("Point", "MultiPoint", "LineString", "MultiLineString"):
        return False

    raise InvalidBufferError()


# ─────────────────────────────────────────────────────────────────────────────
# Intersection tests
# ─────────────────────────────────────────────────────────────────────────────

def _orientation(p: Sequence[float], q: Sequence[float], r: Sequence[float]) -> int:
    """0 = collinear, 1 = clockwise, 2 = counter-clockwise."""
    val = (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])
    if abs(val) < 1e-14:
        return 0
    return 1 if val > 0 else 2


def _on_segment(p: Sequence[float], q: Sequence[float], r: Sequence[float]) -> bool:
    return (
        min(p[0], r[0]) - 1e-14 <= q[0] <= max(p[0], r[0]) + 1e-14
        and min(p[1], r[1]) - 1e-14 <= q[1] <= max(p[1], r[1]) + 1e-14
    )


def segments_intersect(
    p1: Sequence[float],
    p2: Sequence[float],
    p3: Sequence[float],
    p4: Sequence[float],
) -> bool:
    """True if segment p1-p2 crosses or touches segment p3-p4 (planar)."""
    o1 = _orientation(p1, p2, p3)
    o2 = _orientation(p1, p2, p4)
    o3 = _orientation(p3, p4, p1)
    o4 = _orientation(p3, p4, p2)

    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and _on_segment(p1, p3, p2):
        return True
    if o2 == 0 and _on_segment(p1, p4, p2):
        return True
    if o3 == 0 and _on_segment(p3, p1, p4):
        return True
    return o4 == 0 and _on_segment(p3, p2, p4)


def bbox_overlaps(a: Sequence[float], b: Sequence[float]) -> bool:
    """True if two ``[min_lng, min_lat, max_lng, max_lat]`` boxes overlap."""
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _polygon_rings(geometry: Geometry) -> List[List[Ring]]:
    """Normalise Polygon/MultiPolygon into a list of ring-lists."""
    gtype = geometry.get("type")
    if gtype == "Feature":
        return _polygon_rings(geometry.get("geometry") or {})
    if gtype == "Polygon":
        return [geometry.get("coordinates") or []]
    if gtype == "MultiPolygon":
        return list(geometry.get("coordinates") or [])
    return []


def intersects(geom_a: Geometry, geom_b: Geometry) -> bool:
    """
    True if two geometries share any space.

    Handles polygon/polygon, polygon/line and line/line. Cheap bounding-box
    rejection runs first, so non-overlapping shapes cost almost nothing.
    """
    if not isinstance(geom_a, dict) or not isinstance(geom_b, dict):
        raise InvalidBufferError()

    if geom_a.get("type") == "Feature":
        geom_a = geom_a.get("geometry") or {}
    if geom_b.get("type") == "Feature":
        geom_b = geom_b.get("geometry") or {}

    try:
        if not bbox_overlaps(bbox_of(geom_a), bbox_of(geom_b)):
            return False
    except InvalidGeometryError:
        return False

    # A single point of one inside the other is enough.
    pts_a = coords_of(geom_a)
    pts_b = coords_of(geom_b)

    if _has_area(geom_b) and any(point_in_geometry((p[1], p[0]), geom_b) for p in pts_a):
        return True
    if _has_area(geom_a) and any(point_in_geometry((p[1], p[0]), geom_a) for p in pts_b):
        return True

    # Otherwise look for a crossing edge.
    for seg_a in _edges(geom_a):
        for seg_b in _edges(geom_b):
            if segments_intersect(seg_a[0], seg_a[1], seg_b[0], seg_b[1]):
                return True
    return False


def contains(outer: Geometry, inner: Geometry) -> bool:
    """True if every vertex of ``inner`` lies inside ``outer`` and no edge crosses out."""
    if outer.get("type") == "Feature":
        outer = outer.get("geometry") or {}
    if inner.get("type") == "Feature":
        inner = inner.get("geometry") or {}

    if not _has_area(outer):
        return False

    pts = coords_of(inner)
    if not pts:
        return False
    if not all(point_in_geometry((p[1], p[0]), outer) for p in pts):
        return False

    for seg_i in _edges(inner):
        for seg_o in _edges(outer):
            if segments_intersect(seg_i[0], seg_i[1], seg_o[0], seg_o[1]):
                return False
    return True


def _has_area(geometry: Geometry) -> bool:
    return geometry.get("type") in ("Polygon", "MultiPolygon")


def _edges(geometry: Geometry) -> List[Tuple[List[float], List[float]]]:
    """Every edge of a geometry as ``(start, end)`` ``[lng, lat]`` pairs."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    lines: List[List[List[float]]] = []

    if gtype == "LineString":
        lines = [list(coords or [])]
    elif gtype == "MultiLineString":
        lines = [list(line) for line in (coords or [])]
    elif gtype == "Polygon":
        lines = [_closed(ring) for ring in (coords or [])]
    elif gtype == "MultiPolygon":
        lines = [_closed(ring) for poly in (coords or []) for ring in poly]
    elif gtype == "GeometryCollection":
        out: List[Tuple[List[float], List[float]]] = []
        for g in geometry.get("geometries", []):
            out.extend(_edges(g))
        return out

    edges: List[Tuple[List[float], List[float]]] = []
    for line in lines:
        for i in range(len(line) - 1):
            edges.append((list(line[i]), list(line[i + 1])))
    return edges


# ─────────────────────────────────────────────────────────────────────────────
# Convex hull
# ─────────────────────────────────────────────────────────────────────────────

def convex_hull(points: Sequence[Sequence[float]]) -> Ring:
    """
    Convex hull of ``[lng, lat]`` points via Andrew's monotone chain. O(n log n).

    Returns a closed ring. Fewer than three distinct points raises, since
    a hull needs an interior.
    """
    pts = sorted({(float(p[0]), float(p[1])) for p in points})
    if len(pts) < 3:
        raise InvalidGeometryError(
            "ConvexHull", f"need at least 3 distinct points, got {len(pts)}"
        )

    def build(seq: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
        chain: List[Tuple[float, float]] = []
        for p in seq:
            while len(chain) >= 2 and _cross(chain[-2], chain[-1], p) <= 0:
                chain.pop()
            chain.append(p)
        return chain

    lower = build(pts)
    upper = build(list(reversed(pts)))
    ring = lower[:-1] + upper[:-1]

    if len(ring) < 3:
        raise InvalidGeometryError("ConvexHull", "points are collinear. No hull exists")

    out = [[p[0], p[1]] for p in ring]
    out.append(list(out[0]))
    return out


def _cross(o: Tuple[float, float], a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


# ─────────────────────────────────────────────────────────────────────────────
# Simplification
# ─────────────────────────────────────────────────────────────────────────────

def simplify(
    coords: Sequence[Sequence[float]],
    tolerance: float = 0.001,
    closed: bool = False,
) -> List[List[float]]:
    """
    Ramer-Douglas-Peucker simplification of a ``[lng, lat]`` sequence.

    ``tolerance`` is in degrees: 0.001 ≈ 100 m near the equator. Set
    ``closed=True`` for rings so the shape stays closed.
    """
    pts = [list(p[:2]) for p in coords]
    if len(pts) <= 2:
        return pts

    if closed and pts[0] != pts[-1]:
        pts.append(list(pts[0]))

    keep = _rdp(pts, float(tolerance))

    if closed:
        if len(keep) < 4:
            return pts
        if keep[0] != keep[-1]:
            keep.append(list(keep[0]))
    return keep


def _rdp(pts: List[List[float]], tolerance: float) -> List[List[float]]:
    if len(pts) < 3:
        return pts

    start, end = pts[0], pts[-1]
    max_dist = 0.0
    index = 0
    for i in range(1, len(pts) - 1):
        d = _perpendicular_distance(pts[i], start, end)
        if d > max_dist:
            max_dist = d
            index = i

    if max_dist <= tolerance:
        return [start, end]

    left = _rdp(pts[: index + 1], tolerance)
    right = _rdp(pts[index:], tolerance)
    return left[:-1] + right


def _perpendicular_distance(
    point: Sequence[float],
    start: Sequence[float],
    end: Sequence[float],
) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if dx == 0 and dy == 0:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    t = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    px = start[0] + t * dx
    py = start[1] + t * dy
    return math.hypot(point[0] - px, point[1] - py)


# ─────────────────────────────────────────────────────────────────────────────
# Buffers
# ─────────────────────────────────────────────────────────────────────────────

def circle_ring(center: Coord, radius_km: float, num_points: int = 64) -> Ring:
    """Closed geodesic circle as a ``[lng, lat]`` ring."""
    if radius_km <= 0:
        raise InvalidRadiusError(radius_km)
    ring = []
    for i in range(num_points):
        lat, lng = destination(center, i * 360.0 / num_points, radius_km)
        ring.append([lng, lat])
    ring.append(list(ring[0]))
    return ring


def segment_buffer_ring(
    a: Coord,
    b: Coord,
    radius_km: float,
    cap_points: int = 16,
) -> Ring:
    """
    Stadium (capsule) polygon around one segment: two parallel offsets joined
    by semicircular end caps.

    The offset sides are **densified** rather than drawn as two straight
    edges. A great circle is a curve in lng/lat space, so joining the offsets
    at each end with a straight chord would bulge into the corridor on one
    side and out of it on the other. Over a 1000 km leg that error runs to
    tens of kilometres. Sampling along the arc and offsetting from the local
    tangent keeps both sides a true fixed distance from the path.
    """
    length_km = _haversine_km(a, b)

    # One sample per half-radius of travel keeps the chord error well under
    # the corridor width, and the cap stops pathological point counts.
    target = max(radius_km / 2.0, 1.0)
    steps = max(2, min(512, int(math.ceil(length_km / target))))
    samples = [interpolate(a, b, i / steps) for i in range(steps + 1)]

    def tangent(i: int) -> float:
        """Forward bearing at sample ``i``, taken from its neighbour."""
        if i < len(samples) - 1:
            return bearing_degrees(samples[i], samples[i + 1])
        return bearing_degrees(samples[i - 1], samples[i])

    bearings = [tangent(i) for i in range(len(samples))]

    def offset(index: int, side: float) -> List[float]:
        lat, lng = destination(samples[index], (bearings[index] + side) % 360, radius_km)
        return [lng, lat]

    ring: List[List[float]] = [offset(i, 90) for i in range(len(samples))]

    # Cap around b. The sweep *decreases* from bearing+90 to bearing-90 so it
    # passes through the bearing itself: the far side of b. Sweeping the
    # other way would cut back across the segment, leaving the endpoint
    # outside its own buffer.
    end_bearing = bearings[-1]
    for i in range(1, cap_points):
        lat, lng = destination(
            samples[-1], (end_bearing + 90 - 180 * i / cap_points) % 360, radius_km
        )
        ring.append([lng, lat])

    ring.extend(offset(i, -90) for i in range(len(samples) - 1, -1, -1))

    # Cap around a, again decreasing, so it passes through bearing+180.
    start_bearing = bearings[0]
    for i in range(1, cap_points):
        lat, lng = destination(
            samples[0], (start_bearing - 90 - 180 * i / cap_points) % 360, radius_km
        )
        ring.append([lng, lat])

    ring.append(list(ring[0]))
    return ring


def line_buffer(
    line: Sequence[Coord],
    radius_km: float,
    cap_points: int = 16,
) -> Geometry:
    """
    Buffer a polyline into a MultiPolygon corridor.

    Each segment contributes one capsule. Their union is the true buffer, and
    because the caps are full semicircles the joins are already covered, no
    mitring artefacts at corners.
    """
    if radius_km <= 0:
        raise InvalidRadiusError(radius_km)
    if len(line) < 2:
        raise InvalidGeometryError("LineString", "a line buffer needs at least two points")

    polys = []
    for i in range(len(line) - 1):
        a: Coord = (float(line[i][0]), float(line[i][1]))
        b: Coord = (float(line[i + 1][0]), float(line[i + 1][1]))
        if _haversine_km(a, b) == 0:
            polys.append([circle_ring(a, radius_km, cap_points * 2)])
        else:
            polys.append([segment_buffer_ring(a, b, radius_km, cap_points)])

    return {"type": "MultiPolygon", "coordinates": polys}


def geometry_buffer(
    geometry: Geometry,
    radius_km: float,
    num_points: int = 64,
) -> Geometry:
    """
    Buffer any geometry by ``radius_km``.

    Points become circles, lines become corridors, and polygons grow by their
    original shape plus a corridor along every edge.
    """
    if geometry.get("type") == "Feature":
        geometry = geometry.get("geometry") or {}

    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    cap = max(4, num_points // 4)

    if gtype == "Point":
        if not coords:
            raise InvalidGeometryError("Point", "the point has no coordinates")
        centre: Coord = (float(coords[1]), float(coords[0]))
        return {
            "type": "Polygon",
            "coordinates": [circle_ring(centre, radius_km, num_points)],
        }

    if gtype == "MultiPoint":
        return {
            "type": "MultiPolygon",
            "coordinates": [
                [circle_ring((c[1], c[0]), radius_km, num_points)] for c in (coords or [])
            ],
        }

    if gtype == "LineString":
        return line_buffer([(c[1], c[0]) for c in (coords or [])], radius_km, cap)

    if gtype == "MultiLineString":
        polys = []
        for line in (coords or []):
            buf = line_buffer([(c[1], c[0]) for c in line], radius_km, cap)
            polys.extend(buf["coordinates"])
        return {"type": "MultiPolygon", "coordinates": polys}

    if gtype in ("Polygon", "MultiPolygon"):
        grown: List[List[Ring]] = []
        for rings in _polygon_rings(geometry):
            grown.append([_closed(rings[0])])
            outline = [(c[1], c[0]) for c in _closed(rings[0])]
            buf = line_buffer(outline, radius_km, cap)
            grown.extend(buf["coordinates"])
        return {"type": "MultiPolygon", "coordinates": grown}

    raise InvalidGeometryError(str(gtype), "cannot buffer this geometry type")
