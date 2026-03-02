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
    print(km)  # 1153.54

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

from .engine import GeoEngine
from .exceptions import (
    QOREgeoError,
    NoDataError,
    InvalidCoordinateError,
    InvalidUnitError,
    ColumnNotFoundError,
    FileNotFoundError,
    UnsupportedFormatError,
    EmptyDatasetError,
    InvalidRadiusError,
    InvalidBufferError,
)

__version__  = "1.0.3"
__author__   = "Suneel Bose"
__email__    = "suneelbosekarmegam@gmail.com"
__license__  = "MIT"

__all__ = [
    # Main class
    "GeoEngine",
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
]
