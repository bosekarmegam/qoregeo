"""
qoregeo.geohash
===============
Geohashes, slippy map tiles and quadkeys, the three ways spatial data gets
bucketed for storage, caching and joins.

A geohash turns a coordinate into a short string where shared prefixes mean
spatial proximity. That single property makes it useful far beyond mapping:
group rows by ``geohash[:5]`` and you have free clustering; index on it and a
plain B-tree database answers "near me" queries.

Tiles and quadkeys are the same idea in the projection the whole web uses,
and are what you need to talk to tile servers or Bing-style caches.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

from .exceptions import InvalidCoordinateError
from .utils import _validate_coord

Coord = Tuple[float, float]

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_DECODE = {c: i for i, c in enumerate(_BASE32)}

#: Approximate cell dimensions in metres, by geohash precision.
PRECISION_METRES = {
    1: (5_009_400, 4_992_600),
    2: (1_252_300, 624_100),
    3: (156_500, 156_000),
    4: (39_100, 19_500),
    5: (4_900, 4_900),
    6: (1_200, 609),
    7: (152.9, 152.4),
    8: (38.2, 19.1),
    9: (4.8, 4.8),
    10: (1.2, 0.595),
    11: (0.149, 0.149),
    12: (0.037, 0.019),
}


def encode(lat: float, lng: float, precision: int = 9) -> str:
    """
    Encode a coordinate as a geohash string.

    Precision 5 ≈ 5 km, 7 ≈ 150 m, 9 ≈ 5 m, 12 ≈ 4 cm.

    >>> encode(28.6139, 77.2090, 7)
    'ttnfv2u'
    """
    _validate_coord(lat, lng)
    precision = max(1, min(12, int(precision)))

    lat_range = [-90.0, 90.0]
    lng_range = [-180.0, 180.0]
    out: List[str] = []

    bit = 0
    ch = 0
    even = True

    while len(out) < precision:
        if even:
            mid = (lng_range[0] + lng_range[1]) / 2
            if lng > mid:
                ch = (ch << 1) | 1
                lng_range[0] = mid
            else:
                ch <<= 1
                lng_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat > mid:
                ch = (ch << 1) | 1
                lat_range[0] = mid
            else:
                ch <<= 1
                lat_range[1] = mid

        even = not even
        bit += 1
        if bit == 5:
            out.append(_BASE32[ch])
            bit = 0
            ch = 0

    return "".join(out)


def bbox(geohash: str) -> Dict[str, float]:
    """
    Bounding box of a geohash cell.

    Returns ``{"min_lat", "min_lng", "max_lat", "max_lng"}``.
    """
    if not geohash:
        raise InvalidCoordinateError(float("nan"), float("nan"))

    lat_range = [-90.0, 90.0]
    lng_range = [-180.0, 180.0]
    even = True

    for char in str(geohash).lower():
        if char not in _DECODE:
            raise InvalidCoordinateError(float("nan"), float("nan"))
        value = _DECODE[char]
        for mask in (16, 8, 4, 2, 1):
            bit = 1 if value & mask else 0
            target = lng_range if even else lat_range
            mid = (target[0] + target[1]) / 2
            if bit:
                target[0] = mid
            else:
                target[1] = mid
            even = not even

    return {
        "min_lat": lat_range[0],
        "min_lng": lng_range[0],
        "max_lat": lat_range[1],
        "max_lng": lng_range[1],
    }


def decode(geohash: str) -> Coord:
    """Centre ``(lat, lng)`` of a geohash cell."""
    box = bbox(geohash)
    return (
        (box["min_lat"] + box["max_lat"]) / 2,
        (box["min_lng"] + box["max_lng"]) / 2,
    )


def decode_exactly(geohash: str) -> Tuple[float, float, float, float]:
    """Centre plus the ± error margin: ``(lat, lng, lat_err, lng_err)``."""
    box = bbox(geohash)
    lat = (box["min_lat"] + box["max_lat"]) / 2
    lng = (box["min_lng"] + box["max_lng"]) / 2
    return (lat, lng, (box["max_lat"] - box["min_lat"]) / 2, (box["max_lng"] - box["min_lng"]) / 2)


def neighbours(geohash: str) -> Dict[str, str]:
    """
    The eight geohash cells surrounding this one, keyed by compass direction.

    Query a cell plus its neighbours and you cover the edge cases where the
    nearest thing sits just across a cell boundary.
    """
    lat, lng, lat_err, lng_err = decode_exactly(geohash)
    precision = len(geohash)
    step_lat = lat_err * 2
    step_lng = lng_err * 2

    out: Dict[str, str] = {}
    directions = {
        "n": (1, 0),
        "ne": (1, 1),
        "e": (0, 1),
        "se": (-1, 1),
        "s": (-1, 0),
        "sw": (-1, -1),
        "w": (0, -1),
        "nw": (1, -1),
    }
    for name, (d_lat, d_lng) in directions.items():
        n_lat = lat + d_lat * step_lat
        n_lng = lng + d_lng * step_lng
        if not -90.0 <= n_lat <= 90.0:
            continue
        n_lng = (n_lng + 540.0) % 360.0 - 180.0
        out[name] = encode(n_lat, n_lng, precision)
    return out


#: British spelling is the canonical one here; keep the US alias working.
neighbors = neighbours


def geohash_polygon(geohash: str) -> Dict[str, Any]:
    """The geohash cell as a GeoJSON Polygon, handy for drawing the grid."""
    box = bbox(geohash)
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [box["min_lng"], box["min_lat"]],
                [box["max_lng"], box["min_lat"]],
                [box["max_lng"], box["max_lat"]],
                [box["min_lng"], box["max_lat"]],
                [box["min_lng"], box["min_lat"]],
            ]
        ],
    }


def common_prefix(hashes: List[str]) -> str:
    """
    Longest shared prefix across geohashes, the smallest cell containing them all.
    """
    if not hashes:
        return ""
    shortest = min(hashes, key=len)
    for i, char in enumerate(shortest):
        if any(h[i] != char for h in hashes):
            return shortest[:i]
    return shortest


# ─────────────────────────────────────────────────────────────────────────────
# Slippy map tiles / quadkeys (Web Mercator)
# ─────────────────────────────────────────────────────────────────────────────

def tile_of(lat: float, lng: float, zoom: int = 12) -> Tuple[int, int, int]:
    """
    Slippy-map tile ``(x, y, z)`` containing a coordinate, the ``{z}/{x}/{y}``
    in every XYZ tile URL.
    """
    _validate_coord(lat, lng)
    zoom = max(0, min(24, int(zoom)))
    n = 2 ** zoom
    lat = max(-85.05112878, min(85.05112878, lat))
    lat_rad = math.radians(lat)

    x = int((lng + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return (min(n - 1, max(0, x)), min(n - 1, max(0, y)), zoom)


def tile_bounds(x: int, y: int, zoom: int) -> Dict[str, float]:
    """Geographic bounds of a slippy tile."""
    n = 2 ** zoom

    def lat_of(row: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row / n))))

    return {
        "min_lng": x / n * 360.0 - 180.0,
        "max_lng": (x + 1) / n * 360.0 - 180.0,
        "max_lat": lat_of(y),
        "min_lat": lat_of(y + 1),
    }


def quadkey(x: int, y: int, zoom: int) -> str:
    """
    Bing-style quadkey for a tile, the same prefix trick as geohashes,
    in Web Mercator space.
    """
    digits = []
    for i in range(zoom, 0, -1):
        digit = 0
        mask = 1 << (i - 1)
        if x & mask:
            digit += 1
        if y & mask:
            digit += 2
        digits.append(str(digit))
    return "".join(digits)


def quadkey_to_tile(key: str) -> Tuple[int, int, int]:
    """Inverse of :func:`quadkey`."""
    x = y = 0
    zoom = len(key)
    for i in range(zoom, 0, -1):
        mask = 1 << (i - 1)
        digit = key[zoom - i]
        if digit in ("1", "3"):
            x |= mask
        if digit in ("2", "3"):
            y |= mask
        if digit not in ("0", "1", "2", "3"):
            raise InvalidCoordinateError(float("nan"), float("nan"))
    return (x, y, zoom)
