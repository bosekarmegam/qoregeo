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

from ._version import __version__
from .engine import GeoEngine
from .exceptions import (
    ColumnNotFoundError,
    EmptyDatasetError,
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
from .exceptions import (
    FileNotFoundError as FileNotFoundError,  # backward-compatible alias (re-export)
)

__author__   = "Suneel Bose"
__email__    = "suneelbosekarmegam@gmail.com"
__license__  = "MIT"

# ``FileNotFoundError`` is importable by name but deliberately left out of
# ``__all__`` so ``from qoregeo import *`` never shadows the built-in.
__all__ = [
    # Main class
    "GeoEngine",
    # Exceptions
    "QOREgeoError",
    "NoDataError",
    "InvalidCoordinateError",
    "InvalidUnitError",
    "InvalidArgumentError",
    "ColumnNotFoundError",
    "GeoFileNotFoundError",
    "UnsupportedFormatError",
    "InvalidGeoJSONError",
    "EmptyDatasetError",
    "InvalidRadiusError",
    "InvalidBufferError",
    "__version__",
]
