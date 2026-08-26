"""
qoregeo
=======
Quantum-Powered Spatial Intelligence for Python.

Zero dependencies. Pure Python. Installs in two seconds, on every platform
Python runs on, including the ones where GDAL wheels do not.

Quick Start
-----------
    from qoregeo import GeoEngine

    geo = GeoEngine().load("cities.csv")

    # Distance and direction
    geo.distance((28.6139, 77.2090), (19.0760, 72.8777))    # 1153.54 km
    geo.bearing((28.6139, 77.2090), (19.0760, 72.8777))     # 'South-Southwest'

    # Geofencing
    zone = geo.buffer((28.6139, 77.2090), radius=10)
    geo.point_in_polygon((28.65, 77.22), zone)              # True

    # Query, analyse, visualise
    (geo.query("population > 1e6")
        .filter_by_radius(28.61, 77.20, radius=400)
        .map("nearby.html", colour_by="state"))

Command line
------------
    qoregeo info cities.csv
    qoregeo map cities.csv -o map.html --colour-by state
    qoregeo distance 28.6139,77.2090 19.0760,72.8777

Links
-----
- GitHub   : https://github.com/bosekarmegam/qoregeo
- PyPI     : https://pypi.org/project/qoregeo
- Issues   : https://github.com/bosekarmegam/qoregeo/issues
"""

from __future__ import annotations

from . import analysis, crs, formats, geohash, geometry, routing, static_map
from .analysis import (
    centre_of_mass,
    dbscan,
    dispersion,
    hotspots,
    kmeans,
    nearest_neighbour_ratio,
    spatial_join,
    spherical_mean,
)
from .crs import from_utm, from_web_mercator, to_utm, to_web_mercator, utm_zone
from .engine import GeoEngine
from .exceptions import (
    ColumnNotFoundError,
    EmptyDatasetError,
    FileNotFoundError,
    GeocodingError,
    InvalidBufferError,
    InvalidCoordinateError,
    InvalidGeometryError,
    InvalidQueryError,
    InvalidRadiusError,
    InvalidUnitError,
    MissingIndexError,
    NoDataError,
    QOREgeoError,
    UnsupportedFormatError,
)
from .formats import parse_wkt, to_wkt
from .geocode import Geocoder
from .geohash import decode, encode, neighbours, quadkey, tile_of
from .geometry import (
    centroid_of,
    convex_hull,
    destination,
    geometry_area_km2,
    geometry_length_km,
    intersects,
    line_buffer,
    midpoint,
    point_in_geometry,
    simplify,
    vincenty_km,
)
from .index import SpatialIndex
from .query import compile_query, run_query
from .routing import optimise_route, travel_time
from .static_map import build_png, build_svg

__version__ = "1.1.0"
__author__ = "Suneel Bose"
__email__ = "suneelbosekarmegam@gmail.com"
__license__ = "MIT"

#: US spelling alias, for symmetry with the method aliases on GeoEngine.
optimize_route = optimise_route
neighbors = neighbours

__all__ = [
    # Main class
    "GeoEngine",
    # Submodules
    "analysis",
    "crs",
    "formats",
    "geohash",
    "geometry",
    "routing",
    "static_map",
    # Geometry
    "centroid_of",
    "convex_hull",
    "destination",
    "geometry_area_km2",
    "geometry_length_km",
    "intersects",
    "line_buffer",
    "midpoint",
    "point_in_geometry",
    "simplify",
    "vincenty_km",
    # Analysis
    "centre_of_mass",
    "dbscan",
    "dispersion",
    "hotspots",
    "kmeans",
    "nearest_neighbour_ratio",
    "spatial_join",
    "spherical_mean",
    # Indexing and hashing
    "SpatialIndex",
    "encode",
    "decode",
    "neighbours",
    "neighbors",
    "quadkey",
    "tile_of",
    # Projections
    "to_web_mercator",
    "from_web_mercator",
    "to_utm",
    "from_utm",
    "utm_zone",
    # Formats
    "parse_wkt",
    "to_wkt",
    # Routing
    "optimise_route",
    "optimize_route",
    "travel_time",
    # Query
    "compile_query",
    "run_query",
    # Rendering
    "build_svg",
    "build_png",
    # Geocoding
    "Geocoder",
    # Exceptions
    "QOREgeoError",
    "NoDataError",
    "InvalidCoordinateError",
    "InvalidUnitError",
    "ColumnNotFoundError",
    "FileNotFoundError",
    "UnsupportedFormatError",
    "EmptyDatasetError",
    "InvalidRadiusError",
    "InvalidBufferError",
    "InvalidGeometryError",
    "InvalidQueryError",
    "GeocodingError",
    "MissingIndexError",
]
