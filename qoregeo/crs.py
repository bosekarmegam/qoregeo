"""
qoregeo.crs
===========
Coordinate reference system conversions, in pure Python.

Three projections cover the overwhelming majority of practical needs:

* **WGS84** (EPSG:4326), degrees of latitude and longitude, what GPS emits.
* **Web Mercator** (EPSG:3857). Metres, what every web map tile is drawn in.
* **UTM**, metres within a 6°-wide zone, what surveyors and national grids use.

Everything here is a closed-form formula, so there is no projection database
to install and nothing to fail at import time.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Tuple

from .exceptions import InvalidCoordinateError
from .utils import _validate_coord

Coord = Tuple[float, float]

WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)          # first eccentricity squared
MERCATOR_MAX_LAT = 85.05112877980659        # where the projection is clipped
K0 = 0.9996                                 # UTM scale factor on the central meridian


# ─────────────────────────────────────────────────────────────────────────────
# Web Mercator (EPSG:3857)
# ─────────────────────────────────────────────────────────────────────────────

def to_web_mercator(lat: float, lng: float) -> Tuple[float, float]:
    """
    WGS84 degrees → Web Mercator ``(x, y)`` metres.

    Latitude is clamped to ±85.0511°, past which Mercator runs to infinity.
    """
    _validate_coord(lat, lng)
    lat = max(-MERCATOR_MAX_LAT, min(MERCATOR_MAX_LAT, lat))
    x = math.radians(lng) * WGS84_A
    # asinh(tan φ) is the same curve as ln(tan(π/4 + φ/2)) but better
    # conditioned: it returns exactly 0 on the equator, where the log form
    # leaves a rounding crumb from tan(π/4) ≈ 0.9999999999999999.
    y = math.asinh(math.tan(math.radians(lat))) * WGS84_A
    return (x, y)


def from_web_mercator(x: float, y: float) -> Coord:
    """Web Mercator metres → WGS84 ``(lat, lng)`` degrees."""
    lng = math.degrees(x / WGS84_A)
    lat = math.degrees(math.atan(math.sinh(y / WGS84_A)))
    return (lat, (lng + 540.0) % 360.0 - 180.0)


# ─────────────────────────────────────────────────────────────────────────────
# UTM
# ─────────────────────────────────────────────────────────────────────────────

def utm_zone(lat: float, lng: float) -> Dict[str, Any]:
    """
    UTM zone number, hemisphere and MGRS latitude band for a coordinate.

    The two famous exceptions are handled: Norway's widened zone 32, and the
    Svalbard zones that skip 32/34/36.
    """
    _validate_coord(lat, lng)
    zone = int((lng + 180) / 6) + 1

    if 56.0 <= lat < 64.0 and 3.0 <= lng < 12.0:
        zone = 32
    elif 72.0 <= lat < 84.0:
        if 0.0 <= lng < 9.0:
            zone = 31
        elif 9.0 <= lng < 21.0:
            zone = 33
        elif 21.0 <= lng < 33.0:
            zone = 35
        elif 33.0 <= lng < 42.0:
            zone = 37

    bands = "CDEFGHJKLMNPQRSTUVWX"
    band_index = int((max(-80.0, min(83.9, lat)) + 80) / 8)
    band = bands[min(len(bands) - 1, band_index)]

    return {
        "zone": zone,
        "band": band,
        "hemisphere": "N" if lat >= 0 else "S",
        "epsg": (32600 if lat >= 0 else 32700) + zone,
    }


def to_utm(lat: float, lng: float) -> Dict[str, Any]:
    """
    WGS84 degrees → UTM.

    Returns ``{"easting", "northing", "zone", "band", "hemisphere", "epsg"}``
    with easting/northing in metres.

    >>> utm = to_utm(28.6139, 77.2090)
    >>> utm["zone"], utm["hemisphere"]
    (43, 'N')
    """
    _validate_coord(lat, lng)
    if not -80.0 <= lat <= 84.0:
        raise InvalidCoordinateError(lat, lng)

    info = utm_zone(lat, lng)
    zone = int(info["zone"])
    lat_r = math.radians(lat)
    lng_r = math.radians(lng)
    central = math.radians((zone - 1) * 6 - 180 + 3)

    n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(lat_r) ** 2)
    t = math.tan(lat_r) ** 2
    c = WGS84_E2 / (1 - WGS84_E2) * math.cos(lat_r) ** 2
    a = math.cos(lat_r) * (lng_r - central)

    e2, e4, e6 = WGS84_E2, WGS84_E2 ** 2, WGS84_E2 ** 3
    m = WGS84_A * (
        (1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * lat_r
        - (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * lat_r)
        + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * lat_r)
        - (35 * e6 / 3072) * math.sin(6 * lat_r)
    )

    easting = K0 * n * (
        a
        + (1 - t + c) * a ** 3 / 6
        + (5 - 18 * t + t ** 2 + 72 * c - 58 * WGS84_E2 / (1 - WGS84_E2)) * a ** 5 / 120
    ) + 500000.0

    northing = K0 * (
        m
        + n
        * math.tan(lat_r)
        * (
            a ** 2 / 2
            + (5 - t + 9 * c + 4 * c ** 2) * a ** 4 / 24
            + (61 - 58 * t + t ** 2 + 600 * c - 330 * WGS84_E2 / (1 - WGS84_E2))
            * a ** 6
            / 720
        )
    )
    if lat < 0:
        northing += 10000000.0

    return {
        "easting": round(easting, 3),
        "northing": round(northing, 3),
        "zone": zone,
        "band": info["band"],
        "hemisphere": info["hemisphere"],
        "epsg": info["epsg"],
    }


def from_utm(
    easting: float,
    northing: float,
    zone: int,
    hemisphere: str = "N",
) -> Coord:
    """UTM metres → WGS84 ``(lat, lng)`` degrees."""
    northern = str(hemisphere).upper().startswith("N")
    x = easting - 500000.0
    y = northing if northern else northing - 10000000.0

    e1 = (1 - math.sqrt(1 - WGS84_E2)) / (1 + math.sqrt(1 - WGS84_E2))
    m = y / K0
    mu = m / (
        WGS84_A
        * (1 - WGS84_E2 / 4 - 3 * WGS84_E2 ** 2 / 64 - 5 * WGS84_E2 ** 3 / 256)
    )

    phi1 = (
        mu
        + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
        + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
        + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
        + (1097 * e1 ** 4 / 512) * math.sin(8 * mu)
    )

    ep2 = WGS84_E2 / (1 - WGS84_E2)
    c1 = ep2 * math.cos(phi1) ** 2
    t1 = math.tan(phi1) ** 2
    n1 = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(phi1) ** 2)
    r1 = WGS84_A * (1 - WGS84_E2) / (1 - WGS84_E2 * math.sin(phi1) ** 2) ** 1.5
    d = x / (n1 * K0)

    lat = phi1 - (n1 * math.tan(phi1) / r1) * (
        d ** 2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * ep2) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * ep2 - 3 * c1 ** 2)
        * d ** 6
        / 720
    )
    lng = (
        d
        - (1 + 2 * t1 + c1) * d ** 3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * ep2 + 24 * t1 ** 2) * d ** 5 / 120
    ) / math.cos(phi1)

    central = math.radians((zone - 1) * 6 - 180 + 3)
    return (math.degrees(lat), math.degrees(central + lng))
