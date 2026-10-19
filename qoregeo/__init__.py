"""
qoregeo
=======
Quantum-Powered Spatial Intelligence for Python.

Zero C++ dependencies. Pure Python. Installs in 2 seconds.

Quick Start
-----------
    from qoregeo import GeoEngine

    geo = GeoEngine()
    geo.load("cities.csv")

    # Distance between two points
    km = geo.distance((28.6139, 77.2090), (19.0760, 72.8777))
    print(km)  # 1148.0965

    # Compass bearing
    direction = geo.bearing((28.6139, 77.2090), (19.0760, 72.8777))
    print(direction)  # South-Southwest

    # Buffer + geofencing
    zone = geo.buffer((28.6139, 77.2090), radius=10)
    inside = geo.point_in_polygon((28.65, 77.22), zone)

    # Filter by radius and export a map
    geo.filter_by_radius(28.61, 77.20, radius=400).map("nearby.html")

Links
-----
- GitHub   : https://github.com/bosekarmegam/qoregeo
- PyPI     : https://pypi.org/project/qoregeo
- Issues   : https://github.com/bosekarmegam/qoregeo/issues
"""

from typing import Any

from . import exceptions as _exceptions
from ._version import __version__
from .engine import GeoEngine, PointLike
from .exceptions import (
    ColumnNotFoundError,
    EmptyDatasetError,
    FileReadError,
    GeoFileNotFoundError,
    InvalidArgumentError,
    InvalidBufferError,
    InvalidCoordinateError,
    InvalidGeoJSONError,
    InvalidRadiusError,
    InvalidUnitError,
    NoDataError,
    QOREgeoError,
    UnsupportedFormatError,
)

__author__   = "Suneel Bose"
__email__    = "suneelbosekarmegam@gmail.com"
__license__  = "MIT"


def __getattr__(name: str) -> Any:
    # ``qoregeo.FileNotFoundError`` (the 1.0 name of GeoFileNotFoundError) keeps
    # working with a DeprecationWarning; it is deliberately not in ``__all__`` so
    # ``from qoregeo import *`` never shadows the built-in.
    if name == "FileNotFoundError":
        return _exceptions._deprecated_file_not_found()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Main class
    "GeoEngine",
    "PointLike",
    # Exceptions
    "QOREgeoError",
    "NoDataError",
    "InvalidCoordinateError",
    "InvalidUnitError",
    "InvalidArgumentError",
    "FileReadError",
    "ColumnNotFoundError",
    "GeoFileNotFoundError",
    "UnsupportedFormatError",
    "InvalidGeoJSONError",
    "EmptyDatasetError",
    "InvalidRadiusError",
    "InvalidBufferError",
    "__version__",
]
