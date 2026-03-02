"""
qoregeo.exceptions
==================
All custom exceptions raised by QOREgeo.

Every exception includes:
- What went wrong (clear English)
- Exactly how to fix it (copy-paste ready)
"""

from __future__ import annotations
from typing import List, Optional


_LINE = "─" * 44


class QOREgeoError(Exception):
    """Base class for all QOREgeo exceptions."""
    pass


class NoDataError(QOREgeoError):
    """Raised when a method is called before data is loaded."""

    def __init__(self, method_name: str = "this method") -> None:
        msg = (
            f"\n\n❌  QOREgeo — No Data Loaded\n{_LINE}\n"
            f"You called geo.{method_name}() but haven't loaded any data yet.\n\n"
            f"Fix it — load your data first:\n\n"
            f"    geo.load('your_file.csv')\n"
            f"    geo.load('your_file.geojson')\n"
            f"    geo.load_data([...])\n\n"
            f"Then call geo.{method_name}() again."
        )
        super().__init__(msg)


class InvalidCoordinateError(QOREgeoError):
    """Raised when lat/lng values are out of valid range."""

    def __init__(self, lat: float, lng: float) -> None:
        msg = (
            f"\n\n❌  QOREgeo — Invalid Coordinate\n{_LINE}\n"
            f"Received: lat={lat}, lng={lng}\n\n"
            f"Valid ranges are:\n"
            f"    latitude  : -90.0  to  90.0\n"
            f"    longitude : -180.0 to 180.0\n\n"
            f"Fix it:\n"
            f"    Make sure you're passing (latitude, longitude) — not (longitude, latitude).\n"
            f"    Example: (28.6139, 77.2090)  ← Delhi"
        )
        super().__init__(msg)


class InvalidUnitError(QOREgeoError):
    """Raised when an unrecognised distance unit is used."""

    def __init__(self, unit: str, valid: List[str]) -> None:
        valid_str = ", ".join(f"'{u}'" for u in valid)
        msg = (
            f"\n\n❌  QOREgeo — Invalid Unit '{unit}'\n{_LINE}\n"
            f"'{unit}' is not a recognised distance unit.\n\n"
            f"Supported units: {valid_str}\n\n"
            f"Fix it:\n"
            f"    geo.distance(a, b, unit='km')\n"
            f"    geo.distance(a, b, unit='miles')\n"
            f"    geo.distance(a, b, unit='m')"
        )
        super().__init__(msg)


class ColumnNotFoundError(QOREgeoError):
    """Raised when a requested column doesn't exist in the dataset."""

    def __init__(
        self,
        column: str,
        available: List[str],
        filepath: str = "dataset",
    ) -> None:
        available_str = ", ".join(f"'{c}'" for c in available[:10])
        if len(available) > 10:
            available_str += f", … (+{len(available) - 10} more)"

        msg = (
            f"\n\n❌  QOREgeo — Column Not Found\n{_LINE}\n"
            f"File: '{filepath}'\n"
            f"Could not find a '{column}' column.\n\n"
            f"Columns in your file:\n"
            f"    {available_str}\n\n"
            f"Fix it:\n"
            f"    geo.filter('{available[0] if available else 'column_name'}', value)"
        )
        super().__init__(msg)


class FileNotFoundError(QOREgeoError):
    """Raised when the data file doesn't exist."""

    def __init__(self, path: str) -> None:
        msg = (
            f"\n\n❌  QOREgeo — File Not Found\n{_LINE}\n"
            f"Could not find the file: '{path}'\n\n"
            f"Fix it:\n"
            f"    • Check the spelling of the filename\n"
            f"    • Check that the file is in the right folder\n"
            f"    • Use an absolute path if needed:\n"
            f"      geo.load('/full/path/to/your_file.csv')"
        )
        super().__init__(msg)


class UnsupportedFormatError(QOREgeoError):
    """Raised for unsupported file formats."""

    def __init__(self, ext: str) -> None:
        msg = (
            f"\n\n❌  QOREgeo — Unsupported File Format '{ext}'\n{_LINE}\n"
            f"QOREgeo currently supports:\n"
            f"    • .csv\n"
            f"    • .geojson\n"
            f"    • .json (GeoJSON format)\n\n"
            f"Fix it — convert your file first, or use geo.load_data([...]) "
            f"with raw Feature dicts."
        )
        super().__init__(msg)


class EmptyDatasetError(QOREgeoError):
    """Raised when a file or dataset contains no usable features."""

    def __init__(self, path: str) -> None:
        msg = (
            f"\n\n❌  QOREgeo — Empty Dataset\n{_LINE}\n"
            f"No valid spatial features were found in: '{path}'\n\n"
            f"Fix it:\n"
            f"    • Make sure the file has data rows (not just a header)\n"
            f"    • Make sure latitude and longitude values are not all blank\n"
            f"    • If using a CSV, check that lat/lng columns contain numbers"
        )
        super().__init__(msg)


class InvalidRadiusError(QOREgeoError):
    """Raised when a zero or negative radius is given."""

    def __init__(self, radius: float) -> None:
        msg = (
            f"\n\n❌  QOREgeo — Invalid Radius ({radius})\n{_LINE}\n"
            f"Radius must be a positive number greater than zero.\n\n"
            f"Fix it:\n"
            f"    geo.buffer(point, radius=10)          ← 10 km\n"
            f"    geo.filter_by_radius(lat, lng, 50)    ← 50 km"
        )
        super().__init__(msg)


class InvalidBufferError(QOREgeoError):
    """Raised when point_in_polygon receives an invalid polygon."""

    def __init__(self) -> None:
        msg = (
            f"\n\n❌  QOREgeo — Invalid Polygon\n{_LINE}\n"
            f"The polygon passed to point_in_polygon() is not valid.\n\n"
            f"Fix it — use a polygon created with geo.buffer():\n\n"
            f"    zone = geo.buffer((28.61, 77.20), radius=10)\n"
            f"    geo.point_in_polygon((28.65, 77.22), zone)"
        )
        super().__init__(msg)
