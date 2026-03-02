"""
qoregeo.utils
=============
Pure utility functions — no side effects, fully testable.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

from .exceptions import (
    InvalidCoordinateError,
    ColumnNotFoundError,
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
EARTH_RADIUS_KM = 6371.0088   # mean Earth radius (IUGG)

# ─────────────────────────────────────────────────────────────────────────────
# Coordinate helpers
# ─────────────────────────────────────────────────────────────────────────────

def _validate_coord(lat: float, lng: float) -> None:
    """Raise InvalidCoordinateError if lat/lng are out of range."""
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError):
        raise InvalidCoordinateError(lat, lng)

    if not (-90.0 <= lat_f <= 90.0) or not (-180.0 <= lng_f <= 180.0):
        raise InvalidCoordinateError(lat_f, lng_f)


def _to_radians(deg: float) -> float:
    return math.radians(deg)


def _haversine_km(point_a: Tuple[float, float], point_b: Tuple[float, float]) -> float:
    """Return great-circle distance in km using the Haversine formula."""
    lat1, lng1 = _to_radians(point_a[0]), _to_radians(point_a[1])
    lat2, lng2 = _to_radians(point_b[0]), _to_radians(point_b[1])

    d_lat = lat2 - lat1
    d_lng = lng2 - lng1

    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(d_lng / 2) ** 2
    )
    c = 2 * math.asin(math.sqrt(a))
    return EARTH_RADIUS_KM * c


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
    """Convert a bearing in degrees to a 16-point compass string."""
    idx = round(degrees / 22.5) % 16
    return _COMPASS_POINTS[idx]


# ─────────────────────────────────────────────────────────────────────────────
# Circle polygon
# ─────────────────────────────────────────────────────────────────────────────

def _generate_circle_polygon(
    center: Tuple[float, float],
    radius_km: float,
    num_points: int = 64,
) -> List[List[float]]:
    """
    Generate a closed ring of [lng, lat] points approximating a circle.

    Uses spherical geometry to ensure geodesic accuracy.
    """
    lat, lng = center
    lat_r = _to_radians(lat)
    lng_r = _to_radians(lng)
    d = radius_km / EARTH_RADIUS_KM   # angular distance in radians

    points = []
    for i in range(num_points):
        brng = _to_radians(i * 360 / num_points)

        p_lat = math.asin(
            math.sin(lat_r) * math.cos(d)
            + math.cos(lat_r) * math.sin(d) * math.cos(brng)
        )
        p_lng = lng_r + math.atan2(
            math.sin(brng) * math.sin(d) * math.cos(lat_r),
            math.cos(d) - math.sin(lat_r) * math.sin(p_lat),
        )

        points.append([math.degrees(p_lng), math.degrees(p_lat)])

    points.append(points[0])   # close the ring
    return points


# ─────────────────────────────────────────────────────────────────────────────
# Point-in-polygon (ray casting)
# ─────────────────────────────────────────────────────────────────────────────

def _point_in_polygon_ray(
    lat: float,
    lng: float,
    ring: List[List[float]],
) -> bool:
    """
    Ray-casting test.

    ring is a list of [lng, lat] pairs (GeoJSON convention).
    """
    inside = False
    j = len(ring) - 1

    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]   # lng, lat
        xj, yj = ring[j][0], ring[j][1]

        intersect = (
            (yi > lat) != (yj > lat)
        ) and (
            lng < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-15) + xi
        )

        if intersect:
            inside = not inside
        j = i

    return inside


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


def _detect_lat_lng_columns(
    columns: List[str],
    filepath: str,
) -> Tuple[str, str]:
    """
    Auto-detect latitude and longitude column names (case-insensitive).

    Raises ColumnNotFoundError with clear guidance if detection fails.
    """
    lower_map = {c.lower(): c for c in columns}

    lat_col = next(
        (lower_map[cand] for cand in _LAT_CANDIDATES if cand in lower_map),
        None,
    )
    lng_col = next(
        (lower_map[cand] for cand in _LNG_CANDIDATES if cand in lower_map),
        None,
    )

    if lat_col is None:
        raise ColumnNotFoundError("latitude", columns, filepath)
    if lng_col is None:
        raise ColumnNotFoundError("longitude", columns, filepath)

    return lat_col, lng_col


# ─────────────────────────────────────────────────────────────────────────────
# Safe float parsing
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(
    value: Optional[str],
    col_name: str,
    row_num: int,
    filepath: str,
) -> Optional[float]:
    """
    Parse a string to float, return None if blank, warn on bad values.
    """
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        # Non-fatal — skip row rather than crash
        import warnings
        warnings.warn(
            f"QOREgeo: row {row_num} in '{filepath}' — "
            f"could not parse '{col_name}' value '{value}' as a number. Row skipped.",
            stacklevel=4,
        )
        return None
